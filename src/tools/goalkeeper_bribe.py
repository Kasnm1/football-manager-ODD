from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root
from tools.club_reader import (
    apply_goalkeeper_bribe,
    maintain_goalkeeper_bribe,
    restore_goalkeeper_bribe,
)


_LOCK = threading.RLock()


def _state_path() -> Path:
    return save_data_root() / "economy" / "goalkeeper_bribes.json"


def _load() -> dict[str, Any]:
    payload = load_document(
        "goalkeeper_bribes", {"schema_version": 1, "effects": {}},
        legacy_path=_state_path(),
    )
    payload.setdefault("schema_version", 1)
    payload.setdefault("effects", {})
    return payload


def _save(payload: dict[str, Any]) -> None:
    save_document("goalkeeper_bribes", payload, legacy_path=_state_path())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _runtime_status(effect: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(effect.get("item_id") or ""),
        "fixture_key": str(effect.get("fixture_key") or ""),
        "state": str(effect.get("runtime_state") or "pending"),
        "last_verified_at": effect.get("last_verified_at"),
        "last_written_at": effect.get("last_written_at"),
        "last_drift_at": effect.get("last_drift_at"),
        "last_error": effect.get("last_runtime_error"),
        "consecutive_failures": int(effect.get("consecutive_failures") or 0),
        "process_session": str(effect.get("process_session") or ""),
    }


def _mark_verified(
    effect: dict[str, Any], observation: dict[str, Any], *,
    written: bool, drifted: bool = False,
) -> bool:
    now = _now()
    session = str(observation.get("process_session") or "")
    changed = False
    transition = (
        effect.get("runtime_state") != "verified"
        or bool(written)
        or session != str(effect.get("process_session") or "")
    )
    updates = {
        "runtime_state": "verified",
        "consecutive_failures": 0,
        "process_session": session,
    }
    if transition:
        updates["last_verified_at"] = now
    if written:
        updates["last_written_at"] = now
    if drifted:
        updates["last_drift_at"] = now
    for key, value in updates.items():
        if effect.get(key) != value:
            effect[key] = value
            changed = True
    for key in ("last_runtime_error", "last_runtime_error_at"):
        if effect.pop(key, None) is not None:
            changed = True
    return changed


def _mark_failed(effect: dict[str, Any], error: str) -> bool:
    previous = int(effect.get("consecutive_failures") or 0)
    failures = min(3, previous + 1)
    state = "error" if failures >= 3 else "retrying"
    if (
        effect.get("runtime_state") == state
        and effect.get("last_runtime_error") == error
        and previous == failures
    ):
        return False
    effect.update({
        "runtime_state": state,
        "last_runtime_error": error,
        "last_runtime_error_at": _now(),
        "consecutive_failures": failures,
    })
    return True


def sync_goalkeeper_bribes(active_items: list[dict[str, Any]]) -> dict[str, Any]:
    """Write opponent goalkeeper attributes directly and restore them after use."""
    with _LOCK:
        state = _load()
        effects = state["effects"]
        active = {str(item.get("id")): item for item in active_items if item.get("id")}
        errors: list[str] = []
        changed = False
        newly_applied: set[str] = set()
        # Retire inactive effects. Active Hook-era snapshots only contain match
        # sharpness; restore them so the item can be reapplied immediately with
        # a durable copy of the original 54-byte attribute block.
        for item_id in list(effects):
            effect = effects[item_id]
            players = list(effect.get("players") or [])
            direct_write = bool(players) and all(
                row.get("original_attributes") for row in players
            )
            if item_id in active and direct_write:
                continue
            try:
                result = restore_goalkeeper_bribe(
                    int(effect.get("opponent_team_id") or 0),
                    effect.get("opponent_team_address"),
                    players,
                )
                if result["remaining"]:
                    effect["players"] = result["remaining"]
                    effect["opponent_team_address"] = result["team_address"]
                    effect["last_restore_error"] = "; ".join(result["errors"])
                    effect["runtime_state"] = "restore_pending"
                    effect["last_runtime_error"] = effect["last_restore_error"]
                else:
                    effects.pop(item_id, None)
                changed = True
                errors.extend(result["errors"])
            except Exception as error:
                effect["last_restore_error"] = str(error)
                effect["last_restore_attempt_at"] = _now()
                effect["runtime_state"] = "restore_pending"
                changed = True
                errors.append(f"{effect.get('fixture_key') or item_id}: {error}")

        targets: list[dict[str, Any]] = []
        maintained = 0
        for item_id, item in active.items():
            effect = effects.get(item_id)
            try:
                if effect is None:
                    applied = apply_goalkeeper_bribe(
                        int(item.get("opponent_team_id") or 0),
                        item.get("opponent_team_address"),
                    )
                    effect = {
                        "item_id": item_id,
                        "fixture_key": str(item.get("fixture_key") or ""),
                        "fixture_date": str(item.get("fixture_date") or ""),
                        "opponent_team_id": int(applied["team_id"]),
                        "opponent_team_name": str(applied["team_name"]),
                        "opponent_team_address": str(applied["team_address"]),
                        "players": list(applied["players"]),
                        "applied_at": _now(),
                    }
                    _mark_verified(effect, applied, written=True)
                    effects[item_id] = effect
                    newly_applied.add(item_id)
                    changed = True
                else:
                    observation = maintain_goalkeeper_bribe(
                        int(effect.get("opponent_team_id") or 0),
                        effect.get("opponent_team_address"),
                        list(effect.get("players") or []),
                    )
                    maintained += len(observation["maintained"])
                    updated_players = list(observation["players"])
                    updated_team_address = str(observation["team_address"])
                    if (
                        updated_players != effect.get("players")
                        or updated_team_address != effect.get("opponent_team_address")
                    ):
                        effect["players"] = updated_players
                        effect["opponent_team_address"] = updated_team_address
                        changed = True
                    maintenance_error = "; ".join(observation["errors"])
                    if maintenance_error:
                        if _mark_failed(effect, maintenance_error):
                            changed = True
                        errors.extend(observation["errors"])
                    elif _mark_verified(
                        effect, observation, written=bool(observation.get("repaired")),
                        drifted=bool(observation.get("repaired")),
                    ):
                        changed = True
                resolved = {
                    "team_id": int(effect.get("opponent_team_id") or 0),
                    "team_name": str(effect.get("opponent_team_name") or ""),
                    "team_address": str(effect.get("opponent_team_address") or ""),
                    "players": list(effect.get("players") or []),
                }
                targets.append({
                    "item_id": item_id,
                    "fixture_key": str(item.get("fixture_key") or ""),
                    "runtime": _runtime_status(effect),
                    **resolved,
                })
            except Exception as error:
                if effect is not None and _mark_failed(effect, str(error)):
                    changed = True
                errors.append(f"{item.get('fixture_name') or item_id}: {error}")
        if changed:
            state["schema_version"] = 4
            try:
                _save(state)
            except Exception:
                for item_id in newly_applied:
                    effect = effects[item_id]
                    try:
                        restore_goalkeeper_bribe(
                            int(effect.get("opponent_team_id") or 0),
                            effect.get("opponent_team_address"),
                            list(effect.get("players") or []),
                        )
                    except Exception:
                        pass
                raise
        return {
            "active": len(active),
            "tracked": len(effects),
            "mode": "direct_attribute_block",
            "maintained": maintained,
            "targets": targets,
            "effects": [
                _runtime_status(effect) for effect in effects.values()
            ],
            "errors": errors,
        }


def restore_all_goalkeeper_bribes() -> dict[str, Any]:
    """Restore every tracked effect before destructive storage cleanup."""
    with _LOCK:
        state = _load()
        errors: list[str] = []
        for item_id, effect in list(state["effects"].items()):
            try:
                result = restore_goalkeeper_bribe(
                    int(effect.get("opponent_team_id") or 0),
                    effect.get("opponent_team_address"),
                    list(effect.get("players") or []),
                )
                if result["remaining"]:
                    effect["players"] = result["remaining"]
                    errors.extend(result["errors"])
                else:
                    state["effects"].pop(item_id, None)
            except Exception as error:
                errors.append(str(error))
        _save(state)
        if errors:
            raise RuntimeError("买通门将属性尚未全部恢复：" + "; ".join(errors))
        return {"restored": True}
