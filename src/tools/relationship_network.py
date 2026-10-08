"""Scoped and paged relationship-network projections for FModd 2.3.3."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Iterable

from tools.club_reader import resolve_public_person_address
from tools.person_relationships import (
    read_person_public_profile,
    read_person_relationships,
    read_relationship_pair,
)

DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 60
MAX_ENTITY_BATCH = 256


def _team(profile: dict[str, Any]) -> dict[str, Any]:
    return dict(profile.get("team") or {})


def relationship_scopes(
    profiles: Iterable[dict[str, Any]], acquired_clubs: Iterable[dict[str, Any]],
    *, data_version: int = 0,
) -> dict[str, Any]:
    """Return scope metadata without reading any relationship vector."""
    managed: list[dict[str, Any]] = []
    for profile in profiles:
        team = _team(profile)
        team_id = int(team.get("id") or 0)
        if not team_id:
            continue
        squads = []
        for squad in profile.get("squads") or []:
            squads.append({
                "team_id": int(squad.get("team_id") or team_id),
                "label": str(squad.get("label") or squad.get("type") or "球队"),
                "person_count": int(squad.get("player_count") or len(squad.get("players") or [])),
            })
        managed.append({
            "scope_kind": "managed",
            "team_id": team_id,
            "team_name": str(team.get("name") or team_id),
            "team_type": str(team.get("team_type") or "club"),
            "player_count": len(profile.get("players") or []),
            "staff_count": len(profile.get("staff") or []),
            "squads": squads,
        })
    acquired = [{
        "scope_kind": "acquired",
        "team_id": int(row.get("team_id") or row.get("id") or 0),
        "team_name": str(row.get("team_name") or row.get("name") or "已收购俱乐部"),
        "available": bool(row.get("address") or row.get("available", True)),
        "cached": bool(row.get("cached") or row.get("detail_cached")),
    } for row in acquired_clubs if int(row.get("team_id") or row.get("id") or 0) > 0]
    return {"data_version": int(data_version), "managed": managed, "acquired": acquired}


def select_relationship_rows(
    profile: dict[str, Any], payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select exactly one team/squad and return rows plus source metadata."""
    team = _team(profile)
    team_id = int(payload.get("team_id") or team.get("id") or 0)
    if team_id != int(team.get("id") or 0):
        raise ValueError("关系范围与当前球队不一致")
    scope_kind = str(payload.get("scope_kind") or "managed")
    if scope_kind not in {"managed", "youth", "acquired"}:
        raise ValueError("关系范围类型无效")
    person_kind = str(payload.get("person_kind") or "player")
    if person_kind not in {"player", "staff"}:
        raise ValueError("人物类型无效")
    squad_team_id = int(payload.get("squad_team_id") or 0)
    squad_label = ""
    if person_kind == "staff":
        rows = list(profile.get("staff") or [])
    elif squad_team_id:
        squad = next((row for row in profile.get("squads") or []
                      if int(row.get("team_id") or 0) == squad_team_id), None)
        if squad is None:
            raise ValueError("未找到所选青年或预备队")
        rows = list(squad.get("players") or [])
        squad_label = str(squad.get("label") or squad.get("type") or "球队")
    else:
        rows = list(profile.get("players") or [])
    source = {
        "scope_kind": scope_kind if not squad_team_id else "managed_youth",
        "club_id": team_id,
        "club_name": str(team.get("name") or team_id),
        "team_type": str(team.get("team_type") or "club"),
        "squad_team_id": squad_team_id or None,
        "squad_label": squad_label or None,
    }
    return rows, source


def page_relationship_people(
    profile: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    rows, source = select_relationship_rows(profile, payload)
    person_kind = str(payload.get("person_kind") or "player")
    # Keep a lightweight scope roster so the UI can distinguish a current
    # teammate/staff member from an external former colleague across pages.
    scope_people = [{
        "id": int(row.get("id") or 0),
        "name": str(row.get("name") or ""),
        "address": row.get("address"),
        "person_kind": person_kind,
        "role": row.get("role"),
        "job_type": row.get("job_type"),
        "job_type_name": row.get("job_type_name"),
        "positions": deepcopy(row.get("positions") or []),
        "primary_positions": deepcopy(row.get("primary_positions") or []),
        "position_ratings": deepcopy(row.get("position_ratings") or {}),
        "club": source["club_name"],
        "team_name": source["club_name"],
        "team_id": source["club_id"],
        "team_type": source["team_type"],
        "squad_label": source.get("squad_label"),
        "ca": row.get("ca"),
        "pa": row.get("pa"),
    } for row in rows]
    search = str(payload.get("search") or "").strip().casefold()
    if search:
        rows = [row for row in rows if search in str(row.get("name") or "").casefold()]
    total = len(rows)
    show_all = payload.get("all") is True
    page_size = max(1, total) if show_all else max(
        1, min(MAX_PAGE_SIZE, int(payload.get("page_size") or DEFAULT_PAGE_SIZE))
    )
    pages = 1 if show_all else max(1, (total + page_size - 1) // page_size)
    page = 1 if show_all else max(1, min(pages, int(payload.get("page") or 1)))
    start = (page - 1) * page_size
    people = []
    for row in rows[start:start + page_size]:
        item = deepcopy(row)
        item["person_kind"] = person_kind
        item["person_key"] = None
        item["source"] = dict(source)
        item["club"] = item.get("club") or source["club_name"]
        item["team_name"] = item.get("team_name") or source["club_name"]
        item["team_id"] = item.get("team_id") or source["club_id"]
        item["team_type"] = item.get("team_type") or source["team_type"]
        item["squad_label"] = item.get("squad_label") or source.get("squad_label")
        people.append(item)
    return {
        "people": people, "page": page, "page_size": page_size,
        "pages": pages, "total": total, "source": source,
        "scope_people": scope_people,
    }


def enrich_relationship_page(reader: Any, page: dict[str, Any]) -> dict[str, Any]:
    """Read relationship vectors only for the selected page."""
    output = deepcopy(page)
    for person in output.get("scope_people") or []:
        try:
            person["person_key"] = hex(resolve_public_person_address(
                reader, person, person["person_kind"],
            ))
        except (OSError, RuntimeError, TypeError, ValueError):
            person["person_key"] = None
    for person in output.get("people") or []:
        try:
            address = resolve_public_person_address(reader, person, person["person_kind"])
            person["person_key"] = hex(address)
            block = read_person_relationships(reader, address)
            person["person_relations"] = dict(block.get("person_relations") or {})
            person["person_relations_detail"] = dict(block.get("person_relations_detail") or {})
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            person["relationship_error"] = str(error)
            person["person_relations"] = {}
            person["person_relations_detail"] = {}
    return output


def relationship_pair(reader: Any, person_a: Any, person_b: Any) -> dict[str, Any]:
    a = int(str(person_a), 0)
    b = int(str(person_b), 0)
    if a <= 0 or b <= 0 or a == b:
        raise ValueError("关系人物地址无效")
    return {"person_a": hex(a), "person_b": hex(b), **read_relationship_pair(reader, a, b)}


def resolve_relationship_entities(
    reader: Any, keys: Iterable[Any], *, include_relationships: bool = False,
) -> dict[str, Any]:
    unique: list[int] = []
    for value in keys:
        address = int(str(value), 0)
        if address > 0 and address not in unique:
            unique.append(address)
        if len(unique) > MAX_ENTITY_BATCH:
            raise ValueError("一次最多解析 256 个人物")
    entities = {}
    for address in unique:
        profile = read_person_public_profile(reader, address)
        if profile:
            if include_relationships:
                block = read_person_relationships(reader, address)
                profile = {
                    **profile,
                    "person_relations": dict(block.get("person_relations") or {}),
                    "person_relations_detail": dict(
                        block.get("person_relations_detail") or {}
                    ),
                }
            entities[hex(address)] = profile
    return {"entities": entities, "requested": len(unique), "resolved": len(entities)}
