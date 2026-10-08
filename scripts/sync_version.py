"""Synchronize the active FMODD product version and write a release record.

This script only updates active product files. Historical specs, version resources,
changelog entries, and research reports are intentionally left untouched.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility.
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "build" / "protection.toml"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str, *, dry_run: bool) -> None:
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _slug(version: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", version)


def _replace(path: Path, replacements: list[tuple[str, str]], *, dry_run: bool) -> bool:
    content = _read(path)
    updated = content
    for old, new in replacements:
        updated = updated.replace(old, new)
    if updated != content:
        _write(path, updated, dry_run=dry_run)
        return True
    return False


def _update_version_resource(path: Path, version: str, *, dry_run: bool) -> bool:
    content = _read(path) if path.is_file() else _read(ROOT / "FMODD.version.txt")
    updated = re.sub(r"filevers=\([^)]*\)", f"filevers=({_numeric_tuple(version)})", content, count=1)
    updated = re.sub(r"prodvers=\([^)]*\)", f"prodvers=({_numeric_tuple(version)})", updated, count=1)
    updated = re.sub(r"StringStruct\(u'FileVersion', u'[^']*'\)", f"StringStruct(u'FileVersion', u'{version}')", updated, count=1)
    updated = re.sub(r"StringStruct\(u'OriginalFilename', u'[^']*'\)", f"StringStruct(u'OriginalFilename', u'FMODD-V{version}.exe')", updated, count=1)
    updated = re.sub(r"StringStruct\(u'ProductVersion', u'[^']*'\)", f"StringStruct(u'ProductVersion', u'{version}')", updated, count=1)
    if not path.is_file() or updated != content:
        _write(path, updated, dry_run=dry_run)
        return True
    return False


def _numeric_tuple(version: str) -> str:
    numbers = [int(part) for part in re.match(r"^(\d+)\.(\d+)\.(\d+)", version).groups()]
    return ", ".join(str(number) for number in (*numbers, 0))


def _update_config(content: str, old_version: str, target: str) -> str:
    old_slug = _slug(old_version)
    target_slug = _slug(target)
    replacements = {
        f'version = "{old_version}"': f'version = "{target}"',
        f'tag = "V{old_version}"': f'tag = "V{target}"',
        f'build/FMODD-V{old_version}-protected.spec': f'build/FMODD-V{target}-protected.spec',
        f'build/desktop_host_v{old_slug}': f'build/desktop_host_v{target_slug}',
        f'build/protected_runtime_v{old_slug}': f'build/protected_runtime_v{target_slug}',
        f'build/rust_native_v{old_slug}': f'build/rust_native_v{target_slug}',
        f'build/cpp_native_v{old_slug}': f'build/cpp_native_v{target_slug}',
    }
    for old, new in replacements.items():
        content = content.replace(old, new)
    return content


def synchronize(version: str, changes: list[str], removed: list[str], *, dry_run: bool = False) -> list[Path]:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[A-Za-z][A-Za-z0-9.-]*)?", version):
        raise ValueError("版本必须采用 X.Y.Z，可带 beta/c 等后缀")
    config = tomllib.loads(_read(CONFIG_PATH))
    old_version = str(config["version"])
    old_tag = str(config["tag"])
    target_tag = f"V{version}"
    old_slug = _slug(old_version)
    target_slug = _slug(version)
    changed: list[Path] = []

    config_text = _read(CONFIG_PATH)
    updated_config = _update_config(config_text, old_version, version)
    if updated_config != config_text:
        _write(CONFIG_PATH, updated_config, dry_run=dry_run)
        changed.append(CONFIG_PATH)

    active_text_files = {
        ROOT / "README.md": [(old_tag, target_tag), (old_version, version)],
        ROOT / "DEVELOPMENT.md": [(old_tag, target_tag), (old_version, version), (f"v{old_slug}", f"v{target_slug}")],
        ROOT / "fm_odds_web.py": [(f'PRODUCT_VERSION = "{old_tag}"', f'PRODUCT_VERSION = "{target_tag}"')],
        ROOT / "fmodd_desktop.py": [(f'FMODD {old_tag}', f'FMODD {target_tag}')],
        ROOT / "desktop" / "WebViewHost.cs": [(f'FMODD {old_tag}', f'FMODD {target_tag}')],
        ROOT / "desktop" / "WebViewHost.csproj": [
            (f"<Version>{old_version}</Version>", f"<Version>{version}</Version>"),
            (f"<InformationalVersion>{old_version}</InformationalVersion>", f"<InformationalVersion>{version}</InformationalVersion>"),
            (f"<AssemblyVersion>{old_version}.0</AssemblyVersion>", f"<AssemblyVersion>{version}.0</AssemblyVersion>"),
            (f"<FileVersion>{old_version}.0</FileVersion>", f"<FileVersion>{version}.0</FileVersion>"),
        ],
    }
    for path, replacements in active_text_files.items():
        if path.is_file() and _replace(path, replacements, dry_run=dry_run):
            changed.append(path)

    primary_resource = ROOT / "FMODD.version.txt"
    if _update_version_resource(primary_resource, version, dry_run=dry_run):
        changed.append(primary_resource)
    target_resource = ROOT / f"FMODD-V{version}.version.txt"
    if _update_version_resource(target_resource, version, dry_run=dry_run):
        changed.append(target_resource)

    old_spec = ROOT / str(config["spec"])
    for protected in (True, False):
        suffix = "-protected" if protected else ""
        target_spec = ROOT / "build" / f"FMODD-V{version}{suffix}.spec"
        source_spec = old_spec if protected else old_spec.with_name(old_spec.name.replace("-protected", ""))
        if not source_spec.is_file():
            raise FileNotFoundError(source_spec)
        spec_content = _read(source_spec)
        spec_content = spec_content.replace(old_tag, target_tag).replace(f"v{old_slug}", f"v{target_slug}")
        if not target_spec.is_file() or _read(target_spec) != spec_content:
            _write(target_spec, spec_content, dry_run=dry_run)
            changed.append(target_spec)

    record = ROOT / "docs" / "releases" / f"{target_tag}.md"
    if record.exists() and not dry_run:
        raise FileExistsError(f"版本记录已存在：{record.relative_to(ROOT)}；如需追加请手工编辑")
    record_content = _record_content(version, old_version, changes, removed)
    _write(record, record_content, dry_run=dry_run)
    changed.append(record)
    return changed


def _record_content(version: str, previous: str, changes: list[str], removed: list[str]) -> str:
    lines = [f"# FMODD V{version} 变更记录", "", f"- 日期：{date.today().isoformat()}", f"- 基于版本：V{previous}", "", "## 修改", ""]
    lines.extend(f"- {item}" for item in (changes or ["版本配置同步"]))
    lines.extend(["", "## 删除或停用", ""])
    lines.extend(f"- {item}" for item in (removed or ["无"]))
    lines.extend(["", "## 说明", "", "本文件由 `scripts/sync_version.py` 生成；历史版本文件和历史报告不因本次同步而改写。", ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步 FMODD 活动版本并生成 Markdown 变更记录")
    parser.add_argument("--version", required=True, help="目标版本，例如 2.4.2 或 2.4.3beta")
    parser.add_argument("--change", action="append", default=[], help="修改内容，可重复指定")
    parser.add_argument("--removed", action="append", default=[], help="删除/停用内容，可重复指定")
    parser.add_argument("--dry-run", action="store_true", help="只显示将修改的文件")
    args = parser.parse_args()
    try:
        changed = synchronize(args.version.removeprefix("V"), args.change, args.removed, dry_run=args.dry_run)
    except (OSError, KeyError, ValueError, FileNotFoundError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    action = "would update" if args.dry_run else "updated"
    print(f"Version V{args.version.removeprefix('V')}: {action} {len(changed)} file(s)")
    for path in changed:
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
