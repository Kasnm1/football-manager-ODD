from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


COMPETITION_ID = 1301394
TEAM_IDS = {
    "slavia_prague": 474,
    "qarabag": 127787,
    "salzburg": 158,
    "pogon_szczecin": 1459,
    "fenerbahce": 1870,
    "brondby": 496,
}

FIXTURES = (
    {
        "key": "champions_path_slavia_qarabag",
        "ids": (474, 127787),
        "first_leg": ("2:0", "2-0"),
        "terms": (
            "Slavia Prague",
            "SK Slavia Praha",
            "Qarabag",
            "Qaraba\u011f",
            "\u5e03\u62c9\u683c\u65af\u62c9\u7ef4\u4e9a",
            "\u5361\u62c9\u5df4\u8d6b",
        ),
    },
    {
        "key": "champions_path_salzburg_pogon",
        "ids": (158, 1459),
        "first_leg": ("1:2", "1-2"),
        "terms": (
            "Red Bull Salzburg",
            "Salzburg",
            "Pogon Szczecin",
            "Pogo\u0144 Szczecin",
            "\u8428\u5c14\u8328\u5821\u7ea2\u725b",
            "\u8428\u5c14\u8328\u5821",
            "\u4ec0\u5207\u9752\u6ce2\u8d21",
        ),
    },
    {
        "key": "league_path_fenerbahce_brondby",
        "ids": (1870, 496),
        "first_leg": ("2:0", "2-0"),
        "terms": (
            "Fenerbahce",
            "Fenerbah\u00e7e",
            "Brondby",
            "Br\u00f8ndby",
            "\u8d39\u5185\u5df4\u5207",
            "\u5e03\u9686\u5fb7\u6bd4",
        ),
    },
)

STAGE_TERMS = (
    str(COMPETITION_ID),
    "UEFA Champions League",
    "Champions League",
    "Champions Path",
    "League Path",
    "Play-off",
    "Playoff",
    "Second Leg",
    "\u6b27\u51a0",
    "\u6b27\u51a0\u8054\u8d5b",
    "\u6b27\u6d32\u51a0\u519b\u8054\u8d5b",
    "\u51a0\u519b\u4e4b\u8def",
    "\u8054\u8d5b\u4e4b\u8def",
    "\u9644\u52a0\u8d5b\u7b2c\u4e8c\u56de\u5408",
)

SCORE_ONLY = {"2:0", "2-0", "1:2", "1-2"}
NOISE_MARKERS = (
    "StreamingAssets",
    "Assets/",
    "UnityEngine",
    ".uxml",
    ".asset",
    "ScriptableObjects",
    "锟",
)


def clean_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def decode_json_stream(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    decoder = json.JSONDecoder()
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        obj, end = decoder.raw_decode(text, pos)
        yield obj
        pos = end


def region_key(address: int, bits: int = 20) -> str:
    base = address & ~((1 << bits) - 1)
    return f"0x{base:x}"


def term_hits(text: str) -> list[str]:
    low = text.casefold()
    hits: list[str] = []
    for term in STAGE_TERMS:
        if term.casefold() in low:
            hits.append(term)
    for fixture in FIXTURES:
        for term in fixture["terms"]:
            if term.casefold() in low:
                hits.append(f"{fixture['key']}:{term}")
        for score in fixture["first_leg"]:
            if score in text:
                hits.append(f"{fixture['key']}:score:{score}")
    return hits


def row_score(row: dict[str, object]) -> tuple[int, list[str]]:
    keyword = str(row.get("keyword", ""))
    context = clean_text(str(row.get("context_utf8", ""))) + " " + clean_text(str(row.get("context_utf16le", "")))
    hits = term_hits(context)
    score = 0
    reasons: list[str] = []

    if keyword not in SCORE_ONLY:
        score += 4
        reasons.append(f"keyword:{keyword}")
    if keyword in SCORE_ONLY:
        score -= 2
        reasons.append("score_keyword")
    for hit in hits:
        if ":score:" in hit:
            score += 1
        elif hit in STAGE_TERMS:
            score += 5
        else:
            score += 6
        reasons.append(hit)
    if sum(1 for hit in hits if ":score:" not in hit) >= 2:
        score += 6
        reasons.append("multi_non_score_terms")
    if any(marker in context for marker in NOISE_MARKERS):
        score -= 5
        reasons.append("noise_marker")

    printable = sum(1 for ch in context if ch.isprintable())
    if context and printable / max(1, len(context)) < 0.45:
        score -= 3
        reasons.append("low_printable_ratio")
    return score, reasons[:20]


def parse_rows(path: Path) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows = []
    keyword_counts: Counter[str] = Counter()
    address_buckets: Counter[str] = Counter()
    best_by_bucket: dict[str, dict[str, object]] = {}

    for row in decode_json_stream(path):
        address = int(str(row["address"]), 16)
        keyword = str(row.get("keyword", ""))
        keyword_counts[keyword] += 1
        bucket = region_key(address)
        address_buckets[bucket] += 1
        score, reasons = row_score(row)

        context = clean_text(str(row.get("context_utf8", "")))
        if len(context) > 280:
            context = context[:280]
        item = {
            "address": f"0x{address:x}",
            "bucket": bucket,
            "keyword": keyword,
            "encoding": row.get("encoding"),
            "score": score,
            "reasons": reasons,
            "context_preview": context,
        }
        rows.append(item)
        prev = best_by_bucket.get(bucket)
        if prev is None or score > int(prev["score"]):
            best_by_bucket[bucket] = item

    summary = {
        "rows": len(rows),
        "keyword_counts": keyword_counts.most_common(),
        "top_address_buckets": address_buckets.most_common(30),
        "best_by_bucket": sorted(best_by_bucket.values(), key=lambda r: int(r["score"]), reverse=True)[:80],
    }
    return rows, summary


def id_patterns() -> list[tuple[str, int, str, bytes]]:
    patterns = []
    for label, value in {"ucl": COMPETITION_ID, **TEAM_IDS}.items():
        patterns.append((label, value, "u32", struct.pack("<I", value)))
        patterns.append((label, value, "i32", struct.pack("<i", value)))
        patterns.append((label, value, "u64", struct.pack("<Q", value)))
    return patterns


def find_pattern_offsets(data: bytes, pattern: bytes, limit: int = 300) -> list[int]:
    offsets = []
    start = 0
    while len(offsets) < limit:
        idx = data.find(pattern, start)
        if idx < 0:
            break
        offsets.append(idx)
        start = idx + 1
    return offsets


def strings_near(data: bytes, base: int, limit: int = 50) -> list[dict[str, object]]:
    results = []
    for record in extract_strings(data, base, min_len=3):
        text = clean_text(record.text)
        if not text:
            continue
        if len(text) > 180:
            text = text[:180]
        results.append({"address": f"0x{record.address:x}", "encoding": record.encoding, "text": text})
        if len(results) >= limit:
            break
    return results


def local_binary_scan(pid: int, candidates: list[dict[str, object]], out_dir: Path, window: int) -> dict[str, object]:
    selected_addresses = []
    for item in candidates:
        if int(item["score"]) > 0 or str(item["keyword"]) not in SCORE_ONLY:
            selected_addresses.append(int(str(item["address"]), 16))
    if not selected_addresses:
        selected_addresses = [int(str(item["address"]), 16) for item in candidates[:40]]

    ranges = []
    for address in selected_addresses[:120]:
        ranges.append((max(0, address - window), address + window))

    merged = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    hits = []
    capture_rows = []
    bytes_read = 0
    with open_process(pid) as process:
        regions = list(iter_readable_regions(process))
        for want_start, want_end in merged:
            for region in regions:
                if region.type == MEM_IMAGE:
                    continue
                reg_start = region.base_address
                reg_end = region.base_address + region.size
                start = max(want_start, reg_start)
                end = min(want_end, reg_end)
                if start >= end:
                    continue
                data = read_process_memory(process, start, end - start)
                if not data:
                    continue
                bytes_read += len(data)
                ids_here = Counter()
                for label, value, encoding, pattern in id_patterns():
                    for offset in find_pattern_offsets(data, pattern):
                        ids_here[value] += 1
                        hits.append(
                            {
                                "address": f"0x{start + offset:x}",
                                "range": f"0x{start:x}-0x{end:x}",
                                "label": label,
                                "value": value,
                                "encoding": encoding,
                            }
                        )
                if ids_here:
                    capture_rows.append(
                        {
                            "range": f"0x{start:x}-0x{end:x}",
                            "size": len(data),
                            "ids": dict(sorted(ids_here.items())),
                            "strings": strings_near(data, start),
                        }
                    )

    meta = {
        "pid": pid,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_addresses": len(selected_addresses),
        "ranges_scanned": len(merged),
        "bytes_read": bytes_read,
        "binary_hits": len(hits),
        "captures_with_ids": len(capture_rows),
    }

    hits_path = out_dir / "local_binary_hits.jsonl"
    with hits_path.open("w", encoding="utf-8") as f:
        for hit in hits:
            f.write(json.dumps(hit, ensure_ascii=False) + "\n")
    captures_path = out_dir / "local_captures_with_ids.json"
    captures_path.write_text(json.dumps(capture_rows[:250], ensure_ascii=False, indent=2), encoding="utf-8")
    meta["hits_path"] = str(hits_path.resolve())
    meta["captures_path"] = str(captures_path.resolve())
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", type=Path)
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--out-dir", type=Path, default=Path("data/probes/ucl_playoff_index"))
    parser.add_argument("--window", type=lambda s: int(s, 0), default=0x20000)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_dir / f"baseline_{args.pid}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, summary = parse_rows(args.jsonl)
    ranked = sorted(rows, key=lambda r: int(r["score"]), reverse=True)
    useful = [row for row in ranked if int(row["score"]) > 0 or str(row["keyword"]) not in SCORE_ONLY]
    summary["top_ranked"] = ranked[:100]
    summary["useful_count"] = len(useful)
    summary["source_jsonl"] = str(args.jsonl.resolve())
    summary["out_dir"] = str(out_dir.resolve())

    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "ranked_candidates.jsonl").open("w", encoding="utf-8") as f:
        for row in ranked[:500]:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    scan_meta = local_binary_scan(args.pid, ranked[:160], out_dir, args.window)
    (out_dir / "local_binary_meta.json").write_text(json.dumps(scan_meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"out_dir": str(out_dir.resolve()), "summary": summary, "local_binary": scan_meta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
