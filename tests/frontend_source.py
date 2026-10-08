"""Explicit source views for frontend structural tests.

Most frontend tests should inspect the shipped source. A smaller set of
older tests still needs the former Chinese projection while they are migrated;
that view is available explicitly through ``legacy_web_source``.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import re
import subprocess
from typing import Literal


_PROJECT_ROOT = (Path(__file__).resolve().parents[1] / "src")
_ORIGINAL_PATH_READ_TEXT = Path.read_text
SourceMode = Literal["raw", "zh-CN"]


@lru_cache(maxsize=1)
def _simplified_chinese_catalog() -> dict[str, str]:
    """Load the merged source catalog used by the browser runtime."""
    web_root = _PROJECT_ROOT / "web"
    module_names = [
        "i18n.shell.js", "i18n.static.js", "i18n.settings.js",
        "i18n.commerce.js", "i18n.facilities.js", "i18n.items.js",
        "i18n.relationship.js", "i18n.world.js", "i18n.enums.js", "i18n.js",
    ]
    node_script = r'''
const fs=require("fs"), vm=require("vm");
const context={
  window:{FMODDI18nModules:[],FMODDLocalePacks:{}},
  document:{documentElement:{dataset:{}},querySelector:()=>null,dispatchEvent:()=>{}},
  NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}
};
vm.createContext(context);
for(const file of process.argv.slice(1)) vm.runInContext(fs.readFileSync(file,"utf8"),context);
const i18n=context.window.FMODDI18n;
i18n.setLocale("zh-CN",{persist:false,root:null});
const catalog=Object.fromEntries(i18n.catalogKeys("zh-CN").map(key=>[key,i18n.t(key)]));
process.stdout.write(JSON.stringify(catalog));
'''
    completed = subprocess.run(
        ["node", "-e", node_script, *(str(web_root / name) for name in module_names)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(completed.stdout)


def _legacy_web_projection(resolved: Path, source: str) -> str:
    """Project i18n-bearing HTML/JavaScript into the zh-CN structural view."""
    if resolved.name not in {"app.js", "index.html"}:
        return source
    original_source = source
    catalog = _simplified_chinese_catalog()

    def translated(key: str) -> str:
        return str(catalog.get(key, key))

    if resolved.name == "app.js":
        def split_top_level(value: str) -> list[str]:
            parts, start, depth, quote, escaped = [], 0, 0, "", False
            for index, character in enumerate(value):
                if quote:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == quote:
                        quote = ""
                    continue
                if character in "'\"`":
                    quote = character
                elif character in "([{":
                    depth += 1
                elif character in ")]}":
                    depth -= 1
                elif character == "," and depth == 0:
                    parts.append(value[start:index])
                    start = index + 1
            parts.append(value[start:])
            return parts

        call_pattern = re.compile(r'uiText\("([^"]+)",\s*\{')
        offset = 0
        while match := call_pattern.search(source, offset):
            index, depth, quote, escaped = match.end() - 1, 0, "", False
            closing_brace = closing_call = -1
            while index < len(source):
                character = source[index]
                if quote:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == quote:
                        quote = ""
                elif character in "'\"`":
                    quote = character
                elif character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        closing_brace = index
                        tail = index + 1
                        while tail < len(source) and source[tail].isspace():
                            tail += 1
                        if tail < len(source) and source[tail] == ")":
                            closing_call = tail
                        break
                index += 1
            if closing_call < 0:
                offset = match.end()
                continue
            parameters: dict[str, str] = {}
            body = source[match.end():closing_brace]
            for part in split_top_level(body):
                name, separator, expression = part.partition(":")
                if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
                    parameters[name.strip()] = expression.strip()
            text = translated(match.group(1))
            placeholders = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", text))
            if not placeholders.issubset(parameters):
                offset = closing_call + 1
                continue
            rendered = text.replace("`", "\\`")
            for name in placeholders:
                rendered = rendered.replace(f"{{{name}}}", f"${{{parameters[name]}}}")
            replacement = f"`{rendered}`"
            source = source[:match.start()] + replacement + source[closing_call + 1:]
            offset = match.start() + len(replacement)
        source = re.sub(
            r'uiText\("([^"]+)"\)',
            lambda match: json.dumps(translated(match.group(1)), ensure_ascii=False),
            source,
        )
        source = re.sub(
            r'\b([A-Za-z_][A-Za-z0-9_]*)Key:\s*"([^"]+)"',
            lambda match: (
                f'{match.group(1)}:'
                + json.dumps(translated(match.group(2)), ensure_ascii=False)
            ),
            source,
        )
        source = re.sub(
            r'\$\{("(?:[^"\\]|\\.)*")\}',
            lambda match: json.loads(match.group(1)),
            source,
        )
        used_keys = [key for key in catalog if f'"{key}"' in original_source]
        return source + "\n/* referenced i18n keys\n" + "\n".join(
            f'"{key}"' for key in used_keys
        ) + "\n*/\n"

    element_pattern = re.compile(
        r'<(?P<tag>[a-z][a-z0-9-]*)(?P<before>[^>]*)\sdata-i18n="(?P<key>[^"]+)"'
        r'(?P<after>[^>]*)>(?P<body>[^<]*)</(?P=tag)>',
        re.IGNORECASE,
    )
    source = element_pattern.sub(
        lambda match: (
            f'<{match.group("tag")}{match.group("before")}{match.group("after")}>'
            f'{translated(match.group("key"))}</{match.group("tag")}>'
        ),
        source,
    )
    for attribute in ("aria-label", "placeholder", "title", "alt"):
        source = re.sub(
            rf'{attribute}="[^"]*"\s+data-i18n-{re.escape(attribute)}="([^"]+)"',
            lambda match, attr=attribute: (
                f'{attr}="{translated(match.group(1))}"'
            ),
            source,
        )
        source = re.sub(
            rf'data-i18n-{re.escape(attribute)}="([^"]+)"\s+{attribute}="[^"]*"',
            lambda match, attr=attribute: (
                f'{attr}="{translated(match.group(1))}"'
            ),
            source,
        )
    used_keys = [key for key in catalog if f'"{key}"' in original_source]
    return source + "\n<!-- referenced i18n keys\n" + "\n".join(
        f'"{key}"' for key in used_keys
    ) + "\n-->\n"


@lru_cache(maxsize=4)
def _cached_legacy_web_source(path: str, modified_ns: int) -> str:
    del modified_ns
    resolved = Path(path)
    source = _ORIGINAL_PATH_READ_TEXT(resolved, encoding="utf-8")
    return _legacy_web_projection(resolved, source)


def _resolve(path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else _PROJECT_ROOT / candidate


def read_frontend_source(path: str | Path, *, mode: SourceMode = "raw") -> str:
    """Read frontend source in an explicit raw or zh-CN projection mode."""
    resolved = _resolve(path).resolve()
    if mode == "raw":
        return _ORIGINAL_PATH_READ_TEXT(resolved, encoding="utf-8")
    if mode == "zh-CN":
        return _cached_legacy_web_source(
            str(resolved), resolved.stat().st_mtime_ns,
        )
    raise ValueError(f"unsupported frontend source mode: {mode!r}")


def raw_web_source(path: str | Path) -> str:
    """Read the shipped frontend source without a translation projection."""
    return read_frontend_source(path, mode="raw")


def legacy_web_source(path: str | Path) -> str:
    """Read the explicit zh-CN structural projection for legacy tests."""
    return read_frontend_source(path, mode="zh-CN")
