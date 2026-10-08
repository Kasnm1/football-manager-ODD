from __future__ import annotations

import threading
from datetime import date, timedelta
from typing import Any

from tools.account_store import load_document, save_document


_LOCK = threading.RLock()
POLICY_DURATIONS = {
    "departure_mediation": 7,
    "salary_authorization": 15,
    "interview_perfume": 15,
}


def _load() -> dict[str, Any]:
    payload = load_document("club_policies", None)
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("policies", {})
    payload.setdefault("history", [])
    return payload


def _save(payload: dict[str, Any]) -> None:
    save_document("club_policies", payload)


def _context_matches(
    record: dict[str, Any], *, manager_id: int, team_id: int, club_id: int,
) -> bool:
    return bool(
        manager_id > 0
        and team_id > 0
        and int(record.get("manager_id") or 0) == manager_id
        and int(record.get("team_id") or 0) == team_id
        and (
            club_id <= 0
            or int(record.get("club_id") or 0) in {0, club_id}
        )
    )


def _public_record(
    key: str, record: dict[str, Any], game_date: str, *,
    manager_id: int, team_id: int, club_id: int,
) -> dict[str, Any]:
    duration = int(POLICY_DURATIONS[key])
    current: date | None = None
    try:
        current = date.fromisoformat(str(game_date))
    except ValueError:
        pass
    expires_on = str(record.get("expires_on") or "")
    remaining_days = 0
    if current and expires_on:
        try:
            remaining_days = max(0, (date.fromisoformat(expires_on) - current).days)
        except ValueError:
            remaining_days = 0
    context_matches = _context_matches(
        record, manager_id=manager_id, team_id=team_id, club_id=club_id,
    )
    active = bool(record.get("enabled") and context_matches and remaining_days > 0)
    reason = ""
    if record.get("enabled") and not context_matches:
        reason = "仅对召开会议时执教的俱乐部生效"
    elif record.get("enabled") and not current:
        reason = "等待读取游戏日期"
    elif record.get("enabled") and remaining_days <= 0:
        reason = "授权已到期"
    return {
        "key": key,
        "enabled": active,
        "requested_enabled": bool(record.get("enabled")),
        "duration_days": duration,
        "started_on": str(record.get("started_on") or ""),
        "expires_on": expires_on,
        "remaining_days": remaining_days if active else 0,
        "manager_id": int(record.get("manager_id") or 0),
        "team_id": int(record.get("team_id") or 0),
        "club_id": int(record.get("club_id") or 0),
        "inactive_reason": reason,
    }


def public_club_policies(
    game_date: str, *, manager_id: int, team_id: int, club_id: int = 0,
) -> dict[str, Any]:
    with _LOCK:
        payload = _load()
        policies = payload.get("policies") or {}
        return {
            key: _public_record(
                key, dict(policies.get(key) or {}), game_date,
                manager_id=manager_id, team_id=team_id, club_id=club_id,
            )
            for key in POLICY_DURATIONS
        }


def set_club_policy(
    key: str, enabled: bool, game_date: str, *,
    manager_id: int, team_id: int, club_id: int = 0,
) -> dict[str, Any]:
    if key not in POLICY_DURATIONS:
        raise ValueError("俱乐部授权类型无效")
    if manager_id <= 0 or team_id <= 0:
        raise ValueError("尚未确认当前经理与俱乐部")
    try:
        current = date.fromisoformat(str(game_date))
    except ValueError as error:
        raise ValueError("尚未读取到有效的游戏日期") from error
    with _LOCK:
        payload = _load()
        policies = payload.setdefault("policies", {})
        previous = dict(policies.get(key) or {})
        if enabled:
            duration = int(POLICY_DURATIONS[key])
            record = {
                "enabled": True,
                "started_on": current.isoformat(),
                "expires_on": (current + timedelta(days=duration)).isoformat(),
                "manager_id": int(manager_id),
                "team_id": int(team_id),
                "club_id": int(club_id),
            }
        else:
            record = {
                **previous,
                "enabled": False,
                "disabled_on": current.isoformat(),
            }
        policies[key] = record
        payload.setdefault("history", []).append({
            "key": key,
            "enabled": bool(enabled),
            "game_date": current.isoformat(),
            "manager_id": int(manager_id),
            "team_id": int(team_id),
            "club_id": int(club_id),
        })
        payload["history"] = payload["history"][-100:]
        _save(payload)
        return _public_record(
            key, record, current.isoformat(), manager_id=manager_id,
            team_id=team_id, club_id=club_id,
        )
