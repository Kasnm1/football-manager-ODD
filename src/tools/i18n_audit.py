"""Static audit for Chinese text that may be visible in FMODD.

The audit is intentionally conservative and dependency-free.  It only reads the
application shell (``web/index.html`` and ``web/app.js``) and Python entry point
or backend files (root ``*.py`` plus ``tools/**/*.py``).  ``data/`` is never an
input, even when a caller supplies a custom project root.

The command writes JSON to stdout so that CI can consume it.  A one-line summary
is written to stderr.  A report produced by a normal run can be used as a
baseline with ``--check --baseline path.json``; check mode fails only when the
current occurrence count is higher than the baseline count for a stable finding.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import io
import json
import re
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHINESE_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
HTML_TEXT_RE = re.compile(r">([^<]+)<", re.DOTALL)
HTML_ATTRIBUTE_RE = re.compile(
    r"\b(?:title|aria-label|placeholder|alt|value|label|data-label|data-tooltip)"
    r"\s*=\s*([\"'])(.*?)\1",
    re.IGNORECASE | re.DOTALL,
)
PY_STRING_PREFIX_RE = re.compile(r"(?is)^([rubf]*)('''|\"\"\"|'|\")")

# These markers are deliberately narrow.  A broad ``name`` or ``error``
# allowlist would hide real UI labels.  They only change classification; the
# occurrence remains in the report for a reviewer to see.
GAME_DATA_MARKERS = (
    "player_name",
    "club_name",
    "team_name",
    "fixture_name",
    "competition_name",
    "nation_name",
    "manager_name",
    "save_name",
    "custom_name",
    "user_input",
    "user_text",
    "user_note",
    "user_remark",
    "raw_name",
)
INTERNAL_MARKERS = (
    "logger.",
    "logging.",
    "console.debug",
    "console.trace",
    "debug_log",
    "diagnostic",
    "telemetry",
    "development_only",
    "dev_only",
)
INTERNAL_FILE_MARKERS = (
    "diagnostic",
    "debug",
    "initial_data_audit",
)
TRANSLATION_MARKERS = (
    "legacyenglish",
    "legacy_english",
    "legacycatalog",
    "translation_catalog",
    "translation_source",
)
AUDIT_DIRECTIVE_RE = re.compile(r"i18n-audit\s*:\s*(ignore|game-data|internal)", re.I)


@dataclass(frozen=True)
class Finding:
    """One Chinese-containing source literal or HTML text node."""

    file: str
    line: int
    column: int
    kind: str
    text: str
    category: str
    likely_user_visible: bool | None
    allowlisted: bool
    reason: str

    def fingerprint(self) -> str:
        """Return a location-independent identity suitable for baselines.

        Line numbers are intentionally omitted so that adding code above a
        message does not make every existing message appear to be new.
        Repeated identical messages are counted separately by check mode.
        """

        return "\x1f".join((self.file, self.kind, self.text))


def _normalise_text(value: str) -> str:
    value = value.replace("\r", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", value).strip()


def _contains_chinese(value: str) -> bool:
    return bool(CHINESE_RE.search(value))


def _line_starts(source: str) -> list[int]:
    starts = [0]
    starts.extend(match.end() for match in re.finditer(r"\n", source))
    return starts


def _position(starts: Sequence[int], offset: int) -> tuple[int, int]:
    line_index = bisect.bisect_right(starts, offset) - 1
    return line_index + 1, offset - starts[line_index] + 1


def _offsets_for_matches(source: str, pattern: re.Pattern[str]) -> Iterator[tuple[int, str]]:
    for match in pattern.finditer(source):
        yield match.start(1), match.group(1)


def _directive_for_line(source: str, starts: Sequence[int], offset: int) -> str | None:
    line, _ = _position(starts, offset)
    start = starts[line - 1]
    end = source.find("\n", start)
    if end < 0:
        end = len(source)
    match = AUDIT_DIRECTIVE_RE.search(source[start:end])
    return match.group(1).lower() if match else None


def _classify(
    *,
    file: str,
    text: str,
    source: str,
    offset: int,
    kind: str,
    starts: Sequence[int],
) -> tuple[str, bool | None, bool, str]:
    """Classify a finding without attempting to understand application logic."""

    directive = _directive_for_line(source, starts, offset)
    context = source[max(0, offset - 180) : min(len(source), offset + 180)].lower()
    file_lower = file.lower()

    if directive == "ignore":
        return "allowlisted", False, True, "explicit i18n-audit: ignore directive"
    if directive == "game-data":
        return "game-or-user-data", False, True, "explicit i18n-audit: game-data directive"
    if directive == "internal":
        return "internal-diagnostic", False, True, "explicit i18n-audit: internal directive"

    if any(marker in file_lower for marker in INTERNAL_FILE_MARKERS):
        return "internal-diagnostic", False, True, "diagnostic or audit source file"

    if kind.startswith("html-") and kind != "html-inline-js":
        return "user-visible", True, False, "HTML text or user-facing attribute"

    if any(marker in context for marker in TRANSLATION_MARKERS):
        return "translation-source", False, True, "language catalog or translation runtime context"
    if any(marker in context for marker in INTERNAL_MARKERS):
        return "internal-diagnostic", False, False, "logging, diagnostics, or development context"
    if any(marker in context for marker in GAME_DATA_MARKERS):
        return "game-or-user-data", False, False, "near a game-name or user-provided data field"

    # app.js is the view/controller boundary.  On compressed template lines the
    # DOM-writing call can be thousands of characters away from an individual
    # literal, so a small context window under-reports exactly the strings this
    # audit is meant to gate.  Intentional raw game-data classifiers must use a
    # narrow directive instead of relying on that formatting accident.
    if file == "web/app.js" and kind.startswith("js-"):
        return "user-visible", True, False, "FMODD frontend source literal"

    # Exception messages and returned strings are part of the API/UI boundary
    # often enough to be useful audit candidates.  For other code literals we
    # retain uncertainty rather than claiming that static analysis proved them.
    if kind.startswith("python-") and re.search(
        r"\b(?:raise|valueerror|typeerror|domainerror|http\w*error|return)\b", context
    ):
        return "user-visible", True, False, "Python exception or returned message context"
    if kind.startswith("js-") and re.search(
        r"(?:textcontent|innertext|innerhtml|toast|notify|confirm|alert|label|title)", context
    ):
        return "user-visible", True, False, "DOM or notification rendering context"

    return "uncertain", None, False, "Chinese string literal requires human review"


def _finding(
    *,
    file: str,
    source: str,
    starts: Sequence[int],
    offset: int,
    kind: str,
    text: str,
) -> Finding | None:
    text = _normalise_text(text)
    if not text or not _contains_chinese(text):
        return None
    line, column = _position(starts, offset)
    category, likely_visible, allowlisted, reason = _classify(
        file=file,
        text=text,
        source=source,
        offset=offset,
        kind=kind,
        starts=starts,
    )
    return Finding(
        file=file,
        line=line,
        column=column,
        kind=kind,
        text=text,
        category=category,
        likely_user_visible=likely_visible,
        allowlisted=allowlisted,
        reason=reason,
    )


def _mask_regions(source: str, regions: Iterable[tuple[int, int]]) -> str:
    chars = list(source)
    for start, end in regions:
        for index in range(start, end):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _html_regions(source: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Return (ignored regions, inline script regions), preserving offsets."""

    ignored: list[tuple[int, int]] = []
    inline_scripts: list[tuple[int, int]] = []
    for match in re.finditer(r"(?is)<!--.*?-->|<style\b.*?</style\s*>", source):
        ignored.append((match.start(), match.end()))
    for match in re.finditer(r"(?is)<script\b[^>]*>(.*?)</script\s*>", source):
        ignored.append((match.start(1), match.end(1)))
        inline_scripts.append((match.start(1), match.end(1)))
    return ignored, inline_scripts


def scan_html(source: str, file: str) -> list[Finding]:
    starts = _line_starts(source)
    ignored, inline_scripts = _html_regions(source)
    masked = _mask_regions(source, ignored)
    findings: list[Finding] = []

    for offset, text in _offsets_for_matches(masked, HTML_TEXT_RE):
        item = _finding(
            file=file, source=source, starts=starts, offset=offset, kind="html-text", text=text
        )
        if item:
            findings.append(item)
    for match in HTML_ATTRIBUTE_RE.finditer(masked):
        item = _finding(
            file=file,
            source=source,
            starts=starts,
            offset=match.start(2),
            kind="html-attribute",
            text=match.group(2),
        )
        if item:
            findings.append(item)
    for start, end in inline_scripts:
        for item in _scan_javascript(source[start:end], file, source_offset=start, source=source):
            findings.append(item)
    return findings


def _scan_javascript(
    source_fragment: str,
    file: str,
    *,
    source_offset: int = 0,
    source: str | None = None,
) -> list[Finding]:
    """Extract quoted JS strings while skipping line and block comments."""

    full_source = source if source is not None else source_fragment
    starts = _line_starts(full_source)
    findings: list[Finding] = []
    i = 0
    length = len(source_fragment)
    while i < length:
        char = source_fragment[i]
        if char == "/" and i + 1 < length and source_fragment[i + 1] == "/":
            newline = source_fragment.find("\n", i + 2)
            i = length if newline < 0 else newline + 1
            continue
        if char == "/" and i + 1 < length and source_fragment[i + 1] == "*":
            end = source_fragment.find("*/", i + 2)
            i = length if end < 0 else end + 2
            continue
        if char not in "'\"`":
            i += 1
            continue
        quote = char
        start = i
        i += 1
        value_chars: list[str] = []
        while i < length:
            char = source_fragment[i]
            if char == "\\" and i + 1 < length:
                value_chars.append(source_fragment[i : i + 2])
                i += 2
                continue
            if char == quote:
                i += 1
                break
            value_chars.append(char)
            i += 1
        absolute = source_offset + start
        item = _finding(
            file=file,
            source=full_source,
            starts=starts,
            offset=absolute,
            kind="js-template" if quote == "`" else "js-string",
            text="".join(value_chars),
        )
        if item:
            findings.append(item)
    return findings


def scan_javascript(source: str, file: str) -> list[Finding]:
    """Public wrapper for JavaScript source scanning."""

    return _scan_javascript(source, file)


def _python_string_value(token_text: str) -> str:
    try:
        value = ast.literal_eval(token_text)
        return value.decode(errors="replace") if isinstance(value, bytes) else str(value)
    except (SyntaxError, ValueError, TypeError):
        match = PY_STRING_PREFIX_RE.match(token_text)
        if not match:
            return token_text
        prefix, quote = match.groups()
        body = token_text[len(prefix) + len(quote) :]
        if body.endswith(quote):
            body = body[: -len(quote)]
        # F-strings cannot be evaluated safely without executing expressions;
        # retaining the literal body is enough for a static Chinese-text audit.
        return (
            body.replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace("\\'", "'")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def scan_python(source: str, file: str) -> list[Finding]:
    starts = _line_starts(source)
    findings: list[Finding] = []
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", -10_001)
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type not in {tokenize.STRING, fstring_middle}:
                continue
            value = (
                _python_string_value(token.string)
                if token.type == tokenize.STRING
                else token.string
            )
            offset = starts[token.start[0] - 1] + token.start[1]
            item = _finding(
                file=file,
                source=source,
                starts=starts,
                offset=offset,
                kind="python-string" if token.type == tokenize.STRING else "python-fstring",
                text=value,
            )
            if item:
                findings.append(item)
    except (tokenize.TokenError, IndentationError):
        # A partially edited source file should still yield useful findings.
        # Fall back to a small quoted-string scanner rather than failing CI.
        for match in re.finditer(r"(?s)(?:[rubfRUBF]{0,3})(['\"]{1,3})(.*?)\1", source):
            item = _finding(
                file=file,
                source=source,
                starts=starts,
                offset=match.start(),
                kind="python-string",
                text=match.group(2),
            )
            if item:
                findings.append(item)
    return findings


def _is_excluded(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return True
    return any(part.lower() in {"data", ".git", "build", "dist", "__pycache__"} for part in relative.parts)


def default_source_paths(root: Path) -> list[Path]:
    """Return deterministic application source paths, never including data/."""

    candidates: list[Path] = []
    for relative in (Path("web/index.html"), Path("web/app.js")):
        candidates.append(root / relative)
    candidates.extend(sorted(root.glob("*.py")))
    candidates.extend(sorted((root / "tools").rglob("*.py")) if (root / "tools").exists() else [])
    return [
        path
        for path in candidates
        if path.is_file()
        and path.name != "embedded_web_assets.py"
        and not _is_excluded(path, root)
    ]


def _relative_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def scan_project(root: Path = PROJECT_ROOT, paths: Iterable[Path] | None = None) -> dict:
    root = Path(root).resolve()
    source_paths = sorted(
        {Path(path).resolve() for path in (default_source_paths(root) if paths is None else paths)},
        key=lambda path: _relative_path(path, root) if not _is_excluded(path, root) else str(path),
    )
    findings: list[Finding] = []
    scanned: list[str] = []
    skipped: list[str] = []
    for path in source_paths:
        if _is_excluded(path, root):
            skipped.append(str(path))
            continue
        if not path.is_file():
            continue
        relative = _relative_path(path, root)
        scanned.append(relative)
        source = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".html":
            findings.extend(scan_html(source, relative))
        elif path.suffix.lower() == ".js":
            findings.extend(scan_javascript(source, relative))
        elif path.suffix.lower() == ".py":
            findings.extend(scan_python(source, relative))
    findings.sort(key=lambda item: (item.file, item.line, item.column, item.kind, item.text))
    counts = Counter(item.category for item in findings)
    report = {
        "schema_version": 1,
        "root": str(root),
        "scanned_files": scanned,
        "skipped_paths": sorted(skipped),
        "findings": [
            {**asdict(item), "fingerprint": item.fingerprint()}
            for item in findings
        ],
        "summary": {
            "files_scanned": len(scanned),
            "occurrences": len(findings),
            "likely_user_visible": sum(item.likely_user_visible is True for item in findings),
            "allowlisted": sum(item.allowlisted for item in findings),
            "categories": {key: counts[key] for key in sorted(counts)},
        },
    }
    return report


def _baseline_counts(report: object) -> Counter[str]:
    if isinstance(report, list):
        findings = report
    elif isinstance(report, dict):
        findings = report.get("findings", [])
    else:
        findings = []
    counter: Counter[str] = Counter()
    if not isinstance(findings, list):
        return counter
    for item in findings:
        if not isinstance(item, dict):
            continue
        fingerprint = item.get("fingerprint")
        if isinstance(fingerprint, str):
            counter[fingerprint] += 1
            continue
        file = item.get("file")
        kind = item.get("kind")
        text = item.get("text")
        if all(isinstance(value, str) for value in (file, kind, text)):
            counter["\x1f".join((file, kind, _normalise_text(text)))] += 1
    return counter


def apply_baseline(report: dict, baseline_path: Path) -> dict:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read baseline {baseline_path}: {error}") from error
    baseline_counts = _baseline_counts(baseline)
    new_findings: list[dict] = []
    seen_counts: Counter[str] = Counter()
    for item in report["findings"]:
        fingerprint = "\x1f".join((item["file"], item["kind"], item["text"]))
        seen_counts[fingerprint] += 1
        # For duplicate messages, only the baseline-count-th occurrence is
        # considered known; extra occurrences are new findings.
        if seen_counts[fingerprint] > baseline_counts[fingerprint]:
            new_findings.append(item)
    report["baseline"] = str(baseline_path)
    report["check"] = {
        "new_occurrences": len(new_findings),
        "new_findings": new_findings,
        "baseline_occurrences": sum(baseline_counts.values()),
        "passed": not new_findings,
    }
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT, help="project root (default: repository root)")
    parser.add_argument("--baseline", type=Path, help="JSON report to use as the check baseline")
    parser.add_argument("--check", action="store_true", help="fail when new Chinese occurrences exceed baseline counts")
    parser.add_argument("--output", type=Path, help="also write the JSON report to this path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.check and args.baseline is None:
        parser.error("--check requires --baseline PATH")
    try:
        report = scan_project(args.root)
        if args.check:
            report = apply_baseline(report, args.baseline)
    except ValueError as error:
        parser.error(str(error))
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    summary = report["summary"]
    suffix = ""
    if args.check:
        check = report["check"]
        suffix = f", new={check['new_occurrences']}, passed={'yes' if check['passed'] else 'no'}"
    print(
        f"i18n audit: files={summary['files_scanned']}, occurrences={summary['occurrences']}, "
        f"likely-visible={summary['likely_user_visible']}, allowlisted={summary['allowlisted']}{suffix}",
        file=sys.stderr,
    )
    return 1 if args.check and not report["check"]["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
