from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, open_process
from tools.initial_data_audit import Reader, select_process_layout


PLAY_FIXTURE_MANAGER_VTABLE_RVA = 0x4480808
FIXTURE_TO_PLAY_FULL_VTABLE_RVA = 0x44808A8
FIXTURE_TO_PLAY_QUICK_VTABLE_RVA = 0x4480918
PLAY_FIXTURE_MANAGER_POINTER_RVA = 0x4E35E28
MATCH_ENGINE_PHASE_RVA = 0x4E46428
MATCH_ENGINE_MODE_RVA = 0x4E47B0C
MATCH_ENGINE_ACTIVE_STATE_RVA = 0x4E47B08
MAX_SCAN_REGION_BYTES = 128 * 1024 * 1024
STATE_RANGE_SIZE = 0x4000


def _is_pointer(value: int) -> bool:
    return 0x10000 <= value <= 0x7FFFFFFFFFFF


def _scan_instances(reader: Reader, vtable: int) -> list[int]:
    needle = struct.pack("<Q", vtable)
    found: list[int] = []
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > MAX_SCAN_REGION_BYTES:
            continue
        data = reader.bytes(region.base_address, region.size)
        if not data:
            continue
        offset = 0
        while True:
            offset = data.find(needle, offset)
            if offset < 0:
                break
            found.append(region.base_address + offset)
            offset += len(needle)
    return found


def _vector_candidates(data: bytes, object_address: int) -> list[dict[str, Any]]:
    candidates = []
    for offset in range(0, len(data) - 23, 8):
        begin, end, capacity = struct.unpack_from("<QQQ", data, offset)
        if not (_is_pointer(begin) and begin <= end <= capacity):
            continue
        if end - begin > 0x100000 or capacity - begin > 0x200000:
            continue
        candidates.append({
            "offset": hex(offset),
            "field_address": hex(object_address + offset),
            "begin": hex(begin),
            "end": hex(end),
            "capacity": hex(capacity),
            "size_bytes": end - begin,
        })
    return candidates


def capture(label: str, size: int, samples: int, interval: float) -> Path:
    pid, _process_path, layout = select_process_layout()
    if layout.key != "fm26":
        raise RuntimeError(f"expected FM26, found {layout.display_name}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path("data") / "audits" / "match_engine" / f"fm26_manager_{label}_{stamp}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with open_process(pid) as process, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
    ) as archive:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} not loaded")
        reader = Reader(process, module.base_address, layout)
        vtable = module.base_address + PLAY_FIXTURE_MANAGER_VTABLE_RVA
        manager = reader.ptr(module.base_address + PLAY_FIXTURE_MANAGER_POINTER_RVA)
        instances = [manager] if manager and reader.ptr(manager) == vtable else _scan_instances(reader, vtable)
        metadata: dict[str, Any] = {
            "schema_version": 1,
            "label": label,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "pid": pid,
            "module_base": hex(module.base_address),
            "module_size": module.size,
            "manager_vtable": hex(vtable),
            "instance_addresses": [hex(address) for address in instances],
            "samples": [],
        }
        for sample_index in range(samples):
            if sample_index:
                time.sleep(interval)
            sample: dict[str, Any] = {
                "index": sample_index,
                "captured_at": datetime.now().isoformat(timespec="milliseconds"),
                "phase": reader.u32(module.base_address + MATCH_ENGINE_PHASE_RVA),
                "mode": reader.u32(module.base_address + MATCH_ENGINE_MODE_RVA),
                "runtime_state": reader.u8(
                    module.base_address + MATCH_ENGINE_ACTIVE_STATE_RVA
                ),
                "instances": [],
                "state_ranges": [],
            }
            for name, rva in (
                ("manager_pointer", PLAY_FIXTURE_MANAGER_POINTER_RVA),
                ("phase", MATCH_ENGINE_PHASE_RVA),
                ("mode", MATCH_ENGINE_MODE_RVA),
            ):
                start_rva = rva - STATE_RANGE_SIZE // 2
                data = reader.bytes(module.base_address + start_rva, STATE_RANGE_SIZE)
                if not data or len(data) != STATE_RANGE_SIZE:
                    continue
                archive.writestr(f"samples/{sample_index}/state_{name}.bin", data)
                sample["state_ranges"].append({
                    "name": name,
                    "start_rva": hex(start_rva),
                    "size": len(data),
                })
            for instance_index, address in enumerate(instances):
                data = reader.bytes(address, size)
                if not data or len(data) != size:
                    continue
                archive.writestr(f"samples/{sample_index}/instance_{instance_index}.bin", data)
                fixture_rows = []
                vector = reader.bytes(address + 0x08, 0x18)
                if vector and len(vector) == 0x18:
                    begin, end, capacity = struct.unpack("<QQQ", vector)
                    valid = (
                        _is_pointer(begin)
                        and begin <= end <= capacity
                        and (end - begin) % 8 == 0
                        and end - begin <= 4096 * 8
                    )
                    pointers = reader.ptr_array(begin, (end - begin) // 8) if valid else []
                    expected_vtables = {
                        module.base_address + FIXTURE_TO_PLAY_FULL_VTABLE_RVA: "full",
                        module.base_address + FIXTURE_TO_PLAY_QUICK_VTABLE_RVA: "quick",
                    }
                    for fixture_index, fixture_address in enumerate(pointers):
                        fixture_vtable = reader.ptr(fixture_address) if fixture_address else None
                        fixture_kind = expected_vtables.get(fixture_vtable)
                        if not fixture_kind:
                            continue
                        fixture_data = reader.bytes(fixture_address, 0x200)
                        if not fixture_data or len(fixture_data) != 0x200:
                            continue
                        archive.writestr(
                            f"samples/{sample_index}/instance_{instance_index}_fixture_{fixture_index}.bin",
                            fixture_data,
                        )
                        fixture_rows.append({
                            "index": fixture_index,
                            "address": hex(fixture_address),
                            "kind": fixture_kind,
                            "vtable_rva": hex(fixture_vtable - module.base_address),
                            "size": len(fixture_data),
                        })
                sample["instances"].append({
                    "index": instance_index,
                    "address": hex(address),
                    "size": len(data),
                    "vector_candidates": _vector_candidates(data, address),
                    "fixtures_to_play": fixture_rows,
                })
            metadata["samples"].append(sample)
        archive.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture FM26 PLAY_FIXTURE_MANAGER instances")
    parser.add_argument("--label", required=True)
    parser.add_argument("--size", type=lambda value: int(value, 0), default=0x4000)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--interval", type=float, default=3.0)
    args = parser.parse_args()
    if not 0x100 <= args.size <= 0x100000:
        raise SystemExit("size must be between 0x100 and 0x100000")
    if not 1 <= args.samples <= 10:
        raise SystemExit("samples must be between 1 and 10")
    print(capture(args.label, args.size, args.samples, args.interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
