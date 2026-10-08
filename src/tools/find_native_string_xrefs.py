from __future__ import annotations

import argparse
import json
import struct
from collections import deque
from pathlib import Path

import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP


DEFAULT_DLL = Path(r"U:\SteamLibrary\steamapps\common\Football Manager 26\fm_Data\Plugins\x86_64\game_plugin.dll")


def file_offset_to_rva(pe: pefile.PE, offset: int) -> int | None:
    for section in pe.sections:
        start = int(section.PointerToRawData)
        end = start + int(section.SizeOfRawData)
        if start <= offset < end:
            return int(section.VirtualAddress) + offset - start
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("terms", nargs="+", help="ASCII strings to locate")
    parser.add_argument("--dll", type=Path, default=DEFAULT_DLL)
    parser.add_argument("--context", type=int, default=8)
    parser.add_argument("--rva", action="append", default=[], help="Function/data RVA (hex) to find direct references to")
    parser.add_argument("--sections", nargs="*", help="Optional executable section names to scan")
    parser.add_argument("--fast-calls", action="store_true", help="Only scan E8 rel32 calls to --rva targets")
    args = parser.parse_args()
    raw = args.dll.read_bytes()
    pe = pefile.PE(str(args.dll), fast_load=True)
    image_base = int(pe.OPTIONAL_HEADER.ImageBase)
    targets: dict[int, dict[str, object]] = {}
    for value in args.rva:
        rva = int(value, 0)
        targets[image_base + rva] = {"term": f"rva:{hex(rva)}", "file_offset": None, "rva": hex(rva)}
    for term in args.terms:
        needle = term.encode("ascii")
        cursor = 0
        while True:
            offset = raw.find(needle, cursor)
            if offset < 0:
                break
            rva = file_offset_to_rva(pe, offset)
            if rva is not None:
                targets[image_base + rva] = {"term": term, "file_offset": hex(offset), "rva": hex(rva)}
            cursor = offset + 1
    # MSVC metadata frequently reaches strings through an image-base pointer table.
    indirect: dict[int, dict[str, object]] = {}
    for target_va, details in list(targets.items()):
        for needle, kind in ((struct.pack("<Q", target_va), "va64"), (struct.pack("<I", target_va - image_base), "rva32")):
            cursor = 0
            while True:
                offset = raw.find(needle, cursor)
                if offset < 0:
                    break
                rva = file_offset_to_rva(pe, offset)
                if rva is not None:
                    indirect[image_base + rva] = {
                        "term": details["term"], "file_offset": hex(offset), "rva": hex(rva),
                        "indirect": kind, "points_to": details["rva"],
                    }
                cursor = offset + 1
    targets.update(indirect)
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True
    decoder.skipdata = True
    found: list[dict[str, object]] = []
    previous: deque[dict[str, str]] = deque(maxlen=args.context)
    pending: list[dict[str, object]] = []
    requested_sections = set(args.sections or [])
    executable_sections = [
        section for section in pe.sections
        if int(section.Characteristics) & 0x20000000
        and (not requested_sections or section.Name.rstrip(b"\0").decode(errors="ignore") in requested_sections)
    ]
    if args.fast_calls:
        found = []
        function_targets = {image_base + int(value, 0): int(value, 0) for value in args.rva}
        for section in executable_sections:
            data = section.get_data(); section_va = image_base + int(section.VirtualAddress); cursor = 0
            while True:
                hit = data.find(b"\xE8", cursor)
                if hit < 0:
                    break
                if hit + 5 <= len(data):
                    displacement = struct.unpack_from("<i", data, hit + 1)[0]
                    target = section_va + hit + 5 + displacement
                    if target in function_targets:
                        window_start = max(0, hit - 160); window_end = min(len(data), hit + 165)
                        context = [
                            {"va": hex(insn.address), "rva": hex(insn.address - image_base), "mnemonic": insn.mnemonic, "operands": insn.op_str}
                            for insn in decoder.disasm(data[window_start:window_end], section_va + window_start)
                            if insn.id != 0
                        ]
                        found.append({
                            "term": f"rva:{hex(function_targets[target])}", "xref_va": hex(section_va + hit),
                            "xref_rva": hex(section_va + hit - image_base), "section": section.Name.rstrip(b"\0").decode(errors="ignore"),
                            "context": context,
                        })
                cursor = hit + 1
        print(json.dumps({"dll": str(args.dll), "targets": list(targets.values()), "xrefs": found}, indent=2))
        return 0
    instruction_stream = (
        insn
        for section in executable_sections
        for insn in decoder.disasm(section.get_data(), image_base + int(section.VirtualAddress))
    )
    for insn in instruction_stream:
        row = {"va": hex(insn.address), "rva": hex(insn.address - image_base), "mnemonic": insn.mnemonic, "operands": insn.op_str}
        for record in list(pending):
            record["context"].append(row)
            record["remaining"] = int(record["remaining"]) - 1
            if not record["remaining"]:
                record.pop("remaining", None)
                pending.remove(record)
        if insn.id == 0:
            previous.append(row)
            continue
        for operand in insn.operands:
            if operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP:
                target = insn.address + insn.size + operand.mem.disp
            elif operand.type == X86_OP_IMM:
                target = int(operand.imm)
            else:
                continue
            details = targets.get(target)
            if not details:
                continue
            record = {
                **details, "xref_va": hex(insn.address), "xref_rva": hex(insn.address - image_base),
                "context": [*previous, row], "remaining": args.context,
            }
            found.append(record); pending.append(record)
        previous.append(row)
    for record in pending:
        record.pop("remaining", None)
    print(json.dumps({"dll": str(args.dll), "targets": list(targets.values()), "xrefs": found}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
