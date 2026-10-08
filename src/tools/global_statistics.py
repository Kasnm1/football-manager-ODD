from __future__ import annotations

import json
import threading
from datetime import datetime
from time import monotonic
from typing import Any

from tools.account_store import load_document
from tools.app_paths import DATA_ROOT, save_data_root, saved_account_scopes
from tools.money import from_minor, to_minor
from tools.storage_io import atomic_write_json, storage_lock


GLOBAL_STATISTICS_PATH = DATA_ROOT / "global_statistics.json"
USAGE_SCHEMA_VERSION = 2
MAX_HEARTBEAT_GAP_SECONDS = 45.0

_LOCK = threading.RLock()
_LAST_USAGE_HEARTBEAT: float | None = None
_LAST_USAGE_SCOPE_ID: str | None = None


def _now() -> datetime:
    return datetime.now()


def _empty_usage_entry() -> dict[str, Any]:
    return {
        "total_seconds": 0,
        "first_used_at": "",
        "last_used_at": "",
        "active_dates": [],
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        **_empty_usage_entry(),
        "scope_usage": {},
    }


def _normalise_usage_entry(payload: Any) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    active_dates = source.get("active_dates")
    return {
        "total_seconds": max(0, int(source.get("total_seconds") or 0)),
        "first_used_at": str(source.get("first_used_at") or ""),
        "last_used_at": str(source.get("last_used_at") or ""),
        "active_dates": sorted({
            str(value) for value in (active_dates if isinstance(active_dates, list) else [])
            if str(value)
        }),
    }


def _load_usage() -> dict[str, Any]:
    try:
        payload = json.loads(GLOBAL_STATISTICS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_usage()
    if not isinstance(payload, dict):
        return _empty_usage()
    scope_usage = payload.get("scope_usage")
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        **_normalise_usage_entry(payload),
        "scope_usage": {
            str(scope_id): _normalise_usage_entry(value)
            for scope_id, value in (
                scope_usage.items() if isinstance(scope_usage, dict) else []
            )
            if str(scope_id).strip()
        },
    }


def _public_usage(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "usage_seconds": max(0, int(payload.get("total_seconds") or 0)),
        "active_days": len(payload.get("active_dates") or []),
        "first_used_at": str(payload.get("first_used_at") or ""),
        "last_used_at": str(payload.get("last_used_at") or ""),
    }


def _touch_usage_entry(entry: dict[str, Any], current_time: datetime) -> None:
    timestamp = current_time.isoformat(timespec="seconds")
    if not entry["first_used_at"]:
        entry["first_used_at"] = timestamp
    entry["last_used_at"] = timestamp
    active_dates = set(entry.get("active_dates") or [])
    active_dates.add(current_time.date().isoformat())
    entry["active_dates"] = sorted(active_dates)


def record_usage_heartbeat(
    active: bool = True, scope_id: str | None = None,
) -> dict[str, Any]:
    """Count visible-client time without treating background service uptime as use."""
    global _LAST_USAGE_HEARTBEAT, _LAST_USAGE_SCOPE_ID
    scope_id = str(scope_id or "").strip() or None
    with _LOCK, storage_lock(GLOBAL_STATISTICS_PATH):
        payload = _load_usage()
        if not active:
            _LAST_USAGE_HEARTBEAT = None
            _LAST_USAGE_SCOPE_ID = None
            return _public_usage(payload)

        current_monotonic = monotonic()
        current_time = _now()
        if _LAST_USAGE_HEARTBEAT is not None:
            elapsed = current_monotonic - _LAST_USAGE_HEARTBEAT
            if 0 < elapsed <= MAX_HEARTBEAT_GAP_SECONDS:
                payload["total_seconds"] += int(round(elapsed))
                if scope_id and scope_id == _LAST_USAGE_SCOPE_ID:
                    scoped = payload["scope_usage"].setdefault(
                        scope_id, _empty_usage_entry(),
                    )
                    scoped["total_seconds"] += int(round(elapsed))
        _LAST_USAGE_HEARTBEAT = current_monotonic
        _LAST_USAGE_SCOPE_ID = scope_id

        _touch_usage_entry(payload, current_time)
        if scope_id:
            _touch_usage_entry(
                payload["scope_usage"].setdefault(scope_id, _empty_usage_entry()),
                current_time,
            )
        GLOBAL_STATISTICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(GLOBAL_STATISTICS_PATH, payload, indent=2)
        return _public_usage(payload)


def usage_statistics(scope_id: str | None = None) -> dict[str, Any]:
    with _LOCK, storage_lock(GLOBAL_STATISTICS_PATH):
        payload = _load_usage()
        if scope_id:
            return _public_usage(
                payload["scope_usage"].get(str(scope_id), _empty_usage_entry()),
            )
        return _public_usage(payload)


def _analytics_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "bet_id": str(record.get("bet_id") or ""),
        "date": str(record.get("game_date") or record.get("date") or record.get("placed_at") or "")[:10],
        "settled_at": str(record.get("settled_at") or ""),
        "stake_minor": to_minor(record.get("stake") or 0),
        "payout_minor": to_minor(record.get("payout") or 0),
        "status": str(record.get("status") or "void"),
    }


def _scope_analytics(scope_id: str) -> list[dict[str, Any]]:
    root = save_data_root(scope_id)
    archived_payload = load_document(
        "bet_analytics", {}, scope_id,
        legacy_path=root / "bets" / "fm26_bet_analytics.json",
    )
    archived = (
        archived_payload.get("records", [])
        if isinstance(archived_payload, dict) else []
    )
    bets = load_document(
        "bets", [], scope_id,
        legacy_path=root / "bets" / "fm26_bets.jsonl",
    )
    current = [
        record for record in (bets if isinstance(bets, list) else [])
        if isinstance(record, dict)
        and record.get("status") != "pending"
        and not record.get("demo_preview")
    ]
    unique: dict[str, dict[str, Any]] = {}
    anonymous = 0
    for raw in [*archived, *current]:
        if not isinstance(raw, dict):
            continue
        record = _analytics_record(raw)
        bet_id = record["bet_id"]
        if not bet_id:
            anonymous += 1
            bet_id = f"legacy-{anonymous}"
        unique[bet_id] = record
    return list(unique.values())


def _career_statistics(
    scopes: list[dict[str, Any]], usage: dict[str, Any], *, scope_kind: str,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    skipped_scopes = 0
    betting_scopes = 0
    for account_scope in scopes:
        scope_id = str(account_scope.get("scope_id") or "")
        if not scope_id:
            continue
        try:
            rows = _scope_analytics(scope_id)
        except (OSError, RuntimeError, TypeError, ValueError):
            skipped_scopes += 1
            continue
        if rows:
            betting_scopes += 1
        for index, record in enumerate(rows):
            records.append({**record, "scope_id": scope_id, "scope_order": index})

    records.sort(key=lambda record: (
        str(record.get("settled_at") or record.get("date") or ""),
        str(record.get("scope_id") or ""),
        int(record.get("scope_order") or 0),
    ))
    profits_minor = [
        int(record["payout_minor"]) - int(record["stake_minor"])
        for record in records
    ]
    wins = sum(value > 0 for value in profits_minor)
    losses = sum(value < 0 for value in profits_minor)
    pushes = len(profits_minor) - wins - losses
    longest_win = longest_loss = current_streak = 0
    current_direction = 0
    for value in profits_minor:
        direction = 1 if value > 0 else -1 if value < 0 else 0
        if direction == 0:
            continue
        current_streak = current_streak + 1 if direction == current_direction else 1
        current_direction = direction
        if direction > 0:
            longest_win = max(longest_win, current_streak)
        else:
            longest_loss = max(longest_loss, current_streak)

    total_stake_minor = sum(int(record["stake_minor"]) for record in records)
    total_payout_minor = sum(int(record["payout_minor"]) for record in records)
    net_profit_minor = total_payout_minor - total_stake_minor
    decisive = wins + losses
    return {
        **usage,
        "scope": scope_kind,
        "available": bool(scopes) if scope_kind == "current" else True,
        "save_count": len(scopes),
        "betting_save_count": betting_scopes,
        "skipped_save_count": skipped_scopes,
        "settled_bets": len(records),
        "total_stake": from_minor(total_stake_minor),
        "total_payout": from_minor(total_payout_minor),
        "net_profit": from_minor(net_profit_minor),
        "roi": round(net_profit_minor / total_stake_minor * 100, 2) if total_stake_minor else 0.0,
        "win_rate": round(wins / decisive * 100, 2) if decisive else 0.0,
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "maximum_profit": from_minor(max([0] + profits_minor)),
        "maximum_loss": from_minor(min([0] + profits_minor)),
        "current_streak": current_streak * current_direction,
        "longest_win_streak": longest_win,
        "longest_loss_streak": longest_loss,
    }


def global_statistics() -> dict[str, Any]:
    """Build a read-only cross-save betting projection plus global usage."""
    return _career_statistics(
        saved_account_scopes(), usage_statistics(), scope_kind="global",
    )


def save_statistics(scope_id: str | None) -> dict[str, Any]:
    """Build the same projection for the active save/account scope only."""
    resolved = str(scope_id or "").strip()
    scopes = [{"scope_id": resolved}] if resolved else []
    return _career_statistics(
        scopes,
        usage_statistics(resolved) if resolved else _public_usage(_empty_usage_entry()),
        scope_kind="current",
    )
