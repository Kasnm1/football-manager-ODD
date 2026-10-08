from __future__ import annotations

import argparse
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SITEMAP_URL = "https://market.oddslab.gg/sitemap.xml"
URL_PATTERN = re.compile(r"/world-cup-2026/matches/.+-2026-06-(?:1[1-9]|2[0-5])$")
OUTCOME_PATTERN = re.compile(
    r'outcomes-list__outcome-label">(.*?)</span>\s*'
    r'<span class="outcomes-list__outcome-odd">([0-9.]+)</span>',
    re.DOTALL,
)
TITLE_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL)
DATE_PATTERN = re.compile(r"(2026-06-[0-9]{2})$")


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "FM-Odds-Calibration/1.0"})
    with urllib.request.urlopen(request, timeout=25) as response:
        return response.read().decode("utf-8", "replace")


def clean(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value)).strip()


def capture_match(url: str) -> dict[str, object]:
    document = fetch(url)
    outcomes = OUTCOME_PATTERN.findall(document)
    if len(outcomes) < 3:
        raise ValueError(f"no closing money-line outcomes: {url}")
    title = TITLE_PATTERN.search(document)
    date = DATE_PATTERN.search(url)
    return {
        "date": date.group(1) if date else None,
        "title": clean(title.group(1)) if title else None,
        "home": clean(outcomes[0][0]),
        "draw": clean(outcomes[1][0]),
        "away": clean(outcomes[2][0]),
        "odds": {
            "home": float(outcomes[0][1]),
            "draw": float(outcomes[1][1]),
            "away": float(outcomes[2][1]),
        },
        "url": url,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "research" / "world_cup_2026_oddslab_closing.json",
    )
    args = parser.parse_args()

    sitemap = ET.fromstring(fetch(SITEMAP_URL))
    urls = sorted({
        node.text for node in sitemap.findall("{*}url/{*}loc")
        if node.text and URL_PATTERN.search(node.text)
    })
    rows = []
    errors = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(capture_match, url): url for url in urls}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as error:
                errors.append({"url": futures[future], "error": str(error)})

    rows.sort(key=lambda item: (item["date"] or "", item["home"] or ""))
    payload = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "source": "OddsLab World Cup 2026 closing consensus money line",
        "source_index": SITEMAP_URL,
        "date_from": "2026-06-11",
        "date_to": "2026-06-25",
        "matches": rows,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"matches": len(rows), "errors": len(errors), "output": str(args.output)}, ensure_ascii=False))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
