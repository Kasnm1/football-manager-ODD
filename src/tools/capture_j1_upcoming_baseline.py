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


COMPETITION_ID = 102428

TEAM_IDS = {
    "kashiwa_reysol": 1190,
    "vissel_kobe": 106844,
    "jubilo_iwata": 1188,
    "fc_tokyo": 107313,
    "roasso_kumamoto": 786558,
    "yokohama_fc": 7100042,
    "fagiano_okayama": 786547,
    "urawa_reds": 1195,
    "sanfrecce_hiroshima": 1193,
    "machida_zelvia": 788837,
    "shimizu_s_pulse": 1194,
    "tokyo_verdy": 1196,
    "kashima_antlers": 1189,
    "cerezo_osaka": 1185,
    "vegalta_sendai": 107283,
    "v_varen_nagasaki": 786559,
    "yokohama_f_marinos": 1198,
    "gamba_osaka": 1186,
}

FIXTURES = (
    {"key": "kashiwa_tokyo_verdy", "home": 1190, "away": 1196, "time": "15:00", "terms": ("\u67cf\u592a\u9633\u795e", "\u4e1c\u4eac\u7eff\u8335")},
    {"key": "fc_tokyo_urawa", "home": 107313, "away": 1195, "time": "15:00", "terms": ("\u4e1c\u4eacFC", "\u6d66\u548c\u7ea2\u94bb")},
    {"key": "okayama_iwata", "home": 786547, "away": 1188, "time": "15:00", "terms": ("\u5188\u5c71\u7eff\u96c9", "\u78d0\u7530\u559c\u60a6")},
    {"key": "yokohama_fm_gamba", "home": 1198, "away": 1186, "time": "15:00", "terms": ("\u6a2a\u6ee8\u6c34\u624b", "\u5927\u962a\u94a2\u5df4")},
    {"key": "shimizu_kumamoto", "home": 1194, "away": 786558, "time": "15:00", "terms": ("\u6e05\u6c34\u5fc3\u8df3", "\u718a\u672c\u6df1\u7ea2")},
    {"key": "kobe_yokohama_fc", "home": 106844, "away": 7100042, "time": "15:00", "terms": ("\u795e\u6237\u80dc\u5229\u8239", "\u6a2a\u6ee8FC")},
    {"key": "machida_cerezo", "home": 788837, "away": 1185, "time": "15:00", "terms": ("\u753a\u7530\u6cfd\u7ef4\u4e9a", "\u5927\u962a\u6a31\u82b1")},
    {"key": "sendai_kashima", "home": 107283, "away": 1189, "time": "15:00", "terms": ("\u4ed9\u53f0\u4e03\u5915", "\u9e7f\u5c9b\u9e7f\u89d2")},
    {"key": "nagasaki_hiroshima", "home": 786559, "away": 1193, "time": "15:00", "terms": ("\u957f\u5d0e\u6210\u529f\u4e38", "\u5e7f\u5c9b\u4e09\u7bad")},
)

TERMS = (
    str(COMPETITION_ID),
    "J1",
    "J1\u8054\u8d5b",
    "15:00",
    "2028\u5e748\u670826\u65e5",
    "8\u670826\u65e5",
) + tuple(term for fixture in FIXTURES for term in fixture["terms"])


def clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def id_patterns() -> list[tuple[str, int, str, bytes]]:
    patterns = [("j1", COMPETITION_ID, "u32", struct.pack("<I", COMPETITION_ID))]
    patterns.append(("j1", COMPETITION_ID, "i32", struct.pack("<i", COMPETITION_ID)))
    patterns.append(("j1", COMPETITION_ID, "u64", struct.pack("<Q", COMPETITION_ID)))
    for label, value in TEAM_IDS.items():
        patterns.append((label, value, "u32", struct.pack("<I", value)))
        patterns.append((label, value, "i32", struct.pack("<i", value)))
        patterns.append((label, value, "u64", struct.pack("<Q", value)))
    return patterns


def count_ids(data: bytes) -> Counter[str]:
    counts: Counter[str] = Counter()
    for label, value, encoding, pattern in id_patterns():
        n = data.count(pattern)
        if n:
            counts[f"{label}:{value}:{encoding}"] += n
    return counts


def term_hits(text: str) -> list[str]:
    low = text.casefold()
    return [term for term in TERMS if term.casefold() in low]


def load_known_buckets(max_buckets: int) -> list[int]:
    buckets: list[int] = []
    paths = [
        Path("data/probes/ucl_playoff_index/baseline_25820_20260710_235756/summary.json"),
        Path("data/probes/ucl_playoff_post_results/post_25820_20260711_001125/post_result_captures.json"),
    ]
    if paths[0].exists():
        summary = json.loads(paths[0].read_text(encoding="utf-8"))
        for item in summary.get("best_by_bucket", []):
            buckets.append(int(str(item["bucket"]), 16))
        for bucket, _count in summary.get("top_address_buckets", []):
            buckets.append(int(str(bucket), 16))
    if paths[1].exists():
        captures = json.loads(paths[1].read_text(encoding="utf-8"))
        for capture in captures:
            buckets.append(int(str(capture["bucket"]), 16))
    dedup = []
    seen = set()
    for bucket in buckets:
        if bucket in seen:
            continue
        seen.add(bucket)
        dedup.append(bucket)
        if len(dedup) >= max_buckets:
            break
    return dedup


def scan_strings(data: bytes, base: int, string_limit: int) -> tuple[list[dict[str, object]], Counter[str]]:
    rows = []
    counts: Counter[str] = Counter()
    for record in extract_strings(data, base, min_len=2):
        text = clean(record.text)
        if not text:
            continue
        hits = term_hits(text)
        if not hits:
            continue
        counts.update(hits)
        rows.append(
            {
                "address": f"0x{record.address:x}",
                "encoding": record.encoding,
                "hits": hits,
                "text": text if len(text) <= 300 else text[:300],
            }
        )
        if len(rows) >= string_limit:
            break
    return rows, counts


def fixture_status(total_terms: Counter[str], total_ids: Counter[str]) -> list[dict[str, object]]:
    rows = []
    for fixture in FIXTURES:
        home_label = next(label for label, value in TEAM_IDS.items() if value == fixture["home"])
        away_label = next(label for label, value in TEAM_IDS.items() if value == fixture["away"])
        home_id_hits = sum(n for key, n in total_ids.items() if key.startswith(f"{home_label}:{fixture['home']}:"))
        away_id_hits = sum(n for key, n in total_ids.items() if key.startswith(f"{away_label}:{fixture['away']}:"))
        term_hits_count = {term: total_terms.get(term, 0) for term in fixture["terms"]}
        rows.append(
            {
                "key": fixture["key"],
                "home_id": fixture["home"],
                "away_id": fixture["away"],
                "time": fixture["time"],
                "terms": fixture["terms"],
                "term_hits": term_hits_count,
                "home_id_hits": home_id_hits,
                "away_id_hits": away_id_hits,
                "confirmed_by_text": all(total_terms.get(term, 0) > 0 for term in fixture["terms"]),
                "confirmed_by_ids": home_id_hits > 0 and away_id_hits > 0,
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--out-root", type=Path, default=Path("data/probes/j1_upcoming_baseline"))
    parser.add_argument("--max-buckets", type=int, default=120)
    parser.add_argument("--bucket-size", type=lambda s: int(s, 0), default=0x100000)
    parser.add_argument("--string-limit", type=int, default=220)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_root / f"before_matchday_{args.pid}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    buckets = load_known_buckets(args.max_buckets)
    captures = []
    total_terms: Counter[str] = Counter()
    total_ids: Counter[str] = Counter()
    bytes_read = 0
    with open_process(args.pid) as process:
        regions = [region for region in iter_readable_regions(process) if region.type != MEM_IMAGE]
        for bucket in buckets:
            want_start = bucket
            want_end = bucket + args.bucket_size
            for region in regions:
                start = max(want_start, region.base_address)
                end = min(want_end, region.base_address + region.size)
                if start >= end:
                    continue
                data = read_process_memory(process, start, end - start)
                if not data:
                    continue
                bytes_read += len(data)
                ids = count_ids(data)
                strings, terms = scan_strings(data, start, args.string_limit)
                total_ids.update(ids)
                total_terms.update(terms)
                if ids or strings:
                    captures.append(
                        {
                            "range": f"0x{start:x}-0x{end:x}",
                            "bucket": f"0x{bucket:x}",
                            "size": len(data),
                            "ids": dict(sorted(ids.items())),
                            "term_counts": dict(sorted(terms.items())),
                            "strings": strings,
                        }
                    )

    meta = {
        "pid": args.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "competition_id": COMPETITION_ID,
        "fixtures": list(FIXTURES),
        "buckets_requested": len(buckets),
        "bytes_read": bytes_read,
        "captures": len(captures),
        "total_term_counts": dict(sorted(total_terms.items())),
        "total_id_counts": dict(sorted(total_ids.items())),
        "fixture_status": fixture_status(total_terms, total_ids),
    }
    (out_dir / "j1_upcoming_captures.json").write_text(json.dumps(captures, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "j1_upcoming_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir.resolve()), "meta": meta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
