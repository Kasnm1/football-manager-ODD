from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


RARE_IDS = {"ucl": 1301394, "qarabag": 127787}
RELATED_IDS = {
    "slavia_prague": 474,
    "salzburg": 158,
    "pogon_szczecin": 1459,
    "fenerbahce": 1870,
    "brondby": 496,
}


def build_patterns(ids: dict[str, int]):
    out = []
    for label, value in ids.items():
        out.append((label, value, "u32", struct.pack("<I", value)))
        out.append((label, value, "i32", struct.pack("<i", value)))
        out.append((label, value, "u64", struct.pack("<Q", value)))
    return out


def find_all(data: bytes, pat: bytes):
    pos = 0
    while True:
        idx = data.find(pat, pos)
        if idx < 0:
            return
        yield idx
        pos = idx + 1


def clean(text: str) -> str:
    return " ".join(text.replace("\x00", "").split())


def strings_from_window(data: bytes, base: int, limit: int = 60):
    rows = []
    for record in extract_strings(data, base, min_len=3):
        text = clean(record.text)
        if not text:
            continue
        if len(text) > 180:
            text = text[:180]
        rows.append({"address": f"0x{record.address:x}", "encoding": record.encoding, "text": text})
        if len(rows) >= limit:
            break
    return rows


def scan(pid: int, out_dir: Path, chunk_mb: int, context: int):
    out_dir.mkdir(parents=True, exist_ok=True)
    rare_patterns = build_patterns(RARE_IDS)
    related_patterns = build_patterns({**RARE_IDS, **RELATED_IDS})
    chunk = chunk_mb * 1024 * 1024
    overlap = max(len(p[-1]) for p in rare_patterns) - 1
    hits_path = out_dir / "rare_id_hits.jsonl"
    windows_path = out_dir / "rare_id_windows.jsonl"
    meta_path = out_dir / "rare_id_meta.json"
    counts: Counter[int] = Counter()
    saved_windows = 0
    started = time.perf_counter()
    stats = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "chunk_mb": chunk_mb,
        "rare_ids": RARE_IDS,
        "related_ids": RELATED_IDS,
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
    }
    with open_process(pid) as process, hits_path.open("w", encoding="utf-8") as hits, windows_path.open(
        "w", encoding="utf-8"
    ) as windows:
        for region in iter_readable_regions(process):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE:
                stats["regions_skipped_executable"] += 1
                continue
            stats["regions_scanned"] += 1
            carry = b""
            for off in range(0, region.size, chunk):
                addr = region.base_address + off
                data = read_process_memory(process, addr, min(chunk, region.size - off))
                stats["read_attempts"] += 1
                if not data:
                    stats["read_failures"] += 1
                    carry = b""
                    continue
                scan_data = carry + data
                scan_base = addr - len(carry)
                stats["bytes_read"] += len(data)
                for label, value, encoding, pat in rare_patterns:
                    for pos in find_all(scan_data, pat):
                        abs_addr = scan_base + pos
                        if abs_addr < addr - overlap:
                            continue
                        counts[value] += 1
                        hit = {
                            "address": f"0x{abs_addr:x}",
                            "label": label,
                            "value": value,
                            "encoding": encoding,
                            "region": f"0x{region.base_address:x}-0x{region.base_address + region.size:x}",
                        }
                        hits.write(json.dumps(hit, ensure_ascii=False) + "\n")
                        hits.flush()

                        start = max(region.base_address, abs_addr - context)
                        end = min(region.base_address + region.size, abs_addr + context)
                        window_data = read_process_memory(process, start, end - start)
                        related = Counter()
                        if window_data:
                            for r_label, r_value, r_encoding, r_pat in related_patterns:
                                n = window_data.count(r_pat)
                                if n:
                                    related[f"{r_value}:{r_encoding}"] += n
                            windows.write(
                                json.dumps(
                                    {
                                        **hit,
                                        "window": f"0x{start:x}-0x{end:x}",
                                        "related_counts": dict(sorted(related.items())),
                                        "strings": strings_from_window(window_data, start),
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                            windows.flush()
                            saved_windows += 1
                carry = data[-overlap:] if overlap else b""

    stats.update(
        {
            "duration_seconds": round(time.perf_counter() - started, 3),
            "counts": dict(sorted(counts.items())),
            "saved_windows": saved_windows,
            "hits_path": str(hits_path.resolve()),
            "windows_path": str(windows_path.resolve()),
            "meta_path": str(meta_path.resolve()),
        }
    )
    meta_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--out-root", type=Path, default=Path("data/probes/ucl_playoff_rare_ids"))
    parser.add_argument("--chunk-mb", type=int, default=32)
    parser.add_argument("--context", type=lambda s: int(s, 0), default=0x40000)
    args = parser.parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_root / f"baseline_{args.pid}_{stamp}"
    result = scan(args.pid, out_dir, args.chunk_mb, args.context)
    print(json.dumps({"out_dir": str(out_dir.resolve()), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
