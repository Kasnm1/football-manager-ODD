from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


TERMS = (
    "\u67cf\u592a\u9633\u795e",
    "\u6c34\u539f\u4e09\u661f\u84dd\u7ffc",
    "\u9e7f\u5c9b\u9e7f\u89d2",
    "\u4f20\u7403",
    "\u5c04\u95e8",
    "\u8fdb\u7403",
    "\u8d21\u732e",
    "\u4eca\u5929",
    "\u4f18\u52bf",
    "\u91cd\u538b",
    "\u4e0a\u534a\u573a",
    "\u4e0b\u534a\u573a",
    "\u7b2c",
)

NOISE = (
    "Assets",
    "UIAssets",
    "UnityEngine",
    "uxml",
    "stylelink",
    "linkcolor",
    "Sports Interactive",
    "C:Users",
    "Goal of the Year",
    "Top Goalscorer",
    "\u6700\u4f73\u8fdb\u7403",
    "\u6700\u4f73\u5c04\u624b",
)


def iter_json_objects(path: Path):
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        obj, j = decoder.raw_decode(text, i)
        yield obj
        i = j


def clean_text(text: str) -> str:
    text = text.replace("\x00", "").replace("\u200b", "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"AwAAAC[A-Za-z0-9+/=]{12,}", " ", text)
    text = re.sub(r"[A-Za-z0-9+/=]{48,}", " ", text)
    text = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9，。,.、：:；;！!？?（）()·\- ]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def score_snippet(text: str) -> int:
    score = 0
    for term in TERMS:
        if term in text:
            score += 3
    for noise in NOISE:
        if noise in text:
            score -= 6
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    if cjk >= 12:
        score += 4
    if any(marker in text for marker in ("\u7b2c1", "\u7b2c61", "\u4e0a\u534a\u573a", "\u4e0b\u534a\u573a")):
        score += 4
    if any(marker in text for marker in ("\u4f20\u7403", "\u5c04\u95e8", "\u8fdb\u7403", "\u91cd\u538b")):
        score += 3
    return score


def snippet_windows(context: str) -> list[str]:
    context = clean_text(context)
    windows: list[str] = []
    for term in TERMS:
        start = 0
        while True:
            pos = context.find(term, start)
            if pos < 0:
                break
            left = max(0, pos - 180)
            right = min(len(context), pos + 520)
            windows.append(context[left:right].strip())
            start = pos + len(term)
    return windows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl", nargs="+")
    parser.add_argument("--out", default="")
    parser.add_argument("--min-score", type=int, default=8)
    args = parser.parse_args()

    rows = []
    seen: set[str] = set()
    for raw_path in args.jsonl:
        path = Path(raw_path)
        for hit in iter_json_objects(path):
            context = f"{hit.get('context_utf8', '')} {hit.get('context_utf16le', '')}"
            for window in snippet_windows(context):
                score = score_snippet(window)
                key = re.sub(r"\s+", "", window)
                if score < args.min_score or len(key) < 16 or key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "source": str(path),
                        "address": hit.get("address"),
                        "keyword": hit.get("keyword"),
                        "score": score,
                        "text": window,
                    }
                )

    rows.sort(key=lambda row: (-row["score"], row["source"], row["address"] or ""))
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rows[:80], ensure_ascii=False, indent=2))
    print(json.dumps({"total": len(rows), "out": str(Path(args.out).resolve()) if args.out else ""}, ensure_ascii=False))


if __name__ == "__main__":
    main()
