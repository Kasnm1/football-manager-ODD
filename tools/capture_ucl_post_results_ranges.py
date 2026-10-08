from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter
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

TERMS = (
    "\u6b27\u51a0",
    "\u6b27\u51a0\u8054\u8d5b",
    "\u6b27\u6d32\u51a0\u519b\u8054\u8d5b",
    "\u51a0\u519b\u4e4b\u8def",
    "\u8054\u8d5b\u4e4b\u8def",
    "\u9644\u52a0\u8d5b\u7b2c\u4e8c\u56de\u5408",
    "\u5e03\u62c9\u683c\u65af\u62c9\u7ef4\u4e9a",
    "\u5361\u62c9\u5df4\u8d6b",
    "\u8428\u5c14\u8328\u5821\u7ea2\u725b",
    "\u8428\u5c14\u8328\u5821",
    "\u4ec0\u5207\u9752\u6ce2\u8d21",
    "\u8d39\u5185\u5df4\u5207",
    "\u5e03\u9686\u5fb7\u6bd4",
    "Slavia Prague",
    "Qarabag",
    "Salzburg",
    "Pogon Szczecin",
    "Fenerbahce",
    "Brondby",
    "3-2",
    "3 - 2",
    "3:2",
    "3-1",
    "3 - 1",
    "3:1",
    "0-1",
    "0 - 1",
    "0:1",
    "5-2",
    "5 - 2",
    "4-3",
    "4 - 3",
    "2-1",
    "2 - 1",
)


def clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def id_patterns():
    rows = []
    for label, value in TARGET_IDS.items():
        rows.append((label, value, "u32", struct.pack("<I", value)))
        rows.append((label, value, "i32", struct.pack("<i", value)))
        rows.append((label, value, "u64", struct.pack("<Q", value)))
    return rows


def load_baseline_buckets(path: Path, max_buckets: int) -> list[int]:
    summary = json.loads(path.read_text(encoding="utf-8"))
    buckets: list[int] = []
    for item in summary.get("best_by_bucket", []):
        if int(item.get("score", 0)) >= 9:
            buckets.append(int(str(item["bucket"]), 16))
    for bucket, _count in summary.get("top_address_buckets", []):
        buckets.append(int(str(bucket), 16))
    dedup = []
    seen = set()
    for b in buckets:
        if b not in seen:
            seen.add(b)
            dedup.append(b)
        if len(dedup) >= max_buckets:
            break
    return dedup


def count_ids(data: bytes) -> Counter[str]:
    counts: Counter[str] = Counter()
    for label, value, encoding, pattern in id_patterns():
        n = data.count(pattern)
        if n:
            counts[f"{label}:{value}:{encoding}"] += n
    return counts


def term_hits(text: str) -> list[str]:
    low = text.casefold()
    hits = []
    for term in TERMS:
        if term.casefold() in low:
            hits.append(term)
    return hits


def scan_range(data: bytes, base: int, string_limit: int) -> tuple[list[dict[str, object]], Counter[str]]:
    rows = []
    term_counts: Counter[str] = Counter()
    for record in extract_strings(data, base, min_len=2):
        text = clean(record.text)
        if not text:
            continue
        hits = term_hits(text)
        if not hits:
            continue
        for hit in hits:
            term_counts[hit] += 1
        preview = text if len(text) <= 260 else text[:260]
        rows.append(
            {
                "address": f"0x{record.address:x}",
                "encoding": record.encoding,
                "hits": hits,
                "text": preview,
            }
        )
        if len(rows) >= string_limit:
            break
    return rows, term_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--baseline-summary", type=Path, default=Path("data/probes/ucl_playoff_index/baseline_25820_20260710_235756/summary.json"))
    parser.add_argument("--out-root", type=Path, default=Path("data/probes/ucl_playoff_post_results"))
    parser.add_argument("--max-buckets", type=int, default=80)
    parser.add_argument("--bucket-size", type=lambda s: int(s, 0), default=0x100000)
    parser.add_argument("--string-limit", type=int, default=120)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_root / f"post_{args.pid}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets = load_baseline_buckets(args.baseline_summary, args.max_buckets)
    captures = []
    total_terms: Counter[str] = Counter()
    total_ids: Counter[str] = Counter()
    bytes_read = 0
    with open_process(args.pid) as process:
        regions = [r for r in iter_readable_regions(process) if r.type != MEM_IMAGE]
        for b in buckets:
            b_start = b
            b_end = b + args.bucket_size
            for region in regions:
                r_start = region.base_address
                r_end = region.base_address + region.size
                start = max(b_start, r_start)
                end = min(b_end, r_end)
                if start >= end:
                    continue
                data = read_process_memory(process, start, end - start)
                if not data:
                    continue
                bytes_read += len(data)
                ids = count_ids(data)
                strings, terms = scan_range(data, start, args.string_limit)
                total_terms.update(terms)
                total_ids.update(ids)
                if ids or strings:
                    captures.append(
                        {
                            "range": f"0x{start:x}-0x{end:x}",
                            "bucket": f"0x{b:x}",
                            "size": len(data),
                            "ids": dict(sorted(ids.items())),
                            "term_counts": dict(sorted(terms.items())),
                            "strings": strings,
                        }
                    )

    meta = {
        "pid": args.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "baseline_summary": str(args.baseline_summary.resolve()),
        "buckets_requested": len(buckets),
        "bytes_read": bytes_read,
        "captures": len(captures),
        "total_term_counts": dict(sorted(total_terms.items())),
        "total_id_counts": dict(sorted(total_ids.items())),
    }
    (out_dir / "post_result_captures.json").write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "post_result_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir.resolve()), "meta": meta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
