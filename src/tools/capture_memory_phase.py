from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


DEFAULT_RANGES = (
    # Ranges already seen around club/team refs, schedule rows, and current fixture UI.
    (0x17260000000, 0x17264000000),
    (0x172C0000000, 0x172C0200000),
    (0x174D8D00000, 0x174D9000000),
    (0x17646000000, 0x17647000000),
)

SIGNALS = (
    "1190",
    "team1190",
    "club_1190",
    "Fixture",
    "fixture",
    "match",
    "Match",
    "CompetitionFixturesTool",
    "BindingVariables",
    "ClubForObjectLookupData",
    "2028-",
)


def parse_range(value: str) -> tuple[int, int]:
    left, right = value.split("-", 1)
    return int(left, 0), int(right, 0)


def iter_intersections(region_start: int, region_end: int, ranges: tuple[tuple[int, int], ...]):
    for wanted_start, wanted_end in ranges:
        start = max(region_start, wanted_start)
        end = min(region_end, wanted_end)
        if start < end:
            yield start, end


def summarize_strings(data: bytes, base: int) -> list[dict[str, object]]:
    found = []
    for record in extract_strings(data, base, min_len=3):
        if any(sig in record.text for sig in SIGNALS):
            found.append(
                {
                    "address": f"0x{record.address:x}",
                    "encoding": record.encoding,
                    "text": record.text[:220],
                }
            )
            if len(found) >= 80:
                break
    return found


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--out-dir", default="data/probes/memory_phases")
    parser.add_argument("--range", dest="ranges", action="append", default=[])
    parser.add_argument("--all-readable", action="store_true")
    parser.add_argument("--chunk-kb", type=int, default=256)
    args = parser.parse_args()

    ranges = tuple(parse_range(v) for v in args.ranges) if args.ranges else DEFAULT_RANGES
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{args.phase}_{args.pid}_{stamp}.json"

    chunk_size = max(64, args.chunk_kb) * 1024
    regions_out = []
    bytes_read = 0
    with open_process(args.pid) as process:
        for region in iter_readable_regions(process):
            if region.type == MEM_IMAGE:
                continue
            region_start = region.base_address
            region_end = region.base_address + region.size
            spans = [(region_start, region_end)] if args.all_readable else list(
                iter_intersections(region_start, region_end, ranges)
            )
            for start, end in spans:
                offset = start
                sha = hashlib.blake2b(digest_size=16)
                sample = b""
                read_ok = True
                while offset < end:
                    size = min(chunk_size, end - offset)
                    data = read_process_memory(process, offset, size)
                    if data is None:
                        read_ok = False
                        break
                    if not sample:
                        sample = data[: min(len(data), 0x4000)]
                    sha.update(data)
                    bytes_read += len(data)
                    offset += size
                if not read_ok:
                    continue
                regions_out.append(
                    {
                        "start": f"0x{start:x}",
                        "end": f"0x{end:x}",
                        "size": end - start,
                        "hash": sha.hexdigest(),
                        "signals": summarize_strings(sample, start),
                    }
                )

    result = {
        "pid": args.pid,
        "phase": args.phase,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "ranges": [f"0x{s:x}-0x{e:x}" for s, e in ranges],
        "all_readable": bool(args.all_readable),
        "bytes_read": bytes_read,
        "regions": regions_out,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"path": str(out_path.resolve()), "regions": len(regions_out), "bytes_read": bytes_read}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
