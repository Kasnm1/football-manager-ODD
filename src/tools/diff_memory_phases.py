from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def by_span(snapshot: dict) -> dict[tuple[str, str], dict]:
    return {(r["start"], r["end"]): r for r in snapshot.get("regions", [])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("before")
    parser.add_argument("after")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    before = load(Path(args.before))
    after = load(Path(args.after))
    b = by_span(before)
    a = by_span(after)

    rows = []
    for span, ar in a.items():
        br = b.get(span)
        if br is None:
            status = "new"
        elif br.get("hash") != ar.get("hash"):
            status = "changed"
        else:
            status = "same"
        if status == "same":
            continue
        rows.append(
            {
                "status": status,
                "start": span[0],
                "end": span[1],
                "size": ar.get("size"),
                "before_hash": br.get("hash") if br else None,
                "after_hash": ar.get("hash"),
                "before_signals": br.get("signals", []) if br else [],
                "after_signals": ar.get("signals", []),
            }
        )

    rows.sort(key=lambda r: (0 if r["status"] == "new" else 1, -(r["size"] or 0)))
    result = {
        "before": str(Path(args.before).resolve()),
        "after": str(Path(args.after).resolve()),
        "changed_or_new": rows,
    }

    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
