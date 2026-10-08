from __future__ import annotations

import argparse
import hashlib
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

from fm_collector.win32 import open_process
from tools.initial_data_audit import Reader, decode_date, decode_kickoff_minutes, select_process_layout


IMAGE_SCN_MEM_EXECUTE = 0x20000000
IMAGE_SCN_MEM_READ = 0x40000000
IMAGE_SCN_MEM_WRITE = 0x80000000
MAX_SECTION_BYTES = 32 * 1024 * 1024


def writable_data_sections(path: str) -> list[dict[str, Any]]:
    raw = Path(path).read_bytes()
    pe_offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if raw[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise RuntimeError("invalid PE image")
    section_count = struct.unpack_from("<H", raw, pe_offset + 6)[0]
    optional_size = struct.unpack_from("<H", raw, pe_offset + 20)[0]
    table = pe_offset + 24 + optional_size
    sections = []
    for index in range(section_count):
        header = table + index * 40
        name = raw[header:header + 8].rstrip(b"\0").decode("ascii", "replace")
        virtual_size, rva = struct.unpack_from("<II", raw, header + 8)
        characteristics = struct.unpack_from("<I", raw, header + 36)[0]
        readable_writable = (
            characteristics & IMAGE_SCN_MEM_READ
            and characteristics & IMAGE_SCN_MEM_WRITE
            and not characteristics & IMAGE_SCN_MEM_EXECUTE
        )
        if readable_writable and 0 < virtual_size <= MAX_SECTION_BYTES:
            sections.append({
                "name": name,
                "rva": rva,
                "size": virtual_size,
                "characteristics": characteristics,
            })
    return sections


def capture(label: str, samples: int, interval: float, output: Path | None = None) -> Path:
    pid, process_path, layout = select_process_layout()
    sections = writable_data_sections(process_path)
    if not sections:
        raise RuntimeError("no bounded writable module sections found")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = output or (
        Path("data") / "audits" / "match_engine"
        / f"{layout.key}_{label}_{stamp}.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "schema_version": 1,
        "label": label,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "pid": pid,
        "layout": layout.key,
        "game_version": layout.display_name,
        "executable_sha256": layout.executable_sha256,
        "module_name": layout.module_name,
        "samples": [],
        "sections": sections,
    }
    previous: dict[str, bytes] = {}
    with open_process(pid) as process, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6,
    ) as archive:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} not loaded")
        reader = Reader(process, module.base_address, layout)
        metadata["module_base"] = hex(module.base_address)
        metadata["module_size"] = module.size
        for sample_index in range(samples):
            if sample_index:
                time.sleep(interval)
            date_code = (
                reader.u32(module.base_address + layout.game_date_rva)
                if layout.game_date_rva is not None else None
            )
            game_date = decode_date(date_code or 0)
            minutes = decode_kickoff_minutes(date_code or 0)
            sample_meta: dict[str, Any] = {
                "index": sample_index,
                "captured_at": datetime.now().isoformat(timespec="milliseconds"),
                "game_date_code": date_code,
                "game_date": game_date.isoformat() if game_date else None,
                "game_time": (
                    f"{minutes // 60:02d}:{minutes % 60:02d}" if minutes is not None else None
                ),
                "sections": {},
            }
            for section in sections:
                name = str(section["name"])
                payload = reader.bytes(
                    module.base_address + int(section["rva"]), int(section["size"]),
                )
                if payload is None or len(payload) != int(section["size"]):
                    raise RuntimeError(f"unable to capture section {name}")
                archive.writestr(f"samples/{sample_index}/{name}.bin", payload)
                prior = previous.get(name)
                sample_meta["sections"][name] = {
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "changed_bytes_from_previous": (
                        sum(left != right for left, right in zip(prior, payload))
                        if prior is not None else None
                    ),
                }
                previous[name] = payload
            metadata["samples"].append(sample_meta)
        archive.writestr(
            "metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture bounded FM module state for match-engine diffing")
    parser.add_argument("--label", required=True)
    parser.add_argument("--samples", type=int, default=2)
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10:
        raise SystemExit("samples must be between 1 and 10")
    if not 0 <= args.interval <= 60:
        raise SystemExit("interval must be between 0 and 60 seconds")
    print(capture(args.label, args.samples, args.interval, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
