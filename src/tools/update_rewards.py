from __future__ import annotations

import json
from typing import Any

from tools.app_paths import (
    DATA_ROOT, account_save_path, save_data_root, saved_account_scopes,
)
from tools.betting_account import (
    queue_version_update_reward, version_update_reward_key,
)
from tools.storage_io import atomic_write_json, storage_lock


STATE_SCHEMA_VERSION = 1
STATE_PATH = DATA_ROOT / "update_rewards.json"


def _empty_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "observed_version": "",
        "pending_rewards": {},
    }


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return _empty_state()
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"无法读取更新奖励状态：{STATE_PATH}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"更新奖励状态格式无效：{STATE_PATH}")
    try:
        schema_version = int(payload.get("schema_version", 1))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"更新奖励状态版本无效：{STATE_PATH}") from error
    if schema_version != STATE_SCHEMA_VERSION:
        raise RuntimeError(
            f"不支持的更新奖励状态版本 {schema_version}：{STATE_PATH}"
        )
    pending = payload.get("pending_rewards")
    if not isinstance(pending, dict):
        raise RuntimeError(f"更新奖励待处理记录无效：{STATE_PATH}")
    payload["schema_version"] = STATE_SCHEMA_VERSION
    payload["observed_version"] = str(payload.get("observed_version") or "")
    payload["pending_rewards"] = {
        str(version): sorted({str(scope) for scope in scopes if str(scope).strip()})
        for version, scopes in pending.items()
        if str(version).strip() and isinstance(scopes, list)
    }
    return payload


def _save_state(payload: dict[str, Any]) -> None:
    payload["schema_version"] = STATE_SCHEMA_VERSION
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STATE_PATH, payload, indent=2)


def _account_storage_exists(scope_id: str) -> bool:
    account_path = account_save_path(scope_id)
    legacy_root = save_data_root(scope_id)
    return (
        account_path.is_file() and not account_path.is_symlink()
    ) or (
        legacy_root.is_dir() and not legacy_root.is_symlink()
    )


def _existing_account_scopes() -> list[str]:
    scopes: set[str] = set()
    for row in saved_account_scopes():
        raw_scope = str(row.get("scope_id") or "").strip()
        if raw_scope and _account_storage_exists(raw_scope):
            scopes.add(account_save_path(raw_scope).stem)
    return sorted(scopes)


def _complete_pending_reward(app_version: str, scope_id: str) -> None:
    with storage_lock(STATE_PATH):
        state = _load_state()
        pending = state["pending_rewards"]
        scopes = list(pending.get(app_version) or [])
        if scope_id not in scopes:
            return
        remaining = [item for item in scopes if item != scope_id]
        if remaining:
            pending[app_version] = remaining
        else:
            pending.pop(app_version, None)
        _save_state(state)


def process_version_update_rewards(app_version: str) -> dict[str, Any]:
    """Detect a release change and queue claim mail for existing accounts."""
    current_version = str(app_version or "").strip()
    if not current_version:
        raise ValueError("应用版本不能为空")

    with storage_lock(STATE_PATH):
        state = _load_state()
        previous_version = str(state.get("observed_version") or "")
        current_reward_version = version_update_reward_key(current_version)
        previous_reward_version = (
            version_update_reward_key(previous_version) if previous_version else ""
        )
        version_changed = previous_reward_version != current_reward_version
        if version_changed:
            pending = state["pending_rewards"]
            pending[current_version] = _existing_account_scopes()
            if not pending[current_version]:
                pending.pop(current_version, None)
            state["observed_version"] = current_version
            _save_state(state)
        batches = {
            version: list(scopes)
            for version, scopes in state["pending_rewards"].items()
        }

    queued: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for reward_version, scopes in batches.items():
        for scope_id in scopes:
            if not _account_storage_exists(scope_id):
                _complete_pending_reward(reward_version, scope_id)
                continue
            try:
                result = queue_version_update_reward(scope_id, reward_version)
            except Exception as error:
                failures.append({
                    "scope_id": scope_id,
                    "app_version": reward_version,
                    "error": str(error),
                })
                continue
            queued.append(result)
            _complete_pending_reward(reward_version, scope_id)

    return {
        "app_version": current_version,
        "reward_version": current_reward_version,
        "previous_version": previous_version,
        "version_changed": version_changed,
        "processed": len(queued),
        "queued": sum(1 for row in queued if row.get("queued")),
        "failures": failures,
    }
