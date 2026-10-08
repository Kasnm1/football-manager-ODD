from __future__ import annotations

import gzip
import json
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from tools.app_paths import account_save_path, cache_data_root, save_data_root
from tools.money import from_minor, to_minor
from tools.storage_io import atomic_write_bytes, storage_lock


SCHEMA_VERSION = 1
_LOCK = threading.RLock()
_CONTAINER_CACHE: dict[Path, tuple[tuple[int, int], dict[str, Any]]] = {}
_SPLIT_RECOVERY_CHECKED: dict[Path, tuple[int, int]] = {}
_JSON_DOCUMENTS = {
    "wallet": "bets/fm26_wallet.json",
    "bet_analytics": "bets/fm26_bet_analytics.json",
    "economy": "economy/club_economy.json",
    "doping_effects": "economy/doping_effects.json",
    "goalkeeper_bribes": "economy/goalkeeper_bribes.json",
    "training_ground": "training/training_ground.json",
    "points_adjustments": "competitions/points_adjustments.json",
    "match_integrity": "betting/match_integrity.json",
    "player_aliases": "club/player_names.json",
    "manager_records": "club/manager_records.json",
    "acquired_clubs": "world/acquired_clubs.json",
    "youth_intake_plans": "world/youth_intake_plans.json",
    "mail": "notifications/fm_odds_mail.json",
    "favorites": "favorites.json",
}
_LEGACY_CACHE_DIRECTORIES = ("competitions", "model", "odds", "results", "world")
_CACHE_MOVE_BLOCKERS = {
    "competitions": ("points_adjustments.json",),
    "world": ("acquired_clubs.json", "youth_intake_plans.json"),
}


class UnsupportedAccountStoreVersion(RuntimeError):
    pass


class AccountStoreScopeMismatch(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty(scope_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "scope_id": scope_id,
        "updated_at": _now(),
        "documents": {},
    }


def _container_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_mtime_ns, stat.st_size


def _validated_container_schema(payload: dict[str, Any], path: Path) -> int:
    raw_version = payload.get("schema_version", 1)
    if isinstance(raw_version, bool):
        raise RuntimeError(f"FMODD 账户存档版本无效：{path}")
    try:
        version = int(raw_version)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"FMODD 账户存档版本无效：{path}") from error
    if version < 1:
        raise RuntimeError(f"FMODD 账户存档版本无效：{path}")
    if version > SCHEMA_VERSION:
        raise UnsupportedAccountStoreVersion(
            f"FMODD 账户存档由更新版本创建（存档版本 {version}，"
            f"当前最高支持 {SCHEMA_VERSION}）：{path}"
        )
    return version


def _container_backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".backup")


def _decode_container(path: Path, encoded: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(gzip.decompress(encoded).decode("utf-8"))
    except (OSError, EOFError, gzip.BadGzipFile, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"FMODD 账户存档损坏：{path}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), dict):
        raise RuntimeError(f"FMODD 账户存档格式无效：{path}")
    _validated_container_schema(payload, path)
    expected_scope = path.stem
    stored_scope = str(payload.get("scope_id") or "").strip()
    # Pre-2.2 account containers could be stored under the career filename
    # while retaining the manager-qualified scope in their payload. Accept
    # that narrow legacy form so an upgrade can read the account and rewrite
    # it under the canonical filename; unrelated mismatches remain rejected.
    stored_base, manager_separator, manager_id = stored_scope.rpartition(".manager-")
    legacy_manager_scope = (
        bool(manager_separator and manager_id.isdigit() and stored_base)
        and expected_scope in {stored_base, f"{stored_base}.mana"}
    )
    if stored_scope and stored_scope != expected_scope and not legacy_manager_scope:
        raise AccountStoreScopeMismatch(
            f"FMODD 账户存档作用域不匹配：{stored_scope} != {expected_scope}"
        )
    payload["schema_version"] = SCHEMA_VERSION
    payload["scope_id"] = expected_scope
    return payload


def _cached_container(path: Path) -> dict[str, Any]:
    signature = _container_signature(path)
    cached = _CONTAINER_CACHE.get(path)
    if cached and cached[0] == signature:
        return cached[1]
    try:
        payload = _decode_container(path, path.read_bytes())
    except (UnsupportedAccountStoreVersion, AccountStoreScopeMismatch):
        raise
    except (OSError, RuntimeError) as error:
        backup = _container_backup_path(path)
        if not backup.is_file():
            raise RuntimeError(f"FMODD 账户存档损坏且没有可用备份：{path}") from error
        try:
            backup_bytes = backup.read_bytes()
            payload = _decode_container(path, backup_bytes)
        except (UnsupportedAccountStoreVersion, AccountStoreScopeMismatch):
            raise
        except (OSError, RuntimeError) as backup_error:
            raise RuntimeError(f"FMODD 账户存档及备份均无法读取：{path}") from backup_error
        atomic_write_bytes(path, backup_bytes)
        signature = _container_signature(path)
    _CONTAINER_CACHE[path] = (signature, payload)
    return payload


def _current_cached_container(path: Path) -> dict[str, Any] | None:
    """Return a validated hot-cache entry without taking the process mutex."""
    cached = _CONTAINER_CACHE.get(path)
    if cached is None:
        return None
    try:
        signature = _container_signature(path)
    except OSError:
        return None
    return cached[1] if cached[0] == signature else None


def _split_recovery_required(legacy_root: Path) -> bool:
    split_path = legacy_root / "bets" / ".account.fmodd"
    try:
        signature = _container_signature(split_path)
    except OSError:
        return False
    return _SPLIT_RECOVERY_CHECKED.get(split_path) != signature


def _mark_split_recovery_checked(legacy_root: Path) -> None:
    split_path = legacy_root / "bets" / ".account.fmodd"
    try:
        _SPLIT_RECOVERY_CHECKED[split_path] = _container_signature(split_path)
    except OSError:
        _SPLIT_RECOVERY_CHECKED.pop(split_path, None)


def _read_container(path: Path) -> dict[str, Any]:
    return deepcopy(_cached_container(path))


def _write_container(path: Path, payload: dict[str, Any]) -> None:
    with storage_lock(path):
        if "schema_version" in payload:
            _validated_container_schema(payload, path)
        payload["schema_version"] = SCHEMA_VERSION
        payload["scope_id"] = path.stem
        payload["updated_at"] = _now()
        encoded = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        if path.is_file():
            try:
                current = path.read_bytes()
                # Normal read-modify-write paths have just loaded this exact
                # signature into the validated in-process cache. Re-decoding
                # the complete gzip/JSON container here only repeats work; an
                # external replacement changes the signature and retains the
                # full validation fallback before the file can be replaced.
                cached = _CONTAINER_CACHE.get(path)
                if not cached or cached[0] != _container_signature(path):
                    _decode_container(path, current)
            except (UnsupportedAccountStoreVersion, AccountStoreScopeMismatch):
                raise
            except (OSError, RuntimeError):
                pass
        atomic_write_bytes(
            path, gzip.compress(encoded, compresslevel=6, mtime=0),
        )
        _CONTAINER_CACHE[path] = (
            _container_signature(path), deepcopy(payload),
        )


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _storage_paths(
    scope_id: str | None = None, key: str = "", legacy_path: Path | None = None,
) -> tuple[Path, Path]:
    if legacy_path is None:
        path = account_save_path(scope_id)
        if path.name.casefold() == "unidentified.fmodd":
            raise RuntimeError("尚未绑定 FMODD 账户作用域，拒绝访问 unidentified.fmodd")
        return path, save_data_root(scope_id)
    source = Path(legacy_path)
    relative = Path(_JSON_DOCUMENTS.get(key, ""))
    if relative.parts and tuple(source.parts[-len(relative.parts):]) == relative.parts:
        legacy_root = source.parents[len(relative.parts) - 1]
    else:
        legacy_root = source.parent
    default_root = save_data_root(scope_id)
    try:
        source.relative_to(default_root)
    except ValueError:
        pass
    else:
        path = account_save_path(scope_id)
        if path.name.casefold() == "unidentified.fmodd":
            raise RuntimeError("尚未绑定 FMODD 账户作用域，拒绝访问 unidentified.fmodd")
        return path, default_root
    if legacy_root == default_root:
        path = account_save_path(scope_id)
        if path.name.casefold() == "unidentified.fmodd":
            raise RuntimeError("尚未绑定 FMODD 账户作用域，拒绝访问 unidentified.fmodd")
        return path, legacy_root
    return legacy_root / ".account.fmodd", legacy_root


def _merge_bet_records(left: Any, right: Any) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for row in [*(left or []), *(right or [])]:
        if not isinstance(row, dict):
            continue
        bet_id = str(row.get("bet_id") or "")
        if bet_id:
            merged[bet_id] = deepcopy(row)
        else:
            anonymous.append(deepcopy(row))
    return [*merged.values(), *anonymous]


def _merge_wallets(left: Any, right: Any) -> dict[str, Any]:
    wallets = [item for item in (left, right) if isinstance(item, dict)]
    if not wallets:
        return {}
    transaction_sets = [
        {
            str(row.get("id") or "")
            for row in wallet.get("transactions", [])
            if isinstance(row, dict) and row.get("id")
        }
        for wallet in wallets
    ]
    if len(wallets) == 1:
        return deepcopy(wallets[0])
    if transaction_sets[0] <= transaction_sets[1]:
        return deepcopy(wallets[1])
    if transaction_sets[1] <= transaction_sets[0]:
        return deepcopy(wallets[0])

    merged = deepcopy(max(wallets, key=lambda item: len(item.get("transactions", []))))
    transactions: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for wallet in wallets:
        for row in wallet.get("transactions", []):
            if not isinstance(row, dict):
                continue
            transaction_id = str(row.get("id") or "")
            if transaction_id:
                transactions[transaction_id] = deepcopy(row)
            else:
                anonymous.append(deepcopy(row))
    ordered = sorted(
        [*transactions.values(), *anonymous],
        key=lambda row: (str(row.get("at") or ""), str(row.get("id") or "")),
    )
    amounts_minor = []
    for row in ordered:
        try:
            amounts_minor.append(
                int(row["amount_minor"])
                if row.get("amount_minor") is not None
                else to_minor(row.get("amount") or 0)
            )
        except (TypeError, ValueError):
            amounts_minor = []
            break
    merged["transactions"] = ordered
    if amounts_minor:
        balance_minor = sum(amounts_minor)
        merged["balance_minor"] = balance_minor
        merged["balance"] = from_minor(balance_minor)
    merged["credit_effective_bet_count"] = max(
        int(wallet.get("credit_effective_bet_count") or 0) for wallet in wallets
    )
    return merged


def _recover_split_account(path: Path, legacy_root: Path) -> None:
    split_path = legacy_root / "bets" / ".account.fmodd"
    if not path.is_file() or not split_path.is_file() or split_path == path:
        return
    try:
        canonical = _read_container(path)
        split = _read_container(split_path)
    except RuntimeError:
        return
    split_documents = split.get("documents", {})
    if not any(key in split_documents for key in ("bets", "wallet")):
        return
    documents = canonical["documents"]
    documents["bets"] = _merge_bet_records(
        documents.get("bets", []), split_documents.get("bets", []),
    )
    documents["wallet"] = _merge_wallets(
        documents.get("wallet"), split_documents.get("wallet"),
    )
    _write_container(path, canonical)
    _read_container(path)
    backup = split_path.with_name(".account.recovered.fmodd")
    if backup.exists():
        backup = split_path.with_name(
            f".account.recovered-{split_path.stat().st_mtime_ns}.fmodd"
        )
    split_path.replace(backup)
    _CONTAINER_CACHE.pop(split_path, None)


def _migrate_legacy(
    scope_id: str | None = None, *, key: str = "", legacy_path: Path | None = None,
) -> dict[str, Any]:
    path, legacy_root = _storage_paths(scope_id, key, legacy_path)
    requested_key = key
    payload = _empty(path.stem)
    imported: list[Path] = []
    journal_path = legacy_root / "bets" / "fm26_account_transaction.json"
    if journal_path.is_file():
        try:
            journal = _load_json(journal_path)
            payload["documents"]["bets"] = list(journal["bets"])
            payload["documents"]["wallet"] = dict(journal["wallet"])
            imported.append(journal_path)
        except (OSError, ValueError, TypeError, KeyError):
            pass
    bets_path = legacy_root / "bets" / "fm26_bets.jsonl"
    if "bets" not in payload["documents"] and bets_path.is_file():
        records = []
        try:
            for line in bets_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if isinstance(row, dict):
                    records.append(row)
            payload["documents"]["bets"] = records
            imported.append(bets_path)
        except (OSError, json.JSONDecodeError):
            pass
    for document_key, relative in _JSON_DOCUMENTS.items():
        if document_key in payload["documents"]:
            source = legacy_root / relative
            if source.is_file():
                imported.append(source)
            continue
        source = legacy_root / relative
        if not source.is_file():
            continue
        try:
            payload["documents"][document_key] = _load_json(source)
            imported.append(source)
        except (OSError, json.JSONDecodeError):
            continue
    if requested_key and legacy_path is not None and requested_key not in payload["documents"]:
        source = Path(legacy_path)
        if source.is_file():
            try:
                payload["documents"][requested_key] = _load_json(source)
                imported.append(source)
            except (OSError, json.JSONDecodeError):
                pass
    _write_container(path, payload)
    _read_container(path)
    for source in dict.fromkeys(imported):
        try:
            source.unlink()
        except OSError:
            continue
    if legacy_root.parent.name.casefold() == "saves":
        cache_root = cache_data_root(scope_id or legacy_root.name)
        for name in _LEGACY_CACHE_DIRECTORIES:
            source = legacy_root / name
            target = cache_root / name
            blockers = _CACHE_MOVE_BLOCKERS.get(name, ())
            if (
                not source.is_dir() or target.exists()
                or any((source / blocker).exists() for blocker in blockers)
            ):
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                source.replace(target)
            except OSError:
                continue
    if legacy_root.is_dir():
        directories = sorted(
            (item for item in legacy_root.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts), reverse=True,
        )
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            legacy_root.rmdir()
        except OSError:
            pass
    return payload


def load_container(
    scope_id: str | None = None, *, key: str = "", legacy_path: Path | None = None,
) -> dict[str, Any]:
    with _LOCK:
        path, legacy_root = _storage_paths(scope_id, key, legacy_path)
        cached = _current_cached_container(path)
        if cached is not None and not _split_recovery_required(legacy_root):
            return deepcopy(cached)
        with storage_lock(path):
            _recover_split_account(path, legacy_root)
            _mark_split_recovery_checked(legacy_root)
            payload = (
                _cached_container(path) if path.is_file() else _migrate_legacy(
                    scope_id, key=key, legacy_path=legacy_path,
                )
            )
        return deepcopy(payload)


def load_document(
    key: str, default: Any, scope_id: str | None = None,
    *, legacy_path: Path | None = None,
) -> Any:
    with _LOCK:
        path, legacy_root = _storage_paths(scope_id, key, legacy_path)
        cached = _current_cached_container(path)
        if cached is not None and not _split_recovery_required(legacy_root):
            return deepcopy(cached["documents"].get(str(key), default))
        with storage_lock(path):
            _recover_split_account(path, legacy_root)
            _mark_split_recovery_checked(legacy_root)
            payload = (
                _cached_container(path) if path.is_file() else _migrate_legacy(
                    scope_id, key=key, legacy_path=legacy_path,
                )
            )
        return deepcopy(payload["documents"].get(str(key), default))


def update_document(
    key: str, default: Any, mutator: Callable[[Any], Any],
    scope_id: str | None = None, *, legacy_path: Path | None = None,
) -> Any:
    """Atomically mutate one document and return the mutator result."""
    with _LOCK:
        path, legacy_root = _storage_paths(scope_id, key, legacy_path)
        with storage_lock(path):
            _recover_split_account(path, legacy_root)
            payload = (
                deepcopy(_cached_container(path)) if path.is_file()
                else _migrate_legacy(
                    scope_id, key=key, legacy_path=legacy_path,
                )
            )
            document_key = str(key)
            if document_key in payload["documents"]:
                # ``payload`` is already a private deep copy of the cached
                # container, so copying the same document again is redundant.
                document = payload["documents"][document_key]
            else:
                document = deepcopy(default)
            if default is not None and not isinstance(document, type(default)):
                document = deepcopy(default)
            original = deepcopy(document)
            result = mutator(document)
            if document != original:
                payload["documents"][document_key] = document
                _write_container(path, payload)
        return deepcopy(result)


def update_documents(
    defaults: dict[str, Any], mutator: Callable[[dict[str, Any]], Any],
    scope_id: str | None = None, *, legacy_path: Path | None = None,
) -> Any:
    """Atomically mutate several documents in one account container."""
    with _LOCK:
        first_key = next(iter(defaults), "")
        path, legacy_root = _storage_paths(scope_id, first_key, legacy_path)
        with storage_lock(path):
            _recover_split_account(path, legacy_root)
            payload = (
                deepcopy(_cached_container(path)) if path.is_file()
                else _migrate_legacy(
                    scope_id, key=first_key, legacy_path=legacy_path,
                )
            )
            documents: dict[str, Any] = {}
            for key, default in defaults.items():
                document_key = str(key)
                if document_key in payload["documents"]:
                    document = payload["documents"][document_key]
                else:
                    document = deepcopy(default)
                if default is not None and not isinstance(document, type(default)):
                    document = deepcopy(default)
                documents[document_key] = document
            original = deepcopy(documents)
            result = mutator(documents)
            if documents != original:
                payload["documents"].update(documents)
                _write_container(path, payload)
        return deepcopy(result)


def save_documents(
    documents: dict[str, Any], scope_id: str | None = None,
    *, legacy_path: Path | None = None,
) -> None:
    with _LOCK:
        first_key = next(iter(documents), "")
        path, _legacy_root = _storage_paths(scope_id, first_key, legacy_path)
        with storage_lock(path):
            payload = (
                deepcopy(_cached_container(path)) if path.is_file()
                else _migrate_legacy(
                    scope_id, key=first_key, legacy_path=legacy_path,
                )
            )
            for key, value in documents.items():
                payload["documents"][str(key)] = deepcopy(value)
            _write_container(path, payload)


def save_document(
    key: str, value: Any, scope_id: str | None = None,
    *, legacy_path: Path | None = None,
) -> None:
    save_documents({str(key): value}, scope_id, legacy_path=legacy_path)


def _merge_document_lists(values: list[Any]) -> list[Any]:
    """Union list documents by stable row IDs while retaining anonymous rows."""
    merged: list[Any] = []
    positions: dict[str, int] = {}
    for value in values:
        if not isinstance(value, list):
            continue
        for row in value:
            if not isinstance(row, dict):
                merged.append(deepcopy(row))
                continue
            identity = next(
                (
                    str(row.get(field) or "").strip()
                    for field in ("id", "bet_id", "uid", "key", "source_key")
                    if str(row.get(field) or "").strip()
                ),
                "",
            )
            if not identity:
                merged.append(deepcopy(row))
                continue
            if identity in positions:
                merged[positions[identity]] = deepcopy(row)
            else:
                positions[identity] = len(merged)
                merged.append(deepcopy(row))
    return merged


def _document_richness(value: Any) -> int:
    if isinstance(value, dict):
        return sum(
            1 + _document_richness(item)
            for item in value.values()
            if item not in (None, "", [], {})
        )
    if isinstance(value, list):
        return len(value) + sum(_document_richness(item) for item in value)
    return int(value not in (None, "", False, 0))


def _merge_document_values(values: list[Any]) -> Any:
    present = [value for value in values if value is not None]
    if not present:
        return None
    if all(isinstance(value, list) for value in present):
        return _merge_document_lists(present)
    if all(isinstance(value, dict) for value in present):
        result = deepcopy(present[0])
        for value in present[1:]:
            for key, item in value.items():
                if key not in result:
                    result[key] = deepcopy(item)
                elif isinstance(result[key], dict) and isinstance(item, dict):
                    result[key] = _merge_document_values([result[key], item])
                elif isinstance(result[key], list) and isinstance(item, list):
                    result[key] = _merge_document_lists([result[key], item])
                elif item is not None:
                    # Source containers are ordered by updated_at; the newest
                    # scalar wins, while historical lists above are unioned.
                    result[key] = deepcopy(item)
        return result
    for value in reversed(present):
        if value not in (None, ""):
            return deepcopy(value)
    return deepcopy(present[-1])


def merge_account_containers(
    source_scope_ids: list[str], target_scope_id: str,
) -> dict[str, Any]:
    """Merge account documents into an already-registered target account.

    Wallet transactions and bets are deduplicated by their stable IDs. Other
    list documents are unioned similarly; scalar fields use the newest source
    container. Source containers are never deleted or modified.
    """
    sources = list(dict.fromkeys(str(item or "").strip() for item in source_scope_ids if str(item or "").strip()))
    target = str(target_scope_id or "").strip()
    if len(sources) < 2 or not target:
        raise ValueError("至少需要两个源账户和一个目标账户")
    loaded: list[tuple[str, dict[str, Any]]] = []
    for scope_id in sources:
        loaded.append((scope_id, load_container(scope_id)))
    loaded.sort(key=lambda item: str(item[1].get("updated_at") or ""))
    keys = sorted({
        str(key)
        for _scope, payload in loaded
        for key in (payload.get("documents") or {})
    })
    documents: dict[str, Any] = {}
    for key in keys:
        values = [payload.get("documents", {}).get(key) for _scope, payload in loaded]
        if key == "wallet":
            wallet_values = [value for value in values if isinstance(value, dict)]
            merged = deepcopy(wallet_values[0]) if wallet_values else {}
            for value in wallet_values[1:]:
                merged = _merge_wallets(merged, value)
            documents[key] = merged
        elif key == "bets":
            merged_bets: list[dict[str, Any]] = []
            for value in values:
                if isinstance(value, list):
                    merged_bets = _merge_bet_records(merged_bets, value)
            documents[key] = merged_bets
        else:
            # Empty accounts created by a mistaken identity split are often
            # newer than the real account. Prefer the branch with more durable
            # content; an equally complete newer branch still wins conflicts.
            documents[key] = _merge_document_values(sorted(
                values, key=_document_richness,
            ))
    target_path = account_save_path(target)
    payload = _empty(target_path.stem)
    payload["documents"] = documents
    with _LOCK, storage_lock(target_path):
        _write_container(target_path, payload)
    return {
        "source_count": len(loaded),
        "document_count": len(documents),
        "target_scope_id": target,
    }


def migrate_all_legacy_accounts() -> int:
    saves_root = account_save_path("placeholder").parent
    if not saves_root.is_dir():
        return 0
    migrated = 0
    for directory in list(saves_root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        path = account_save_path(directory.name)
        if not path.is_file():
            with _LOCK, storage_lock(path):
                if path.is_file():
                    continue
                _migrate_legacy(directory.name)
            migrated += 1
    return migrated
