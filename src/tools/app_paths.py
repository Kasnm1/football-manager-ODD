from __future__ import annotations

import os
import json
import re
import sys
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path

from tools.save_context import SaveContextRegistry
from tools.domain_errors import UnavailableError
from tools.storage_migration import migrate_data_root
from tools.storage_io import atomic_write_json, storage_lock


FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", PROJECT_ROOT))
DATA_ROOT_CONFIG_PATH = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "FMODD-Config" / "data-root.json"
DATA_ROOT_MARKER = ".fmodd-data-root"


def _configured_data_root() -> Path | None:
    try:
        payload = json.loads(DATA_ROOT_CONFIG_PATH.read_text(encoding="utf-8"))
        value = str(payload.get("path") or "").strip()
        path = Path(value).expanduser()
        return path if value and path.is_absolute() else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _storage_root_available(path: Path) -> bool:
    """Return whether the volume containing *path* is currently mounted.

    A configured removable-drive path can remain valid in the settings file
    after the drive is unplugged.  Checking the anchor before selecting it
    prevents the desktop entry point from crashing while creating its normal
    subdirectories.  The configured path itself need not exist yet; it is
    still valid when its volume is mounted and the directory can be created.
    """
    try:
        anchor = Path(path.anchor)
        return bool(path.is_absolute() and anchor and anchor.exists())
    except (OSError, RuntimeError, ValueError):
        return False


def _select_data_root() -> Path:
    configured = os.environ.get("FMODD_DATA_ROOT")
    if configured:
        candidate = Path(configured).expanduser()
        if _storage_root_available(candidate):
            return candidate
    if FROZEN and _CUSTOM_DATA_ROOT and _storage_root_available(_CUSTOM_DATA_ROOT):
        return _CUSTOM_DATA_ROOT
    return Path.home() / "Documents" / "FMODD" if FROZEN else PROJECT_ROOT / "data"


_CUSTOM_DATA_ROOT = _configured_data_root() if FROZEN else None
DATA_ROOT = _select_data_root()
SAVE_REGISTRY_PATH = DATA_ROOT / "save_registry.json"
CONTEXT_REGISTRY_PATH = DATA_ROOT / "context_registry.v2.json"
SAVE_CONTEXTS = SaveContextRegistry(CONTEXT_REGISTRY_PATH, DATA_ROOT, SAVE_REGISTRY_PATH)
_SAVE_CONTEXT_LOCK = storage_lock(SAVE_REGISTRY_PATH)
_ACTIVE_SAVE_ID: ContextVar[str | None] = ContextVar("fmodd_active_save_id", default=None)
_ACCOUNT_BINDING_REVISION: ContextVar[int] = ContextVar("fmodd_account_binding_revision", default=0)
_LAST_ACTIVE_SAVE_ID = SAVE_CONTEXTS.last_active_scope()


def safe_save_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value).strip())
    return cleaned or "unknown"


def active_save_id() -> str | None:
    return _ACTIVE_SAVE_ID.get()


def active_account_context() -> tuple[str | None, int]:
    """Thread/task-local binding provenance; never persisted as account identity."""
    return _ACTIVE_SAVE_ID.get(), _ACCOUNT_BINDING_REVISION.get()


def set_active_save_id(value: str | None) -> str | None:
    resolved = safe_save_id(value) if value else None
    if resolved != _ACTIVE_SAVE_ID.get():
        _ACCOUNT_BINDING_REVISION.set(_ACCOUNT_BINDING_REVISION.get() + 1)
    _ACTIVE_SAVE_ID.set(resolved)
    return resolved


def remember_active_save_id(value: str | None) -> str | None:
    global _LAST_ACTIVE_SAVE_ID
    resolved = set_active_save_id(value)
    if resolved and resolved != _LAST_ACTIVE_SAVE_ID:
        SAVE_CONTEXTS.remember_active_scope(resolved)
        _LAST_ACTIVE_SAVE_ID = resolved
    return resolved


def restore_active_save_id() -> str | None:
    """Restore the last validated account scope without clearing it on shutdown."""
    if not _LAST_ACTIVE_SAVE_ID:
        return None
    return set_active_save_id(_LAST_ACTIVE_SAVE_ID)


def _required_storage_identity(
    save_id: str | None, *, allow_active: bool, label: str,
) -> str:
    raw_identity = str(
        save_id or (active_save_id() if allow_active else None) or ""
    ).strip()
    if not raw_identity or safe_save_id(raw_identity).casefold() == "unidentified":
        raise RuntimeError(f"尚未绑定 FMODD {label}作用域，拒绝使用 unidentified")
    return safe_save_id(raw_identity)


def save_data_root(save_id: str | None = None) -> Path:
    """Return the legacy account directory used only for one-time migration."""
    identity = safe_save_id(save_id or active_save_id() or "unidentified")
    resolved = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(identity) or identity)
    return DATA_ROOT / "saves" / resolved


def account_save_path(save_id: str | None = None) -> Path:
    identity = safe_save_id(save_id or active_save_id() or "unidentified")
    resolved = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(identity) or identity)
    return DATA_ROOT / "saves" / f"{resolved}.fmodd"


def cache_data_root(save_id: str | None = None) -> Path:
    identity = _required_storage_identity(
        save_id, allow_active=True, label="缓存",
    )
    resolved = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(identity) or identity)
    return DATA_ROOT / "cache" / resolved


def career_data_root(save_id: str | None) -> Path:
    """Return durable, career-scoped data that must survive cache deletion."""
    identity = _required_storage_identity(
        save_id, allow_active=False, label="生涯",
    )
    return DATA_ROOT / "careers" / identity


def save_identity_matches(left: str | None, right: str | None) -> bool:
    raw_first = str(left or "").strip()
    raw_second = str(right or "").strip()
    if not raw_first or not raw_second:
        return False
    first = safe_save_id(raw_first)
    second = safe_save_id(raw_second)
    if first == second:
        return True
    return (
        safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(first) or first)
        == safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(second) or second)
    )


def configure_data_root(value: str) -> dict[str, str | bool | int]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("请选择数据保存目录")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise ValueError("数据保存目录必须是绝对路径")
    target = target.resolve()
    if target == Path(target.anchor) or not target.name:
        raise ValueError("不能将磁盘根目录直接设为 FMODD 数据目录")
    migration = migrate_data_root(DATA_ROOT, target, marker_name=DATA_ROOT_MARKER)
    try:
        DATA_ROOT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(DATA_ROOT_CONFIG_PATH, {"path": str(target)}, indent=2)
    except OSError as error:
        raise UnavailableError(
            f"无法保存 FMODD 数据目录配置：{DATA_ROOT_CONFIG_PATH}",
            code="storage_migration_failed",
            phase="configure_storage_root",
            retryable=True,
            details={"path": str(DATA_ROOT_CONFIG_PATH)},
            message_key="storage.migration.failed",
        ) from error
    return {
        "path": str(DATA_ROOT),
        "configured_path": str(target),
        "restart_required": target != DATA_ROOT.resolve(),
        **migration,
    }


def configured_data_root() -> Path | None:
    return _configured_data_root()


def saved_account_scopes() -> list[dict[str, object]]:
    """List durable account scopes without reading their potentially large data files."""
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            registry = {}
    names = registry.get("save_names") or {}
    rows: dict[str, dict[str, object]] = {
        str(item["scope_id"]): dict(item)
        for item in SAVE_CONTEXTS.list_accounts()
        if not str(item.get("merged_into") or "").strip()
    }
    claimed_storage = {
        str(item.get("storage_scope") or "") for item in rows.values()
    }
    career_storage = SAVE_CONTEXTS.career_storage_scopes()
    for account_key, raw_scope in (registry.get("account_scopes") or {}).items():
        scope_id = safe_save_id(raw_scope)
        if scope_id in claimed_storage or scope_id in career_storage:
            continue
        save_id, separator, manager_text = str(account_key).rpartition("|manager-")
        manager_id = int(manager_text) if separator and manager_text.isdigit() else None
        canonical_save = safe_save_id(save_id or scope_id.split(".manager-", 1)[0])
        rows[scope_id] = {
            "scope_id": scope_id,
            "save_id": canonical_save,
            "save_name": str(names.get(canonical_save) or canonical_save),
            "namespace": "fm24" if canonical_save.startswith("fm24-") else "fm26",
            "manager_id": manager_id,
        }
    saves_root = DATA_ROOT / "saves"
    if saves_root.is_dir():
        try:
            directories = [path for path in saves_root.iterdir() if path.is_dir() and not path.is_symlink()]
        except OSError:
            directories = []
        for directory in directories:
            scope_id = safe_save_id(directory.name)
            if scope_id in claimed_storage or scope_id in career_storage:
                continue
            base, separator, manager_text = scope_id.rpartition(".manager-")
            canonical_save = safe_save_id(base if separator else scope_id)
            rows.setdefault(scope_id, {
                "scope_id": scope_id,
                "save_id": canonical_save,
                "save_name": str(names.get(canonical_save) or canonical_save),
                "namespace": "fm24" if canonical_save.startswith("fm24-") else "fm26",
                "manager_id": int(manager_text) if separator and manager_text.isdigit() else None,
            })
        try:
            save_files = [
                path for path in saves_root.glob("*.fmodd")
                if path.is_file() and not path.is_symlink()
            ]
        except OSError:
            save_files = []
        for path in save_files:
            scope_id = safe_save_id(path.stem)
            if scope_id in claimed_storage or scope_id in career_storage:
                continue
            rows.setdefault(scope_id, {
                "scope_id": scope_id,
                "save_id": scope_id,
                "save_name": str(names.get(scope_id) or scope_id),
                "namespace": "fm24" if scope_id.startswith("fm24-") else "fm26",
                "manager_id": None,
            })
    def account_created_time(row: dict[str, object]) -> float:
        created_at = str(row.get("created_at") or "").strip()
        if created_at:
            try:
                return datetime.fromisoformat(created_at).timestamp()
            except ValueError:
                pass
        storage_scope = safe_save_id(
            str(row.get("storage_scope") or row.get("scope_id") or "")
        )
        for path in (
            saves_root / f"{storage_scope}.fmodd",
            saves_root / storage_scope,
        ):
            try:
                if path.exists() and not path.is_symlink():
                    return path.stat().st_mtime
            except OSError:
                continue
        return 0.0

    return sorted(rows.values(), key=lambda row: (
        -account_created_time(row), str(row["save_name"]).casefold(),
        int(row.get("manager_id") or 0), str(row["scope_id"]),
    ))


def forget_saved_storage_scopes(storage_scopes: set[str]) -> int:
    """Remove registry references after their .fmodd files are explicitly deleted."""
    targets = {safe_save_id(item) for item in storage_scopes if str(item or "").strip()}
    if not targets:
        return 0
    removed = SAVE_CONTEXTS.forget_storage_scopes(targets)
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return removed
        changed = False
        scopes = registry.get("account_scopes")
        if isinstance(scopes, dict):
            kept = {
                key: value for key, value in scopes.items()
                if safe_save_id(value) not in targets
            }
            changed = kept != scopes
            registry["account_scopes"] = kept
        selected = registry.get("selected_managers")
        if isinstance(selected, dict):
            kept = {
                key: value for key, value in selected.items()
                if safe_save_id(key) not in targets
            }
            changed = changed or kept != selected
            registry["selected_managers"] = kept
        if changed:
            atomic_write_json(SAVE_REGISTRY_PATH, registry, indent=2)
    return removed


def resolve_save_identity(
    telemetry_id: str | None, manager_id: int | None, *, preferred_id: str | None = None,
    stable_id: str | None = None, equivalent_stable_id: str | None = None,
    namespace: str = "fm26",
) -> str | None:
    return SAVE_CONTEXTS.resolve_career(
        namespace=str(namespace or "fm26"),
        stable_key=safe_save_id(stable_id) if stable_id else None,
        equivalent_stable_key=(
            safe_save_id(equivalent_stable_id) if equivalent_stable_id else None
        ),
        telemetry_key=safe_save_id(telemetry_id) if telemetry_id else None,
        manager_id=manager_id,
        preferred_career_id=(
            safe_save_id(preferred_id)
            if str(preferred_id or "").startswith("career-") else None
        ),
    )


def create_merged_account(
    career_id: str, manager_id: int, source_account_ids: list[str], *,
    manager_name: str | None = None, team_name: str | None = None,
) -> dict[str, object]:
    return SAVE_CONTEXTS.create_merged_account(
        career_id, manager_id, source_account_ids,
        manager_name=manager_name, team_name=team_name,
    )


def rollback_merged_account(
    career_id: str, manager_id: int, merge: dict[str, object],
) -> None:
    SAVE_CONTEXTS.rollback_merged_account(career_id, manager_id, merge)


def known_save_identity(stable_id: str | None, *, namespace: str) -> str | None:
    return SAVE_CONTEXTS.lookup_career(
        namespace=str(namespace or "fm26"), stable_key=stable_id,
    )


def has_save_contexts(*, namespace: str) -> bool:
    return SAVE_CONTEXTS.has_careers(str(namespace or "fm26"))


def resolve_account_scope(
    save_id: str | None, manager_id: int | None, *, preferred_scope: str | None = None,
    manager_name: str | None = None, team_name: str | None = None,
) -> str | None:
    """Return a durable wallet/data scope for one save and one human manager.

    Legacy installs used the base save directory for single-manager saves and a
    manager suffix only when the current scan happened to see multiple managers.
    Preserve whichever legacy directory already owns data, then persist that
    choice so transient manager discovery can never switch the account again.
    """
    raw_save_id = str(save_id or "").strip()
    if not raw_save_id:
        return None
    canonical_save = safe_save_id(raw_save_id)
    manager_value = int(manager_id or 0)
    preferred = safe_save_id(preferred_scope) if preferred_scope else None
    context_scope = SAVE_CONTEXTS.resolve_account(
        canonical_save, manager_value, preferred_scope=preferred,
        manager_name=manager_name, team_name=team_name,
    )
    if context_scope:
        return context_scope
    if manager_value <= 0:
        if preferred and (
            preferred == canonical_save or preferred.startswith(f"{canonical_save}.manager-")
        ):
            return preferred
        return canonical_save

    account_key = f"{canonical_save}|manager-{manager_value}"
    legacy_manager_scope = safe_save_id(f"{canonical_save}.manager-{manager_value}")
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            registry = {"manager_aliases": {}, "telemetry_aliases": {}, "savegame_aliases": {}}
        scopes = registry.setdefault("account_scopes", {})
        existing = scopes.get(account_key)
        if existing:
            return safe_save_id(existing)

        valid_preferred = preferred and (
            preferred == canonical_save or preferred.startswith(f"{canonical_save}.manager-")
        )
        assigned = {safe_save_id(value) for key, value in scopes.items() if key != account_key}
        saves_root = DATA_ROOT / "saves"
        base_exists = (
            (saves_root / canonical_save).exists()
            or (saves_root / f"{canonical_save}.fmodd").exists()
        )
        manager_exists = (
            (saves_root / legacy_manager_scope).exists()
            or (saves_root / f"{legacy_manager_scope}.fmodd").exists()
        )
        if valid_preferred:
            resolved = preferred
        elif manager_exists:
            resolved = legacy_manager_scope
        elif base_exists and canonical_save not in assigned:
            resolved = canonical_save
        else:
            resolved = legacy_manager_scope
        scopes[account_key] = resolved
        SAVE_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(SAVE_REGISTRY_PATH, registry, indent=2)
        return resolved


def account_scope_display_name(scope_id: str | None) -> str | None:
    value = str(scope_id or "").strip()
    if not value:
        return None
    return SAVE_CONTEXTS.account_name(safe_save_id(value))


def confirmed_save_name(save_id: str | None = None) -> str | None:
    raw_identity = str(save_id or active_save_id() or "").strip()
    if not raw_identity:
        return None
    canonical = safe_save_id(raw_identity)
    context_name = SAVE_CONTEXTS.career_name(canonical)
    if context_name:
        return context_name
    storage_scope = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(canonical) or canonical)
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = str((registry.get("save_names") or {}).get(storage_scope) or "").strip()
        return value or None


def remember_save_name(save_id: str | None, value: str | None) -> str | None:
    raw_identity = str(save_id or "").strip()
    cleaned = str(value or "").strip()
    if not raw_identity or not cleaned:
        return None
    canonical = safe_save_id(raw_identity)
    if SAVE_CONTEXTS.remember_name(canonical, cleaned):
        storage_scope = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(canonical) or canonical)
    else:
        storage_scope = canonical
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            registry = {"manager_aliases": {}, "telemetry_aliases": {}, "savegame_aliases": {}}
        names = registry.setdefault("save_names", {})
        if names.get(storage_scope) != cleaned:
            names[storage_scope] = cleaned
            SAVE_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(SAVE_REGISTRY_PATH, registry, indent=2)
    return cleaned


def selected_manager_id(save_id: str | None) -> int | None:
    raw_identity = str(save_id or "").strip()
    if not raw_identity:
        return None
    canonical = safe_save_id(raw_identity)
    context_manager = SAVE_CONTEXTS.selected_manager(canonical)
    if context_manager:
        return context_manager
    storage_scope = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(canonical) or canonical)
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
            value = int((registry.get("selected_managers") or {}).get(storage_scope) or 0)
            return value if value > 0 else None
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None


def remember_selected_manager(save_id: str | None, manager_id: int | None) -> int | None:
    raw_identity = str(save_id or "").strip()
    value = int(manager_id or 0)
    if not raw_identity or value <= 0:
        return None
    canonical = safe_save_id(raw_identity)
    SAVE_CONTEXTS.remember_selected_manager(canonical, value)
    storage_scope = safe_save_id(SAVE_CONTEXTS.resolve_storage_scope(canonical) or canonical)
    with _SAVE_CONTEXT_LOCK:
        try:
            registry = json.loads(SAVE_REGISTRY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            registry = {"manager_aliases": {}, "telemetry_aliases": {}, "savegame_aliases": {}}
        selections = registry.setdefault("selected_managers", {})
        selections[storage_scope] = value
        SAVE_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(SAVE_REGISTRY_PATH, registry, indent=2)
    return value


def ensure_data_directories() -> None:
    for relative in ("cache", "careers", "saves", "references"):
        (DATA_ROOT / relative).mkdir(parents=True, exist_ok=True)
