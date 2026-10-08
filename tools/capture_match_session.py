from __future__ import annotations

import argparse
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import iter_readable_regions, open_process
from tools.initial_data_audit import Reader, select_process_layout


FM24_MATCH_SESSION_POINTER_RVA = 0x6374480
FM24_MATCH_SESSION_VTABLE_RVA = 0x5836A90
DEFAULT_CAPTURE_BYTES = 256 * 1024


def capture(label: str, size: int = DEFAULT_CAPTURE_BYTES) -> Path:
    pid, _process_path, layout = select_process_layout()
    if layout.key != "fm24":
        raise RuntimeError("match-session capture currently supports FM24 only")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} not loaded")
        reader = Reader(process, module.base_address, layout)
        object_address = reader.ptr(module.base_address + FM24_MATCH_SESSION_POINTER_RVA)
        if not object_address:
            raise RuntimeError("MATCH_SESSION_MANAGER pointer is empty")
        vtable = reader.ptr(object_address)
        expected_vtable = module.base_address + FM24_MATCH_SESSION_VTABLE_RVA
        if vtable != expected_vtable:
            raise RuntimeError(
                f"unexpected MATCH_SESSION_MANAGER vtable: {vtable!r}"
            )
        containing_region = next(
            (
                region for region in iter_readable_regions(process)
                if region.base_address <= object_address < region.base_address + region.size
            ),
            None,
        )
        if not containing_region:
            raise RuntimeError("MATCH_SESSION_MANAGER memory region is unavailable")
        readable_size = containing_region.base_address + containing_region.size - object_address
        size = min(size, readable_size)
        payload = reader.bytes(object_address, size)
        if not payload or len(payload) != size:
            raise RuntimeError("unable to read MATCH_SESSION_MANAGER payload")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = (
        Path("data") / "audits" / "match_engine"
        / f"fm24_session_{label}_{stamp}.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema_version": 1,
        "label": label,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "pid": pid,
        "module_base": hex(module.base_address),
        "object_address": hex(object_address),
        "pointer_rva": hex(FM24_MATCH_SESSION_POINTER_RVA),
        "vtable_rva": hex(FM24_MATCH_SESSION_VTABLE_RVA),
        "size": size,
    }
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.json", json.dumps(metadata, indent=2))
        archive.writestr("session.bin", payload)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture the FM24 native match-session object")
    parser.add_argument("--label", required=True)
    parser.add_argument("--size", type=int, default=DEFAULT_CAPTURE_BYTES)
    args = parser.parse_args()
    if not 4096 <= args.size <= 4 * 1024 * 1024:
        raise SystemExit("size must be between 4096 and 4194304 bytes")
    print(capture(args.label, args.size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
