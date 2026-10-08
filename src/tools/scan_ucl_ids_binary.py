from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


TARGET_IDS = {
    "ucl": 1301394,
    "slavia_prague": 474,
    "qarabag": 127787,
    "salzburg": 158,
    "pogon_szczecin": 1459,
    "fenerbahce": 1870,
    "brondby": 496,
}

FIXTURE_KEYS = {
    "champions_path_slavia_qarabag": (474, 127787),
    "champions_path_salzburg_pogon": (158, 1459),
    "league_path_fenerbahce_brondby": (1870, 496),
}


def patterns():
    rows = []
    for label, value in TARGET_IDS.items():
        rows.append((label, value, "u32", struct.pack("<I", value)))
        rows.append((label, value, "i32", struct.pack("<i", value)))
        rows.append((label, value, "u64", struct.pack("<Q", value)))
    return rows


def find_offsets(data: bytes, pat: bytes):
    pos = 0
    while True:
        idx = data.find(pat, pos)
        if idx < 0:
            return
        yield idx
        pos = idx + 1


def bucket(address: int, bits: int = 20) -> int:
    return address & ~((1 << bits) - 1)


def clean_text(text: str) -> str:
    return " ".join(text.replace("\x00", "").split())


def extract_nearby_strings(process, start: int, size: int, limit: int = 35):
    data = read_process_memory(process, start, size)
    if not data:
        return []
    rows = []
    for record in extract_strings(data, start, min_len=3):
        text = clean_text(record.text)
        if not text:
            continue
        if len(text) > 160:
            text = text[:160]
        rows.append({"address": f"0x{record.address:x}", "encoding": record.encoding, "text": text})
        if len(rows) >= limit:
            break
    return rows


def scan(pid: int, out_dir: Path, chunk_mb: int, max_hits_per_value: int):
    out_dir.mkdir(parents=True, exist_ok=True)
    pats = patterns()
    chunk_size = chunk_mb * 1024 * 1024
    overlap = max(len(p[-1]) for p in pats) - 1

    counts: Counter[int] = Counter()
    encoding_counts: Counter[str] = Counter()
    bucket_values: dict[int, Counter[int]] = defaultdict(Counter)
    saved_hits_by_value: Counter[int] = Counter()
    hits_path = out_dir / "binary_id_hits.jsonl"
    start_time = time.perf_counter()
    stats = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
        "chunk_mb": chunk_mb,
        "target_ids": TARGET_IDS,
    }

    with open_process(pid) as process, hits_path.open("w", encoding="utf-8") as hits:
        for region in iter_readable_regions(process):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE:
                stats["regions_skipped_executable"] += 1
                continue
            stats["regions_scanned"] += 1

            carry = b""
            for offset in range(0, region.size, chunk_size):
                address = region.base_address + offset
                data = read_process_memory(process, address, min(chunk_size, region.size - offset))
                stats["read_attempts"] += 1
                if not data:
                    stats["read_failures"] += 1
                    carry = b""
                    continue
                scan_data = carry + data
                scan_base = address - len(carry)
                stats["bytes_read"] += len(data)

                for label, value, encoding, pat in pats:
                    for pos in find_offsets(scan_data, pat):
                        abs_addr = scan_base + pos
                        if abs_addr < address - overlap:
                            continue
                        counts[value] += 1
                        encoding_counts[f"{value}:{encoding}"] += 1
                        bucket_values[bucket(abs_addr)][value] += 1
                        if saved_hits_by_value[value] < max_hits_per_value:
                            hits.write(
                                json.dumps(
                                    {
                                        "address": f"0x{abs_addr:x}",
                                        "bucket": f"0x{bucket(abs_addr):x}",
                                        "label": label,
                                        "value": value,
                                        "encoding": encoding,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                            saved_hits_by_value[value] += 1
                carry = data[-overlap:] if overlap else b""

    bucket_rows = []
    for b, value_counts in bucket_values.items():
        values = set(value_counts)
        fixture_matches = [
            key for key, ids in FIXTURE_KEYS.items() if all(value in values for value in ids)
        ]
        score = len(values) * 10 + sum(value_counts.values())
        if TARGET_IDS["ucl"] in values:
            score += 40
        if fixture_matches:
            score += 35 * len(fixture_matches)
        bucket_rows.append(
            {
                "bucket": f"0x{b:x}",
                "values": dict(sorted(value_counts.items())),
                "distinct_values": len(values),
                "fixture_matches": fixture_matches,
                "score": score,
            }
        )
    bucket_rows.sort(key=lambda row: int(row["score"]), reverse=True)

    captures = []
    with open_process(pid) as process:
        for row in bucket_rows[:80]:
            b = int(str(row["bucket"]), 16)
            captures.append(
                {
                    **row,
                    "strings": extract_nearby_strings(process, b, 0x100000),
                }
            )

    stats.update(
        {
            "duration_seconds": round(time.perf_counter() - start_time, 3),
            "counts": dict(sorted(counts.items())),
            "encoding_counts": dict(sorted(encoding_counts.items())),
            "saved_hits_by_value": dict(sorted(saved_hits_by_value.items())),
            "buckets_with_any_id": len(bucket_rows),
            "top_buckets": bucket_rows[:120],
            "hits_path": str(hits_path.resolve()),
            "captures_path": str((out_dir / "binary_id_bucket_captures.json").resolve()),
            "meta_path": str((out_dir / "binary_id_scan_meta.json").resolve()),
        }
    )
    (out_dir / "binary_id_bucket_captures.json").write_text(
        json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "binary_id_scan_meta.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return stats


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--out-root", type=Path, default=Path("data/probes/ucl_playoff_binary_ids"))
    parser.add_argument("--chunk-mb", type=int, default=8)
    parser.add_argument("--max-hits-per-value", type=int, default=1000)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_root / f"baseline_{args.pid}_{stamp}"
    result = scan(args.pid, out_dir, args.chunk_mb, args.max_hits_per_value)
    print(json.dumps({"out_dir": str(out_dir.resolve()), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
