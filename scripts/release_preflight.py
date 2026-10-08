from __future__ import annotations

import argparse
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "build" / "protection.toml"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"无法读取 {path.relative_to(ROOT)}: {error}") from error


def _new_relevant_paths() -> list[Path]:
    if not (ROOT / ".git").exists():
        paths: list[Path] = []
        ignored_parts = {"__pycache__", "bin", "node_modules", "obj"}
        for root_name in ("assets", "build", "desktop", "scripts", "tools", "web"):
            for path in (ROOT / root_name).rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(ROOT)
                if any(part in ignored_parts for part in relative.parts):
                    continue
                if relative.parts[:2] == ("assets", "source"):
                    continue
                if relative == Path("tools/embedded_web_assets.py"):
                    continue
                if root_name == "build" and len(relative.parts) > 2:
                    continue
                paths.append(relative)
        return sorted(paths, key=lambda path: str(path).lower())

    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths: list[Path] = []
    for entry in result.stdout.decode("utf-8", errors="replace").split("\0"):
        if not entry.startswith("?? "):
            continue
        relative = Path(entry[3:])
        if relative.parts and relative.parts[0] in {"assets", "build", "desktop", "scripts", "tools", "web"}:
            paths.append(relative)
    return paths


def validate(version: str) -> list[str]:
    config = tomllib.loads(_read(CONFIG_PATH))
    errors: list[str] = []
    tag = f"V{version}"
    if config.get("version") != version or config.get("tag") != tag:
        errors.append("build/protection.toml 的版本与目标版本不一致")

    required_markers = {
        ROOT / "README.md": tag,
        ROOT / "fmodd_desktop.py": f"FMODD {tag}",
        ROOT / "desktop" / "WebViewHost.cs": f"FMODD {tag}",
        ROOT / "FMODD.version.txt": version,
        ROOT / f"FMODD-{tag}.version.txt": version,
        ROOT / str(config.get("spec", "")): f'name="FMODD-{tag}"',
    }
    for path, marker in required_markers.items():
        if not path.is_file():
            errors.append(f"缺少发布文件：{path.relative_to(ROOT)}")
        elif marker not in _read(path):
            errors.append(f"版本标记不一致：{path.relative_to(ROOT)}（缺少 {marker}）")

    web_entry = ROOT / "fm_odds_web.py"
    web_markers = (f'APP_VERSION = "{tag}"', f'PRODUCT_VERSION = "{tag}"')
    if not web_entry.is_file():
        errors.append(f"缺少发布文件：{web_entry.relative_to(ROOT)}")
    elif not any(marker in _read(web_entry) for marker in web_markers):
        errors.append(
            f"版本标记不一致：{web_entry.relative_to(ROOT)}"
            f"（缺少 {web_markers[0]} 或 {web_markers[1]}）"
        )

    record_path = ROOT / "docs" / "releases" / f"V{version}.md"
    if not record_path.is_file():
        errors.append(f"缺少版本变更记录：{record_path.relative_to(ROOT)}")

    for module_name in config.get("protected_modules", []):
        source = ROOT.joinpath(*module_name.split(".")).with_suffix(".py")
        if not source.is_file():
            errors.append(f"受保护模块不存在：{source.relative_to(ROOT)}")

    notice_path = ROOT / str(config.get("ai_usage_notice", ""))
    if not notice_path.is_file():
        errors.append("缺少 AI 授权使用声明")
    elif "unauthorized unpacking" not in _read(notice_path):
        errors.append("AI 授权使用声明缺少禁止未经授权解包的标记")

    accepted_suffixes = {
        "", ".css", ".cs", ".csproj", ".dll", ".exe", ".html", ".ico",
        ".js", ".json", ".lua", ".manifest", ".md", ".mjs", ".png", ".props", ".ps1",
        ".py", ".pyx", ".spec", ".toml", ".cmd",
        ".txt", ".version", ".webp", ".xml",
    }
    unclassified = [path for path in _new_relevant_paths() if path.suffix.lower() not in accepted_suffixes]
    if unclassified:
        errors.append("新增未分类文件：" + ", ".join(map(str, unclassified)))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an FMODD release without modifying files.")
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.repo.resolve() != ROOT:
        print(f"ERROR: 当前脚本仅允许仓库 {ROOT}", file=sys.stderr)
        return 2
    errors = validate(args.version.removeprefix("V"))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Packaging preflight: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
