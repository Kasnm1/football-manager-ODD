from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fm_collector.strings import extract_strings
from fm_collector.win32 import MEM_IMAGE, iter_readable_regions, open_process, read_process_memory


KASHIWA_ID = 1190
J1_ID = 102428

TEAM_TERMS = (
    "\u67cf\u592a\u9633\u795e",
    "\u4e1c\u4eac\u7eff\u8335",
    "\u6d66\u548c\u7ea2\u94bb",
    "\u78d0\u7530\u559c\u60a6",
    "\u4e1c\u4eacFC",
    "\u718a\u672c\u6df1\u7ea2",
    "\u6a2a\u6ee8FC",
    "\u5188\u5c71\u7eff\u96c9",
    "\u5e7f\u5c9b\u4e09\u7bad",
    "\u753a\u7530\u6cfd\u7ef4\u4e9a",
    "\u6e05\u6c34\u5fc3\u8df3",
    "\u9e7f\u5c9b\u9e7f\u89d2",
    "\u5927\u962a\u6a31\u82b1",
    "\u4ed9\u53f0\u4e03\u5915",
    "\u957f\u5d0e\u6210\u529f\u4e38",
    "\u6a2a\u6ee8\u6c34\u624b",
    "\u5927\u962a\u94a2\u5df4",
    "\u795e\u6237\u80dc\u5229\u8239",
)

DATE_TERMS = (
    "2028",
    "2029",
    "\u5e74",
    "\u6708",
    "\u661f\u671f",
    "15:00",
    "16:00",
    "17:00",
    "18:00",
    "19:00",
    "J1",
    "J1\u8054\u8d5b",
    "\u6bd4\u8d5b\u65e5\u7a0b",
    "\u8d5b\u7a0b",
    "\u65e5\u7a0b",
)


def clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def text_patterns():
    terms = tuple(dict.fromkeys(TEAM_TERMS + DATE_TERMS + (str(KASHIWA_ID), str(J1_ID))))
    rows = []
    for term in terms:
        for enc in ("utf-8", "utf-16le"):
            rows.append((term, enc, term.encode(enc)))
    rows.sort(key=lambda row: len(row[2]), reverse=True)
    return rows


def id_patterns():
    ids = {"kashiwa": KASHIWA_ID, "j1": J1_ID}
    rows = []
    for label, value in ids.items():
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


def count_ids(data: bytes) -> Counter[str]:
    counts: Counter[str] = Counter()
    for label, value, enc, pat in id_patterns():
        n = data.count(pat)
        if n:
            counts[f"{label}:{value}:{enc}"] += n
    return counts


def useful_text(text: str) -> bool:
    if not text or len(text) > 240:
        return False
    if any(term in text for term in TEAM_TERMS):
        return True
    if any(term in text for term in DATE_TERMS):
        return True
    if re.search(r"20(28|29).{0,8}\u6708|\d{1,2}\u6708\d{1,2}\u65e5|\d{1,2}:\d{2}", text):
        return True
    return False


def extract_window_strings(data: bytes, base: int, limit: int = 160) -> list[dict[str, object]]:
    rows = []
    seen = set()
    for rec in extract_strings(data, base, min_len=2):
        text = clean(rec.text)
        if not useful_text(text):
            continue
        key = (text, rec.encoding)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"address": f"0x{rec.address:x}", "encoding": rec.encoding, "text": text})
        if len(rows) >= limit:
            break
    return rows


def score_window(strings: list[dict[str, object]], ids: Counter[str]) -> tuple[int, list[str]]:
    texts = [str(row["text"]) for row in strings]
    joined = "\n".join(texts)
    reasons = []
    score = 0
    if "\u67cf\u592a\u9633\u795e" in joined:
        score += 10
        reasons.append("kashiwa_text")
    if any(k.startswith("kashiwa:1190:") for k in ids):
        score += 8
        reasons.append("kashiwa_id")
    if "J1" in joined or "J1\u8054\u8d5b" in joined:
        score += 4
        reasons.append("j1_text")
    if any(k.startswith("j1:102428:") for k in ids):
        score += 4
        reasons.append("j1_id")
    dates = [t for t in texts if re.search(r"20(28|29)|\d{1,2}\u6708|\d{1,2}:\d{2}", t)]
    opponents = [term for term in TEAM_TERMS if term != "\u67cf\u592a\u9633\u795e" and term in joined]
    score += min(len(dates), 5) * 2
    score += min(len(opponents), 6) * 3
    if dates:
        reasons.append(f"dates={len(dates)}")
    if opponents:
        reasons.append(f"opponents={len(opponents)}")
    return score, reasons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, default=25820)
    parser.add_argument("--out-root", type=Path, default=Path("data/probes/kashiwa_future_schedule"))
    parser.add_argument("--min-address", type=lambda s: int(s, 0), default=0x17200000000)
    parser.add_argument("--max-address", type=lambda s: int(s, 0), default=0x17500000000)
    parser.add_argument("--chunk-mb", type=int, default=8)
    parser.add_argument("--context", type=lambda s: int(s, 0), default=0x50000)
    parser.add_argument("--max-hits-per-term", type=int, default=30)
    parser.add_argument("--max-windows", type=int, default=250)
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = args.out_root / f"scan_{args.pid}_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    hits_path = out_dir / "hits.jsonl"
    windows_path = out_dir / "windows.jsonl"
    meta_path = out_dir / "meta.json"

    pats = text_patterns()
    overlap = max(len(pat) for _term, _enc, pat in pats) + args.context
    chunk = args.chunk_mb * 1024 * 1024
    term_counts: Counter[str] = Counter()
    stats = {
        "pid": args.pid,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "regions_seen": 0,
        "regions_scanned": 0,
        "regions_skipped_executable": 0,
        "read_attempts": 0,
        "read_failures": 0,
        "bytes_read": 0,
        "min_address": f"0x{args.min_address:x}",
        "max_address": f"0x{args.max_address:x}",
    }
    window_keys = set()
    ranked = []
    started = time.perf_counter()

    with open_process(args.pid) as proc, hits_path.open("w", encoding="utf-8") as hits, windows_path.open(
        "w", encoding="utf-8"
    ) as wins:
        for region in iter_readable_regions(proc):
            stats["regions_seen"] += 1
            if region.type == MEM_IMAGE:
                stats["regions_skipped_executable"] += 1
                continue
            r_start = region.base_address
            r_end = region.base_address + region.size
            if r_end <= args.min_address or r_start >= args.max_address:
                continue
            stats["regions_scanned"] += 1
            scan_start = max(r_start, args.min_address)
            scan_end = min(r_end, args.max_address)
            carry = b""
            for offset in range(scan_start - r_start, scan_end - r_start, chunk):
                addr = r_start + offset
                data = read_process_memory(proc, addr, min(chunk, scan_end - addr))
                stats["read_attempts"] += 1
                if not data:
                    stats["read_failures"] += 1
                    carry = b""
                    continue
                stats["bytes_read"] += len(data)
                scan_data = carry + data
                scan_base = addr - len(carry)
                for term, enc, pat in pats:
                    if term_counts[term] >= args.max_hits_per_term:
                        continue
                    for pos in find_all(scan_data, pat):
                        abs_addr = scan_base + pos
                        if abs_addr < addr - len(carry):
                            continue
                        if term_counts[term] >= args.max_hits_per_term:
                            break
                        term_counts[term] += 1
                        hit = {
                            "address": f"0x{abs_addr:x}",
                            "term": term,
                            "encoding": enc,
                            "region": f"0x{r_start:x}-0x{r_end:x}",
                        }
                        hits.write(json.dumps(hit, ensure_ascii=False) + "\n")
                        hits.flush()
                        w_start = max(r_start, abs_addr - args.context)
                        w_end = min(r_end, abs_addr + args.context)
                        w_key = (w_start, w_end)
                        if len(window_keys) >= args.max_windows or w_key in window_keys:
                            continue
                        window_keys.add(w_key)
                        w_data = read_process_memory(proc, w_start, w_end - w_start)
                        if not w_data:
                            continue
                        strings = extract_window_strings(w_data, w_start)
                        ids = count_ids(w_data)
                        score, reasons = score_window(strings, ids)
                        row = {
                            **hit,
                            "window": f"0x{w_start:x}-0x{w_end:x}",
                            "score": score,
                            "reasons": reasons,
                            "ids": dict(sorted(ids.items())),
                            "strings": strings,
                        }
                        wins.write(json.dumps(row, ensure_ascii=False) + "\n")
                        wins.flush()
                        ranked.append({k: row[k] for k in ("address", "term", "window", "score", "reasons", "ids")})
                carry = scan_data[-overlap:] if len(scan_data) > overlap else scan_data

    ranked.sort(key=lambda row: int(row["score"]), reverse=True)
    meta = {
        **stats,
        "duration_seconds": round(time.perf_counter() - started, 3),
        "term_counts": dict(sorted(term_counts.items())),
        "windows": len(window_keys),
        "top_windows": ranked[:60],
        "paths": {
            "hits": str(hits_path.resolve()),
            "windows": str(windows_path.resolve()),
            "meta": str(meta_path.resolve()),
        },
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir.resolve()), "meta": meta}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
