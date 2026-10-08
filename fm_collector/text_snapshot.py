from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .collector import _choose_pid, _read_region_chunks
from .strings import StringRecord
from .win32 import MEM_IMAGE, open_process, iter_readable_regions


@dataclass(frozen=True)
class TextSnapshotConfig:
    pid: int | None
    name: str
    output_dir: Path
    ranges: tuple[tuple[int, int], ...]
    chunk_mb: int
    min_len: int
    max_records: int
    max_unique: int
    include_executable: bool


def run_text_snapshot(config: TextSnapshotConfig) -> dict[str, object]:
    from .collector import SnapshotConfig

    if not config.ranges:
        raise RuntimeError("at least one address range is required")

    pid = _choose_pid(
        SnapshotConfig(
            pid=config.pid,
            name=config.name,
            output_dir=config.output_dir,
            max_read_mb=0,
            chunk_mb=config.chunk_mb,
            min_len=config.min_len,
            keywords=(),
            max_records=1,
            max_unique=1,
            include_executable=config.include_executable,
        )
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"fm26_text_{pid}_{stamp}"
    jsonl_path = config.output_dir / f"{base_name}.jsonl"
    txt_path = config.output_dir / f"{base_name}.txt"
    meta_path = config.output_dir / f"{base_name}.meta.json"

    start = time.perf_counter()
    chunk_size = config.chunk_mb * 1024 * 1024
    unique: set[str] = set()
    examples: list[dict[str, object]] = []
    stats = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "ranges": [f"0x{a:x}-0x{b:x}" for a, b in config.ranges],
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
        "records_written": 0,
        "unique_written": 0,
        "chunk_mb": config.chunk_mb,
        "min_len": config.min_len,
        "include_executable": config.include_executable,
    }

    with open_process(pid) as process, jsonl_path.open("w", encoding="utf-8") as jsonl:
        for region in iter_readable_regions(process):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE and not config.include_executable:
                stats["regions_skipped_executable"] += 1
                continue

            region_start = region.base_address
            region_end = region.base_address + region.size
            for wanted_start, wanted_end in config.ranges:
                start_addr = max(region_start, wanted_start)
                end_addr = min(region_end, wanted_end)
                if start_addr >= end_addr:
                    continue
                stats["regions_scanned"] += 1
                size = end_addr - start_addr
                for address, data in _read_region_chunks(process, start_addr, size, chunk_size):
                    stats["read_attempts"] += 1
                    if not data:
                        stats["read_failures"] += 1
                        continue
                    stats["bytes_read"] += len(data)
                    for record in extract_clean_text(data, address, config.min_len):
                        if record.text in unique:
                            continue
                        if len(unique) < config.max_unique:
                            unique.add(record.text)
                        if stats["records_written"] < config.max_records:
                            row = {
                                "address": f"0x{record.address:x}",
                                "encoding": record.encoding,
                                "text": record.text,
                            }
                            jsonl.write(json.dumps(row, ensure_ascii=False) + "\n")
                            stats["records_written"] += 1
                            if len(examples) < 50:
                                examples.append(row)

    with txt_path.open("w", encoding="utf-8") as txt:
        for text in sorted(unique, key=lambda x: (x.casefold(), x)):
            txt.write(text.replace("\r", " ").replace("\n", " ") + "\n")

    stats["unique_written"] = len(unique)
    stats["duration_seconds"] = round(time.perf_counter() - start, 3)
    stats["jsonl_path"] = str(jsonl_path.resolve())
    stats["txt_path"] = str(txt_path.resolve())
    stats["meta_path"] = str(meta_path.resolve())
    stats["examples"] = examples
    meta_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    return stats


def extract_clean_text(data: bytes, base_address: int, min_len: int):
    yield from _extract_ascii(data, base_address, min_len)
    yield from _extract_utf16le(data, base_address, min_len)


def _extract_ascii(data: bytes, base_address: int, min_len: int):
    start = None
    chars: list[str] = []
    for idx, value in enumerate(data):
        if 0x20 <= value <= 0x7E:
            if start is None:
                start = idx
            chars.append(chr(value))
        else:
            if start is not None:
                text = "".join(chars).strip()
                if _looks_like_text(text, min_len):
                    yield StringRecord(base_address + start, "ascii", text)
            start = None
            chars = []
    if start is not None:
        text = "".join(chars).strip()
        if _looks_like_text(text, min_len):
            yield StringRecord(base_address + start, "ascii", text)


def _extract_utf16le(data: bytes, base_address: int, min_len: int):
    for align in (0, 1):
        start = None
        chars: list[str] = []
        idx = align
        while idx + 1 < len(data):
            code = data[idx] | (data[idx + 1] << 8)
            if _is_allowed_codepoint(code):
                if start is None:
                    start = idx
                chars.append(chr(code))
            else:
                if start is not None:
                    text = "".join(chars).strip()
                    if _looks_like_text(text, min_len):
                        yield StringRecord(base_address + start, "utf-16le", text)
                start = None
                chars = []
            idx += 2
        if start is not None:
            text = "".join(chars).strip()
            if _looks_like_text(text, min_len):
                yield StringRecord(base_address + start, "utf-16le", text)


def _is_allowed_codepoint(code: int) -> bool:
    if code in {0x09, 0x0A, 0x0D}:
        return False
    if 0x20 <= code <= 0x7E:
        return True
    if 0x4E00 <= code <= 0x9FFF:
        return True
    if 0x3040 <= code <= 0x30FF:
        return True
    if 0xAC00 <= code <= 0xD7AF:
        return True
    if 0x2000 <= code <= 0x206F:
        return True
    if 0x20A0 <= code <= 0x20CF:
        return True
    if 0x3000 <= code <= 0x303F:
        return True
    if 0xFF00 <= code <= 0xFFEF:
        return True
    return False


def _looks_like_text(text: str, min_len: int) -> bool:
    if len(text) < min_len:
        return False
    if len(text) > 500:
        return False
    useful = sum(1 for ch in text if ch.isalnum() or _is_cjk(ch))
    if useful < min_len:
        return False
    if useful / max(1, len(text)) < 0.35:
        return False
    return True


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF or 0x3040 <= code <= 0x30FF or 0xAC00 <= code <= 0xD7AF
