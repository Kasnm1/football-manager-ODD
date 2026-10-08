from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .collector import MemorySearchConfig, SnapshotConfig, make_keyword_patterns, run_memory_search, run_snapshot
from .text_snapshot import TextSnapshotConfig, run_text_snapshot
from .win32 import find_processes


DEFAULT_NAMES = ("fm", "fm.exe", "football manager 26")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fm_collect",
        description="Read-only local Football Manager process collector.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="Find matching FM processes.")
    discover.add_argument(
        "--name",
        default="fm",
        help="Case-insensitive executable/name fragment to search for.",
    )
    discover.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    snap = sub.add_parser("snapshot", help="Create a read-only string snapshot.")
    target = snap.add_mutually_exclusive_group()
    target.add_argument("--pid", type=int, help="Target process id.")
    target.add_argument(
        "--name",
        default="fm",
        help="Case-insensitive process name fragment when --pid is omitted.",
    )
    snap.add_argument(
        "--output-dir",
        default="data/snapshots",
        help="Directory for snapshot output.",
    )
    snap.add_argument(
        "--max-read-mb",
        type=int,
        default=512,
        help="Maximum memory bytes to read, in MiB. Use 0 for no explicit cap.",
    )
    snap.add_argument(
        "--chunk-mb",
        type=int,
        default=1,
        help="Read chunk size, in MiB.",
    )
    snap.add_argument(
        "--min-len",
        type=int,
        default=5,
        help="Minimum extracted string length.",
    )
    snap.add_argument(
        "--keywords",
        default="",
        help="Comma-separated strings to highlight in the snapshot.",
    )
    snap.add_argument(
        "--max-records",
        type=int,
        default=200000,
        help="Maximum string records to write to JSONL.",
    )
    snap.add_argument(
        "--max-unique",
        type=int,
        default=100000,
        help="Maximum unique strings to write to TXT.",
    )
    snap.add_argument(
        "--include-executable",
        action="store_true",
        help="Also scan executable image regions. This is noisier.",
    )

    mem = sub.add_parser("mem-search", help="Search exact keywords in process memory.")
    target = mem.add_mutually_exclusive_group()
    target.add_argument("--pid", type=int, help="Target process id.")
    target.add_argument(
        "--name",
        default="fm",
        help="Case-insensitive process name fragment when --pid is omitted.",
    )
    mem.add_argument("--keywords", required=True, help="Comma-separated keywords.")
    mem.add_argument("--output-dir", default="data/searches", help="Directory for search output.")
    mem.add_argument("--max-read-mb", type=int, default=2048, help="Maximum memory to read, in MiB. 0 means no cap.")
    mem.add_argument("--chunk-mb", type=int, default=4, help="Read chunk size, in MiB.")
    mem.add_argument("--max-hits", type=int, default=1000, help="Stop after this many hits.")
    mem.add_argument(
        "--range",
        dest="ranges",
        action="append",
        default=[],
        help="Limit search to address range 0xSTART-0xEND. Can be passed multiple times.",
    )
    mem.add_argument(
        "--encodings",
        default="utf-8,utf-16le,utf-16be",
        help="Comma-separated encodings to search. Useful values: utf-8,utf-16le,utf-16be.",
    )
    mem.add_argument(
        "--context-bytes",
        type=int,
        default=96,
        help="Bytes of context to include around each hit.",
    )
    mem.add_argument(
        "--include-executable",
        action="store_true",
        help="Also scan executable image regions. This is noisier.",
    )

    files = sub.add_parser("file-search", help="Search exact keywords in local files.")
    files.add_argument("paths", nargs="+", help="Files or directories to scan.")
    files.add_argument("--keywords", required=True, help="Comma-separated keywords.")
    files.add_argument("--output-dir", default="data/searches", help="Directory for search output.")
    files.add_argument("--max-file-mb", type=int, default=512, help="Skip files larger than this MiB. 0 means no cap.")
    files.add_argument("--max-hits", type=int, default=1000, help="Stop after this many hits.")

    text = sub.add_parser("text-snapshot", help="Extract clean text from selected process memory ranges.")
    target = text.add_mutually_exclusive_group()
    target.add_argument("--pid", type=int, help="Target process id.")
    target.add_argument(
        "--name",
        default="fm",
        help="Case-insensitive process name fragment when --pid is omitted.",
    )
    text.add_argument(
        "--range",
        dest="ranges",
        action="append",
        required=True,
        help="Address range as 0xSTART-0xEND. Can be passed multiple times.",
    )
    text.add_argument("--output-dir", default="data/text", help="Directory for text snapshot output.")
    text.add_argument("--chunk-mb", type=int, default=2, help="Read chunk size, in MiB.")
    text.add_argument("--min-len", type=int, default=3, help="Minimum extracted text length.")
    text.add_argument("--max-records", type=int, default=100000, help="Maximum text records to write to JSONL.")
    text.add_argument("--max-unique", type=int, default=100000, help="Maximum unique strings to write to TXT.")
    text.add_argument(
        "--include-executable",
        action="store_true",
        help="Also scan executable image regions. This is noisier.",
    )
    return parser


def _print_processes(name: str, as_json: bool) -> int:
    processes = find_processes(name)
    normalized = name.lower().removesuffix(".exe")
    exact = [
        p
        for p in processes
        if p.name.lower().removesuffix(".exe") == normalized
        or (p.path and Path(p.path).stem.lower() == normalized)
    ]
    if exact:
        processes = exact
    if as_json:
        print(json.dumps([p.to_dict() for p in processes], indent=2, ensure_ascii=False))
        return 0

    if not processes:
        print(f"No process matched {name!r}.")
        return 1

    for proc in processes:
        path = f" path={proc.path}" if proc.path else ""
        print(f"pid={proc.pid} name={proc.name}{path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "discover":
        return _print_processes(args.name, args.json)

    if args.command == "snapshot":
        keywords = tuple(x.strip() for x in args.keywords.split(",") if x.strip())
        config = SnapshotConfig(
            pid=args.pid,
            name=args.name,
            output_dir=Path(args.output_dir),
            max_read_mb=args.max_read_mb,
            chunk_mb=max(1, args.chunk_mb),
            min_len=max(3, args.min_len),
            keywords=keywords,
            max_records=max(1, args.max_records),
            max_unique=max(1, args.max_unique),
            include_executable=bool(args.include_executable),
        )
        try:
            result = run_snapshot(config)
        except Exception as exc:  # Keep CLI failures readable for first-pass use.
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 2

        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "mem-search":
        keywords = tuple(x.strip() for x in args.keywords.split(",") if x.strip())
        ranges = tuple(_parse_range(value) for value in args.ranges)
        encodings = tuple(x.strip() for x in args.encodings.split(",") if x.strip())
        config = MemorySearchConfig(
            pid=args.pid,
            name=args.name,
            output_dir=Path(args.output_dir),
            max_read_mb=args.max_read_mb,
            chunk_mb=max(1, args.chunk_mb),
            keywords=keywords,
            max_hits=max(1, args.max_hits),
            include_executable=bool(args.include_executable),
            ranges=ranges,
            encodings=encodings,
            context_bytes=max(32, args.context_bytes),
        )
        try:
            result = run_memory_search(config)
        except Exception as exc:
            print(f"mem-search failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "file-search":
        keywords = tuple(x.strip() for x in args.keywords.split(",") if x.strip())
        try:
            result = _run_file_search(
                paths=[Path(p) for p in args.paths],
                keywords=keywords,
                output_dir=Path(args.output_dir),
                max_file_mb=args.max_file_mb,
                max_hits=max(1, args.max_hits),
            )
        except Exception as exc:
            print(f"file-search failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if args.command == "text-snapshot":
        try:
            ranges = tuple(_parse_range(value) for value in args.ranges)
            config = TextSnapshotConfig(
                pid=args.pid,
                name=args.name,
                output_dir=Path(args.output_dir),
                ranges=ranges,
                chunk_mb=max(1, args.chunk_mb),
                min_len=max(2, args.min_len),
                max_records=max(1, args.max_records),
                max_unique=max(1, args.max_unique),
                include_executable=bool(args.include_executable),
            )
            result = run_text_snapshot(config)
        except Exception as exc:
            print(f"text-snapshot failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    parser.error("unknown command")
    return 2


def _parse_range(value: str) -> tuple[int, int]:
    if "-" not in value:
        raise ValueError(f"invalid range {value!r}")
    left, right = value.split("-", 1)
    start = int(left, 0)
    end = int(right, 0)
    if end <= start:
        raise ValueError(f"invalid range {value!r}")
    return start, end


def _iter_files(paths: list[Path]):
    for path in paths:
        if path.is_file():
            yield path
        elif path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    yield child


def _run_file_search(
    paths: list[Path],
    keywords: tuple[str, ...],
    output_dir: Path,
    max_file_mb: int,
    max_hits: int,
) -> dict[str, object]:
    from datetime import datetime

    if not keywords:
        raise RuntimeError("at least one keyword is required")

    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    hits_path = output_dir / f"fm26_filesearch_{stamp}.jsonl"
    meta_path = output_dir / f"fm26_filesearch_{stamp}.meta.json"
    patterns = make_keyword_patterns(keywords)
    max_file_bytes = None if max_file_mb <= 0 else max_file_mb * 1024 * 1024
    stats = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "paths": [str(p) for p in paths],
        "keywords": list(keywords),
        "files_seen": 0,
        "files_scanned": 0,
        "files_skipped_size": 0,
        "bytes_scanned": 0,
        "hits": 0,
        "hits_path": str(hits_path.resolve()),
        "meta_path": str(meta_path.resolve()),
        "hit_examples": [],
    }
    examples: list[dict[str, object]] = []

    with hits_path.open("w", encoding="utf-8") as out:
        for path in _iter_files(paths):
            stats["files_seen"] += 1
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if max_file_bytes is not None and size > max_file_bytes:
                stats["files_skipped_size"] += 1
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            stats["files_scanned"] += 1
            stats["bytes_scanned"] += len(data)
            for hit in _search_file_block(data, path, patterns):
                out.write(json.dumps(hit, ensure_ascii=False) + "\n")
                if len(examples) < 50:
                    examples.append(hit)
                stats["hits"] += 1
                if stats["hits"] >= max_hits:
                    break
            if stats["hits"] >= max_hits:
                break

    stats["hit_examples"] = examples
    meta_path.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    return stats


def _search_file_block(data: bytes, path: Path, patterns: tuple[tuple[str, str, bytes], ...]):
    for keyword, encoding, pattern in patterns:
        start = 0
        while True:
            idx = data.find(pattern, start)
            if idx < 0:
                break
            context_start = max(0, idx - 96)
            context_end = min(len(data), idx + len(pattern) + 96)
            context = data[context_start:context_end]
            if len(context) % 2:
                context_for_utf16 = context[:-1]
            else:
                context_for_utf16 = context
            yield {
                "path": str(path.resolve()),
                "offset": f"0x{idx:x}",
                "keyword": keyword,
                "encoding": encoding,
                "context_hex": context.hex(" "),
                "context_utf8": context.decode("utf-8", errors="replace").replace("\x00", ""),
                "context_utf16le": context_for_utf16.decode("utf-16le", errors="replace").replace("\x00", ""),
            }
            start = idx + 1
