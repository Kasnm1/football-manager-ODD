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
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


PID = 25820
TEAM_ID = 1190
OUT_DIR = Path("data/probes/prematch_object_graph")

# String addresses found after the user stopped on the pre-match squad screen.
ANCHOR_ADDRESSES = (
    0x17261C4D914,  # selectedfixture
    0x17261CA3734,  # selectedfixture
    0x172630E007E,  # ClubForObjectLookupData
    0x172631D501E,  # NationalTeamContainer
    0x172631D53DE,  # NationalTeamContainer
    0x176466F282C,  # NationalTeamContainer
    0x176465646A4,  # AwayTeam
    0x176469F38BC,  # AwayTeam
    0x17646990A9C,  # HomeTeam
    0x17646564B58,  # hometeam
    0x172629A319E,  # team1190
    0x176463870DE,  # club_1190
)

WATCH_RANGES = (
    (0x17260000000, 0x17264000000),
    (0x172C0000000, 0x172C0200000),
    (0x174D8D00000, 0x174D9000000),
    (0x17646000000, 0x17647000000),
)

GOOD_TEXT = (
    "selectedfixture",
    "fixture",
    "Fixture",
    "HomeTeam",
    "AwayTeam",
    "hometeam",
    "awayteam",
    "TeamSheet",
    "teamSheet",
    "lineup",
    "Lineup",
    "Squad",
    "squad",
    "Player",
    "player",
    "ClubForObjectLookupData",
    "team1190",
    "club_1190",
    "1190",
    "2028-7-8",
    "2028-07-08",
    "2028-7-15",
    "\u67cf\u592a\u9633\u795e",
    "\u6c34\u539f\u4e09\u661f\u84dd\u7ffc",
    "\u4e0a\u6d77\u7533\u82b1",
)

NOISE_TEXT = (
    "Assets/",
    "UIAssets",
    ".uxml",
    ".uss",
    "ScriptableObjects",
    "UnityEngine.UIElements",
    "margin-",
    "padding-",
    "color-",
    "PortalMessages",
)

TEAM_ID_PATTERNS = (
    struct.pack("<I", TEAM_ID),
    struct.pack("<Q", TEAM_ID),
)


@dataclass(frozen=True)
class Region:
    start: int
    end: int
    protect: int
    type: int

    @property
    def size(self) -> int:
        return self.end - self.start


def find_region(regions: list[Region], address: int) -> Region | None:
    lo = 0
    hi = len(regions)
    while lo < hi:
        mid = (lo + hi) // 2
        r = regions[mid]
        if address < r.start:
            hi = mid
        elif address >= r.end:
            lo = mid + 1
        else:
            return r
    return None


def read_window(process, regions: list[Region], address: int, radius: int) -> tuple[int, bytes, Region] | None:
    region = find_region(regions, address)
    if region is None:
        return None
    start = max(region.start, address - radius)
    end = min(region.end, address + radius)
    data = read_process_memory(process, start, end - start)
    if not data:
        return None
    return start, data, region


def iter_qwords(data: bytes, base: int):
    for off in range(0, len(data) - 7, 8):
        value = struct.unpack_from("<Q", data, off)[0]
        yield base + off, value


def plausible_ptr(value: int) -> bool:
    return 0x10000000000 <= value <= 0x7FFFFFFFFFFF


def scan_small_ints(data: bytes) -> dict[str, int]:
    counts = {
        "team_id_u32": 0,
        "team_id_u64": 0,
        "future_dates": 0,
        "small_counts_1_30": 0,
        "minus_one": 0,
    }
    counts["team_id_u32"] = data.count(TEAM_ID_PATTERNS[0])
    counts["team_id_u64"] = data.count(TEAM_ID_PATTERNS[1])
    for marker in (20280708, 20280715, 2028):
        counts["future_dates"] += data.count(struct.pack("<I", marker))
    for off in range(0, len(data) - 3, 4):
        value = struct.unpack_from("<i", data, off)[0]
        if 1 <= value <= 30:
            counts["small_counts_1_30"] += 1
        elif value == -1:
            counts["minus_one"] += 1
    return counts


def summarize_strings(data: bytes, base: int) -> list[dict[str, object]]:
    records = []
    for record in extract_strings(data, base, min_len=2):
        text = record.text.replace("\r", " ").replace("\n", " ")
        if len(text) > 180:
            text = text[:180]
        records.append({"address": f"0x{record.address:x}", "encoding": record.encoding, "text": text})
        if len(records) >= 120:
            break
    return records


def score_blob(data: bytes, base: int) -> tuple[int, list[str], list[dict[str, object]], dict[str, int]]:
    strings = summarize_strings(data, base)
    joined = "\n".join(str(item["text"]) for item in strings)
    ints = scan_small_ints(data)
    score = 0
    reasons: list[str] = []

    for term in GOOD_TEXT:
        if term in joined:
            score += 5
            reasons.append(term)
    for term in NOISE_TEXT:
        if term in joined:
            score -= 4
            reasons.append(f"noise:{term}")

    if ints["team_id_u32"]:
        score += 12 * ints["team_id_u32"]
        reasons.append(f"team_id_u32={ints['team_id_u32']}")
    if ints["team_id_u64"]:
        score += 12 * ints["team_id_u64"]
        reasons.append(f"team_id_u64={ints['team_id_u64']}")
    if ints["future_dates"]:
        score += 4 * ints["future_dates"]
        reasons.append(f"date_ints={ints['future_dates']}")
    if 10 <= ints["small_counts_1_30"] <= 200:
        score += 3
        reasons.append("many_small_counts")

    if re.search(r"\b20(2[6-9]|3\d)[-/]\d{1,2}[-/]\d{1,2}\b", joined):
        score += 5
        reasons.append("date_text")
    if re.search(r"\b\d{1,2}:\d{2}\b", joined):
        score += 3
        reasons.append("time_text")
    if re.search(r"\b\d+\s*[:\uff1a]\s*\d+\b", joined):
        score += 3
        reasons.append("score_text")

    return score, reasons, strings, ints


def collect_pointers_from_windows(process, regions: list[Region], centers: list[int], radius: int) -> dict[int, list[int]]:
    refs: dict[int, list[int]] = {}
    for center in centers:
        window = read_window(process, regions, center, radius)
        if window is None:
            continue
        base, data, _ = window
        for source, value in iter_qwords(data, base):
            if plausible_ptr(value) and find_region(regions, value):
                refs.setdefault(value, []).append(source)
    return refs


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"prematch_object_graph_{PID}_{stamp}.jsonl"
    meta_path = OUT_DIR / f"prematch_object_graph_{PID}_{stamp}.meta.json"

    with open_process(PID) as process:
        regions = [
            Region(r.base_address, r.base_address + r.size, r.protect, r.type)
            for r in iter_readable_regions(process)
        ]
        regions.sort(key=lambda r: r.start)

        valid_anchors = [addr for addr in ANCHOR_ADDRESSES if find_region(regions, addr)]
        first_hop = collect_pointers_from_windows(process, regions, valid_anchors, 0x6000)

        second_centers = list(first_hop.keys())[:4000]
        second_hop = collect_pointers_from_windows(process, regions, second_centers, 0x1000)

        combined: dict[int, list[int]] = {}
        for mapping in (first_hop, second_hop):
            for target, sources in mapping.items():
                combined.setdefault(target, []).extend(sources[:8])

        rows = []
        seen_pages: set[int] = set()
        for target, sources in sorted(combined.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            page = target & ~0xFFF
            if page in seen_pages:
                continue
            seen_pages.add(page)
            window = read_window(process, regions, target, 0x2800)
            if window is None:
                continue
            base, data, region = window
            score, reasons, strings, ints = score_blob(data, base)
            if score <= 0:
                continue
            if region.type == MEM_IMAGE:
                score -= 5
                reasons.append("image_region")
            value_strings = [
                item
                for item in strings
                if any(term in str(item["text"]) for term in GOOD_TEXT)
                or re.search(r"\b20(2[6-9]|3\d)[-/]\d{1,2}[-/]\d{1,2}\b", str(item["text"]))
                or re.search(r"\b\d{1,2}:\d{2}\b", str(item["text"]))
                or re.search(r"\b\d+\s*[:\uff1a]\s*\d+\b", str(item["text"]))
            ][:40]
            rows.append(
                {
                    "target": f"0x{target:x}",
                    "window": f"0x{base:x}-0x{base + len(data):x}",
                    "region": f"0x{region.start:x}-0x{region.end:x}",
                    "ref_count": len(sources),
                    "sample_sources": [f"0x{s:x}" for s in sources[:16]],
                    "score": score,
                    "reasons": reasons[:30],
                    "ints": ints,
                    "value_strings": value_strings,
                }
            )

        rows.sort(key=lambda row: (-int(row["score"]), -int(row["ref_count"]), row["target"]))
        with out_path.open("w", encoding="utf-8") as out:
            for row in rows[:500]:
                out.write(json.dumps(row, ensure_ascii=False) + "\n")

    meta = {
        "pid": PID,
        "team_id": TEAM_ID,
        "anchors": [f"0x{x:x}" for x in ANCHOR_ADDRESSES],
        "valid_anchors": [f"0x{x:x}" for x in valid_anchors],
        "watch_ranges": [f"0x{s:x}-0x{e:x}" for s, e in WATCH_RANGES],
        "first_hop_targets": len(first_hop),
        "second_hop_targets": len(second_hop),
        "scored_rows": len(rows),
        "out_path": str(out_path.resolve()),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"meta": meta, "top": rows[:20]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
