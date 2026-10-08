from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


INPUTS = [
    Path("data/probes/next_fixture/fm26_memsearch_25820_20260710_161321.jsonl"),
    Path("data/probes/live_verify/fm26_memsearch_25820_20260710_160638.jsonl"),
    Path("data/probes/schedule_verify/fm26_memsearch_25820_20260710_160638.jsonl"),
    Path("data/probes/live_fixture_1559/team_terms/fm26_memsearch_25820_20260710_155925.jsonl"),
    Path("data/probes/live_fixture_1559/fixture_terms/fm26_memsearch_25820_20260710_155916.jsonl"),
]

TEAMS = (
    "\u67cf\u592a\u9633\u795e",
    "\u6c34\u539f\u4e09\u661f\u84dd\u7ffc",
    "\u957f\u6625\u4e9a\u6cf0",
)
SIGNALS = (
    "\u4e3b\u573a",
    "\u5ba2\u573a",
    "\u4e1c\u4e9a\u4ff1\u4e50\u90e8\u676f",
    "\u53cb\u8c0a\u8d5b",
    "\u661f\u671f",
    "\u6bd4\u8d5b",
    "17:00",
    "2028",
)
NEWS_NOISE = (
    "\u63d0\u51fa",
    "\u9080\u8bf7",
    "\u540c\u610f",
    "\u62d2\u7edd",
    "\u65b0\u95fb",
    "\u6d88\u606f",
    "\u95e8\u6237",
    "News",
    "Portal",
    "\u7ecf\u7eaa\u4eba",
    "\u8f6c\u4f1a",
)
STATIC_NOISE = ("Assets/", "ScriptableObjects", "UIAssets", ".asset")


def clean(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[\ufffd\u0001-\u001f]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def score(text: str) -> tuple[int, list[str]]:
    reasons = []
    value = 0
    team_count = sum(1 for t in TEAMS if t in text)
    if team_count:
        value += team_count * 5
        reasons.append(f"teams={team_count}")
    for sig in SIGNALS:
        if sig in text:
            value += 3
            reasons.append(sig)
    if re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", text):
        value += 5
        reasons.append("date")
    if re.search(r"\b\d{1,2}:\d{2}\b", text):
        value += 4
        reasons.append("time")
    if re.search(r"\b\d+\s*[:：-]\s*\d+\b", text):
        value += 3
        reasons.append("score")
    for noise in STATIC_NOISE:
        if noise in text:
            value -= 10
            reasons.append(f"static:{noise}")
    for noise in NEWS_NOISE:
        if noise in text:
            value -= 4
            reasons.append(f"news:{noise}")
    return value, reasons


def main() -> None:
    rows = []
    for path in INPUTS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            text = clean((row.get("context_utf8") or "") + " " + (row.get("context_utf16le") or ""))
            if not any(t in text for t in TEAMS):
                continue
            value, reasons = score(text)
            rows.append(
                {
                    "source": str(path),
                    "address": row.get("address"),
                    "keyword": row.get("keyword"),
                    "encoding": row.get("encoding"),
                    "score": value,
                    "reasons": reasons,
                    "text": text[:2500],
                }
            )
    rows.sort(key=lambda r: (-r["score"], r["source"], r["address"] or ""))
    out = Path("data/probes/team_hit_classification.json")
    out.write_text(json.dumps(rows[:300], ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows[:30]:
        print("\nSCORE", row["score"], row["address"], row["keyword"], row["encoding"], row["reasons"])
        print(row["text"][:1400])
    print("\nwritten", out.resolve(), "rows", len(rows))


if __name__ == "__main__":
    main()
