from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


COMPETITION_ID = 102428

TEAMS = {
    "kashiwa_reysol": (1190, "\u67cf\u592a\u9633\u795e"),
    "vissel_kobe": (106844, "\u795e\u6237\u80dc\u5229\u8239"),
    "jubilo_iwata": (1188, "\u78d0\u7530\u559c\u60a6"),
    "fc_tokyo": (107313, "\u4e1c\u4eacFC"),
    "roasso_kumamoto": (786558, "\u718a\u672c\u6df1\u7ea2"),
    "yokohama_fc": (7100042, "\u6a2a\u6ee8FC"),
    "fagiano_okayama": (786547, "\u5188\u5c71\u7eff\u96c9"),
    "urawa_reds": (1195, "\u6d66\u548c\u7ea2\u94bb"),
    "sanfrecce_hiroshima": (1193, "\u5e7f\u5c9b\u4e09\u7bad"),
    "machida_zelvia": (788837, "\u753a\u7530\u6cfd\u7ef4\u4e9a"),
    "shimizu_s_pulse": (1194, "\u6e05\u6c34\u5fc3\u8df3"),
    "tokyo_verdy": (1196, "\u4e1c\u4eac\u7eff\u8335"),
    "kashima_antlers": (1189, "\u9e7f\u5c9b\u9e7f\u89d2"),
    "cerezo_osaka": (1185, "\u5927\u962a\u6a31\u82b1"),
    "vegalta_sendai": (107283, "\u4ed9\u53f0\u4e03\u5915"),
    "v_varen_nagasaki": (786559, "\u957f\u5d0e\u6210\u529f\u4e38"),
    "yokohama_f_marinos": (1198, "\u6a2a\u6ee8\u6c34\u624b"),
    "gamba_osaka": (1186, "\u5927\u962a\u94a2\u5df4"),
}

FIXTURES = (
    ("kashiwa_tokyo_verdy", "kashiwa_reysol", "tokyo_verdy"),
    ("fc_tokyo_urawa", "fc_tokyo", "urawa_reds"),
    ("okayama_iwata", "fagiano_okayama", "jubilo_iwata"),
    ("yokohama_fm_gamba", "yokohama_f_marinos", "gamba_osaka"),
    ("shimizu_kumamoto", "shimizu_s_pulse", "roasso_kumamoto"),
    ("kobe_yokohama_fc", "vissel_kobe", "yokohama_fc"),
    ("machida_cerezo", "machida_zelvia", "cerezo_osaka"),
    ("sendai_kashima", "vegalta_sendai", "kashima_antlers"),
    ("nagasaki_hiroshima", "v_varen_nagasaki", "sanfrecce_hiroshima"),
)

EXTRA_TERMS = (
    str(COMPETITION_ID),
    "J1",
    "J1\u8054\u8d5b",
    "15:00",
    "2028\u5e748\u670826\u65e5",
    "8\u670826\u65e5",
)


def clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def text_patterns():
    terms = list(EXTRA_TERMS) + [name for _label, (_team_id, name) in TEAMS.items()]
    rows = []
    for term in terms:
        for encoding in ("utf-8", "utf-16le"):
            rows.append((term, encoding, term.encode(encoding)))
    rows.sort(key=lambda row: len(row[2]), reverse=True)
    return rows


def binary_patterns(include_small: bool = False):
    values = {"j1": COMPETITION_ID}
    for label, (value, _name) in TEAMS.items():
        if include_small or value >= 100000:
            values[label] = value
    rows = []
    for label, value in values.items():
        rows.append((label, value, "u32", struct.pack("<I", value)))
        rows.append((label, value, "i32", struct.pack("<i", value)))
        rows.append((label, value, "u64", struct.pack("<Q", value)))
    return rows


def all_id_patterns():
    rows = [("j1", COMPETITION_ID, "u32", struct.pack("<I", COMPETITION_ID))]
    rows.append(("j1", COMPETITION_ID, "i32", struct.pack("<i", COMPETITION_ID)))
    rows.append(("j1", COMPETITION_ID, "u64", struct.pack("<Q", COMPETITION_ID)))
    for label, (value, _name) in TEAMS.items():
        rows.append((label, value, "u32", struct.pack("<I", value)))
        rows.append((label, value, "i32", struct.pack("<i", value)))
        rows.append((label, value, "u64", struct.pack("<Q", value)))
    return rows


def find_all(data: bytes, needle: bytes):
    pos = 0
    while True:
        idx = data.find(needle, pos)
        if idx < 0:
            return
        yield idx
        pos = idx + 1


def count_local_ids(data: bytes) -> Counter[str]:
    counts: Counter[str] = Counter()
    for label, value, encoding, pattern in all_id_patterns():
        n = data.count(pattern)
        if n:
            counts[f"{label}:{value}:{encoding}"] += n
    return counts


def strings_near(data: bytes, base: int, focus_terms: set[str], limit: int = 80) -> list[dict[str, object]]:
    rows = []
    for record in extract_strings(data, base, min_len=1):
        text = clean(record.text)
        if not text:
            continue
        relevant = any(term in text for term in focus_terms)
        relevant = relevant or text in {"15:00", "J1", "J1\u8054\u8d5b"}
        if not relevant:
            continue
        rows.append(
            {
                "address": f"0x{record.address:x}",
                "encoding": record.encoding,
                "text": text if len(text) <= 260 else text[:260],
            }
        )
        if len(rows) >= limit:
            break
    return rows


def fixture_status(team_text_hits: dict[str, list[dict[str, object]]], local_id_counts_by_team: dict[str, Counter[str]]):
    rows = []
    for key, home_label, away_label in FIXTURES:
        home_id, home_name = TEAMS[home_label]
        away_id, away_name = TEAMS[away_label]
        home_id_hits = sum(v for k, v in local_id_counts_by_team.get(home_label, Counter()).items() if f":{home_id}:" in k)
        away_id_hits = sum(v for k, v in local_id_counts_by_team.get(away_label, Counter()).items() if f":{away_id}:" in k)
        rows.append(
            {
                "key": key,
                "home": {"label": home_label, "id": home_id, "name": home_name, "text_hits": len(team_text_hits.get(home_label, [])), "local_id_hits": home_id_hits},
                "away": {"label": away_label, "id": away_id, "name": away_name, "text_hits": len(team_text_hits.get(away_label, [])), "local_id_hits": away_id_hits},
                "confirmed_by_text": bool(team_text_hits.get(home_label)) and bool(team_text_hits.get(away_label)),
                "confirmed_by_local_ids": home_id_hits > 0 and away_id_hits > 0,
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--out-root", type=Path, default=Path("data/probes/j1_full_targeted"))
    parser.add_argument("--chunk-mb", type=int, default=16)
    parser.add_argument("--context", type=lambda s: int(s, 0), default=0x60000)
    parser.add_argument("--max-text-hits-per-term", type=int, default=20)
    parser.add_argument("--max-binary-hits-per-value", type=int, default=80)
    parser.add_argument("--min-address", type=lambda s: int(s, 0), default=0)
    parser.add_argument("--max-address", type=lambda s: int(s, 0), default=(1 << 64) - 1)
    parser.add_argument("--stop-when-all-team-text-found", action="store_true")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_root / f"full_{args.pid}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    text_hits_path = out_dir / "text_hits.jsonl"
    binary_hits_path = out_dir / "large_binary_hits.jsonl"
    windows_path = out_dir / "windows.jsonl"
    meta_path = out_dir / "meta.json"

    text_pats = text_patterns()
    bin_pats = binary_patterns(include_small=False)
    max_pat_len = max(max(len(pat) for *_rest, pat in text_pats), max(len(pat) for *_rest, pat in bin_pats))
    overlap = max_pat_len + args.context
    chunk_size = args.chunk_mb * 1024 * 1024
    started = time.perf_counter()

    team_name_to_label = {name: label for label, (_team_id, name) in TEAMS.items()}
    focus_terms = set(EXTRA_TERMS) | {name for _label, (_team_id, name) in TEAMS.items()}
    text_counts: Counter[str] = Counter()
    binary_counts: Counter[str] = Counter()
    team_text_hits: dict[str, list[dict[str, object]]] = defaultdict(list)
    local_id_counts_by_team: dict[str, Counter[str]] = defaultdict(Counter)
    window_keys = set()
    stats = {
        "pid": args.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "competition_id": COMPETITION_ID,
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
        "chunk_mb": args.chunk_mb,
    }

    with open_process(args.pid) as process, text_hits_path.open("w", encoding="utf-8") as text_out, binary_hits_path.open(
        "w", encoding="utf-8"
    ) as bin_out, windows_path.open("w", encoding="utf-8") as win_out:
        for region in iter_readable_regions(process):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE:
                stats["regions_skipped_executable"] += 1
                continue
            region_start = region.base_address
            region_end = region.base_address + region.size
            if region_end <= args.min_address or region_start >= args.max_address:
                continue
            stats["regions_scanned"] += 1
            carry = b""
            read_start = max(region_start, args.min_address)
            read_end = min(region_end, args.max_address)
            for offset in range(read_start - region_start, read_end - region_start, chunk_size):
                address = region.base_address + offset
                data = read_process_memory(process, address, min(chunk_size, read_end - address))
                stats["read_attempts"] += 1
                if not data:
                    stats["read_failures"] += 1
                    carry = b""
                    continue
                stats["bytes_read"] += len(data)
                scan_data = carry + data
                scan_base = address - len(carry)

                for term, encoding, pattern in text_pats:
                    if text_counts[term] >= args.max_text_hits_per_term:
                        continue
                    for pos in find_all(scan_data, pattern):
                        abs_addr = scan_base + pos
                        if abs_addr < address - len(carry):
                            continue
                        if text_counts[term] >= args.max_text_hits_per_term:
                            break
                        text_counts[term] += 1
                        row = {
                            "address": f"0x{abs_addr:x}",
                            "term": term,
                            "encoding": encoding,
                            "region": f"0x{region.base_address:x}-0x{region.base_address + region.size:x}",
                        }
                        text_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                        text_out.flush()

                        label = team_name_to_label.get(term)
                        if label:
                            team_text_hits[label].append(row)
                        window_start = max(region.base_address, abs_addr - args.context)
                        window_end = min(region.base_address + region.size, abs_addr + args.context)
                        window_key = (window_start, window_end, term)
                        if window_key not in window_keys:
                            window_keys.add(window_key)
                            window_data = read_process_memory(process, window_start, window_end - window_start)
                            if window_data:
                                ids = count_local_ids(window_data)
                                if label:
                                    local_id_counts_by_team[label].update(ids)
                                win_row = {
                                    **row,
                                    "window": f"0x{window_start:x}-0x{window_end:x}",
                                    "local_ids": dict(sorted(ids.items())),
                                    "strings": strings_near(window_data, window_start, focus_terms),
                                }
                                win_out.write(json.dumps(win_row, ensure_ascii=False) + "\n")
                                win_out.flush()

                for label, value, encoding, pattern in bin_pats:
                    key = f"{label}:{value}:{encoding}"
                    if binary_counts[key] >= args.max_binary_hits_per_value:
                        continue
                    for pos in find_all(scan_data, pattern):
                        abs_addr = scan_base + pos
                        if abs_addr < address - len(carry):
                            continue
                        if binary_counts[key] >= args.max_binary_hits_per_value:
                            break
                        binary_counts[key] += 1
                        bin_out.write(
                            json.dumps(
                                {
                                    "address": f"0x{abs_addr:x}",
                                    "label": label,
                                    "value": value,
                                    "encoding": encoding,
                                    "region": f"0x{region.base_address:x}-0x{region.base_address + region.size:x}",
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        bin_out.flush()
                carry = scan_data[-overlap:] if len(scan_data) > overlap else scan_data
            if args.stop_when_all_team_text_found and all(team_text_hits.get(label) for label in TEAMS):
                break

    meta = {
        **stats,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "team_text_found": {label: len(team_text_hits.get(label, [])) for label in TEAMS},
        "missing_team_text": [label for label in TEAMS if not team_text_hits.get(label)],
        "text_counts": dict(sorted(text_counts.items())),
        "large_binary_counts": dict(sorted(binary_counts.items())),
        "fixture_status": fixture_status(team_text_hits, local_id_counts_by_team),
        "paths": {
            "text_hits": str(text_hits_path.resolve()),
            "large_binary_hits": str(binary_hits_path.resolve()),
            "windows": str(windows_path.resolve()),
            "meta": str(meta_path.resolve()),
        },
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir.resolve()), "meta": meta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
