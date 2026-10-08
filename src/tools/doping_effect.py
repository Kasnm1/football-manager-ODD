from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root
from tools.club_reader import (
    apply_doping_effect,
    maintain_doping_effect,
    restore_doping_effect,
)


_LOCK = threading.RLock()


def _state_path() -> Path:
    return save_data_root() / "economy" / "doping_effects.json"


def _load() -> dict[str, Any]:
    payload = load_document(
        "doping_effects", {"schema_version": 1, "effects": {}},
        legacy_path=_state_path(),
    )
    payload.setdefault("schema_version", 1)
    payload.setdefault("effects", {})
    return payload


def _save(payload: dict[str, Any]) -> None:
    save_document("doping_effects", payload, legacy_path=_state_path())


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


def _targets(item: dict[str, Any]) -> list[dict[str, Any]]:
    stored = list(item.get("player_targets") or [])
    if stored:
        return stored
    if item.get("player_id"):
        return [{
            "player_id": int(item["player_id"]),
            "player_name": str(item.get("player_name") or item["player_id"]),
            "player_address": str(item.get("player_address") or ""),
        }]
    return [{"player_id": int(value)} for value in item.get("player_ids") or []]


def _inherit_original_snapshots(
    players: list[dict[str, Any]], effects: dict[str, Any], team_id: int,
) -> list[dict[str, Any]]:
    """Keep the first pre-effect values when active doping windows overlap."""
    if not int(team_id):
        return [dict(player) for player in players]
    inherited: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for effect in effects.values():
        if int(effect.get("managed_team_id") or 0) != int(team_id):
            continue
        sources.extend(dict(row) for row in effect.get("players") or [])
    for applied_player in players:
        player = dict(applied_player)
        player_id = int(player.get("player_id") or 0)
        player_name = str(player.get("player_name") or "")
        source = next(
            (
                row for row in sources
                if player_id and int(row.get("player_id") or 0) == player_id
            ),
            None,
        )
        if source is None and player_name:
            name_matches = [
                row for row in sources
                if str(row.get("player_name") or "") == player_name
            ]
            if len(name_matches) == 1:
                source = name_matches[0]
        if source is not None:
            for key in (
                "original_attributes", "original_sharpness", "original_fitness",
            ):
                if key in source:
                    player[key] = source[key]
        inherited.append(player)
    return inherited


def sync_doping_effects(active_items: list[dict[str, Any]]) -> dict[str, Any]:
    with _LOCK:
        state = _load()
        effects = state["effects"]
        active = {str(item.get("id")): item for item in active_items if item.get("id")}
        errors: list[str] = []
        changed = False
        newly_applied: dict[str, dict[str, Any]] = {}

        # Repair already-persisted overlapping records while the earliest
        # snapshot is still available (including records created by older builds).
        earlier_effects: dict[str, Any] = {}
        for item_id, effect in effects.items():
            inherited_players = _inherit_original_snapshots(
                list(effect.get("players") or []), earlier_effects,
                int(effect.get("managed_team_id") or 0),
            )
            if inherited_players != effect.get("players"):
                effect["players"] = inherited_players
                changed = True
            earlier_effects[item_id] = effect

        # Restore yesterday's effects before capturing originals for a newly
        # active fixture, including when matches fall on consecutive days.
        for item_id in list(effects):
            if item_id in active:
                continue
            effect = effects[item_id]
            try:
                result = restore_doping_effect(
                    int(effect.get("managed_team_id") or 0),
                    effect.get("managed_team_address"),
                    list(effect.get("players") or []),
                )
                if result["remaining"]:
                    effect["players"] = result["remaining"]
                    effect["managed_team_address"] = result["team_address"]
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

        maintained = 0
        for item_id, item in active.items():
            effect = effects.get(item_id)
            try:
                if effect is not None:
                    result = maintain_doping_effect(
                        int(effect.get("managed_team_id") or 0),
                        effect.get("managed_team_address"),
                        list(effect.get("players") or []),
                    )
                    maintained += len(result["maintained"])
                    updated_players = list(result["players"])
                    updated_team_address = str(result["team_address"])
                    if (
                        updated_players != effect.get("players")
                        or updated_team_address != effect.get("managed_team_address")
                    ):
                        effect["players"] = updated_players
                        effect["managed_team_address"] = updated_team_address
                        changed = True
                    maintenance_error = "; ".join(result["errors"])
                    if maintenance_error:
                        if _mark_failed(effect, maintenance_error):
                            changed = True
                        errors.extend(result["errors"])
                    elif _mark_verified(
                        effect, result, written=bool(result.get("repaired")),
                        drifted=bool(result.get("repaired")),
                    ):
                        changed = True
                    continue
                applied = apply_doping_effect(
                    int(item.get("managed_team_id") or 0),
                    item.get("managed_team_address"),
                    _targets(item),
                    all_team=item.get("sku") == "team_doping",
                )
                rollback_players = list(applied["players"])
                persistent_players = _inherit_original_snapshots(
                    rollback_players, effects, int(applied["team_id"]),
                )
                effects[item_id] = {
                    "item_id": item_id,
                    "fixture_key": str(item.get("fixture_key") or ""),
                    "fixture_date": str(item.get("fixture_date") or ""),
                    "managed_team_id": int(applied["team_id"]),
                    "managed_team_name": str(applied["team_name"]),
                    "managed_team_address": str(applied["team_address"]),
                    "players": persistent_players,
                    "applied_at": _now(),
                }
                _mark_verified(effects[item_id], applied, written=True)
                newly_applied[item_id] = {
                    "team_id": int(applied["team_id"]),
                    "team_address": str(applied["team_address"]),
                    "players": rollback_players,
                }
                changed = True
            except Exception as error:
                if effect is not None and _mark_failed(effect, str(error)):
                    changed = True
                errors.append(f"{item.get('fixture_name') or item_id}: {error}")

        if changed:
            try:
                _save(state)
            except Exception:
                # Undo stacked writes in reverse application order so an
                # earlier clean snapshot is the final value restored.
                for applied in reversed(list(newly_applied.values())):
                    try:
                        restore_doping_effect(
                            int(applied["team_id"]),
                            applied["team_address"],
                            list(applied["players"]),
                        )
                    except Exception:
                        pass
                raise
        return {
            "active": len(active), "tracked": len(effects),
            "maintained": maintained, "errors": errors,
            "effects": [
                _runtime_status(effect) for effect in effects.values()
            ],
        }


def restore_all_doping_effects() -> dict[str, Any]:
    with _LOCK:
        state = _load()
        errors: list[str] = []
        for item_id, effect in list(state["effects"].items()):
            try:
                result = restore_doping_effect(
                    int(effect.get("managed_team_id") or 0),
                    effect.get("managed_team_address"),
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
            raise RuntimeError("兴奋剂属性尚未全部恢复：" + "; ".join(errors))
        return {"restored": True}
