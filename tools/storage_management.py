from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tools.app_paths import (
    DATA_ROOT, DATA_ROOT_MARKER, FROZEN, PROJECT_ROOT, account_save_path,
    configured_data_root, ensure_data_directories, forget_saved_storage_scopes,
)
from tools.storage_retention import STORAGE_BUDGET_BYTES


CACHE_ENTRY_NAMES = ("cache", "model", "odds", "results", "world")
PRESERVED_RESULT_HISTORY_NAME = "fm26_result_history.json"
LEGACY_ACCOUNT_CACHE_NAMES = ("competitions", "model", "odds", "results", "world")
LEGACY_CACHE_BLOCKERS = {
    "competitions": ("points_adjustments.json",),
    "world": ("acquired_clubs.json", "youth_intake_plans.json"),
}


def _validated_data_root() -> Path:
    root = DATA_ROOT.resolve()
    expected_source = (PROJECT_ROOT / "data").resolve()
    if root == Path(root.anchor) or not root.name:
        raise RuntimeError("拒绝清理不安全的数据目录")
    if FROZEN:
        if root.name.casefold() != "fmodd" and not (root / DATA_ROOT_MARKER).is_file():
            raise RuntimeError("应用数据目录校验失败")
    elif root != expected_source:
        raise RuntimeError("开发数据目录校验失败")
    return root


def _measure(
    paths: list[Path], *, excluded: set[Path] | None = None,
) -> tuple[int, int]:
    excluded = {path.resolve() for path in (excluded or set())}
    total_bytes = 0
    file_count = 0
    for source in paths:
        if source.is_file() and not source.is_symlink():
            try:
                if source.resolve() in excluded:
                    continue
                total_bytes += source.stat().st_size
                file_count += 1
            except OSError:
                pass
            continue
        if not source.is_dir() or source.is_symlink():
            continue
        for directory, _subdirectories, filenames in os.walk(source, followlinks=False):
            for filename in filenames:
                path = Path(directory) / filename
                try:
                    if path.is_symlink() or path.resolve() in excluded:
                        continue
                    total_bytes += path.stat().st_size
                    file_count += 1
                except OSError:
                    continue
    return total_bytes, file_count


def _protected_result_histories(root: Path) -> set[Path]:
    """Keep persisted settlement evidence out of ordinary cache deletion."""
    cache_root = root / "cache"
    if not cache_root.is_dir() or cache_root.is_symlink():
        return set()
    protected: set[Path] = set()
    try:
        scopes = list(cache_root.iterdir())
    except OSError:
        return protected
    for scope in scopes:
        candidate = scope / "results" / PRESERVED_RESULT_HISTORY_NAME
        if candidate.is_file() and not candidate.is_symlink():
            protected.add(candidate.resolve())
    return protected


def _normalized_protected_paths(root: Path, paths: set[Path] | None) -> set[Path]:
    protected = _protected_result_histories(root)
    for raw in paths or set():
        try:
            candidate = Path(raw).expanduser().resolve()
            candidate.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate.is_file() and not candidate.is_symlink():
            protected.add(candidate)
    return protected


def _remove_path_preserving(path: Path, protected: set[Path]) -> None:
    if path.is_symlink() or path.is_file():
        if path.resolve() not in protected:
            path.unlink()
        return
    if not path.is_dir():
        return
    for directory, subdirectories, filenames in os.walk(
        path, topdown=False, followlinks=False,
    ):
        current = Path(directory)
        for filename in filenames:
            child = current / filename
            if child.is_symlink() or child.resolve() not in protected:
                child.unlink()
        for name in subdirectories:
            child = current / name
            if child.is_symlink():
                child.unlink()
                continue
            try:
                child.rmdir()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass


def _clear_targets(root: Path) -> list[Path]:
    targets = [root / name for name in CACHE_ENTRY_NAMES if (root / name).exists()]
    saves_root = root / "saves"
    if saves_root.is_dir():
        for account in saves_root.iterdir():
            if not account.is_dir() or account.is_symlink():
                continue
            targets.extend(
                account / name for name in LEGACY_ACCOUNT_CACHE_NAMES
                if (account / name).exists()
                and not any(
                    (account / name / blocker).exists()
                    for blocker in LEGACY_CACHE_BLOCKERS.get(name, ())
                )
            )
    return targets


def storage_info(*, protected_paths: set[Path] | None = None) -> dict[str, Any]:
    root = _validated_data_root()
    protected = _normalized_protected_paths(root, protected_paths)
    total_bytes, file_count = _measure([root])
    clearable_bytes, clearable_files = _measure(
        _clear_targets(root), excluded=protected,
    )
    save_bytes, save_files = _measure([root / "saves"])
    return {
        "path": str(root),
        "configured_path": str(configured_data_root() or ""),
        "bytes": total_bytes,
        "files": file_count,
        "clearable_bytes": clearable_bytes,
        "clearable_files": clearable_files,
        "save_bytes": save_bytes,
        "save_files": save_files,
        "budget_bytes": STORAGE_BUDGET_BYTES,
        "over_budget": total_bytes > STORAGE_BUDGET_BYTES,
        "development_protected": not FROZEN,
    }


def clear_cache_files(*, protected_paths: set[Path] | None = None) -> dict[str, Any]:
    root = _validated_data_root()
    protected = _normalized_protected_paths(root, protected_paths)
    before = storage_info(protected_paths=protected)
    root.mkdir(parents=True, exist_ok=True)
    for child in _clear_targets(root):
        _remove_path_preserving(child, protected)
    ensure_data_directories()
    return {
        **before,
        "cleared_bytes": before["clearable_bytes"],
        "cleared_files": before["clearable_files"],
    }


def delete_other_save_files(current_scope_id: str) -> dict[str, Any]:
    root = _validated_data_root()
    scope_id = str(current_scope_id or "").strip()
    if not scope_id:
        raise ValueError("尚未识别当前存档，无法安全删除其他存档")
    saves_root = (root / "saves").resolve()
    current_path = account_save_path(scope_id).resolve()
    if current_path.parent != saves_root or current_path.suffix.casefold() != ".fmodd":
        raise RuntimeError("当前存档路径校验失败")
    deleted_bytes = 0
    deleted_files = 0
    deleted_scopes: set[str] = set()
    if saves_root.is_dir():
        for path in saves_root.iterdir():
            if (
                not path.is_file() or path.is_symlink()
                or path.suffix.casefold() != ".fmodd"
                or path.resolve() == current_path
            ):
                continue
            size = path.stat().st_size
            path.unlink()
            deleted_bytes += size
            deleted_files += 1
            backup = path.with_suffix(path.suffix + ".backup")
            if backup.is_file() and not backup.is_symlink():
                deleted_bytes += backup.stat().st_size
                backup.unlink()
                deleted_files += 1
            deleted_scopes.add(path.stem)
    forgotten_accounts = forget_saved_storage_scopes(deleted_scopes)
    ensure_data_directories()
    return {
        "current_save": current_path.name,
        "deleted_bytes": deleted_bytes,
        "deleted_files": deleted_files,
        "forgotten_accounts": forgotten_accounts,
    }


def delete_current_save_file(current_scope_id: str) -> dict[str, Any]:
    root = _validated_data_root()
    scope_id = str(current_scope_id or "").strip()
    if not scope_id:
        raise ValueError("尚未识别当前存档，无法安全删除当前存档")
    saves_root = (root / "saves").resolve()
    current_path = account_save_path(scope_id).resolve()
    if current_path.parent != saves_root or current_path.suffix.casefold() != ".fmodd":
        raise RuntimeError("当前存档路径校验失败")
    deleted_bytes = 0
    deleted_files = 0
    for path in (current_path, current_path.with_suffix(current_path.suffix + ".backup")):
        if not path.is_file() or path.is_symlink():
            continue
        deleted_bytes += path.stat().st_size
        path.unlink()
        deleted_files += 1
    ensure_data_directories()
    return {
        "current_save": current_path.name,
        "deleted_bytes": deleted_bytes,
        "deleted_files": deleted_files,
    }
