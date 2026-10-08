from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.collector import make_keyword_patterns_for_encodings
from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


PID = 25820
OUT_ROOT = Path("data/probes/ucl_playoff_baseline")

COMPETITION_ID = 1301394
TEAM_IDS = {
    "slavia_prague": 474,
    "qarabag": 127787,
    "salzburg": 158,
    "pogon_szczecin": 1459,
    "fenerbahce": 1870,
    "brondby": 496,
}
ALL_IDS = {"ucl": COMPETITION_ID, **TEAM_IDS}

FIXTURES = (
    {
        "key": "champions_path_slavia_qarabag",
        "home_id": 474,
        "away_id": 127787,
        "first_leg": "2:0",
        "terms": (
            "\u5e03\u62c9\u683c\u65af\u62c9\u7ef4\u4e9a",
            "Slavia Prague",
            "SK Slavia Praha",
            "\u5361\u62c9\u5df4\u8d6b",
            "Qarabag",
            "Qaraba\u011f",
        ),
    },
    {
        "key": "champions_path_salzburg_pogon",
        "home_id": 158,
        "away_id": 1459,
        "first_leg": "1:2",
        "terms": (
            "\u8428\u5c14\u8328\u5821\u7ea2\u725b",
            "\u8428\u5c14\u8328\u5821",
            "Red Bull Salzburg",
            "Salzburg",
            "\u4ec0\u5207\u9752\u6ce2\u8d21",
            "Pogon Szczecin",
            "Pogo\u0144 Szczecin",
        ),
    },
    {
        "key": "league_path_fenerbahce_brondby",
        "home_id": 1870,
        "away_id": 496,
        "first_leg": "2:0",
        "terms": (
            "\u8d39\u5185\u5df4\u5207",
            "Fenerbahce",
            "Fenerbah\u00e7e",
            "\u5e03\u9686\u5fb7\u6bd4",
            "Brondby",
            "Br\u00f8ndby",
        ),
    },
)

TEXT_TERMS = (
    str(COMPETITION_ID),
    "\u6b27\u51a0",
    "\u6b27\u51a0\u8054\u8d5b",
    "\u6b27\u6d32\u51a0\u519b\u8054\u8d5b",
    "\u51a0\u519b\u4e4b\u8def",
    "\u8054\u8d5b\u4e4b\u8def",
    "\u9644\u52a0\u8d5b\u7b2c\u4e8c\u56de\u5408",
    "\u51a0\u519b\u4e4b\u8def\u9644\u52a0\u8d5b\u7b2c\u4e8c\u56de\u5408",
    "\u8054\u8d5b\u4e4b\u8def\u9644\u52a0\u8d5b\u7b2c\u4e8c\u56de\u5408",
    "UEFA Champions League",
    "Champions League",
    "Champions Path",
    "League Path",
    "Playoff",
    "Play-off",
    "Second Leg",
    "2:0",
    "2-0",
    "1:2",
    "1-2",
) + tuple(term for fixture in FIXTURES for term in fixture["terms"])

NOISE_TEXT = (
    "StreamingAssets",
    "Assets/",
    "UIAssets",
    ".uxml",
    ".asset",
    "UnityEngine",
    "ScriptableObjects",
)


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    type: int

    @property
    def size(self) -> int:
        return self.end - self.start


def is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\u200b", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def summarize_context(data: bytes, base: int, limit: int = 80) -> list[dict[str, object]]:
    rows = []
    for record in extract_strings(data, base, min_len=2):
        text = clean_text(record.text)
        if not text:
            continue
        if len(text) > 240:
            text = text[:240]
        rows.append({"address": f"0x{record.address:x}", "encoding": record.encoding, "text": text})
        if len(rows) >= limit:
            break
    return rows


def wanted_id_patterns() -> list[tuple[str, int, str, bytes]]:
    patterns: list[tuple[str, int, str, bytes]] = []
    for label, value in ALL_IDS.items():
        patterns.append((label, value, "u32", struct.pack("<I", value)))
        patterns.append((label, value, "i32", struct.pack("<i", value)))
        patterns.append((label, value, "u64", struct.pack("<Q", value)))
    return patterns


def score_text(text: str, ids_found: Counter[int]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    lowered = text.casefold()

    for term in TEXT_TERMS:
        if term.casefold() in lowered:
            score += 4
            reasons.append(term)
    if COMPETITION_ID in ids_found:
        score += 12
        reasons.append(f"id:{COMPETITION_ID}")
    fixture_ids = 0
    for fixture in FIXTURES:
        home = fixture["home_id"]
        away = fixture["away_id"]
        if home in ids_found and away in ids_found:
            score += 12
            reasons.append(f"fixture_ids:{fixture['key']}")
        fixture_ids += int(home in ids_found) + int(away in ids_found)
        for term in fixture["terms"]:
            if term.casefold() in lowered:
                score += 5
                reasons.append(f"fixture_term:{fixture['key']}:{term}")
        if fixture["first_leg"] in text or fixture["first_leg"].replace(":", "-") in text:
            score += 3
            reasons.append(f"first_leg:{fixture['first_leg']}")
    if fixture_ids >= 2:
        score += 6
        reasons.append(f"target_ids={fixture_ids}")
    if "\u7b2c\u4e8c\u56de\u5408" in text or "Second Leg" in text:
        score += 5
        reasons.append("second_leg")
    if any(noise in text for noise in NOISE_TEXT):
        score -= 6
        reasons.append("noise")
    return score, reasons[:24]


def ids_in_data(data: bytes) -> Counter[int]:
    counts: Counter[int] = Counter()
    for _, value, _, pattern in wanted_id_patterns():
        count = data.count(pattern)
        if count:
            counts[value] += count
    return counts


def write_jsonl(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=PID)
    parser.add_argument("--out-root", default=str(OUT_ROOT))
    parser.add_argument("--chunk-mb", type=int, default=4)
    parser.add_argument("--max-text-hits", type=int, default=2500)
    parser.add_argument("--max-binary-hits", type=int, default=2500)
    parser.add_argument("--min-binary-score", type=int, default=8)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_root) / f"before_continue_{args.pid}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    text_path = out_dir / "text_hits.jsonl"
    binary_path = out_dir / "binary_hits.jsonl"
    candidates_path = out_dir / "candidates.json"
    meta_path = out_dir / "meta.json"

    patterns = make_keyword_patterns_for_encodings(TEXT_TERMS, ("utf-8", "utf-16le"))
    id_patterns = wanted_id_patterns()
    chunk_size = max(1, args.chunk_mb) * 1024 * 1024
    overlap = 4096

    text_hits = []
    binary_hits = []
    candidates = []
    stats = Counter()
    started = time.perf_counter()

    with open_process(args.pid) as process:
        regions = [
            Region(r.base_address, r.base_address + r.size, r.type)
            for r in iter_readable_regions(process)
            if r.type != MEM_IMAGE
        ]
        for region in regions:
            stats["regions"] += 1
            offset = 0
            carry = b""
            carry_base = region.start
            while offset < region.size:
                address = region.start + offset
                size = min(chunk_size, region.size - offset)
                data = read_process_memory(process, address, size)
                stats["read_attempts"] += 1
                if not data:
                    stats["read_failures"] += 1
                    carry = b""
                    carry_base = address + size
                    offset += size
                    continue
                stats["bytes_read"] += len(data)
                block = carry + data
                block_base = carry_base if carry else address

                ids_found = ids_in_data(block)
                rough_text = ""
                if len(text_hits) < args.max_text_hits or len(binary_hits) < args.max_binary_hits:
                    for term, encoding, pattern in patterns:
                        start = 0
                        while len(text_hits) < args.max_text_hits:
                            idx = block.find(pattern, start)
                            if idx < 0:
                                break
                            context_start = max(0, idx - 512)
                            context_end = min(len(block), idx + len(pattern) + 512)
                            context = block[context_start:context_end]
                            context_text = clean_text(
                                context.decode("utf-8", errors="ignore")
                                + " "
                                + context.decode("utf-16le", errors="ignore")
                            )
                            score, reasons = score_text(context_text, ids_found)
                            row = {
                                "address": f"0x{block_base + idx:x}",
                                "term": term,
                                "encoding": encoding,
                                "score": score,
                                "reasons": reasons,
                                "context_strings": summarize_context(context, block_base + context_start, limit=40),
                            }
                            text_hits.append(row)
                            if score > 0:
                                candidates.append({"kind": "text", **row})
                            start = idx + 1

                    if len(binary_hits) < args.max_binary_hits:
                        if not rough_text:
                            rough_text = clean_text(
                                block[: min(len(block), 256 * 1024)].decode("utf-8", errors="ignore")
                                + " "
                                + block[: min(len(block), 256 * 1024)].decode("utf-16le", errors="ignore")
                            )
                        for label, value, width, pattern in id_patterns:
                            start = 0
                            while len(binary_hits) < args.max_binary_hits:
                                idx = block.find(pattern, start)
                                if idx < 0:
                                    break
                                context_start = max(0, idx - 2048)
                                context_end = min(len(block), idx + len(pattern) + 2048)
                                context = block[context_start:context_end]
                                local_ids = ids_in_data(context)
                                local_text = clean_text(
                                    context.decode("utf-8", errors="ignore")
                                    + " "
                                    + context.decode("utf-16le", errors="ignore")
                                )
                                score, reasons = score_text(local_text, local_ids)
                                if score >= args.min_binary_score:
                                    row = {
                                        "address": f"0x{block_base + idx:x}",
                                        "id_label": label,
                                        "id_value": value,
                                        "width": width,
                                        "score": score,
                                        "reasons": reasons,
                                        "ids_found": dict(local_ids),
                                        "context_strings": summarize_context(
                                            context, block_base + context_start, limit=60
                                        ),
                                    }
                                    binary_hits.append(row)
                                    candidates.append({"kind": "binary", **row})
                                start = idx + 1

                if len(block) > overlap:
                    carry = block[-overlap:]
                    carry_base = block_base + len(block) - len(carry)
                else:
                    carry = block
                    carry_base = block_base
                offset += size

    candidates.sort(key=lambda row: (-int(row.get("score", 0)), row.get("address", "")))
    write_jsonl(text_path, text_hits)
    write_jsonl(binary_path, binary_hits)
    candidates_path.write_text(json.dumps(candidates[:500], ensure_ascii=False, indent=2), encoding="utf-8")
    meta = {
        "pid": args.pid,
        "phase": "before_continue",
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "competition_id": COMPETITION_ID,
        "team_ids": TEAM_IDS,
        "fixtures": FIXTURES,
        "text_terms": TEXT_TERMS,
        "stats": dict(stats),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "out_dir": str(out_dir.resolve()),
        "text_hits": len(text_hits),
        "binary_hits": len(binary_hits),
        "candidates": len(candidates),
        "paths": {
            "text_hits": str(text_path.resolve()),
            "binary_hits": str(binary_path.resolve()),
            "candidates": str(candidates_path.resolve()),
            "meta": str(meta_path.resolve()),
        },
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"meta": meta, "top": candidates[:30]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
