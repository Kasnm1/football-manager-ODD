from __future__ import annotations

import json
import re
import struct
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import iter_readable_regions, open_process, read_process_memory


PID = 25820
TEAM_ID = 1190
OUT_DIR = Path("data/probes/id1190_binary")

# Ranges already associated with schedule/team UI after the user opened the schedule page.
SEED_RANGES = (
    (0x17260000000, 0x17264000000),
    (0x174D8D00000, 0x174D9000000),
    (0x17646000000, 0x17647000000),
)

TEXT_SIGNALS = (
    "team1190",
    "club_1190",
    "team",
    "club",
    "fixture",
    "Fixture",
    "BindingVariables",
    "ClubForObjectLookupData",
    "2028-",
)


def printable_context(data: bytes, base: int) -> list[dict[str, object]]:
    records = []
    for record in extract_strings(data, base, min_len=2):
        if len(record.text) > 160:
            text = record.text[:160]
        else:
            text = record.text
        records.append({"address": f"0x{record.address:x}", "encoding": record.encoding, "text": text})
    return records


def score(records: list[dict[str, object]]) -> tuple[int, list[str]]:
    joined = "\n".join(str(r["text"]) for r in records)
    reasons = []
    value = 0
    for sig in TEXT_SIGNALS:
        if sig in joined:
            value += 3
            reasons.append(sig)
    if re.search(r"\b20\d{2}-\d{1,2}-\d{1,2}\b", joined):
        value += 5
        reasons.append("date")
    return value, reasons


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"id1190_binary_{PID}_{stamp}.jsonl"
    meta_path = OUT_DIR / f"id1190_binary_{PID}_{stamp}.meta.json"

    patterns = {
        "u32_le_1190": struct.pack("<I", TEAM_ID),
        "i32_le_1190": struct.pack("<i", TEAM_ID),
        "u64_le_1190": struct.pack("<Q", TEAM_ID),
    }

    written = 0
    scanned = 0
    hits = 0
    with open_process(PID) as process, out_path.open("w", encoding="utf-8") as out:
        regions = list(iter_readable_regions(process))
        for region in regions:
            region_start = region.base_address
            region_end = region.base_address + region.size
            for wanted_start, wanted_end in SEED_RANGES:
                start = max(region_start, wanted_start)
                end = min(region_end, wanted_end)
                if start >= end:
                    continue
                data = read_process_memory(process, start, end - start)
                if not data:
                    continue
                scanned += len(data)
                for label, pattern in patterns.items():
                    pos = 0
                    while True:
                        idx = data.find(pattern, pos)
                        if idx < 0:
                            break
                        hits += 1
                        addr = start + idx
                        ctx_start = max(0, idx - 0x800)
                        ctx_end = min(len(data), idx + 0x800)
                        ctx = data[ctx_start:ctx_end]
                        records = printable_context(ctx, start + ctx_start)
                        value, reasons = score(records)
                        if value > 0:
                            row = {
                                "address": f"0x{addr:x}",
                                "pattern": label,
                                "score": value,
                                "reasons": reasons,
                                "strings": records[:80],
                            }
                            out.write(json.dumps(row, ensure_ascii=False) + "\n")
                            written += 1
                        pos = idx + 1

    meta = {
        "pid": PID,
        "team_id": TEAM_ID,
        "ranges": [f"0x{s:x}-0x{e:x}" for s, e in SEED_RANGES],
        "bytes_scanned": scanned,
        "raw_hits": hits,
        "written": written,
        "out_path": str(out_path.resolve()),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
