from __future__ import annotations

import gzip
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.app_paths import DATA_ROOT, cache_data_root, career_data_root
from tools.app_settings import DEFAULT_RESULT_RETENTION_SEASONS
from tools.storage_io import atomic_write_bytes, storage_lock


ODDS_RETENTION_DAYS = 7
ODDS_FILE_PATTERN = re.compile(r"^fm26_all_odds_(\d{4}-\d{2}-\d{2})_\d{8}_\d{6}\.json$")
STORAGE_BUDGET_BYTES = 100 * 1024 * 1024
STORAGE_TARGET_BYTES = 80 * 1024 * 1024
ORPHAN_TEMP_MIN_AGE_SECONDS = 300
_ATOMIC_TEMP_PATTERN = re.compile(r"^\..+\.\d+\.\d+\.[0-9a-f]{32}\.tmp$")


def decode_result_history_bytes(encoded: bytes) -> dict[str, Any]:
    raw = gzip.decompress(encoded) if encoded.startswith(b"\x1f\x8b") else encoded
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("赛果历史格式无效")
    return payload


def encode_result_history_payload(payload: dict[str, Any]) -> bytes:
    raw = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return gzip.compress(raw, compresslevel=6, mtime=0)


def _tree_size(root: Path) -> int:
    total = 0
    if not root.is_dir() or root.is_symlink():
        return total
    for directory, _subdirectories, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            path = Path(directory) / filename
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _compress_result_histories(root: Path) -> tuple[int, int]:
    compressed_files = 0
    saved_bytes = 0
    careers = root / "careers"
    if not careers.is_dir() or careers.is_symlink():
        return compressed_files, saved_bytes
    for pattern in ("*/result_history.json", "*/result_history.json.backup"):
        for path in careers.glob(pattern):
            try:
                current = path.read_bytes()
                if current.startswith(b"\x1f\x8b"):
                    continue
                payload = decode_result_history_bytes(current)
                encoded = encode_result_history_payload(payload)
                atomic_write_bytes(path, encoded)
            except (OSError, EOFError, ValueError):
                continue
            compressed_files += 1
            saved_bytes += max(0, len(current) - len(encoded))
    return compressed_files, saved_bytes


def _remove_stale_atomic_temps(root: Path, now: float) -> tuple[int, int]:
    removed_files = 0
    removed_bytes = 0
    for directory, _subdirectories, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            if not _ATOMIC_TEMP_PATTERN.match(filename):
                continue
            path = Path(directory) / filename
            try:
                stat = path.stat()
                if path.is_symlink() or now - stat.st_mtime < ORPHAN_TEMP_MIN_AGE_SECONDS:
                    continue
                path.unlink()
            except OSError:
                continue
            removed_files += 1
            removed_bytes += stat.st_size
    return removed_files, removed_bytes


def enforce_storage_budget(
    *, protected_paths: set[Path] | None = None,
    root: Path | None = None,
    budget_bytes: int = STORAGE_BUDGET_BYTES,
    target_bytes: int = STORAGE_TARGET_BYTES,
) -> dict[str, int | bool]:
    """Keep steady-state FMODD data bounded without deleting account truth."""
    data_root = Path(root or DATA_ROOT).resolve()
    budget = max(1, int(budget_bytes))
    target = min(budget, max(1, int(target_bytes)))
    protected: set[Path] = set()
    for raw in protected_paths or set():
        try:
            path = Path(raw).resolve()
            path.relative_to(data_root)
        except (OSError, RuntimeError, ValueError):
            continue
        protected.add(path)

    compressed_files, compressed_bytes = _compress_result_histories(data_root)
    temp_files, temp_bytes = _remove_stale_atomic_temps(data_root, time.time())
    current_bytes = _tree_size(data_root)
    deleted_files = temp_files
    deleted_bytes = temp_bytes

    candidates: list[tuple[float, int, Path]] = []
    cache_root = data_root / "cache"
    if cache_root.is_dir() and not cache_root.is_symlink():
        for directory, _subdirectories, filenames in os.walk(cache_root, followlinks=False):
            for filename in filenames:
                path = Path(directory) / filename
                try:
                    resolved = path.resolve()
                    if path.is_symlink() or resolved in protected:
                        continue
                    # Legacy result evidence remains durable until its one-time
                    # import marker has been written to the career ledger.
                    if path.name == "fm26_result_history.json":
                        continue
                    stat = path.stat()
                except OSError:
                    continue
                candidates.append((stat.st_mtime, -stat.st_size, path))
    for _modified, _negative_size, path in sorted(candidates):
        if current_bytes <= target:
            break
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        current_bytes -= size
        deleted_files += 1
        deleted_bytes += size

    # Backups are recoverability copies rather than the authoritative ledger.
    # Drop the oldest only if compressed histories plus protected files still
    # leave the directory above the target.
    backups: list[tuple[float, Path]] = []
    careers_root = data_root / "careers"
    if careers_root.is_dir() and not careers_root.is_symlink():
        for path in careers_root.glob("*/result_history.json.backup"):
            try:
                if not path.is_symlink() and path.resolve() not in protected:
                    backups.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for _modified, path in sorted(backups):
        if current_bytes <= target:
            break
        try:
            size = path.stat().st_size
            path.unlink()
        except OSError:
            continue
        current_bytes -= size
        deleted_files += 1
        deleted_bytes += size

    return {
        "budget_bytes": budget,
        "target_bytes": target,
        "bytes": current_bytes,
        "over_budget": current_bytes > budget,
        "compressed_files": compressed_files,
        "compressed_bytes": compressed_bytes,
        "deleted_files": deleted_files,
        "deleted_bytes": deleted_bytes,
    }


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def current_season_start(game_date: date) -> date:
    year = game_date.year if (game_date.month, game_date.day) >= (7, 1) else game_date.year - 1
    return date(year, 7, 1)


def result_retention_start(game_date: date) -> date:
    cross_year_start = current_season_start(game_date)
    calendar_year_start = date(game_date.year, 1, 1)
    current_start = min(cross_year_start, calendar_year_start)
    return date(
        current_start.year - (DEFAULT_RESULT_RETENTION_SEASONS - 1),
        current_start.month,
        1,
    )


def prune_odds_snapshots(
    game_date: str,
    keep_path: Path | None = None,
    directory: Path | None = None,
    protected_paths: set[Path] | None = None,
) -> int:
    directory = directory or cache_data_root() / "odds" / "live"
    current_date = date.fromisoformat(game_date)
    oldest_kept_date = current_date - timedelta(days=ODDS_RETENTION_DAYS)
    kept = keep_path.resolve() if keep_path else None
    protected = {
        path.resolve() for path in (protected_paths or set())
        if path.exists()
    }
    snapshots: list[tuple[Path, date]] = []
    for path in directory.glob("fm26_all_odds_*.json"):
        match = ODDS_FILE_PATTERN.match(path.name)
        snapshot_date = _parse_date(match.group(1)) if match else None
        if snapshot_date:
            snapshots.append((path, snapshot_date))
    latest_by_date: dict[date, Path] = {}
    for path, snapshot_date in snapshots:
        current = latest_by_date.get(snapshot_date)
        if current is None or path.name > current.name:
            latest_by_date[snapshot_date] = path
    deleted = 0
    for path, snapshot_date in snapshots:
        resolved = path.resolve()
        if (kept and resolved == kept) or resolved in protected:
            continue
        expired = snapshot_date < oldest_kept_date
        duplicate = latest_by_date.get(snapshot_date) != path
        if expired or duplicate:
            try:
                path.unlink()
                deleted += 1
            except OSError:
                continue
    return deleted


def prune_result_history(
    game_date: str,
    save_instance_id: str | None,
    directory: Path | None = None,
    protected_result_keys: set[tuple[str, int, int, int]] | None = None,
) -> tuple[int, int]:
    if not save_instance_id:
        return 0, 0
    season_start = result_retention_start(date.fromisoformat(game_date))
    removed_results = 0
    updated_files = 0
    paths = (
        [directory / "fm26_result_history.json"]
        if directory is not None
        else [career_data_root(save_instance_id) / "result_history.json"]
    )
    for path in paths:
        if not path.exists():
            continue
        with storage_lock(path):
            try:
                current = path.read_bytes()
                payload = decode_result_history_bytes(current)
            except (OSError, EOFError, ValueError):
                continue
            results = payload["results"]
            retained = []
            protected = set(protected_result_keys or ())
            for item in results:
                result_date = _parse_date(item.get("date")) if isinstance(item, dict) else None
                try:
                    result_key = (
                        str(item["date"]), int(item["competition"]["id"]),
                        int(item["home_team"]["id"]), int(item["away_team"]["id"]),
                    )
                except (KeyError, TypeError, ValueError):
                    result_key = None
                if result_date is None or result_date >= season_start or result_key in protected:
                    retained.append(item)
            removed = len(results) - len(retained)
            if not removed:
                continue
            payload["results"] = retained
            payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
            payload["retention_season_start"] = season_start.isoformat()
            try:
                atomic_write_bytes(
                    path.with_suffix(path.suffix + ".backup"), current,
                )
                atomic_write_bytes(path, encode_result_history_payload(payload))
            except OSError:
                continue
        removed_results += removed
        updated_files += 1
    return removed_results, updated_files


def apply_storage_retention(
    game_date: str,
    save_instance_id: str | None,
    current_odds_path: Path | None = None,
    protected_result_keys: set[tuple[str, int, int, int]] | None = None,
    protected_odds_paths: set[Path] | None = None,
) -> dict[str, int | bool]:
    removed_results, updated_result_files = prune_result_history(
        game_date, save_instance_id, protected_result_keys=protected_result_keys,
    )
    report: dict[str, int | bool] = {
        "odds_files": prune_odds_snapshots(
            game_date, current_odds_path, protected_paths=protected_odds_paths,
        ),
        "results": removed_results,
        "result_files": updated_result_files,
    }
    budget_paths = set(protected_odds_paths or set())
    if current_odds_path is not None:
        budget_paths.add(current_odds_path)
    report.update(enforce_storage_budget(protected_paths=budget_paths))
    return report
