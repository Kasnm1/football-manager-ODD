"""Career-wide transfer history merged from durable facts and native archives."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
import re
from typing import Any

from tools.account_store import load_document
from tools.club_legacy import public_club_legacy, public_club_legacy_index
from tools.future_transfers import read_historical_transfer_archive
from tools.game_session import borrow_game_reader


SCHEMA_VERSION = 2


def _manager_team_ids(scope_id: str, manager_id: int) -> set[int]:
    """Return clubs already attributed to this player-manager account."""
    payload = load_document(
        "manager_records", {"schema_version": 1, "records": {}}, scope_id,
    )
    records = payload.get("records") if isinstance(payload, dict) else {}
    if not isinstance(records, dict):
        return set()
    result = set()
    for row in records.values():
        if not isinstance(row, dict):
            continue
        try:
            row_manager_id = int(row.get("manager_id") or 0)
            team_id = int(row.get("team_id") or 0)
        except (TypeError, ValueError):
            continue
        if team_id > 0 and (manager_id <= 0 or row_manager_id == manager_id):
            result.add(team_id)
    return result


def _event(row: dict[str, Any], event_type: str) -> dict[str, Any] | None:
    candidates = [
        item for item in (row.get("events") or [])
        if isinstance(item, dict) and str(item.get("type") or "") == event_type
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: (
        str(item.get("game_date") or ""), str(item.get("recorded_at") or ""),
    ))


def _amount(raw: Any) -> int | None:
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _fee_status(value: int | None) -> str:
    if value is None:
        return "unknown"
    return "free" if value == 0 else "known"


def _event_year(value: Any) -> str | None:
    match = re.match(r"^(\d{4})", str(value or "").strip())
    return match.group(1) if match else None


def _transfer_event(row: dict[str, Any], direction: str) -> dict[str, Any]:
    incoming = direction == "in"
    event_date = row.get("joined_on") if incoming else row.get("left_on")
    fee = row.get("purchase_fee") if incoming else row.get("sale_fee")
    date_source = row.get("joined_source") if incoming else row.get("left_source")
    return {
        "id": f'{row["id"]}:{direction}',
        "direction": direction,
        "direction_label": "转入" if incoming else "转出",
        "player_id": row["player_id"],
        "player_name": row["player_name"],
        "team_id": row["team_id"],
        "team_name": row["team_name"],
        "counterparty_name": (
            row.get("previous_club_name") if incoming
            else row.get("next_club_name")
        ),
        "date": event_date,
        "year": _event_year(event_date),
        "date_source": date_source,
        "fee": fee,
        "fee_status": _fee_status(fee),
        "fee_source": "manual_record" if fee is not None else "unknown",
        "status": row["status"],
        "status_label": row["status_label"],
        "active": row["active"],
        "source": row["source"],
    }


def read_native_transfer_history(
    team_ids: Any, *, save_identity: str = "",
) -> dict[str, Any]:
    """Read the build-gated native archive in the shared game-reader session."""
    with borrow_game_reader() as reader:
        return read_historical_transfer_archive(
            reader, team_ids, save_identity=save_identity,
        )


def _transfer_key(row: dict[str, Any]) -> tuple[str, int, int]:
    return (
        str(row.get("direction") or ""),
        int(row.get("team_id") or 0),
        int(row.get("player_id") or 0),
    )


def _is_native_archive_row(row: dict[str, Any]) -> bool:
    return str(row.get("source") or "").endswith("_native_transfer_archive")


def _iso_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def merge_native_transfer_history(
    payload: dict[str, Any], native: dict[str, Any],
) -> dict[str, Any]:
    """Merge validated FM archive rows without discarding manual fee overrides."""
    result = deepcopy(payload)
    native_rows = [
        deepcopy(row) for row in (native.get("transfers") or [])
        if isinstance(row, dict)
        and int(row.get("team_id") or 0) > 0
        and int(row.get("player_id") or 0) > 0
        and str(row.get("direction") or "") in {"in", "out"}
    ]
    native_by_key: dict[tuple[str, int, int], list[int]] = {}
    for index, row in enumerate(native_rows):
        native_by_key.setdefault(_transfer_key(row), []).append(index)

    matched_native: set[int] = set()
    merged_rows: list[dict[str, Any]] = []
    for durable in result.get("transfers") or []:
        if not isinstance(durable, dict):
            continue
        indexes = [
            index for index in native_by_key.get(_transfer_key(durable), [])
            if index not in matched_native
        ]
        chosen: int | None = None
        if indexes:
            exact = [
                index for index in indexes
                if str(native_rows[index].get("date") or "")
                == str(durable.get("date") or "")
            ]
            if len(exact) == 1:
                chosen = exact[0]
            elif len(indexes) == 1:
                chosen = indexes[0]
            else:
                durable_date = _iso_date(durable.get("date"))
                dated = [
                    (abs((_iso_date(native_rows[index].get("date")) - durable_date).days), index)
                    for index in indexes
                    if durable_date is not None and _iso_date(native_rows[index].get("date")) is not None
                ]
                if dated:
                    dated.sort()
                    if len(dated) == 1 or dated[0][0] < dated[1][0]:
                        chosen = dated[0][1]
        if chosen is None:
            merged_rows.append(deepcopy(durable))
            continue
        matched_native.add(chosen)
        native_row = deepcopy(native_rows[chosen])
        manual_fee = _amount(durable.get("fee"))
        merged = {**deepcopy(durable), **native_row}
        if manual_fee is not None:
            merged["fee"] = manual_fee
            merged["fee_status"] = _fee_status(manual_fee)
            merged["fee_source"] = "manual_record"
        durable_name = str(durable.get("player_name") or "").strip()
        if durable_name:
            merged["player_name"] = durable_name
        merged_rows.append(merged)
    merged_rows.extend(
        row for index, row in enumerate(native_rows) if index not in matched_native
    )

    unique_rows: dict[str, dict[str, Any]] = {}
    for row in merged_rows:
        row_id = str(row.get("id") or "")
        key = row_id or "|".join(map(str, (
            row.get("direction"), row.get("team_id"), row.get("player_id"),
            row.get("date"), row.get("counterparty_id"),
        )))
        unique_rows.setdefault(key, row)
    transfers = list(unique_rows.values())
    transfers.sort(key=lambda row: (
        str(row.get("date") or ""), str(row.get("player_name") or "").casefold(),
        str(row.get("id") or ""),
    ), reverse=True)
    result["transfers"] = transfers
    result["years"] = sorted({
        str(row.get("year") or "") for row in transfers if row.get("year")
    }, reverse=True)

    players = [
        deepcopy(row) for row in (result.get("players") or [])
        if isinstance(row, dict)
    ]
    player_rows = {
        (int(row.get("team_id") or 0), int(row.get("player_id") or 0)): row
        for row in players
    }
    for transfer in transfers:
        pair = (
            int(transfer.get("team_id") or 0),
            int(transfer.get("player_id") or 0),
        )
        if pair[0] <= 0 or pair[1] <= 0:
            continue
        player = player_rows.get(pair)
        if player is None:
            player = {
                "id": f"{pair[0]}:{pair[1]}",
                "player_id": pair[1],
                "player_name": str(transfer.get("player_name") or pair[1]),
                "team_id": pair[0],
                "team_name": str(transfer.get("team_name") or pair[0]),
                "status": "history", "status_label": "历史成员",
                "active": False,
                "source": str(transfer.get("source") or "native_transfer_archive"),
            }
            players.append(player)
            player_rows[pair] = player
        if not _is_native_archive_row(transfer):
            continue
        if transfer.get("direction") == "in":
            player["joined_on"] = transfer.get("date")
            player["joined_source"] = transfer.get("date_source")
            player["previous_club_name"] = transfer.get("counterparty_name")
            if player.get("purchase_fee") is None:
                player["purchase_fee"] = transfer.get("fee")
                player["purchase_fee_status"] = transfer.get("fee_status")
        else:
            player["left_on"] = transfer.get("date")
            player["left_source"] = transfer.get("date_source")
            player["next_club_name"] = transfer.get("counterparty_name")
            player["active"] = False
            if player.get("sale_fee") is None:
                player["sale_fee"] = transfer.get("fee")
                player["sale_fee_status"] = transfer.get("fee_status")
    result["players"] = players

    complete = bool(native.get("native_history_complete"))
    clubs_by_id = {
        int(row.get("id") or 0): deepcopy(row)
        for row in (result.get("clubs") or []) if isinstance(row, dict)
    }
    for native_club in native.get("clubs") or []:
        if not isinstance(native_club, dict):
            continue
        team_id = int(native_club.get("id") or 0)
        if team_id <= 0:
            continue
        club = clubs_by_id.setdefault(team_id, {
            "id": team_id, "name": str(native_club.get("name") or team_id),
        })
        club["native_history_complete"] = complete
        club["players"] = len({
            int(row.get("player_id") or 0) for row in players
            if int(row.get("team_id") or 0) == team_id
        })
    result["clubs"] = sorted(
        clubs_by_id.values(),
        key=lambda row: (
            str(row.get("last_synced_game_date") or ""),
            str(row.get("name") or "").casefold(),
        ), reverse=True,
    )

    incoming = [row for row in transfers if row.get("direction") == "in"]
    outgoing = [row for row in transfers if row.get("direction") == "out"]
    known_incoming = [int(row["fee"]) for row in incoming if row.get("fee") is not None]
    known_outgoing = [int(row["fee"]) for row in outgoing if row.get("fee") is not None]
    summary = dict(result.get("summary") or {})
    summary.update({
        "clubs": len(result["clubs"]),
        "players": len(players),
        "incoming": len(incoming),
        "outgoing": len(outgoing),
        "active": sum(bool(row.get("active")) for row in players),
        "historical": sum(not bool(row.get("active")) for row in players),
        "known_purchase_total": sum(known_incoming),
        "known_sale_total": sum(known_outgoing),
        "known_purchase_count": len(known_incoming),
        "known_sale_count": len(known_outgoing),
    })
    result["summary"] = summary
    coverage = dict(result.get("coverage") or {})
    coverage.update({
        "native_history_complete": complete,
        "level": "native_history" if complete else (
            "native_history_partial" if native_rows else coverage.get("level", "fmodd_tracking")
        ),
    })
    result["coverage"] = coverage
    return result


def _player_row(
    club: dict[str, Any], row: dict[str, Any], coverage: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        player_id = int(row.get("id") or 0)
        team_id = int(club.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if player_id <= 0 or team_id <= 0:
        return None

    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    contract = (
        snapshot.get("player_contract")
        if isinstance(snapshot.get("player_contract"), dict) else {}
    )
    record = (
        row.get("career_record")
        if isinstance(row.get("career_record"), dict) else {}
    )
    joined_event = _event(row, "joined_current_club")
    left_event = _event(row, "left_current_roster")
    joined_on = str(
        (joined_event or {}).get("game_date")
        or contract.get("joined_club_date")
        or contract.get("start_date")
        or row.get("first_seen_game_date")
        or ""
    )
    left_on = str(
        (left_event or {}).get("game_date")
        or row.get("left_current_roster_date")
        or ""
    )
    purchase_fee = _amount(record.get("transfer_in_fee"))
    sale_fee = _amount(record.get("transfer_out_fee"))
    status = str(row.get("status") or "unknown")
    active = status in {"current", "loaned"}
    return {
        "id": f"{team_id}:{player_id}",
        "player_id": player_id,
        "player_name": str(snapshot.get("name") or f"球员 {player_id}"),
        "team_id": team_id,
        "team_name": str(club.get("name") or team_id),
        "previous_club_id": int(snapshot.get("previous_club_id") or 0) or None,
        "previous_club_name": str(snapshot.get("previous_club_name") or "").strip() or None,
        "status": status,
        "status_label": str(row.get("status_label") or "历史记录"),
        "active": active,
        "joined_on": joined_on or None,
        "joined_source": (
            "native_joined_club_date" if joined_event or contract.get("joined_club_date")
            else "first_observed"
        ),
        "left_on": None if active else (left_on or None),
        "left_source": "roster_observation" if left_on else "unknown",
        "purchase_fee": purchase_fee,
        "purchase_fee_status": _fee_status(purchase_fee),
        "sale_fee": sale_fee,
        "sale_fee_status": _fee_status(sale_fee),
        "known_profit": (
            sale_fee - purchase_fee
            if purchase_fee is not None and sale_fee is not None else None
        ),
        "first_observed_on": row.get("first_seen_game_date"),
        "last_observed_on": row.get("last_seen_game_date"),
        "tracking_started": coverage.get("tracking_started"),
        "native_history_complete": bool(coverage.get("native_history_complete")),
        "source": "club_legacy_tracking",
    }


def _fill_transfer_counterparties(
    rows: list[dict[str, Any]], watched_players: dict[int, dict[str, Any]],
) -> None:
    by_player: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_player.setdefault(int(row.get("player_id") or 0), []).append(row)

    for player_rows in by_player.values():
        for row in player_rows:
            team_id = int(row.get("team_id") or 0)
            joined_on = str(row.get("joined_on") or "")
            left_on = str(row.get("left_on") or "")
            if not row.get("previous_club_name") and joined_on:
                previous = sorted((
                    candidate for candidate in player_rows
                    if int(candidate.get("team_id") or 0) != team_id
                    and candidate.get("left_on")
                    and str(candidate["left_on"]) <= joined_on
                ), key=lambda candidate: str(candidate.get("left_on") or ""), reverse=True)
                if previous:
                    row["previous_club_name"] = previous[0].get("team_name")

            next_club = sorted((
                candidate for candidate in player_rows
                if int(candidate.get("team_id") or 0) != team_id
                and candidate.get("joined_on")
                and (not left_on or str(candidate["joined_on"]) >= left_on)
            ), key=lambda candidate: str(candidate.get("joined_on") or ""))
            if next_club:
                row["next_club_name"] = next_club[0].get("team_name")
                continue

            watched = watched_players.get(int(row.get("player_id") or 0), {})
            watched_date = str(watched.get("observed_game_date") or "")
            watched_team_id = int(watched.get("observed_team_id") or 0)
            watched_team_name = str(watched.get("observed_team_name") or "").strip()
            if (
                watched_team_name and watched_team_id != team_id
                and (not left_on or not watched_date or watched_date >= left_on)
            ):
                row["next_club_name"] = watched_team_name


def public_transfer_history(
    career_id: str, scope_id: str, *, manager_id: int = 0,
    current_team_id: int = 0,
) -> dict[str, Any]:
    """Return the durable fallback for all clubs managed by one player."""
    career = str(career_id or "").strip()
    scope = str(scope_id or "").strip()
    if not career or not scope:
        raise ValueError("尚未识别当前存档和经理账户")

    managed_ids = _manager_team_ids(scope, int(manager_id or 0))
    if int(current_team_id or 0) > 0:
        managed_ids.add(int(current_team_id))
    index = public_club_legacy_index(career, scope)
    available_clubs = [
        row for row in (index.get("clubs") or [])
        if isinstance(row, dict) and int(row.get("id") or 0) > 0
    ]
    # Older accounts may predate manager-record persistence. Preserve their
    # already archived clubs instead of hiding durable history after upgrade.
    selected_clubs = [
        row for row in available_clubs
        if not managed_ids or int(row.get("id") or 0) in managed_ids
    ]

    rows: list[dict[str, Any]] = []
    clubs: list[dict[str, Any]] = []
    for club_meta in selected_clubs:
        team_id = int(club_meta.get("id") or 0)
        payload = public_club_legacy(career, scope, team_id)
        club = payload.get("club") if isinstance(payload.get("club"), dict) else {}
        coverage = (
            payload.get("coverage")
            if isinstance(payload.get("coverage"), dict) else {}
        )
        club_rows = []
        for player in payload.get("players") or []:
            if not isinstance(player, dict):
                continue
            projected = _player_row(club, player, coverage)
            if projected:
                club_rows.append(projected)
                rows.append(projected)
        clubs.append({
            "id": team_id,
            "name": str(club.get("name") or club_meta.get("name") or team_id),
            "players": len(club_rows),
            "tracking_started": coverage.get("tracking_started"),
            "last_synced_game_date": coverage.get("last_synced_game_date"),
            "native_history_complete": bool(coverage.get("native_history_complete")),
        })

    watched_players: dict[int, dict[str, Any]] = {}
    if any(int(row.get("id") or 0) == -1 for row in (index.get("clubs") or [])):
        watched_payload = public_club_legacy(career, scope, -1)
        for watched_row in watched_payload.get("players") or []:
            if not isinstance(watched_row, dict):
                continue
            snapshot = watched_row.get("snapshot")
            if not isinstance(snapshot, dict):
                continue
            player_id = int(watched_row.get("id") or 0)
            if player_id > 0:
                watched_players[player_id] = snapshot

    _fill_transfer_counterparties(rows, watched_players)

    rows.sort(key=lambda row: (
        str(row.get("joined_on") or ""), str(row.get("player_name") or "").casefold(),
    ), reverse=True)
    transfers = []
    for row in rows:
        transfers.append(_transfer_event(row, "in"))
        if (
            not bool(row.get("active")) or row.get("left_on") is not None
            or row.get("sale_fee") is not None
        ):
            transfers.append(_transfer_event(row, "out"))
    transfers.sort(key=lambda row: (
        str(row.get("date") or ""), str(row.get("player_name") or "").casefold(),
    ), reverse=True)
    clubs.sort(key=lambda row: (
        str(row.get("last_synced_game_date") or ""), str(row.get("name") or "").casefold(),
    ), reverse=True)
    purchase_known = [
        int(row["purchase_fee"]) for row in rows if row.get("purchase_fee") is not None
    ]
    sale_known = [
        int(row["sale_fee"]) for row in rows if row.get("sale_fee") is not None
    ]
    complete = bool(clubs) and all(row["native_history_complete"] for row in clubs)
    tracking_dates = sorted(
        str(row.get("tracking_started") or "") for row in clubs
        if row.get("tracking_started")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "manager_id": int(manager_id or 0) or None,
        "current_team_id": int(current_team_id or 0) or None,
        "clubs": clubs,
        "players": rows,
        "transfers": transfers,
        "years": sorted({
            str(row["year"]) for row in transfers if row.get("year")
        }, reverse=True),
        "summary": {
            "clubs": len(clubs),
            "players": len(rows),
            "incoming": sum(row.get("direction") == "in" for row in transfers),
            "outgoing": sum(row.get("direction") == "out" for row in transfers),
            "active": sum(bool(row.get("active")) for row in rows),
            "historical": sum(not bool(row.get("active")) for row in rows),
            "known_purchase_total": sum(purchase_known),
            "known_sale_total": sum(sale_known),
            "known_purchase_count": len(purchase_known),
            "known_sale_count": len(sale_known),
        },
        "coverage": {
            "native_history_complete": complete,
            "tracking_started": tracking_dates[0] if tracking_dates else None,
            "manager_scope_inferred": not bool(_manager_team_ids(scope, int(manager_id or 0))),
            "level": "native_history" if complete else "fmodd_tracking",
        },
    }


__all__ = [
    "merge_native_transfer_history", "public_transfer_history",
    "read_native_transfer_history",
]
