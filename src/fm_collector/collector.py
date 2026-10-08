from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .strings import StringRecord, extract_strings
from .win32 import (
    MEM_IMAGE,
    ProcessHandle,
    find_processes,
    iter_readable_regions,
    open_process,
    read_process_memory,
)


@dataclass(frozen=True)
class SnapshotConfig:
    pid: int | None
    name: str
    output_dir: Path
    max_read_mb: int
    chunk_mb: int
    min_len: int
    keywords: tuple[str, ...]
    max_records: int
    max_unique: int
    include_executable: bool


def _choose_pid(config: SnapshotConfig) -> int:
    if config.pid is not None:
        return config.pid

    matches = [p for p in find_processes(config.name) if p.name.lower() == "fm.exe" or p.name.lower() == "fm"]
    if not matches:
        matches = find_processes(config.name)
    if not matches:
        raise RuntimeError(f"no process matched {config.name!r}")
    if len(matches) > 1:
        fm_named = [p for p in matches if p.name.lower() in {"fm", "fm.exe"}]
        if len(fm_named) == 1:
            return fm_named[0].pid
    return matches[0].pid


def _contains_keyword(text: str, keywords: Iterable[str]) -> bool:
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def make_keyword_patterns(keywords: Iterable[str]) -> tuple[tuple[str, str, bytes], ...]:
    patterns: list[tuple[str, str, bytes]] = []
    for keyword in keywords:
        if not keyword:
            continue
        for encoding in ("utf-8", "utf-16le", "utf-16be"):
            patterns.append((keyword, encoding, keyword.encode(encoding)))
    return tuple(patterns)


def _record_to_json(record: StringRecord) -> dict[str, object]:
    return {
        "address": f"0x{record.address:x}",
        "encoding": record.encoding,
        "text": record.text,
    }


def run_snapshot(config: SnapshotConfig) -> dict[str, object]:
    pid = _choose_pid(config)
    config.output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"fm26_snapshot_{pid}_{stamp}"
    jsonl_path = config.output_dir / f"{base_name}.jsonl"
    txt_path = config.output_dir / f"{base_name}.txt"
    meta_path = config.output_dir / f"{base_name}.meta.json"
    hits_path = config.output_dir / f"{base_name}.hits.jsonl"

    max_bytes = None if config.max_read_mb <= 0 else config.max_read_mb * 1024 * 1024
    chunk_size = config.chunk_mb * 1024 * 1024
    start = time.perf_counter()

    stats = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
        "records_written": 0,
        "unique_written": 0,
        "keyword_hits": 0,
        "max_read_mb": config.max_read_mb,
        "chunk_mb": config.chunk_mb,
        "min_len": config.min_len,
        "include_executable": config.include_executable,
        "keywords": list(config.keywords),
    }

    unique: set[str] = set()
    hit_examples: list[dict[str, object]] = []

    with open_process(pid) as process, jsonl_path.open("w", encoding="utf-8") as jsonl, hits_path.open(
        "w", encoding="utf-8"
    ) as hits:
        for region in iter_readable_regions(process):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE and not config.include_executable:
                stats["regions_skipped_executable"] += 1
                continue

            if max_bytes is not None and stats["bytes_read"] >= max_bytes:
                break

            stats["regions_scanned"] += 1
            for address, data in _read_region_chunks(process, region.base_address, region.size, chunk_size):
                stats["read_attempts"] += 1
                if not data:
                    stats["read_failures"] += 1
                    continue

                remaining = None if max_bytes is None else max_bytes - stats["bytes_read"]
                if remaining is not None and remaining <= 0:
                    break
                if remaining is not None and len(data) > remaining:
                    data = data[:remaining]

                stats["bytes_read"] += len(data)

                for record in extract_strings(data, address, min_len=config.min_len):
                    if stats["records_written"] < config.max_records:
                        jsonl.write(json.dumps(_record_to_json(record), ensure_ascii=False) + "\n")
                        stats["records_written"] += 1

                    if len(unique) < config.max_unique:
                        unique.add(record.text)

                    if config.keywords and _contains_keyword(record.text, config.keywords):
                        row = _record_to_json(record)
                        hits.write(json.dumps(row, ensure_ascii=False) + "\n")
                        stats["keyword_hits"] += 1
                        if len(hit_examples) < 50:
                            hit_examples.append(row)

                if max_bytes is not None and stats["bytes_read"] >= max_bytes:
                    break
            if max_bytes is not None and stats["bytes_read"] >= max_bytes:
                break

    with txt_path.open("w", encoding="utf-8") as txt:
        for text in sorted(unique, key=lambda x: (x.casefold(), x)):
            txt.write(text.replace("\r", " ").replace("\n", " ") + "\n")
    stats["unique_written"] = len(unique)
    stats["duration_seconds"] = round(time.perf_counter() - start, 3)
    stats["jsonl_path"] = str(jsonl_path.resolve())
    stats["txt_path"] = str(txt_path.resolve())
    stats["meta_path"] = str(meta_path.resolve())
    stats["hits_path"] = str(hits_path.resolve())
    stats["hit_examples"] = hit_examples

    meta_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    return stats


@dataclass(frozen=True)
class MemorySearchConfig:
    pid: int | None
    name: str
    output_dir: Path
    max_read_mb: int
    chunk_mb: int
    keywords: tuple[str, ...]
    max_hits: int
    include_executable: bool
    ranges: tuple[tuple[int, int], ...] = ()
    encodings: tuple[str, ...] = ("utf-8", "utf-16le", "utf-16be")
    context_bytes: int = 96


def run_memory_search(config: MemorySearchConfig) -> dict[str, object]:
    if not config.keywords:
        raise RuntimeError("at least one keyword is required")

    pid = _choose_pid(
        SnapshotConfig(
            pid=config.pid,
            name=config.name,
            output_dir=config.output_dir,
            max_read_mb=config.max_read_mb,
            chunk_mb=config.chunk_mb,
            min_len=3,
            keywords=config.keywords,
            max_records=1,
            max_unique=1,
            include_executable=config.include_executable,
        )
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = f"fm26_memsearch_{pid}_{stamp}"
    hits_path = config.output_dir / f"{base_name}.jsonl"
    meta_path = config.output_dir / f"{base_name}.meta.json"

    max_bytes = None if config.max_read_mb <= 0 else config.max_read_mb * 1024 * 1024
    chunk_size = config.chunk_mb * 1024 * 1024
    patterns = make_keyword_patterns_for_encodings(config.keywords, config.encodings)
    overlap = max(len(pat) for _, _, pat in patterns) + max(128, config.context_bytes)
    start = time.perf_counter()
    hits: list[dict[str, object]] = []

    stats = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
        "hits": 0,
        "max_read_mb": config.max_read_mb,
        "chunk_mb": config.chunk_mb,
        "include_executable": config.include_executable,
        "keywords": list(config.keywords),
        "ranges": [f"0x{start:x}-0x{end:x}" for start, end in config.ranges],
        "encodings": list(config.encodings),
        "context_bytes": config.context_bytes,
    }

    with open_process(pid) as process, hits_path.open("w", encoding="utf-8") as out:
        for region in iter_readable_regions(process):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE and not config.include_executable:
                stats["regions_skipped_executable"] += 1
                continue
            if max_bytes is not None and stats["bytes_read"] >= max_bytes:
                break

            for scan_start, scan_size in _iter_region_scan_spans(region.base_address, region.size, config.ranges):
                stats["regions_scanned"] += 1
                carry = b""
                carry_base = scan_start
                for address, data in _read_region_chunks(process, scan_start, scan_size, chunk_size):
                    stats["read_attempts"] += 1
                    if not data:
                        stats["read_failures"] += 1
                        carry = b""
                        carry_base = address + chunk_size
                        continue

                    remaining = None if max_bytes is None else max_bytes - stats["bytes_read"]
                    if remaining is not None and remaining <= 0:
                        break
                    if remaining is not None and len(data) > remaining:
                        data = data[:remaining]
                    stats["bytes_read"] += len(data)

                    block = carry + data
                    block_base = carry_base if carry else address
                    for hit in _search_block(block, block_base, patterns, config.context_bytes):
                        out.write(json.dumps(hit, ensure_ascii=False) + "\n")
                        hits.append(hit)
                        stats["hits"] += 1
                        if stats["hits"] >= config.max_hits:
                            break
                    if stats["hits"] >= config.max_hits:
                        break

                    if len(block) > overlap:
                        carry = block[-overlap:]
                        carry_base = block_base + len(block) - len(carry)
                    else:
                        carry = block
                        carry_base = block_base
                if stats["hits"] >= config.max_hits:
                    break
                if max_bytes is not None and stats["bytes_read"] >= max_bytes:
                    break
            if stats["hits"] >= config.max_hits:
                break
            if max_bytes is not None and stats["bytes_read"] >= max_bytes:
                break

    stats["duration_seconds"] = round(time.perf_counter() - start, 3)
    stats["hits_path"] = str(hits_path.resolve())
    stats["meta_path"] = str(meta_path.resolve())
    stats["hit_examples"] = hits[:50]
    meta_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    return stats


def make_keyword_patterns_for_encodings(
    keywords: Iterable[str], encodings: Iterable[str]
) -> tuple[tuple[str, str, bytes], ...]:
    allowed = tuple(dict.fromkeys(enc.strip().lower() for enc in encodings if enc.strip()))
    if not allowed:
        raise RuntimeError("at least one encoding is required")
    patterns: list[tuple[str, str, bytes]] = []
    for keyword in keywords:
        if not keyword:
            continue
        for encoding in allowed:
            patterns.append((keyword, encoding, keyword.encode(encoding)))
    return tuple(patterns)


def _iter_region_scan_spans(
    region_base: int, region_size: int, ranges: tuple[tuple[int, int], ...]
):
    if not ranges:
        yield region_base, region_size
        return

    region_end = region_base + region_size
    for wanted_start, wanted_end in ranges:
        start = max(region_base, wanted_start)
        end = min(region_end, wanted_end)
        if start < end:
            yield start, end - start


def _search_block(
    block: bytes,
    base_address: int,
    patterns: tuple[tuple[str, str, bytes], ...],
    context_bytes: int,
):
    for keyword, encoding, pattern in patterns:
        start = 0
        while True:
            idx = block.find(pattern, start)
            if idx < 0:
                break
            context_start = max(0, idx - context_bytes)
            context_end = min(len(block), idx + len(pattern) + context_bytes)
            context = block[context_start:context_end]
            yield {
                "address": f"0x{base_address + idx:x}",
                "keyword": keyword,
                "encoding": encoding,
                "context_hex": context.hex(" "),
                "context_utf8": context.decode("utf-8", errors="replace").replace("\x00", ""),
                "context_utf16le": _decode_context(context, "utf-16le"),
            }
            start = idx + 1


def _decode_context(data: bytes, encoding: str) -> str:
    if len(data) % 2:
        data = data[:-1]
    return data.decode(encoding, errors="replace").replace("\x00", "")


def _read_region_chunks(
    process: ProcessHandle,
    base_address: int,
    size: int,
    chunk_size: int,
):
    offset = 0
    while offset < size:
        address = base_address + offset
        length = min(chunk_size, size - offset)
        data = read_process_memory(process, address, length)
        if data is None and length > 0x10000:
            sub_offset = 0
            while sub_offset < length:
                sub_address = address + sub_offset
                sub_length = min(0x10000, length - sub_offset)
                yield sub_address, read_process_memory(process, sub_address, sub_length)
                sub_offset += sub_length
        else:
            yield address, data
        offset += length
