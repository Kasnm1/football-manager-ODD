from __future__ import annotations

import argparse
import json
import struct
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import open_process
from tools.initial_data_audit import Reader, select_game_layout, select_process_layout


MANAGER_VECTOR_OFFSETS = (0x20, 0x48, 0xA0, 0x550)
MANAGER_BYTES = 0x700
RECORD_BYTES = 0x800
MAX_VECTOR_BYTES = 0x10000


def _hex_bytes(value: bytes | None) -> str:
    return (value or b"").hex(" ")


def _read_vector(reader: Reader, manager: int, offset: int) -> dict[str, object]:
    header = reader.bytes(manager + offset, 24)
    if not header or len(header) != 24:
        return {"offset": hex(offset), "error": "unreadable header"}
    begin, end, capacity = struct.unpack("<QQQ", header)
    used = end - begin if begin and begin <= end <= capacity else -1
    if used < 0 or used > MAX_VECTOR_BYTES:
        return {
            "offset": hex(offset),
            "begin": hex(begin),
            "end": hex(end),
            "capacity": hex(capacity),
            "error": "invalid vector bounds",
        }
    raw = reader.bytes(begin, used) if used else b""
    pointers = struct.unpack(f"<{used // 8}Q", raw) if raw and used % 8 == 0 else ()
    records = {}
    for pointer in pointers:
        if not pointer or pointer in records:
            continue
        block = reader.bytes(pointer, RECORD_BYTES)
        if block:
            records[hex(pointer)] = _hex_bytes(block)
    return {
        "offset": hex(offset),
        "begin": hex(begin),
        "end": hex(end),
        "capacity": hex(capacity),
        "used_bytes": used,
        "raw_hex": _hex_bytes(raw),
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture FM24 TRAINING_MANAGER containers without writing memory",
    )
    parser.add_argument("output", type=Path)
    parser.add_argument("--manager", required=True, type=lambda value: int(value, 0))
    parser.add_argument("--label", required=True)
    parser.add_argument("--player-uid", type=int, default=28106491)
    args = parser.parse_args()

    select_game_layout("fm24")
    pid, executable, layout = select_process_layout()
    if layout.key != "fm24":
        raise RuntimeError("FM24 is not the selected process")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} is not loaded")
        reader = Reader(process, module.base_address, layout)
        manager_vtable = reader.ptr(args.manager)
        if not manager_vtable:
            raise RuntimeError("TRAINING_MANAGER address is no longer readable")
        payload = {
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "label": args.label,
            "player_uid": args.player_uid,
            "pid": pid,
            "executable": executable,
            "layout": layout.key,
            "distribution": layout.distribution,
            "game_version": layout.game_version,
            "module": module.path,
            "module_base": hex(module.base_address),
            "module_size": module.size,
            "manager": hex(args.manager),
            "manager_vtable": hex(manager_vtable),
            "manager_hex": _hex_bytes(reader.bytes(args.manager, MANAGER_BYTES)),
            "vectors": {
                hex(offset): _read_vector(reader, args.manager, offset)
                for offset in MANAGER_VECTOR_OFFSETS
            },
            "read_metrics": reader.read_metrics(),
        }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output),
        "manager": hex(args.manager),
        "manager_vtable": hex(manager_vtable),
        "vectors": {
            key: {
                "used_bytes": value.get("used_bytes"),
                "record_count": len(value.get("records", {})),
                "error": value.get("error"),
            }
            for key, value in payload["vectors"].items()
        },
        "read_metrics": payload["read_metrics"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
