from __future__ import annotations

import json
import re
import struct
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import iter_readable_regions, open_process, read_process_memory


PID = 25820
SEED_RANGES = (
    (0x174D8D00000, 0x174D8F00000),
)
OUT_DIR = Path("data/probes/schedule_pointer_probe")

TEAM_TERMS = (
    "\u67cf\u592a\u9633\u795e",
    "\u6c34\u539f\u4e09\u661f\u84dd\u7ffc",
    "\u957f\u6625\u4e9a\u6cf0",
)
VALUE_TERMS = TEAM_TERMS + (
    "2028",
    "17:00",
    "\u4e1c\u4e9a\u4ff1\u4e50\u90e8\u676f",
    "\u4e3b\u573a",
    "\u5ba2\u573a",
)
NOISE_TERMS = (
    "Assets/",
    "ScriptableObjects",
    "UIAssets",
    ".asset",
    "PortalMessages",
    "NewsAggregator",
)


@dataclass(frozen=True)
class Region:
    start: int
    end: int


def find_region(regions: list[Region], ptr: int) -> Region | None:
    lo = 0
    hi = len(regions)
    while lo < hi:
        mid = (lo + hi) // 2
        r = regions[mid]
        if ptr < r.start:
            hi = mid
        elif ptr >= r.end:
            lo = mid + 1
        else:
            return r
    return None


def strings_near(process, addr: int, region: Region, radius: int = 0x3000):
    start = max(region.start, addr - radius)
    end = min(region.end, addr + radius)
    data = read_process_memory(process, start, end - start)
    if not data:
        return []
    records = list(extract_strings(data, start, min_len=2))
    return records


def score_records(records) -> tuple[int, list[str]]:
    texts = [r.text for r in records]
    joined = "\n".join(texts)
    score = 0
    reasons: list[str] = []
    for term in VALUE_TERMS:
        if term in joined:
            score += 5
            reasons.append(term)
    if re.search(r"\b20(2[6-9]|3\d)[-/]\d{1,2}[-/]\d{1,2}\b", joined):
        score += 4
        reasons.append("date")
    if re.search(r"\b\d{1,2}:\d{2}\b", joined):
        score += 3
        reasons.append("time")
    if re.search(r"\b\d+\s*[:：-]\s*\d+\b", joined):
        score += 2
        reasons.append("score")
    if any(n in joined for n in NOISE_TERMS):
        score -= 5
        reasons.append("noise")
    return score, reasons


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"schedule_pointer_probe_{PID}_{stamp}.jsonl"
    meta_path = OUT_DIR / f"schedule_pointer_probe_{PID}_{stamp}.meta.json"

    with open_process(PID) as process:
        regions = [
            Region(r.base_address, r.base_address + r.size)
            for r in iter_readable_regions(process)
        ]
        regions.sort(key=lambda r: r.start)

        seed_data = []
        for seed_start, seed_end in SEED_RANGES:
            for region in regions:
                start = max(seed_start, region.start)
                end = min(seed_end, region.end)
                if start >= end:
                    continue
                data = read_process_memory(process, start, end - start)
                if data:
                    seed_data.append((start, data))

        candidates: dict[int, int] = {}
        for base, data in seed_data:
            for off in range(0, len(data) - 8):
                ptr = struct.unpack_from("<Q", data, off)[0]
                if 0x10000000000 <= ptr <= 0x7FFFFFFFFFFF and find_region(regions, ptr):
                    candidates[ptr] = candidates.get(ptr, 0) + 1

        written = 0
        best = []
        seen_windows: set[int] = set()
        with out_path.open("w", encoding="utf-8") as out:
            for ptr, count in sorted(candidates.items(), key=lambda kv: (-kv[1], kv[0]))[:5000]:
                region = find_region(regions, ptr)
                if not region:
                    continue
                window_key = ptr & ~0xFFF
                if window_key in seen_windows:
                    continue
                seen_windows.add(window_key)
                records = strings_near(process, ptr, region)
                score, reasons = score_records(records)
                if score <= 0:
                    continue
                sample = [
                    {"address": f"0x{r.address:x}", "encoding": r.encoding, "text": r.text}
                    for r in records
                    if any(term in r.text for term in VALUE_TERMS)
                    or re.search(r"\b20(2[6-9]|3\d)[-/]\d{1,2}[-/]\d{1,2}\b", r.text)
                    or re.search(r"\b\d{1,2}:\d{2}\b", r.text)
                    or re.search(r"\b\d+\s*[:：-]\s*\d+\b", r.text)
                ][:60]
                row = {
                    "pointer": f"0x{ptr:x}",
                    "count_in_seed": count,
                    "region": f"0x{region.start:x}-0x{region.end:x}",
                    "score": score,
                    "reasons": reasons,
                    "sample": sample,
                }
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                best.append(row)
                written += 1
                if written >= 300:
                    break

    meta = {
        "pid": PID,
        "seed_ranges": [f"0x{s:x}-0x{e:x}" for s, e in SEED_RANGES],
        "candidate_pointers": len(candidates),
        "written": written,
        "out_path": str(out_path.resolve()),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"meta": meta, "top": best[:10]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
