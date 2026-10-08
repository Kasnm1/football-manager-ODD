from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.win32 import MEM_PRIVATE, iter_readable_regions, open_process
from tools.initial_data_audit import Reader, select_process_layout


CHUNK_SIZE = 8 * 1024 * 1024
MAX_REGION_SIZE = 512 * 1024 * 1024


def _scan(reader: Reader, needles: dict[bytes, str]) -> dict[str, list[int]]:
    hits = {label: [] for label in needles.values()}
    overlap = max(map(len, needles)) - 1
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE or region.size > MAX_REGION_SIZE:
            continue
        offset, carry = 0, b""
        while offset < region.size:
            length = min(CHUNK_SIZE, region.size - offset)
            chunk = reader.bytes(region.base_address + offset, length)
            if not chunk:
                carry = b""
                offset += length
                continue
            data = carry + chunk
            base = region.base_address + offset - len(carry)
            for needle, label in needles.items():
                cursor = 0
                while True:
                    cursor = data.find(needle, cursor)
                    if cursor < 0:
                        break
                    address = base + cursor
                    if address not in hits[label]:
                        hits[label].append(address)
                    cursor += 1
            carry = data[-overlap:] if len(data) >= overlap else data
            offset += length
    return hits


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only FM26 PPM localization probe")
    parser.add_argument("terms", nargs="+")
    args = parser.parse_args()
    pid, _path, layout = select_process_layout()
    if layout.key != "fm26":
        raise RuntimeError("请选择正在运行的 FM26 进程")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        text_needles = {}
        for term in args.terms:
            text_needles[term.encode("utf-8")] = f"{term}:utf8"
            text_needles[term.encode("utf-16le")] = f"{term}:utf16"
        hits = _scan(reader, text_needles)
        headers: dict[int, str] = {}
        for label, addresses in hits.items():
            print("text", label, [hex(value) for value in addresses[:32]])
            if not label.endswith(":utf8"):
                continue
            length = len(label.removesuffix(":utf8").encode("utf-8"))
            for address in addresses:
                if reader.u32(address - 4) == length:
                    headers[address - 4] = label
                    print("fm_string", label, hex(address - 4))
        if not headers:
            return
        references = _scan(
            reader,
            {struct.pack("<Q", address): f"{label}@{hex(address)}" for address, label in headers.items()},
        )
        for label, addresses in references.items():
            print("reference", label, [hex(value) for value in addresses[:64]])
            for address in addresses[:16]:
                context = reader.bytes(address - 0x20, 0x60) or b""
                print("context", hex(address), context.hex(" "))


if __name__ == "__main__":
    main()
