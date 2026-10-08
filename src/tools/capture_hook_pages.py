from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import (
    MEM_IMAGE,
    MEM_PRIVATE,
    PAGE_EXECUTE_READ,
    PAGE_EXECUTE_READWRITE,
    PAGE_EXECUTE_WRITECOPY,
    find_module,
    iter_readable_regions,
    open_process,
    read_process_memory,
)


EXECUTE_MASK = PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY
PRIVATE_DUMP_LIMIT = 1024 * 1024
NEW_PRIVATE_DUMP_LIMIT = 16 * 1024 * 1024
CONTEXT_SIZE = 32


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().upper()


def _read_region(process, base: int, size: int) -> bytes:
    data = read_process_memory(process, base, size)
    if data is None or len(data) != size:
        raise RuntimeError(f"Unable to read 0x{size:X} bytes at 0x{base:X}")
    return data


def capture(pid: int, module_name: str, output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    with open_process(pid) as process:
        module = find_module(process, module_name)
        if module is None:
            raise RuntimeError(f"Module {module_name!r} was not found in PID {pid}")
        module_end = module.base_address + module.size
        for index, region in enumerate(iter_readable_regions(process)):
            if not region.protect & EXECUTE_MASK:
                continue
            in_module = (
                region.type == MEM_IMAGE
                and module.base_address <= region.base_address < module_end
            )
            private = region.type == MEM_PRIVATE
            if not in_module and not private:
                continue
            data = _read_region(process, region.base_address, region.size)
            dump = in_module or region.size <= PRIVATE_DUMP_LIMIT
            file_name = f"region_{index:05d}.bin" if dump else ""
            if file_name:
                (output / file_name).write_bytes(data)
            rows.append({
                "base": region.base_address,
                "rva": region.base_address - module.base_address if in_module else None,
                "size": region.size,
                "protect": region.protect,
                "type": region.type,
                "kind": "module" if in_module else "private",
                "sha256": _sha256(data),
                "file": file_name,
            })
    manifest = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "pid": pid,
        "module": {
            "name": module.name,
            "path": module.path,
            "base": module.base_address,
            "size": module.size,
        },
        "regions": rows,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8",
    )
    return manifest


def _changed_spans(before: bytes, after: bytes) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start: int | None = None
    common_length = min(len(before), len(after))
    for index, (old, new) in enumerate(zip(before, after)):
        if old != new and start is None:
            start = index
        elif old == new and start is not None:
            spans.append((start, index))
            start = None
    if len(before) != len(after) and start is None:
        start = common_length
    if start is not None:
        spans.append((start, max(len(before), len(after))))
    return spans


def diff(pid: int, baseline: Path, output: Path) -> dict[str, object]:
    baseline_manifest = json.loads(
        (baseline / "manifest.json").read_text(encoding="utf-8"),
    )
    module_name = str(baseline_manifest["module"]["name"])
    output.mkdir(parents=True, exist_ok=True)
    current_dir = output / "current"
    current = capture(pid, module_name, current_dir)
    if int(current["module"]["base"]) != int(baseline_manifest["module"]["base"]):
        raise RuntimeError("Module base changed; baseline belongs to another process session")

    before_rows = {int(row["base"]): row for row in baseline_manifest["regions"]}
    after_rows = {int(row["base"]): row for row in current["regions"]}
    changed: list[dict[str, object]] = []
    new_regions: list[dict[str, object]] = []
    removed_regions: list[dict[str, object]] = []

    for base, after_row in after_rows.items():
        before_row = before_rows.get(base)
        if before_row is None:
            row = dict(after_row)
            file_name = ""
            if row["kind"] == "private" and int(row["size"]) <= NEW_PRIVATE_DUMP_LIMIT:
                data = _read_current_region(pid, base, int(row["size"]))
                file_name = f"new_private_{base:016X}.bin"
                (output / file_name).write_bytes(data)
            row["diff_file"] = file_name
            new_regions.append(row)
            continue
        if before_row["sha256"] == after_row["sha256"]:
            continue
        row: dict[str, object] = {
            "base": base,
            "rva": after_row.get("rva"),
            "size": after_row["size"],
            "kind": after_row["kind"],
            "before_sha256": before_row["sha256"],
            "after_sha256": after_row["sha256"],
            "spans": [],
        }
        before_file = str(before_row.get("file") or "")
        after_file = str(after_row.get("file") or "")
        if before_file and after_file:
            old = (baseline / before_file).read_bytes()
            new = (current_dir / after_file).read_bytes()
            for start, end in _changed_spans(old, new):
                context_start = max(0, start - CONTEXT_SIZE)
                context_end = min(max(len(old), len(new)), end + CONTEXT_SIZE)
                row["spans"].append({
                    "address": base + start,
                    "rva": (base + start) - int(current["module"]["base"]),
                    "offset": start,
                    "length": end - start,
                    "context_offset": context_start,
                    "before": old[context_start:context_end].hex(" "),
                    "after": new[context_start:context_end].hex(" "),
                })
        changed.append(row)

    for base, before_row in before_rows.items():
        if base not in after_rows:
            removed_regions.append(before_row)

    result = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "pid": pid,
        "baseline": str(baseline.resolve()),
        "changed_regions": changed,
        "new_regions": new_regions,
        "removed_regions": removed_regions,
    }
    (output / "diff.json").write_text(
        json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8",
    )
    return result


def _read_current_region(pid: int, base: int, size: int) -> bytes:
    with open_process(pid) as process:
        return _read_region(process, base, size)


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture executable-page changes made by a CE Hook")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--pid", type=int, required=True)
    snapshot_parser.add_argument("--module", default="fm.exe")
    snapshot_parser.add_argument("--output", type=Path, required=True)
    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("--pid", type=int, required=True)
    diff_parser.add_argument("--baseline", type=Path, required=True)
    diff_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "snapshot":
        result = capture(args.pid, args.module, args.output)
        print(json.dumps({
            "output": str(args.output.resolve()),
            "region_count": len(result["regions"]),
        }))
    else:
        result = diff(args.pid, args.baseline, args.output)
        print(json.dumps({
            "output": str(args.output.resolve()),
            "changed_regions": len(result["changed_regions"]),
            "new_regions": len(result["new_regions"]),
            "removed_regions": len(result["removed_regions"]),
        }))


if __name__ == "__main__":
    main()
