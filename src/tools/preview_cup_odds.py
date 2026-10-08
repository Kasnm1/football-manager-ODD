from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
from decimal import Decimal, ROUND_HALF_UP
import re
import struct
import sys
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from time import monotonic, perf_counter
from types import SimpleNamespace
from typing import Any, Callable

from tools.app_paths import (
    DATA_ROOT, cache_data_root, confirmed_save_name,
    remember_save_name, resolve_save_identity, save_data_root, save_identity_matches,
    selected_manager_id as stored_selected_manager_id, set_active_save_id,
)

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.initial_data_audit import (
    ENTITY_UID,
    NATION_NAMES,
    PERSON_COMMON_NAME,
    PERSON_FIRST_NAME,
    PERSON_FULL_NAME,
    PERSON_LAST_NAME,
    Reader,
    decode_date,
    decode_kickoff_minutes,
    deduplicate_results,
    parse_completed_result,
    parse_completed_result_from_raw,
    parse_goal_events,
    parse_result_event_summary,
    parse_season_completed_result,
    parse_fixture,
    parse_fixture_from_raw,
    read_fixture_pool_addresses,
    read_fixture_snapshots_at_addresses,
    read_result_fingerprints_at_addresses,
    scan_fixture_and_result_addresses,
    scan_fixture_addresses,
    scan_result_fingerprints_in_spans,
    scan_result_addresses,
    scan_season_result_addresses,
    select_process_layout,
    TEAM_MANAGER,
)
from tools.game_session import borrow_game_reader
from tools.odds_profiles import (
    STAR_PLAYER_COUNT,
    OTHER_STARTER_COUNT,
    BENCH_PLAYER_COUNT,
    PRICED_PLAYER_COUNT,
    STAR_CA_WEIGHT,
    OTHER_STARTER_CA_WEIGHT,
    BENCH_CA_WEIGHT,
    RAW_CA_WEIGHT,
    CONDITIONED_CA_WEIGHT,
    RECENT_MATCH_LIMIT,
    RECENT_MATCH_WEIGHTS,
    _POSITION_ROLE_NAMES,
    _BALANCED_STARTING_SLOTS,
    fitness_factor,
    sharpness_factor,
    nonlinear_ca,
    _position_role_rating,
    balanced_starting_eleven,
    tiered_squad_average,
    build_team_profile,
)
from tools.database_index import database_index_for_reader
from tools.storage_io import atomic_write_json
from tools.league_standings import build_standings
from tools.native_layout_core import decode_vector_header
from tools.odds_math_core import (
    asian_weights,
    poisson,
    score_matrix,
    team_total_weights,
    total_weights,
)
from fm_collector.win32 import (
    MEM_PRIVATE, PAGE_READWRITE, find_module, iter_readable_regions,
    open_process, write_process_memory,
)
from tools.save_memory import (
    read_cached_save_name, read_savegame_identity, read_save_name,
)
from tools.result_evidence import (
    RESULT_DETAIL_FIELDS,
    _result_identity,
    discard_result_details,
    first_scorer_detail_consistent,
    halftime_details_consistent,
    result_details_consistent,
    result_snapshot_matches,
    sanitize_result_details,
)
from tools.result_history import (
    RESULT_KICKOFF_FIELDS,
    ResultHistoryOwner,
    legacy_result_history_path as owned_legacy_result_history_path,
    result_history_path as owned_result_history_path,
)


@contextmanager
def open_supported_reader(
    selected_process: tuple[int, str, Any] | None = None,
):
    """Open the supported FM build with its validated memory layout."""
    borrowed = (
        borrow_game_reader(selected_process)
        if selected_process is not None else borrow_game_reader()
    )
    with borrowed as reader:
        yield reader.process_path, reader.layout, reader.module, reader


def read_layout_game_date_code(reader: Reader) -> int:
    rva = reader.layout.game_date_rva
    if rva is None:
        raise RuntimeError(
            f"{reader.layout.display_name} current game-date address is not mapped yet"
        )
    return reader.u32(reader.module_base + rva) or 0


def native_competition_reputation(reader: Reader, competition_season: int) -> int | None:
    """Read the version-specific FM competition reputation, not the model fallback."""
    offset = reader.layout.competition_reputation_offset
    width = reader.layout.competition_reputation_bytes
    if offset is None or width not in {1, 2}:
        return None
    competition = reader.competition(int(competition_season or 0))
    if not competition:
        return None
    try:
        address = int(str(competition.get("address") or "0"), 16)
    except ValueError:
        return None
    value = reader.u16(address + int(offset)) if width == 2 else reader.u8(address + int(offset))
    return int(value) if value is not None and 0 <= int(value) <= 200 else None


def apply_competition_reputation_delta(
    competition_id: int, competition_season_address: Any, delta: int,
) -> dict[str, Any]:
    """Write a validated FM competition reputation and verify the result."""
    delta = int(delta)
    if not delta:
        raise ValueError("联赛声望调整值无效")
    pid, _path, layout = select_process_layout()
    offset = layout.competition_reputation_offset
    width = layout.competition_reputation_bytes
    if offset is None or width not in {1, 2}:
        raise RuntimeError("当前游戏版本尚未验证联赛声望写入")
    try:
        season_address = int(str(competition_season_address or "0"), 0)
    except (TypeError, ValueError):
        season_address = 0
    if not season_address:
        raise ValueError("联赛赛季对象地址无效，请完整刷新后重试")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        competition = reader.competition(season_address)
        if not competition or int(competition.get("id") or 0) != int(competition_id):
            raise ValueError("联赛对象与赛事 ID 不一致，请完整刷新后重试")
        try:
            competition_address = int(str(competition.get("address") or "0"), 0)
        except (TypeError, ValueError):
            competition_address = 0
        if not competition_address:
            raise RuntimeError("无法定位联赛对象")
        field_address = competition_address + int(offset)
        before = reader.u16(field_address) if width == 2 else reader.u8(field_address)
        if before is None or not 0 <= int(before) <= 200:
            raise RuntimeError("联赛声望数据无效")
        after = max(0, min(200, int(before) + delta))
        original = struct.pack("<H" if width == 2 else "<B", int(before))
        encoded = struct.pack("<H" if width == 2 else "<B", after)
        write_process_memory(process, field_address, encoded)
        verified = reader.u16(field_address) if width == 2 else reader.u8(field_address)
        if verified != after:
            write_process_memory(process, field_address, original)
            raise RuntimeError("联赛声望写入校验失败")
        return {
            "competition_id": int(competition_id),
            "competition_name": str(competition.get("name") or competition_id),
            "before": int(before), "after": after,
            "applied": after - int(before),
        }


def _native_pointer_vector(
    reader: Reader, address: int, *, maximum: int,
) -> list[int] | None:
    raw = reader.bytes(address, 0x18)
    if not raw or len(raw) != 0x18:
        return None
    header = decode_vector_header(raw, 8, maximum)
    if header is None:
        return None
    begin, _end, _capacity, count, _capacity_count = header
    values = reader.ptr_array(begin, count)
    return [int(value) for value in values if value] if all(values) else None


def _native_pointer_vector_slots(
    reader: Reader, address: int, *, maximum: int,
) -> tuple[list[int], int] | None:
    """Read a native pointer vector while preserving empty slot capacity."""
    raw = reader.bytes(address, 0x18)
    if not raw or len(raw) != 0x18:
        return None
    header = decode_vector_header(raw, 8, maximum)
    if header is None:
        return None
    begin, _end, _capacity, count, _capacity_count = header
    values = reader.ptr_array(begin, count)
    return [int(value) for value in values if value], int(count)


STAGE_TYPE_MARKERS = {
    b"uorg": "group",
    b"gael": "league",
    b" puc": "cup",
}

MAX_KNOCKOUT_STAGE_TEAM_SLOTS = 4096
MAX_KNOCKOUT_ROUND_TIES = MAX_KNOCKOUT_STAGE_TEAM_SLOTS // 2


def _native_league_standing_stats(
    reader: Reader, standing: int,
) -> dict[str, int] | None:
    primary = reader.bytes(standing + 0x08, 12)
    if not primary or len(primary) != 12:
        return None
    goals_for, goals_against, points = struct.unpack_from("<HHh", primary, 0)
    played, played_mirror, won, drawn, lost = primary[6:11]
    if (
        played != played_mirror
        or played != won + drawn + lost
        or goals_for > 1000 or goals_against > 1000
        or not -32768 <= points <= 32767
    ):
        return None
    return {
        "played": int(played),
        "won": int(won),
        "drawn": int(drawn),
        "lost": int(lost),
        "goals_for": int(goals_for),
        "goals_against": int(goals_against),
        "goal_difference": int(goals_for - goals_against),
        "points": int(points),
    }


def _native_stage_teams(
    reader: Reader, stage: int, stage_type: str,
) -> tuple[list[dict[str, Any]], int]:
    layout = reader.layout
    pointers: list[int] = []
    standing_pointers: list[int] = []
    slot_count = 0
    if layout.key == "fm26" and stage_type == "group":
        holder = reader.ptr(stage + 0x90)
        begin = reader.ptr(holder) if holder else None
        slot_count = int(reader.u32(holder + 8) or 0) if holder else 0
        if begin and 0 < slot_count <= 256:
            pointers = [
                int(reader.ptr(begin + 0x18 + index * 0x30) or 0)
                for index in range(slot_count)
            ]
    elif layout.key == "fm26" and stage_type == "league":
        vector = _native_pointer_vector_slots(reader, stage + 0xC0, maximum=256)
        standings, slot_count = vector or ([], 0)
        standing_pointers = list(standings)
        pointers = [int(reader.ptr(standing + 0x88) or 0) for standing in standings]
    elif layout.key == "fm24" and stage_type == "league":
        vector = _native_pointer_vector_slots(reader, stage + 0x98, maximum=256)
        standings, slot_count = vector or ([], 0)
        standing_pointers = list(standings)
        pointers = [int(reader.ptr(standing + 0x78) or 0) for standing in standings]
    elif stage_type == "cup":
        offset = int(layout.cup_stage_teams_offset or 0)
        holder = reader.ptr(stage + offset) if offset else None
        begin = reader.ptr(holder) if holder else None
        slot_count = int(reader.u32(holder + 8) or 0) if holder else 0
        if begin and 0 < slot_count <= 256:
            pointers = [int(reader.ptr(begin + index * 0x10) or 0) for index in range(slot_count)]
    elif layout.key == "fm24" and stage_type == "group":
        holder = reader.ptr(stage + 0x98)
        begin = reader.ptr(holder) if holder else None
        slot_count = int(reader.u32(holder + 8) or 0) if holder else 0
        if begin and 0 < slot_count <= 256:
            pointers = [int(reader.ptr(begin + index * 0x18) or 0) for index in range(slot_count)]

    teams: dict[int, dict[str, Any]] = {}
    for index, pointer in enumerate(pointers):
        team = reader.team(pointer) if pointer else None
        team_id = int((team or {}).get("id") or 0)
        if not team_id or not has_resolved_team_name(team or {}):
            continue
        canonical = canonical_team(team or {})
        row = {
            "id": team_id,
            "name": str(canonical.get("short_name") or canonical.get("name") or team_id),
            "reputation": int(canonical.get("reputation") or 0),
            "address": canonical.get("address"),
        }
        if index < len(standing_pointers):
            stats = _native_league_standing_stats(
                reader, standing_pointers[index],
            )
            if stats is not None:
                row.update({
                    **stats,
                    "position": index + 1,
                    "native_standing_verified": True,
                    "native_standing_address": hex(standing_pointers[index]),
                })
        teams[team_id] = row
    return list(teams.values()), slot_count


def _native_cup_round_counts(reader: Reader, stage: int) -> list[int]:
    offset = int(reader.layout.cup_stage_round_ties_offset or 0)
    rounds_header = reader.ptr(stage + offset) if offset else None
    rounds_vector = (
        _native_pointer_vector_slots(reader, rounds_header, maximum=64)
        if rounds_header else None
    )
    if not rounds_vector:
        return []
    counts = []
    for round_pointer in rounds_vector[0]:
        ties = _native_pointer_vector_slots(reader, round_pointer, maximum=256)
        counts.append(int(ties[1]) if ties else 0)
    return counts


def _native_cup_contender_ids(
    reader: Reader, stage: int,
) -> tuple[list[int], list[int]]:
    """Return active and explicitly eliminated teams from native RoundTie outcomes."""
    teams_offset = int(reader.layout.cup_stage_teams_offset or 0)
    teams_holder = reader.ptr(stage + teams_offset) if teams_offset else None
    teams_begin = reader.ptr(teams_holder) if teams_holder else None
    team_count = int(reader.u32(teams_holder + 8) or 0) if teams_holder else 0
    if not teams_begin or not 2 <= team_count <= 256:
        return [], []
    team_pointers = [
        int(reader.ptr(teams_begin + index * 0x10) or 0)
        for index in range(team_count)
    ]
    team_ids = []
    for pointer in team_pointers:
        team = reader.team(pointer) if pointer else None
        team_ids.append(int((team or {}).get("id") or 0))
    if any(not team_id for team_id in team_ids):
        return [], []

    rounds_offset = int(reader.layout.cup_stage_round_ties_offset or 0)
    rounds_header = reader.ptr(stage + rounds_offset) if rounds_offset else None
    rounds = (
        _native_pointer_vector_slots(reader, rounds_header, maximum=64)
        if rounds_header else None
    )
    if not rounds:
        return sorted(set(team_ids)), []
    eliminated: set[int] = set()
    for round_pointer in rounds[0]:
        ties = _native_pointer_vector_slots(reader, round_pointer, maximum=256)
        for tie in (ties or ([], 0))[0]:
            raw = reader.bytes(tie, 0x20)
            if not raw or len(raw) != 0x20:
                continue
            home_index = struct.unpack_from("<H", raw, 0)[0]
            away_index = struct.unpack_from("<H", raw, 8)[0]
            if home_index >= len(team_ids) or away_index >= len(team_ids):
                continue
            if raw[0x1B] == 10:
                eliminated.add(team_ids[home_index])
            if raw[0x1F] == 10:
                eliminated.add(team_ids[away_index])
    active = set(team_ids) - eliminated
    return sorted(active), sorted(eliminated)


def native_terminal_final_fixture(
    reader: Reader, fixtures: list[Any], stages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Use the last native cup stage/round, never the last observed match."""
    if not stages or stages[-1].get("stage_type") != "cup":
        return None
    terminal = stages[-1]
    counts = terminal.get("round_tie_counts") or []
    if not counts or int(counts[-1]) != 1:
        return None
    rows = []
    for fixture in fixtures:
        if (
            getattr(fixture, "stage_index", None) != terminal["stage_index"]
            or getattr(fixture, "group_index", None) != len(counts) - 1
        ):
            continue
        home = reader.team(fixture.home_team)
        away = reader.team(fixture.away_team)
        if not home or not away:
            continue
        rows.append((fixture, {}, home, away))
    contexts = annotate_knockout_fixture_rows(reader, rows)
    finals = []
    for fixture, _meta, home, away in rows:
        context = contexts.get(int(fixture.address)) or {}
        final = competition_final_metadata({
            "fixture_date": fixture.match_date.isoformat(),
            "home": home, "away": away,
            "kickoff_minutes": getattr(fixture, "kickoff_minutes", None),
        }, context)
        if final:
            finals.append(final)
    identities = {
        (item["date"], item["home_id"], item["away_id"], item["tie_address"])
        for item in finals
    }
    return finals[0] if len(identities) == 1 else None


def native_competition_format_snapshots(
    reader: Reader,
    rows: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]],
    season_fixture_bounds: dict[int, tuple[date, date]] | None = None,
    season_fixtures: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Describe competition formats from native stages without name matching."""
    if any(value is None for value in (
        reader.layout.competition_actual_offset,
        reader.layout.actual_competition_stages_offset,
        reader.layout.competition_stage_type_offset,
    )):
        return []
    snapshots = []
    seen: set[tuple[int, int]] = set()
    fixtures_by_season: dict[int, list[Any]] = defaultdict(list)
    for fixture in (season_fixtures if season_fixtures is not None else [row[0] for row in rows]):
        fixtures_by_season[int(fixture.competition_season)].append(fixture)
    for fixture, meta, _home, _away in rows:
        competition = reader.competition(int(fixture.competition_season or 0))
        if not competition:
            continue
        competition_id = int(competition.get("id") or 0)
        competition_address = int(str(competition.get("address") or "0"), 16)
        actual = reader.ptr(
            competition_address + int(reader.layout.competition_actual_offset or 0)
        )
        identity = (competition_id, int(actual or 0))
        if not competition_id or not actual or identity in seen:
            continue
        seen.add(identity)
        stages = _native_pointer_vector(
            reader,
            actual + int(reader.layout.actual_competition_stages_offset or 0),
            maximum=32,
        )
        if not stages:
            continue
        stage_rows = []
        for stage_index, stage in enumerate(stages):
            marker = reader.bytes(
                stage + int(reader.layout.competition_stage_type_offset or 0), 4,
            )
            stage_type = STAGE_TYPE_MARKERS.get(marker or b"", "unknown")
            teams, slot_count = _native_stage_teams(reader, stage, stage_type)
            active_team_ids, eliminated_team_ids = (
                _native_cup_contender_ids(reader, stage)
                if stage_type == "cup" else ([], [])
            )
            fixture_dates = sorted({
                item.match_date.isoformat()
                for item in fixtures_by_season[int(fixture.competition_season)]
                if int(item.stage_index if item.stage_index is not None else -1) == stage_index
            })
            stage_rows.append({
                "stage_index": stage_index,
                "stage_type": stage_type,
                "stage_marker": (marker or b"").decode("ascii", errors="replace"),
                "slot_count": slot_count,
                "teams": teams,
                "team_ids": sorted(int(team["id"]) for team in teams),
                "field_complete": bool(slot_count >= 2 and len(teams) == slot_count),
                "first_fixture_date": fixture_dates[0] if fixture_dates else None,
                "last_fixture_date": fixture_dates[-1] if fixture_dates else None,
                "round_tie_counts": (
                    _native_cup_round_counts(reader, stage) if stage_type == "cup" else []
                ),
                "active_team_ids": active_team_ids,
                "eliminated_team_ids": eliminated_team_ids,
            })
        opening_candidates = [
            item for item in stage_rows if item["stage_type"] in {"group", "league"}
        ]
        if not opening_candidates:
            opening_candidates = [item for item in stage_rows if item["stage_type"] == "cup"]
        opening_stage = opening_candidates[-1] if opening_candidates else None
        knockout_stage = next(
            (
                item for item in stage_rows
                if opening_stage and item["stage_index"] > opening_stage["stage_index"]
                and item["stage_type"] == "cup"
            ),
            opening_stage if opening_stage and opening_stage["stage_type"] == "cup" else None,
        )
        season_bounds = (season_fixture_bounds or {}).get(
            int(fixture.competition_season),
        )
        season_bounds_verified = bool(
            season_bounds
            and (season_bounds[1] - season_bounds[0]).days >= 120
        )
        terminal_final = native_terminal_final_fixture(
            reader, fixtures_by_season[int(fixture.competition_season)], stage_rows,
        )
        snapshots.append({
            "terminal_final_fixture": terminal_final,
            "competition_id": competition_id,
            "competition_name": str(meta.get("name") or competition_name(competition)),
            "competition_kind": str(meta.get("kind") or "cup"),
            "competition_season_address": hex(int(fixture.competition_season)),
            "actual_competition_address": hex(actual),
            "stage_count": len(stage_rows),
            "stages": stage_rows,
            "opening_stage_index": opening_stage.get("stage_index") if opening_stage else None,
            "opening_stage_type": opening_stage.get("stage_type") if opening_stage else None,
            "opening_slot_count": int(opening_stage.get("slot_count") or 0) if opening_stage else 0,
            "opening_field_complete": bool(opening_stage and opening_stage.get("field_complete")),
            "opening_teams": list(opening_stage.get("teams") or []) if opening_stage else [],
            "first_fixture_date": (
                season_bounds[0].isoformat()
                if season_bounds else opening_stage.get("first_fixture_date")
                if opening_stage else None
            ),
            "last_fixture_date": (
                season_bounds[1].isoformat()
                if season_bounds else opening_stage.get("last_fixture_date")
                if opening_stage else None
            ),
            "season_fixture_bounds_verified": season_bounds_verified,
            "knockout_slot_count": int(knockout_stage.get("slot_count") or 0) if knockout_stage else 0,
            "knockout_round_tie_counts": list(knockout_stage.get("round_tie_counts") or []) if knockout_stage else [],
            "knockout_active_team_ids": list(knockout_stage.get("active_team_ids") or []) if knockout_stage else [],
            "knockout_eliminated_team_ids": list(knockout_stage.get("eliminated_team_ids") or []) if knockout_stage else [],
            "has_terminal_cup_stage": bool(knockout_stage),
        })
    return sorted(snapshots, key=lambda item: int(item["competition_id"]))


def native_result_competition_format_snapshots(
    reader: Reader,
    results: list[dict[str, Any]],
    season_fixture_bounds: dict[int, tuple[date, date]] | None = None,
) -> list[dict[str, Any]]:
    """Recover league formats from live season objects carried by results.

    Completed competitions can have no open fixture inside the current odds
    window. Their verified result objects still retain the exact competition
    season address, which lets patched calendars follow the save instead of a
    country or competition-ID default.
    """
    representatives = []
    bounds = dict(season_fixture_bounds or {})
    seen_addresses: set[int] = set()
    accepted_addresses: set[int] = set()
    for result in results:
        competition = result.get("competition") or {}
        try:
            competition_id = int(competition.get("id") or 0)
            season_address = int(str(competition.get("address") or "0"), 0)
            result_date = date.fromisoformat(str(result.get("date") or ""))
        except (TypeError, ValueError):
            continue
        if not competition_id or not season_address:
            continue
        if season_address not in seen_addresses:
            seen_addresses.add(season_address)
            live_competition = reader.competition(season_address)
            if int((live_competition or {}).get("id") or 0) != competition_id:
                continue
            meta = competition_metadata(
                competition_id, competition_name(live_competition or competition),
            )
            if not meta or str(meta.get("kind") or "") != "league":
                continue
            accepted_addresses.add(season_address)
            representatives.append((
                SimpleNamespace(
                    competition_season=season_address,
                    stage_index=None,
                    match_date=result_date,
                ),
                meta,
                {},
                {},
            ))
        if season_address not in accepted_addresses:
            continue
        previous_bounds = bounds.get(season_address)
        bounds[season_address] = (
            min(previous_bounds[0], result_date) if previous_bounds else result_date,
            max(previous_bounds[1], result_date) if previous_bounds else result_date,
        )
    return native_competition_format_snapshots(reader, representatives, bounds)


def native_retained_league_format_snapshots(
    reader: Reader,
    retained_formats: list[dict[str, Any]],
    game_date: str,
) -> list[dict[str, Any]]:
    """Revalidate retained league season objects and refresh their live table.

    A completed league can leave the odds window before its final result is
    captured. The persisted format still carries the validated season and
    actual-competition addresses, so reread that exact object instead of
    leaving its native standings frozen at the earlier snapshot.
    """
    try:
        observed_date = date.fromisoformat(str(game_date))
    except ValueError:
        return []
    representatives = []
    bounds: dict[int, tuple[date, date]] = {}
    retained_by_season: dict[int, dict[str, Any]] = {}
    for item in retained_formats:
        if str(item.get("competition_kind") or "") != "league":
            continue
        try:
            competition_id = int(item.get("competition_id") or 0)
            season_address = int(
                str(item.get("competition_season_address") or "0"), 0,
            )
        except (TypeError, ValueError):
            continue
        if (
            not competition_id
            or not season_address
            or season_address in retained_by_season
        ):
            continue
        live_competition = reader.competition(season_address)
        if int((live_competition or {}).get("id") or 0) != competition_id:
            continue
        retained_by_season[season_address] = item
        representatives.append((
            SimpleNamespace(
                competition_season=season_address,
                stage_index=None,
                match_date=observed_date,
            ),
            {
                "id": competition_id,
                "name": str(
                    item.get("competition_name")
                    or competition_name(live_competition or {})
                ),
                "kind": "league",
            },
            {},
            {},
        ))
        try:
            first_fixture = date.fromisoformat(
                str(item.get("first_fixture_date") or ""),
            )
            last_fixture = date.fromisoformat(
                str(item.get("last_fixture_date") or ""),
            )
        except ValueError:
            continue
        if first_fixture <= last_fixture:
            bounds[season_address] = (first_fixture, last_fixture)

    refreshed = native_competition_format_snapshots(
        reader, representatives, bounds,
    )
    verified = []
    for candidate in refreshed:
        try:
            season_address = int(
                str(candidate.get("competition_season_address") or "0"), 0,
            )
        except (TypeError, ValueError):
            continue
        retained = retained_by_season.get(season_address)
        if not retained:
            continue
        try:
            expected_actual = int(
                str(retained.get("actual_competition_address") or "0"), 0,
            )
            observed_actual = int(
                str(candidate.get("actual_competition_address") or "0"), 0,
            )
        except (TypeError, ValueError):
            continue
        if expected_actual and expected_actual != observed_actual:
            continue
        verified.append(candidate)
    return verified


def select_current_competition_format_snapshots(
    snapshots: list[dict[str, Any]], game_date: str,
) -> list[dict[str, Any]]:
    """Choose one current or nearest upcoming season per competition."""
    try:
        current_date = date.fromisoformat(str(game_date))
    except ValueError:
        current_date = date.min

    def priority(item: dict[str, Any]) -> tuple[Any, ...]:
        try:
            first = date.fromisoformat(str(item.get("first_fixture_date") or ""))
        except ValueError:
            first = None
        try:
            last = date.fromisoformat(str(item.get("last_fixture_date") or ""))
        except ValueError:
            last = first
        complete = bool(item.get("opening_field_complete"))
        if first and last and first <= current_date <= last:
            date_rank = (0, 0)
        elif first and first > current_date:
            date_rank = (1, (first - current_date).days)
        elif last:
            date_rank = (2, (current_date - last).days)
        else:
            date_rank = (3, 0)
        return (
            *date_rank,
            0 if complete else 1,
            -int(item.get("opening_slot_count") or 0),
            str(item.get("competition_season_address") or ""),
        )

    selected: dict[int, dict[str, Any]] = {}
    for item in snapshots:
        competition_id = int(item.get("competition_id") or 0)
        if not competition_id:
            continue
        existing = selected.get(competition_id)
        if existing is None or priority(item) < priority(existing):
            selected[competition_id] = dict(item)
    return sorted(selected.values(), key=lambda item: int(item["competition_id"]))


def persistent_competition_format_snapshots(
    current: list[dict[str, Any]],
    previous_snapshot: dict[str, Any] | None,
    save_instance_id: str,
    game_date: str,
) -> list[dict[str, Any]]:
    """Retain the selected competition season formats within one save."""
    current = select_current_competition_format_snapshots(current, game_date)
    previous = []
    if (
        previous_snapshot
        and str(previous_snapshot.get("save_instance_id") or "") == str(save_instance_id or "")
    ):
        previous = list(previous_snapshot.get("competition_formats") or [])

    retained: dict[int, dict[str, Any]] = {}
    for item in previous:
        competition_id = int(item.get("competition_id") or 0)
        if competition_id:
            retained[competition_id] = dict(item)

    for item in current:
        competition_id = int(item.get("competition_id") or 0)
        if not competition_id:
            continue
        candidate = dict(item)
        old = retained.get(competition_id)
        same_season = bool(
            old
            and str(old.get("competition_season_address") or old.get("actual_competition_address") or "")
            == str(candidate.get("competition_season_address") or candidate.get("actual_competition_address") or "")
        )
        if same_season:
            candidate["format_discovered_at"] = str(
                old.get("format_discovered_at") or game_date
            )
        else:
            candidate["format_discovered_at"] = game_date
        candidate["format_last_observed_at"] = game_date
        retained[competition_id] = candidate

    return sorted(retained.values(), key=lambda item: int(item["competition_id"]))


def is_competition_final_round(
    stage_index: int, stage_count: int,
    round_index: int, round_count: int, tie_count: int,
) -> bool:
    return bool(
        stage_count > 0 and round_count > 0
        and stage_index == stage_count - 1
        and round_index == round_count - 1
        and tie_count == 1
    )


def competition_match_role(
    stage_index: int, stage_count: int,
    round_index: int, round_count: int,
    tie_count: int, final_round_tie_count: int,
) -> str:
    if is_competition_final_round(
        stage_index, stage_count, round_index, round_count, tie_count,
    ):
        return "final"
    if (
        stage_count > 0 and round_count >= 2
        and stage_index == stage_count - 1
        and round_index == round_count - 2
        and tie_count == 1 and final_round_tie_count == 1
    ):
        return "third_place"
    return "knockout"


def fixture_knockout_context(
    reader: Reader,
    fixture: Any,
    *,
    stage_cache: dict[tuple[int, int], dict[str, Any] | None] | None = None,
) -> dict[str, Any] | None:
    """Resolve a version-mapped fixture through CupStage -> RoundTie.

    The offsets are enabled only on layouts backed by the FMRTE 24.4.2
    accessors and the matching live FM24 structure sample.
    """
    layout = reader.layout
    required = (
        layout.competition_actual_offset,
        layout.actual_competition_stages_offset,
        layout.competition_stage_type_offset,
        layout.competition_stage_index_offset,
        layout.cup_stage_teams_offset,
        layout.cup_stage_round_ties_offset,
    )
    if layout.key not in {"fm24", "fm26"} or any(value is None for value in required):
        return None
    competition_season = int(fixture.competition_season or 0)
    if not competition_season or fixture.stage_index is None or fixture.group_index is None:
        return None
    stage_index = int(fixture.stage_index)
    cache_key = (competition_season, stage_index)
    cache = stage_cache if stage_cache is not None else {}
    if cache_key not in cache:
        competition = reader.competition(competition_season)
        stage_context = None
        if competition:
            competition_address = int(str(competition["address"]), 16)
            actual_competition = reader.ptr(
                competition_address + int(layout.competition_actual_offset)
            )
            stages = (
                _native_pointer_vector(
                    reader,
                    actual_competition + int(layout.actual_competition_stages_offset),
                    maximum=32,
                )
                if actual_competition else None
            )
            if stages and 0 <= stage_index < len(stages):
                stage = stages[stage_index]
                stage_type = reader.bytes(
                    stage + int(layout.competition_stage_type_offset), 4,
                )
                native_stage_index = reader.u8(
                    stage + int(layout.competition_stage_index_offset),
                )
                teams_header = (
                    reader.ptr(stage + int(layout.cup_stage_teams_offset))
                    if stage_type == b" puc" and native_stage_index == stage_index else None
                )
                teams_begin = reader.ptr(teams_header) if teams_header else None
                team_count = reader.u32(teams_header + 8) if teams_header else None
                if (
                    teams_begin and team_count is not None
                    and 2 <= team_count <= MAX_KNOCKOUT_STAGE_TEAM_SLOTS
                ):
                    team_entries = reader.bytes(
                        teams_begin, int(team_count) * 0x10,
                    )
                    if team_entries and len(team_entries) == int(team_count) * 0x10:
                        teams = [
                            struct.unpack_from("<Q", team_entries, index * 0x10)[0]
                            for index in range(team_count)
                        ]
                        rounds_header = reader.ptr(
                            stage + int(layout.cup_stage_round_ties_offset),
                        )
                        rounds = (
                            _native_pointer_vector(reader, rounds_header, maximum=64)
                            if rounds_header else None
                        )
                        if rounds:
                            final_round_ties = _native_pointer_vector(
                                reader, rounds[-1], maximum=MAX_KNOCKOUT_ROUND_TIES,
                            )
                            stage_context = {
                                "stage": stage,
                                "stage_count": len(stages),
                                "teams": teams,
                                "rounds": rounds,
                                "final_round_tie_count": len(final_round_ties or []),
                                "round_cache": {},
                            }
        cache[cache_key] = stage_context

    stage_context = cache.get(cache_key)
    if not stage_context:
        return None
    stage = int(stage_context["stage"])
    stages_count = int(stage_context["stage_count"])
    teams = list(stage_context["teams"])
    rounds = list(stage_context["rounds"])
    round_index = int(fixture.group_index)
    if not rounds or not 0 <= round_index < len(rounds):
        return None
    round_cache: dict[int, tuple[int, list[tuple[int, bytes]]] | None] = (
        stage_context["round_cache"]
    )
    if round_index not in round_cache:
        ties = _native_pointer_vector(
            reader, rounds[round_index], maximum=MAX_KNOCKOUT_ROUND_TIES,
        )
        tie_rows = []
        for tie in ties or []:
            raw = reader.bytes(tie, 0x20)
            if raw and len(raw) == 0x20:
                tie_rows.append((tie, raw))
        round_cache[round_index] = (len(ties), tie_rows) if tie_rows else None
    round_context = round_cache.get(round_index)
    if not round_context:
        return None
    round_tie_count, tie_rows = round_context
    final_round_tie_count = int(stage_context["final_round_tie_count"])
    fixture_teams = {int(fixture.home_team), int(fixture.away_team)}
    for tie, raw in tie_rows:
        home_index = struct.unpack_from("<H", raw, 0)[0]
        away_index = struct.unpack_from("<H", raw, 8)[0]
        if home_index >= len(teams) or away_index >= len(teams):
            continue
        tie_home, tie_away = int(teams[home_index]), int(teams[away_index])
        if {tie_home, tie_away} != fixture_teams:
            continue
        if fixture.home_team == tie_home and fixture.away_team == tie_away:
            orientation = "first"
        elif fixture.home_team == tie_away and fixture.away_team == tie_home:
            orientation = "second"
        else:
            continue
        home_result, away_result = raw[0x1B], raw[0x1F]
        winner_pointer = (
            tie_home if home_result in {1, 2, 3}
            else tie_away if away_result in {1, 2, 3} else None
        )
        winner_method = (
            "extra_time" if 2 in {home_result, away_result}
            else "penalties" if 3 in {home_result, away_result}
            else "regular" if winner_pointer else None
        )
        match_role = competition_match_role(
            stage_index, stages_count, round_index, len(rounds),
            round_tie_count, final_round_tie_count,
        )
        return {
            "tie_address": tie,
            "stage_address": stage,
            "stage_index": stage_index,
            "stage_count": stages_count,
            "round_index": round_index,
            "round_count": len(rounds),
            "round_tie_count": round_tie_count,
            "final_round_tie_count": final_round_tie_count,
            "competition_match_role": match_role,
            "is_competition_final": is_competition_final_round(
                stage_index, stages_count, round_index, len(rounds), round_tie_count,
            ),
            "orientation": orientation,
            "tie_home_pointer": tie_home,
            "tie_away_pointer": tie_away,
            "first_home_goals": None if raw[0x18] == 0xFF else int(raw[0x18]),
            "second_home_goals": None if raw[0x19] == 0xFF else int(raw[0x19]),
            "first_away_goals": None if raw[0x1C] == 0xFF else int(raw[0x1C]),
            "second_away_goals": None if raw[0x1D] == 0xFF else int(raw[0x1D]),
            "winner_pointer": winner_pointer,
            "decided_by": winner_method,
        }
    return None


def normalize_two_legged_fixture_result(
    reader: Reader,
    fixture: Any,
    result: dict[str, Any],
    *,
    stage_cache: dict[tuple[int, int], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Replace an FM26 aggregate-looking score with this fixture's leg score.

    A completed native ``RoundTie`` retains separate first- and second-game
    scores.  Requiring both pairs avoids guessing from competition names or
    fixture orientation while the return leg is still unplayed.
    """
    layout = getattr(reader, "layout", None)
    if getattr(layout, "result_event_record_format", None) != "fm26":
        return result
    context = fixture_knockout_context(
        reader, fixture, stage_cache=stage_cache,
    )
    if not context:
        return result
    score_fields = (
        "first_home_goals", "first_away_goals",
        "second_home_goals", "second_away_goals",
    )
    if any(context.get(field) is None for field in score_fields):
        return result
    orientation = str(context.get("orientation") or "")
    if orientation == "first":
        leg_score = (
            int(context["first_home_goals"]),
            int(context["first_away_goals"]),
        )
        leg_index = 1
    elif orientation == "second":
        # RoundTie scores stay in tie-home/tie-away order; the return fixture
        # reverses those sides.
        leg_score = (
            int(context["second_away_goals"]),
            int(context["second_home_goals"]),
        )
        leg_index = 2
    else:
        return result
    current_score = (
        int(result.get("home_goals") or 0),
        int(result.get("away_goals") or 0),
    )
    if current_score == leg_score:
        return result
    normalized = {
        **result,
        "home_goals": leg_score[0],
        "away_goals": leg_score[1],
        "aggregate_home_goals": current_score[0],
        "aggregate_away_goals": current_score[1],
        "score_source": "round_tie_leg",
        "two_legged_tie": True,
        "leg_index": leg_index,
        "tie_address": hex(int(context["tie_address"])),
    }
    # Detail fields parsed against an aggregate full-time score cannot be
    # trusted even when they happen to fit inside the corrected leg score.
    for field in ("half_home_goals", "half_away_goals", "first_scoring_team"):
        normalized.pop(field, None)
    return normalized


def normalize_two_legged_results_from_fixture_hints(
    reader: Reader,
    results: list[dict[str, Any]],
    fixture_hints: dict[tuple[str, int, int, int], int] | None,
) -> list[dict[str, Any]]:
    """Apply exact bet-time fixture hints without scanning the fixture pool."""
    hints = dict(fixture_hints or {})
    if not hints or not results:
        return results
    stage_cache: dict[tuple[int, int], dict[str, Any] | None] = {}
    normalized = []
    for item in results:
        try:
            key = _result_identity(item)
        except (KeyError, TypeError, ValueError):
            normalized.append(item)
            continue
        address = int(hints.get(key) or 0)
        fixture = parse_fixture(reader, address) if address else None
        if not fixture:
            normalized.append(item)
            continue
        competition = reader.competition(fixture.competition_season)
        home = reader.team(fixture.home_team)
        away = reader.team(fixture.away_team)
        if not competition or not home or not away:
            normalized.append(item)
            continue
        actual = (
            fixture.match_date.isoformat(),
            int(competition.get("id") or 0),
            int(home.get("id") or 0),
            int(away.get("id") or 0),
        )
        if actual != key:
            normalized.append(item)
            continue
        normalized.append(normalize_two_legged_fixture_result(
            reader, fixture, item, stage_cache=stage_cache,
        ))
    return normalized


def annotate_knockout_fixture_rows(
    reader: Reader,
    rows: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> dict[int, dict[str, Any]]:
    contexts = {}
    stage_cache: dict[tuple[int, int], dict[str, Any] | None] = {}
    by_tie: dict[int, list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for fixture, meta, home, away in rows:
        context = fixture_knockout_context(
            reader, fixture, stage_cache=stage_cache,
        )
        if context:
            by_tie[int(context["tie_address"])].append((fixture, meta, home, away, context))
    for group in by_tie.values():
        orientations = {item[4]["orientation"] for item in group}
        two_legged = orientations == {"first", "second"} or "second" in orientations
        deciding = max(group, key=lambda item: (item[0].match_date, item[0].address))
        for fixture, _meta, _home, _away, context in group:
            context = dict(context)
            context["leg_count"] = 2 if two_legged else 1
            context["leg_index"] = 2 if two_legged and context["orientation"] == "second" else 1
            settlement_kickoff = getattr(deciding[0], "kickoff_minutes", None)
            context["settlement_fixture"] = {
                "date": deciding[0].match_date.isoformat(),
                "home_id": int(deciding[2]["id"]),
                "away_id": int(deciding[3]["id"]),
                "kickoff_minutes": settlement_kickoff,
                "kickoff_time": (
                    f"{int(settlement_kickoff) // 60:02d}:{int(settlement_kickoff) % 60:02d}"
                    if settlement_kickoff is not None else None
                ),
            }
            contexts[int(fixture.address)] = context
    return contexts


def competition_final_metadata(
    match: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any] | None:
    if not context.get("is_competition_final"):
        return None
    settlement_fixture = context.get("settlement_fixture") or {}
    return {
        **({"leg_count": int(context["leg_count"])} if context.get("leg_count") else {}),
        "date": str(settlement_fixture.get("date") or match["fixture_date"]),
        "home_id": int(settlement_fixture.get("home_id") or match["home"]["id"]),
        "away_id": int(settlement_fixture.get("away_id") or match["away"]["id"]),
        "kickoff_minutes": settlement_fixture.get(
            "kickoff_minutes", match.get("kickoff_minutes")
        ),
        "stage_index": int(context.get("stage_index") or 0),
        "round_index": int(context.get("round_index") or 0),
        "tie_address": int(context.get("tie_address") or 0),
    }


def competition_round_metadata(context: dict[str, Any]) -> dict[str, Any]:
    """Expose only verified native round identity needed by outright markets."""
    return {
        "stage_index": int(context.get("stage_index") or 0),
        "stage_count": int(context.get("stage_count") or 0),
        "round_index": int(context.get("round_index") or 0),
        "round_count": int(context.get("round_count") or 0),
        "round_tie_count": int(context.get("round_tie_count") or 0),
        "match_role": str(context.get("competition_match_role") or "knockout"),
        "tie_address": int(context.get("tie_address") or 0),
    }


def fixture_result_knockout_decision(
    reader: Reader, fixture_addresses: list[int], result: dict[str, Any],
) -> dict[str, Any] | None:
    target_date = str(result.get("date") or "")
    target_competition = int((result.get("competition") or {}).get("id") or 0)
    target_home = int((result.get("home_team") or {}).get("id") or 0)
    target_away = int((result.get("away_team") or {}).get("id") or 0)
    if not target_date or not target_competition or not target_home or not target_away:
        return None
    for address in fixture_addresses:
        fixture = parse_fixture(reader, address)
        if not fixture or fixture.match_date.isoformat() != target_date:
            continue
        if reader.u32(fixture.home_team + ENTITY_UID) != target_home:
            continue
        if reader.u32(fixture.away_team + ENTITY_UID) != target_away:
            continue
        competition = reader.competition(fixture.competition_season)
        if not competition or int(competition.get("id") or 0) != target_competition:
            continue
        context = fixture_knockout_context(reader, fixture)
        winner_pointer = int((context or {}).get("winner_pointer") or 0)
        decided_by = (context or {}).get("decided_by")
        winner_id = reader.u32(winner_pointer + ENTITY_UID) if winner_pointer else None
        if winner_id and decided_by:
            return {
                "advanced_team_id": int(winner_id),
                "decided_by": str(decided_by),
            }
    return None


def result_knockout_identity(
    competition_kind: str,
    result: dict[str, Any],
    display_result: dict[str, Any],
) -> dict[str, Any]:
    """Build settlement identity directly from a verified cup-result winner."""
    side = str(result.get("winner_side") or "")
    method = str(result.get("decided_by") or "")
    decisive_national_result = bool(
        competition_kind == "national"
        and method in {"extra_time", "penalties"}
    )
    if (
        competition_kind != "cup"
        and not decisive_national_result
    ) or side not in {"home", "away"}:
        return {}
    team = display_result.get(side) or {}
    team_id = int(team.get("id") or 0)
    if not team_id:
        return {}
    return {
        "advanced_side": side,
        "advanced_team": team,
        "advanced_team_id": team_id,
    }


def annotate_result_fixture_stages(
    reader: Reader,
    results: list[dict[str, Any]],
    fixtures: list[Any],
) -> list[dict[str, Any]]:
    """Attach native fixture-stage identity without changing result authority."""
    stages: dict[tuple[str, int, int, int], tuple[int, int | None] | None] = {}
    for fixture in fixtures:
        if fixture.stage_index is None:
            continue
        competition = reader.competition(int(fixture.competition_season or 0))
        try:
            competition_address = int(str((competition or {}).get("address") or "0"), 0)
        except (TypeError, ValueError):
            competition_address = 0
        if not competition_address:
            continue
        key = (
            fixture.match_date.isoformat(), competition_address,
            int(fixture.home_team), int(fixture.away_team),
        )
        value = (
            int(fixture.stage_index),
            int(fixture.group_index) if fixture.group_index is not None else None,
        )
        stages[key] = value if key not in stages or stages[key] == value else None

    annotated = []
    for result in results:
        if result.get("competition_stage_index") is not None:
            annotated.append(result)
            continue
        try:
            key = (
                str(result["date"]),
                int(str(result["competition"]["address"]), 0),
                int(str(result["home_team"]["address"]), 0),
                int(str(result["away_team"]["address"]), 0),
            )
        except (KeyError, TypeError, ValueError):
            annotated.append(result)
            continue
        stage = stages.get(key)
        if stage is None:
            annotated.append(result)
            continue
        row = dict(result)
        row["competition_stage_index"] = stage[0]
        if stage[1] is not None:
            row["competition_group_index"] = stage[1]
        annotated.append(row)
    return annotated


def layout_save_identity(layout: Any, identity: str | None) -> str | None:
    """Keep FM24 save-scoped state separate from existing FM26 user data."""
    if not identity or layout.key != "fm24" or identity.startswith("career-"):
        return identity
    return identity if identity.startswith("fm24-") else f"fm24-{identity}"


def stable_save_key(
    layout: Any, identity: dict[str, Any] | None, save_name: str | None,
) -> str | None:
    if identity and identity.get("savegame_id"):
        value = str(identity["savegame_id"])
        return f"savegame-id-{value}" if layout.key == "fm24" else value
    if layout.key == "fm24" and save_name:
        digest = hashlib.sha256(save_name.encode("utf-8")).hexdigest()[:20]
        return f"save-name-{digest}"
    return None


def _fm24_manager_survives_save_name_change(
    reader: Reader, preferred_manager_id: int | None,
    known_manager_sessions: list[dict[str, Any]] | None = None,
) -> bool:
    """Return whether a renamed FM24 save still contains the confirmed manager.

    FM24 local network saves can be renamed by the game when the human manager
    changes clubs.  The save-name hash therefore cannot be treated as a new
    career on its own.  Revalidate the previously known manager first and only
    use the bounded manager discovery fallback when its address was rebuilt.
    """
    manager_id = int(preferred_manager_id or 0)
    if manager_id <= 0:
        return False
    known = [
        dict(item) for item in (known_manager_sessions or [])
        if isinstance(item, dict)
    ]
    try:
        validated = _validated_known_human_manager_sessions(reader, known)
    except (OSError, RuntimeError, TypeError, ValueError):
        validated = []
    if any(int(item.get("manager_id") or item.get("id") or 0) == manager_id for item in validated):
        return True
    # A minimal cached option containing only the manager UID is still useful
    # during the short object-rebuild window.  Do not use this shortcut when
    # richer team references exist: those must be revalidated or rediscovered.
    if known and not any(
        item.get("managed_team_refs") or item.get("teams") for item in known
    ) and any(
        int(item.get("manager_id") or item.get("id") or 0) == manager_id
        for item in known
    ):
        return True
    try:
        sessions = discover_human_managers(
            reader, [], preferred_manager_id=manager_id,
            allow_expensive_direct_scan=True,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        sessions = []
    return any(
        int(item.get("manager_id") or item.get("id") or 0) == manager_id
        for item in sessions if isinstance(item, dict)
    )


def fm24_network_save_key(
    layout: Any, manager_sessions: list[dict[str, Any]],
) -> str | None:
    """Build a deterministic scope when an FM24 network client has no local save provider.

    Network clients can expose all human managers and their teams while the local
    save-name object is absent. Using the complete manager/team set avoids the
    multi-manager startup deadlock and is safer than assigning the data to whichever
    manager happens to be discovered first.
    """
    if layout.key != "fm24" or not manager_sessions:
        return None
    identities = []
    for session in manager_sessions:
        manager_id = int(session.get("manager_id") or 0)
        teams = sorted(
            (
                int(ref.get("team_id") or 0),
                str(ref.get("team_type") or "club"),
            )
            for ref in (session.get("managed_team_refs") or [session])
            if int(ref.get("team_id") or 0) > 0
        )
        if manager_id > 0:
            identities.append((manager_id, teams))
    if not identities:
        return None
    payload = json.dumps(sorted(identities), ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"network-session-{digest}"


def anchored_save_context_evidence(
    layout: Any, stable_key: str | None, equivalent_key: str | None,
    preferred_save_id: str | None,
) -> tuple[str | None, str | None]:
    """Keep weak FM24 network evidence inside its confirmed connection scope."""
    if (
        layout.key == "fm24"
        and preferred_save_id
        and str(stable_key or "").startswith("network-session-")
    ):
        return None, None
    return stable_key, equivalent_key


def fm24_save_evidence(
    layout: Any, reader: Reader, module: Any,
    identity: dict[str, Any] | None, *,
    manager_sessions: list[dict[str, Any]] | None = None,
    discover_managers: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """Return one stable FM24 key plus an older equivalent used for migration."""
    direct_key = stable_save_key(layout, identity, None)
    if layout.key != "fm24":
        return direct_key, None, None
    # The former absolute numeric value is deliberately ignored. Two observed
    # save switches only showed correlation; V1.9.1c runtime evidence proved it
    # is not invariant during a normal career session.
    save_name, _save_candidates = read_save_name(reader, module, None)
    local_key = stable_save_key(layout, None, save_name)
    if local_key:
        return local_key, None, save_name

    sessions = manager_sessions
    if sessions is None and discover_managers:
        sessions = discover_human_managers(reader, [])
    network_key = fm24_network_save_key(layout, sessions or [])
    return network_key, None, None


WATCHED_COMPETITIONS: dict[int, tuple[str, str, int]] = {
    91: ("欧国联A级联赛", "national", 96),
    92: ("欧国联B级联赛", "national", 92),
    93: ("欧国联C级联赛", "national", 88),
    94: ("欧国联D级联赛", "national", 82),
    100102: ("世界杯亚洲区预选赛", "national", 94),
    100103: ("世界杯南美区预选赛", "national", 98),
    10: ("荷甲联赛", "league", 89),
    11: ("英超联赛", "league", 100),
    67: ("西甲联赛", "league", 98),
    22: ("德甲联赛", "league", 96),
    32: ("意甲联赛", "league", 95),
    16: ("法甲联赛", "league", 91),
    130931: ("中超联赛", "league", 72),
    102428: ("J1联赛", "league", 74),
    115424: ("J2联赛", "league", 62),
    791180: ("J3联赛", "league", 55),
    1301394: ("欧冠联赛", "cup", 100),
    1301396: ("欧联杯", "cup", 94),
    31051584: ("欧协联", "cup", 88),
    1301426: ("英格兰足总杯", "cup", 91),
    1301427: ("英格兰联赛杯", "cup", 84),
    1301422: ("西班牙国王杯", "cup", 90),
    1301410: ("德国杯", "cup", 89),
    1301412: ("意大利杯", "cup", 88),
    1301407: ("法国杯", "cup", 85),
    135941: ("中国足协杯", "cup", 68),
    102429: ("天皇杯", "cup", 72),
    102430: ("日本联赛杯", "cup", 69),
}
# ``managed_schedule`` remains accepted for cached snapshots created before
# the settings option was replaced by the favorite-team competition scope.
ODDS_SCOPE_OPTIONS = {"favorite_schedule", "managed_schedule", "famous", "all"}
FAMOUS_COMPETITION_IDS = {
    10, 11, 16, 22, 32, 67, 102428, 130931,
    1301394, 1301396, 31051584,
    1301426, 1301427, 1301422, 1301410, 1301412, 1301407,
    135941, 102429, 102430,
    100102, 100103,
}
FAMOUS_COMPETITION_MARKERS = (
    "荷甲", "荷兰足球甲级联赛", "eredivisie",
    "J1联赛", "明治安田J1", "j1 league",
    "世界杯", "世预赛", "world cup",
    "欧洲杯", "欧洲足球锦标赛", "uefa european championship",
    "亚洲杯", "asian cup",
    "美洲杯", "南美足球锦标赛", "copa america",
    "欧冠", "uefa champions league",
    "欧联", "uefa europa league",
    "欧协联", "uefa conference league", "europa conference league",
    "亚冠", "亚洲冠军联赛", "亚足联冠军", "afc champions league",
    "英格兰足总杯", "fa cup", "英格兰联赛杯", "efl cup", "carabao cup",
    "西班牙国王杯", "copa del rey", "德国杯", "dfb-pokal",
    "意大利杯", "coppa italia", "法国杯", "coupe de france",
    "中国足协杯", "天皇杯", "日本联赛杯",
)

RESULT_OVERRIDES_PATH = DATA_ROOT / "results" / "verified_results.json"
MODEL_VERSION = "fm24-fm26-ca-form-dc-v1.33-margin55-live-coherent-markets-star4-xi7-bench11-50-30-20-extreme-tail-k00145-exact-score-rtp-73-equivalent-ah"
MODEL_CACHE_VERSION = 9
CHAMPIONSHIP_DISCOVERY_DAYS = 14
ONE_X_TWO_MARGIN = 0.055
TWO_WAY_MARGIN = 0.05
TEAM_TOTAL_MARGIN = 0.055
HALF_MARKET_ONE_X_TWO_MARGIN = 0.085
HALF_MARKET_TWO_WAY_MARGIN = 0.08
BTTS_MARGIN = 0.07
DOUBLE_CHANCE_MARGIN = 0.07
CLEAN_SHEET_MARGIN = 0.09
FIRST_SCORE_MARGIN = 0.08
GOAL_PARITY_MARGIN = 0.03
HALF_FULL_MARGIN = 0.17
WINNING_MARGIN_MARGIN = 0.19
HIGHEST_SCORING_HALF_MARGIN = 0.07
KNOCKOUT_ADVANCE_MARGIN = 0.09
KNOCKOUT_BINARY_MARGIN = 0.11
KNOCKOUT_METHOD_MARGIN = 0.19
EXACT_SCORE_ODDS_DIVISOR = 1.26
EXACT_GOALS_ODDS_DIVISOR = 1.17
# 全场比分盘目标返还率约 73%；0-0 保留较低抽水，其余比分按覆盖概率质量折算除数。
EXACT_SCORE_TARGET_RETURN_RATE = 0.73
EXACT_SCORE_RETURN_RATE_BAND = (0.72, 0.75)
FIRST_HALF_GOAL_SHARE = 0.45
MARGIN_WEIGHT_POWER = 0.80
WORLD_CUP_FINALS_IDS = {1301385}
WORLD_CUP_2026_HOST_IDS = {364, 379, 390}
STRENGTH_LOG_COEFFICIENT = 0.0145
STRENGTH_LOG_EDGE_CAP = 0.825
# The normal strength signal is intentionally bounded, but a hard ±50 CA gap
# compresses extreme national-team mismatches into the same edge as merely
# large mismatches.  Apply a small, separately capped tail only beyond that
# range; ordinary fixtures and the existing cap behaviour remain unchanged.
EXTREME_STRENGTH_GAP_THRESHOLD = 50.0
EXTREME_STRENGTH_LOG_COEFFICIENT = 0.004
EXTREME_STRENGTH_GAP_TAIL_CAP = 30.0
COMPETITION_HOME_AWAY_SPLIT_WEIGHT = 0.60
GOAL_LEVEL_LOG_ADJUSTMENT = -0.04
LOW_CA_GOAL_THRESHOLD = 90.0
LOW_CA_GOAL_LOG_COEFFICIENT = 0.0025
LOW_CA_GOAL_LOG_EDGE_CAP = 0.12
DIXON_COLES_RHO = 0.04
RECENT_FORM_COEFFICIENT = 0.04
RECENT_FORM_LOG_EDGE_CAP = 0.225
MORALE_LOG_EDGE_CAP = 0.10
RECENT_TOTAL_GOALS_LOG_COEFFICIENT = 0.04
RECENT_TOTAL_GOALS_LOG_ADJUSTMENT_CAP = 0.10
ELO_INITIAL_RATING = 1500.0
ELO_UPDATE_K = 20.0
ELO_HOME_ADVANTAGE = 60.0
ELO_OPPONENT_SCALE = 250.0
ELO_OPPONENT_CORRECTION_CAP = 0.60
ELO_CONFIDENCE_MATCHES = 10
STANDINGS_PRIOR_MATCHES = 10
STANDINGS_GOAL_LOG_COEFFICIENT = 0.35
STANDINGS_GOAL_LOG_ADJUSTMENT_CAP = 0.10
STANDINGS_ELO_LOG_COEFFICIENT = 0.00035
STANDINGS_ELO_LOG_EDGE_CAP = 0.16
TEAM_REPUTATION_LOG_COEFFICIENT = 0.06
TEAM_REPUTATION_LOG_EDGE_CAP = 0.08

# FM's process addresses remain stable across normal same-session refreshes.  The
# cache avoids repeatedly sweeping its private memory while keeping a short TTL
# for newly created fixture objects.
_RUNTIME_CACHE_LOCK = RLock()
_RUNTIME_CACHE: dict[str, Any] = {
    "scope": None,
    "fixture_addresses": [],
    "fixture_scanned_at": 0.0,
    "result_addresses": [],
    "result_region_spans": [],
    "result_fingerprints": {},
    "result_pool_rescanned_at": 0.0,
    "result_game_date": None,
    "generic_results": [],
    "merged_results": [],
    "merged_result_game_date": None,
    "persistent_checked_game_date": None,
    "persistent_checked_keys": {},
    "persistent_results_by_key": {},
    "profiles": {},
    "baselines": {},
    "managed_schedule_competitions": {},
    "human_manager_sessions": [],
    "human_manager_scanned_at": 0.0,
    "elo_signature": None,
    "elo_adjustments": {},
    "elo_ratings": {},
    "elo_match_counts": {},
}
FIXTURE_ADDRESS_CACHE_SECONDS = 120.0
HUMAN_MANAGER_CACHE_SECONDS = 120.0
RESULT_POOL_RESCAN_SECONDS = 10.0
LIVE_RESULT_POOL_RESCAN_SECONDS = 1.0
SESSION_SCAN_BLOCK_BYTES = 8 * 1024 * 1024
SESSION_SCAN_LIMIT_BYTES = 1536 * 1024 * 1024
MANAGER_PERSON = 0x450
HUMAN_MANAGER_VTABLE_RVA = 0x44A51CC
PERSON_CONTRACT = 0xA8
PERSON_CONTRACT_COLLECTION = 0xB0
FM24_PERSON_FULL_CONTRACT = 0xC8
FM24_PERSON_OTHER_CONTRACTS = 0xD0
FM24_OTHER_CONTRACTS_NATIONAL_TEAM = 0x10
COACHING_LICENSE_NAMES = {
    0: "无证书", 7: "国家 C 级", 6: "国家 B 级", 5: "国家 A 级",
    4: "洲际 C 级", 3: "洲际 B 级", 2: "洲际 A 级", 1: "洲际职业级",
}
COACHING_LICENSE_NEXT = {0: 7, 7: 6, 6: 5, 5: 4, 4: 3, 3: 2, 2: 1}


def _manager_coaching_license(
    reader: Reader, person: int,
) -> dict[str, Any] | None:
    offset = reader.layout.staff_coaching_license_offset
    code = reader.u8(person + int(offset)) if person and offset is not None else None
    if code not in COACHING_LICENSE_NAMES:
        return None
    next_code = COACHING_LICENSE_NEXT.get(int(code))
    return {
        "code": int(code), "name": COACHING_LICENSE_NAMES[int(code)],
        "next_code": next_code, "next_name": COACHING_LICENSE_NAMES.get(next_code),
        "maximum": next_code is None,
    }


def _reset_runtime_cache(scope: tuple[Any, ...] | None) -> None:
    _RUNTIME_CACHE.update({
        "scope": scope,
        "fixture_addresses": [],
        "fixture_scanned_at": 0.0,
        "result_addresses": [],
        "result_region_spans": [],
        "result_fingerprints": {},
        "result_pool_rescanned_at": 0.0,
        "result_game_date": None,
        "generic_results": [],
        "merged_results": [],
        "merged_result_game_date": None,
        "persistent_checked_game_date": None,
        "persistent_checked_keys": {},
        "persistent_results_by_key": {},
        "profiles": {},
        "baselines": {},
        "managed_schedule_competitions": {},
        "human_manager_sessions": [],
        "human_manager_scanned_at": 0.0,
        "elo_signature": None,
        "elo_adjustments": {},
        "elo_ratings": {},
        "elo_match_counts": {},
        "manager_session": None,
    })


def invalidate_runtime_cache() -> None:
    """Drop every process-local odds hint without touching disk or FM memory."""
    with _RUNTIME_CACHE_LOCK:
        _reset_runtime_cache(None)


def _reader_runtime_identity(reader: Reader) -> tuple[int, int, int]:
    return (
        int(reader.process.pid),
        int(reader.module_base),
        int(getattr(reader, "session_generation", 0) or 0),
    )


def _runtime_state(reader: Reader) -> dict[str, Any]:
    identity = _reader_runtime_identity(reader)
    with _RUNTIME_CACHE_LOCK:
        current_scope = _RUNTIME_CACHE.get("scope")
        save_scope = (
            current_scope[3]
            if (
                isinstance(current_scope, tuple)
                and len(current_scope) == 4
                and tuple(current_scope[:3]) == identity
            )
            else None
        )
        scope = (*identity, save_scope)
        if _RUNTIME_CACHE["scope"] != scope:
            _reset_runtime_cache(scope)
        return _RUNTIME_CACHE


def _telemetry_value(window: bytes, field: str) -> str | None:
    match = re.search(rb'"' + field.encode("ascii") + rb'"\s*:\s*"([^"}]*)"', window)
    return match.group(1).decode("utf-8", "ignore") if match else None


def discover_manager_session(
    reader: Reader, game_date: Any, *, stable_scope: str | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> dict[str, Any] | None:
    """Read the active career identity from FM telemetry, newest memory first."""
    date_tokens = (
        f"{game_date.year}/{game_date.month}/{game_date.day}".encode("ascii"),
        game_date.isoformat().encode("ascii"),
    )
    scanned = 0
    regions = [
        region for region in iter_readable_regions(reader.process)
        if region.type == MEM_PRIVATE and 0x10000 <= region.size <= 128 * 1024 * 1024
    ]
    found: dict[str, Any] | None = None
    for region in reversed(regions):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        for end in range(region.size, 0, -SESSION_SCAN_BLOCK_BYTES):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            start = max(0, end - SESSION_SCAN_BLOCK_BYTES)
            raw = reader.bytes(region.base_address + start, end - start)
            if not raw:
                continue
            scanned += len(raw)
            if b'"gameInstanceID"' not in raw:
                continue
            for token in date_tokens:
                marker = b'"game_date":"' + token + b'"'
                position = len(raw)
                while True:
                    position = raw.rfind(marker, 0, position)
                    if position < 0:
                        break
                    window = raw[max(0, position - 800):min(len(raw), position + 1800)]
                    instance_id = _telemetry_value(window, "gameInstanceID")
                    club_id = _telemetry_value(window, "team_id")
                    nation_id = _telemetry_value(window, "nation_team_id")
                    nation_gender = _telemetry_value(window, "nation_team_gender_id")
                    positive_nation = int(nation_id) if nation_id and nation_id.isdigit() and int(nation_id) > 0 else None
                    positive_club = int(club_id) if club_id and club_id.isdigit() and int(club_id) > 0 else None
                    if instance_id and (positive_nation or positive_club):
                        has_national_job = bool(positive_nation and nation_gender not in (None, "-1"))
                        managed_refs = []
                        if positive_club:
                            managed_refs.append({"team_id": positive_club, "team_type": "club"})
                        if has_national_job:
                            managed_refs.append({"team_id": positive_nation, "team_type": "national"})
                        primary = next(
                            (item for item in managed_refs if item["team_type"] == "club"),
                            managed_refs[0],
                        )
                        found = {
                            "game_instance_id": instance_id,
                            "team_id": primary["team_id"],
                            "team_type": primary["team_type"],
                            "managed_team_refs": managed_refs,
                            "screen_id": _telemetry_value(window, "screen_id"),
                            "client_time_stamp": _telemetry_value(window, "client_time_stamp"),
                            "source": "current_save_telemetry",
                            "scan_bytes": scanned,
                        }
                        break
                    position -= 1
                if found:
                    break
            if found or scanned >= SESSION_SCAN_LIMIT_BYTES:
                break
        if found or scanned >= SESSION_SCAN_LIMIT_BYTES:
            break

    scope = (
        *_reader_runtime_identity(reader),
        stable_scope or (found.get("game_instance_id") if found else None),
    )
    with _RUNTIME_CACHE_LOCK:
        if _RUNTIME_CACHE.get("scope") != scope:
            _reset_runtime_cache(scope)
        _RUNTIME_CACHE["manager_session"] = found
    return found


def _manager_name(reader: Reader, person: int) -> str | None:
    if reader.layout.key == "fm24":
        # FM24's HUMAN_NON_PLAYER person view places the given/family/common
        # name references eight bytes after the generic PERSON constants.
        first = reader.fm_nested_string_at(person + 0x58)
        last = reader.fm_nested_string_at(person + 0x60)
        common = reader.fm_nested_string_at(person + 0x68)
        full = None
        if first and last and all("\u3400" <= char <= "\u9fff" for char in f"{first}{last}"):
            return f"{last}{first}"
        return full or common or " ".join(part for part in (first, last) if part)

    def name_part(offset: int) -> str | None:
        entry = reader.ptr(person + offset)
        return reader.fm_string_at(entry + 0x60) if entry else None

    common = name_part(PERSON_COMMON_NAME)
    first = name_part(PERSON_FIRST_NAME)
    last = name_part(PERSON_LAST_NAME)
    full = reader.fm_string_at(person + PERSON_FULL_NAME)
    if first and last and all("\u3400" <= char <= "\u9fff" for char in f"{first}{last}"):
        return f"{last}{first}"
    full_parts = str(full or "").split()
    if len(full_parts) == 2 and all(
        "\u3400" <= char <= "\u9fff" for char in "".join(full_parts)
    ):
        return f"{full_parts[1]}{full_parts[0]}"
    return full or common or " ".join(part for part in (first, last) if part)


def localized_manager_name(manager_id: int | None, team_id: int | None) -> str | None:
    """Return a localized manager name only when the indexed club and IDs agree."""
    if not manager_id or not team_id:
        return None
    try:
        path = cache_data_root() / "world" / "world_index.json"
        club = json.loads(path.read_text(encoding="utf-8")).get("club", {})
        indexed = club.get("manager") or {}
        if int(club.get("id")) != int(team_id) or int(indexed.get("id")) != int(manager_id):
            return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    name = str(indexed.get("name") or "").strip()
    return name or None


def resolve_managed_team(
    reader: Reader,
    team_id: int,
    team_type: str,
    fixture_addresses: list[int],
    fixture_rows: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> dict[str, Any] | None:
    candidates = [team for _fixture, _meta, home, away in fixture_rows for team in (home, away)]
    for address in fixture_addresses:
        fixture = parse_fixture(reader, address)
        if fixture:
            candidates.extend(filter(None, (reader.team(fixture.home_team), reader.team(fixture.away_team))))
        if any(team["id"] == team_id and team.get("team_type") == team_type for team in candidates[-2:]):
            break
    return next((team for team in candidates if team["id"] == team_id and team.get("team_type") == team_type), None)


def _human_manager_matches(reader: Reader, address: int, manager_id: int) -> bool:
    vtable_address = (
        address + reader.layout.manager_person_offset
        if reader.layout.human_manager_vtable_on_person else address
    )
    return bool(
        address
        and (reader.ptr(vtable_address) or 0) - reader.module_base
        in reader.layout.human_manager_vtable_rvas
        and reader.u32(address + reader.layout.manager_person_offset + ENTITY_UID) == int(manager_id)
    )


def _find_human_manager_by_id(reader: Reader, manager_id: int) -> int:
    directory = database_index_for_reader(reader)
    if directory is not None:
        for address in directory.human_manager_addresses(int(manager_id)):
            if _human_manager_matches(reader, int(address), int(manager_id)):
                return int(address)
    needles = [
        struct.pack("<Q", reader.module_base + rva)
        for rva in reader.layout.human_manager_vtable_rvas
    ]
    for region in iter_readable_regions(reader.process):
        if region.type != MEM_PRIVATE:
            continue
        carry = b""
        offset = 0
        while offset < region.size:
            length = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if block:
                data = carry + block
                base = region.base_address + offset - len(carry)
                for needle in needles:
                    position = 0
                    while True:
                        position = data.find(needle, position)
                        if position < 0:
                            break
                        vtable_address = base + position
                        address = (
                            vtable_address - reader.layout.manager_person_offset
                            if reader.layout.human_manager_vtable_on_person else vtable_address
                        )
                        if _human_manager_matches(reader, address, manager_id):
                            return address
                        position += 8
                carry = data[-7:]
            else:
                carry = b""
            offset += length
    return 0


def resolve_managed_team_by_identity(
    reader: Reader, team_id: int, team_type: str, manager_id: int,
    manager_address: Any = 0, *, scan_manager: bool = False,
) -> dict[str, Any] | None:
    """Recover a moved team object from the manager's contracts and stable IDs."""
    try:
        manager = int(str(manager_address or "0"), 16)
    except (TypeError, ValueError):
        manager = 0
    if not _human_manager_matches(reader, manager, manager_id):
        manager = _find_human_manager_by_id(reader, manager_id) if scan_manager else 0
    if not manager:
        return None
    person = manager + reader.layout.manager_person_offset
    for contract in _person_contract_candidates(reader, person):
        team_address = reader.ptr(contract + 0x10)
        team = reader.team(team_address) if team_address else None
        if (
            team
            and int(team.get("id") or 0) == int(team_id)
            and str(team.get("team_type") or "club") == str(team_type or "club")
        ):
            return team
    return None


def _person_contract_candidates(reader: Reader, person: int) -> list[int]:
    """Return only contracts that point back to this person object.

    FM24 live samples normally expose a full contract at ``person+0xC8``.
    A Win10 24.4.2 sample instead exposed valid HumanManager persons with that
    slot empty, while the older manager recovery path already used ``+0xA8``
    and the adjacent contract collection.  Keep both known routes, then use a
    small FM24-only pointer-window fallback.  Every result still requires the
    contract's Person and Team links before callers accept a managed team.
    """
    candidates: list[int] = []

    def add(candidate: int | None) -> None:
        value = int(candidate or 0)
        if (
            value and value not in candidates
            and int(reader.ptr(value + 0x08) or 0) == int(person)
            and int(reader.ptr(value + 0x10) or 0) > 0
        ):
            candidates.append(value)

    primary_offsets = (
        (FM24_PERSON_FULL_CONTRACT, PERSON_CONTRACT)
        if reader.layout.key == "fm24" else (PERSON_CONTRACT,)
    )
    for offset in primary_offsets:
        add(reader.ptr(person + offset))

    if reader.layout.key == "fm24":
        other_contracts = reader.ptr(person + FM24_PERSON_OTHER_CONTRACTS)
        if other_contracts:
            add(reader.ptr(
                other_contracts + FM24_OTHER_CONTRACTS_NATIONAL_TEAM
            ))

    collection = reader.ptr(person + PERSON_CONTRACT_COLLECTION)
    if collection:
        for offset in range(0, 0x80, 8):
            add(reader.ptr(collection + offset))

    if reader.layout.key == "fm24" and not candidates:
        for offset in range(0x80, 0xD8, 8):
            if offset not in primary_offsets and offset != PERSON_CONTRACT_COLLECTION:
                add(reader.ptr(person + offset))
    return candidates


def discover_human_managers(
    reader: Reader,
    fixture_addresses: list[int],
    preferred_manager_id: int | None = None,
    known_manager_sessions: list[dict[str, Any]] | None = None,
    fixture_snapshots: dict[int, bytes] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    allow_expensive_direct_scan: bool = True,
) -> list[dict[str, Any]]:
    """Resolve every human manager and their managed teams from live fixture objects."""
    requested_manager_id = int(preferred_manager_id or 0)
    raw_known_sessions = [
        dict(item) for item in (known_manager_sessions or [])
        if isinstance(item, dict)
    ]
    known_sessions = _validated_known_human_manager_sessions(
        reader, raw_known_sessions,
    )
    known_ids = {
        int(item.get("manager_id") or item.get("id") or 0)
        for item in raw_known_sessions
        if int(item.get("manager_id") or item.get("id") or 0) > 0
    }
    validated_ids = {
        int(item.get("manager_id") or item.get("id") or 0)
        for item in known_sessions
        if int(item.get("manager_id") or item.get("id") or 0) > 0
    }
    if (
        requested_manager_id
        and requested_manager_id in validated_ids
        and known_ids <= validated_ids
    ):
        return known_sessions

    candidates: dict[int, dict[str, Any]] = {}
    seen_team_addresses: set[int] = set()
    ordered_team_addresses: list[int] = []
    fixture_total = len(fixture_addresses)
    for index, address in enumerate(fixture_addresses, start=1):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        fixture = (
            parse_fixture_from_raw(
                reader, address, fixture_snapshots.get(address),
                validate_references=False,
            )
            if fixture_snapshots is not None
            else parse_fixture(reader, address)
        )
        if not fixture:
            if progress_callback:
                progress_callback("fixtures", index, fixture_total)
            continue
        for team_address in (fixture.home_team, fixture.away_team):
            if team_address in seen_team_addresses:
                continue
            seen_team_addresses.add(team_address)
            ordered_team_addresses.append(team_address)
        if progress_callback:
            progress_callback("fixtures", index, fixture_total)

    team_total = len(ordered_team_addresses)
    for index, team_address in enumerate(ordered_team_addresses, start=1):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        manager_address = reader.ptr(team_address + TEAM_MANAGER)
        if manager_address:
            team = reader.team(team_address)
            if not team:
                if progress_callback:
                    progress_callback("teams", index, team_total)
                continue
            manager_id = reader.u32(
                manager_address + reader.layout.manager_person_offset + ENTITY_UID
            )
            if not manager_id or not _human_manager_matches(reader, manager_address, manager_id):
                if progress_callback:
                    progress_callback("teams", index, team_total)
                continue
            candidates[team_address] = {
                "game_instance_id": f"human-{manager_id}",
                "team_id": int(team["id"]),
                "team_type": str(team.get("team_type") or "club"),
                "manager_id": int(manager_id),
                "address": team.get("address"),
                "club_address": team.get("club_address"),
                "manager_address": team.get("manager_address"),
                "source": "human_manager_object",
            }
        if progress_callback:
            progress_callback("teams", index, team_total)
    unique = {
        (item["team_id"], item["team_type"], item["manager_id"]): item
        for item in candidates.values()
    }
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in unique.values():
        grouped[int(item["manager_id"])].append(item)
    sessions = []
    for manager_id, managed_refs in grouped.items():
        managed_refs.sort(key=lambda item: (item["team_type"] != "club", item["team_id"]))
        primary = managed_refs[0]
        manager_address = int(str(primary.get("manager_address") or "0"), 16)
        person = manager_address + reader.layout.manager_person_offset if manager_address else 0
        sessions.append({
            **primary,
            "manager_name": _manager_name(reader, person) if person else None,
            "managed_team_refs": managed_refs,
            "source": "human_manager_object",
        })
    known_manager_ids = {int(item["manager_id"]) for item in sessions}
    needs_direct_scan = (
        requested_manager_id == 0
        or len(sessions) <= 1
        or requested_manager_id not in known_manager_ids
    )
    if not needs_direct_scan:
        direct_sessions = []
    elif allow_expensive_direct_scan:
        direct_sessions = _cached_direct_human_managers(reader)
    else:
        direct_sessions = _cached_direct_human_managers(
            reader, allow_heap_fallback=False,
        )
    # Keep already revalidated sessions in the merge while rediscovering any
    # cached option whose team object changed.  Otherwise a direct scan that
    # only finds the newly changed manager could accidentally drop the
    # currently selected manager from the candidate list.
    return _merge_human_manager_sessions(
        [*known_sessions, *sessions], direct_sessions,
    )


def _revalidate_known_human_manager_session(
    reader: Reader, manager_id: int, known_session: dict[str, Any],
    known_manager_sessions: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Revalidate a manager-switch hint before trusting its team assignments."""
    sessions = discover_human_managers(
        reader, [], preferred_manager_id=int(manager_id),
        known_manager_sessions=(
            list(known_manager_sessions)
            if known_manager_sessions is not None else [known_session]
        ),
    )
    selected = next(
        (
            item for item in sessions
            if int(item.get("manager_id") or 0) == int(manager_id)
        ),
        None,
    )
    return selected, sessions


def _validated_known_human_manager_sessions(
    reader: Reader, known_sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Revalidate cached manager/team addresses before avoiding a heap scan."""
    validated = []
    for item in known_sessions:
        manager_id = int(item.get("manager_id") or item.get("id") or 0)
        raw_refs = list(item.get("managed_team_refs") or item.get("teams") or [])
        raw_manager_address = item.get("manager_address") or next(
            (ref.get("manager_address") for ref in raw_refs if ref.get("manager_address")),
            None,
        )
        try:
            manager_address = int(str(raw_manager_address or "0"), 0)
        except (TypeError, ValueError):
            manager_address = 0
        if (
            manager_id <= 0
            or not manager_address
            or not _human_manager_matches(reader, manager_address, manager_id)
            or not raw_refs
        ):
            continue

        managed_refs = []
        valid = True
        for ref in raw_refs:
            team_id = int(ref.get("team_id") or ref.get("id") or 0)
            try:
                team_address = int(str(ref.get("address") or "0"), 0)
            except (TypeError, ValueError):
                team_address = 0
            team = reader.team(team_address) if team_address else None
            if (
                team_id <= 0
                or not team
                or int(team.get("id") or 0) != team_id
                or reader.ptr(team_address + TEAM_MANAGER) != manager_address
            ):
                valid = False
                break
            managed_refs.append({
                "game_instance_id": f"human-{manager_id}",
                "team_id": team_id,
                "team_type": str(team.get("team_type") or ref.get("team_type") or "club"),
                "manager_id": manager_id,
                "address": team.get("address") or hex(team_address),
                "club_address": team.get("club_address") or ref.get("club_address"),
                "manager_address": hex(manager_address),
                "source": "verified_snapshot_manager",
            })
        if not valid or not managed_refs:
            continue
        managed_refs.sort(key=lambda ref: (ref["team_type"] != "club", ref["team_id"]))
        validated.append({
            **managed_refs[0],
            "manager_name": item.get("manager_name") or item.get("name"),
            "managed_team_refs": managed_refs,
            "source": "verified_snapshot_manager",
        })
    return sorted(validated, key=lambda item: int(item["manager_id"]))


def _merge_human_manager_sessions(
    fixture_sessions: list[dict[str, Any]], direct_sessions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = {int(item["manager_id"]): dict(item) for item in fixture_sessions}
    for item in direct_sessions:
        manager_id = int(item["manager_id"])
        if manager_id not in merged:
            merged[manager_id] = dict(item)
            continue
        current = merged[manager_id]
        refs = {
            (int(ref.get("team_id") or 0), str(ref.get("team_type") or "club")): dict(ref)
            for ref in current.get("managed_team_refs") or []
            if int(ref.get("team_id") or 0) > 0
        }
        for ref in item.get("managed_team_refs") or []:
            key = (int(ref.get("team_id") or 0), str(ref.get("team_type") or "club"))
            if key[0] > 0:
                refs.setdefault(key, dict(ref))
        current["managed_team_refs"] = sorted(
            refs.values(), key=lambda ref: (ref.get("team_type") != "club", int(ref["team_id"])),
        )
        if not current.get("manager_name") and item.get("manager_name"):
            current["manager_name"] = item["manager_name"]
    return sorted(merged.values(), key=lambda item: int(item["manager_id"]))


def _human_manager_session_from_address(
    reader: Reader, manager: int,
) -> dict[str, Any] | None:
    person = int(manager) + int(reader.layout.manager_person_offset)
    manager_id = int(reader.u32(person + ENTITY_UID) or 0)
    if not manager_id or not _human_manager_matches(reader, int(manager), manager_id):
        return None
    managed_refs = []
    for contract in _person_contract_candidates(reader, person):
        team_address = reader.ptr(contract + 0x10)
        team = reader.team(team_address) if team_address else None
        if not team or reader.ptr(team_address + TEAM_MANAGER) != int(manager):
            continue
        managed_refs.append({
            "game_instance_id": f"human-{manager_id}",
            "team_id": int(team["id"]),
            "team_type": str(team.get("team_type") or "club"),
            "manager_id": manager_id,
            "address": team.get("address"),
            "club_address": team.get("club_address"),
            "manager_address": hex(int(manager)),
            "source": "native_database_index",
        })
    managed_refs = list({
        (ref["team_id"], ref["team_type"]): ref for ref in managed_refs
    }.values())
    managed_refs.sort(key=lambda ref: (ref["team_type"] != "club", ref["team_id"]))
    session = {
        "game_instance_id": f"human-{manager_id}",
        "manager_id": manager_id,
        "manager_address": hex(int(manager)),
        "manager_name": _manager_name(reader, person),
        "managed_team_refs": managed_refs,
        "source": "native_database_index",
    }
    if managed_refs:
        session.update(managed_refs[0])
    return session


def _discover_human_managers_direct(
    reader: Reader, *, allow_heap_fallback: bool = True,
) -> list[dict[str, Any]]:
    """Find human managers even when the current fixture pool omits their club.

    Human-manager objects share a compact allocation in verified FM24/FM26
    builds. Scan that allocation directly and validate every manager through
    their contract and the managed team's back-reference.
    """
    if reader.layout.key not in {"fm24", "fm26"} or not reader.layout.human_manager_vtable_rvas:
        return []
    directory = database_index_for_reader(reader)
    if directory is not None:
        indexed = {
            int(session["manager_id"]): session
            for address in directory.human_manager_addresses()
            if (session := _human_manager_session_from_address(reader, int(address)))
        }
        if indexed:
            return sorted(indexed.values(), key=lambda item: int(item["manager_id"]))
    if not allow_heap_fallback:
        return []
    needles = [
        struct.pack("<Q", reader.module_base + rva)
        for rva in reader.layout.human_manager_vtable_rvas
    ]
    regions = sorted(
        (
            region for region in iter_readable_regions(reader.process)
            if region.type == MEM_PRIVATE and region.size <= 64 * 1024 * 1024
        ),
        key=lambda region: region.base_address,
        reverse=True,
    )
    for region in regions:
        found: dict[int, dict[str, Any]] = {}
        carry = b""
        for offset in range(0, region.size, 8 * 1024 * 1024):
            length = min(8 * 1024 * 1024, region.size - offset)
            block = reader.bytes(region.base_address + offset, length)
            if not block:
                carry = b""
                continue
            data = carry + block
            base = region.base_address + offset - len(carry)
            for needle in needles:
                position = 0
                while True:
                    position = data.find(needle, position)
                    if position < 0:
                        break
                    vtable_address = base + position
                    manager = (
                        vtable_address - reader.layout.manager_person_offset
                        if reader.layout.human_manager_vtable_on_person else vtable_address
                    )
                    person = manager + reader.layout.manager_person_offset
                    manager_id = int(reader.u32(person + ENTITY_UID) or 0)
                    position += 8
                    if not manager_id or not _human_manager_matches(reader, manager, manager_id):
                        continue
                    managed_refs = []
                    for contract in _person_contract_candidates(reader, person):
                        team_address = reader.ptr(contract + 0x10)
                        team = reader.team(team_address) if team_address else None
                        if not team or reader.ptr(team_address + TEAM_MANAGER) != manager:
                            continue
                        managed_refs.append({
                            "game_instance_id": f"human-{manager_id}",
                            "team_id": int(team["id"]),
                            "team_type": str(team.get("team_type") or "club"),
                            "manager_id": manager_id,
                            "address": team.get("address"),
                            "club_address": team.get("club_address"),
                            "manager_address": hex(manager),
                            "source": "human_manager_object_direct",
                        })
                    # A no-team manager is trusted only when it came from the
                    # independently validated HumanManager root container above.
                    # A heap vtable hit without a team back-reference may be stale.
                    if not managed_refs:
                        continue
                    managed_refs = list({
                        (ref["team_id"], ref["team_type"]): ref for ref in managed_refs
                    }.values())
                    managed_refs.sort(key=lambda ref: (ref["team_type"] != "club", ref["team_id"]))
                    session = {
                        "game_instance_id": f"human-{manager_id}",
                        "manager_id": manager_id,
                        "manager_address": hex(manager),
                        "manager_name": _manager_name(reader, person),
                        "managed_team_refs": managed_refs,
                        "source": "human_manager_object_direct",
                    }
                    if managed_refs:
                        session.update(managed_refs[0])
                    found[manager_id] = session
            carry = data[-7:]
        if found:
            return sorted(found.values(), key=lambda item: int(item["manager_id"]))
    return []


def _cached_direct_human_managers(
    reader: Reader, *, allow_heap_fallback: bool = True,
) -> list[dict[str, Any]]:
    state = _runtime_state(reader)
    now = monotonic()
    with _RUNTIME_CACHE_LOCK:
        cached = list(state.get("human_manager_sessions") or [])
        scanned_at = float(state.get("human_manager_scanned_at") or 0.0)
        # A save can expose its date and identity before the human-manager
        # allocation or the manager's team links are ready.  Reusing either an
        # empty result or a manager-only partial result makes reconnects stay
        # manager-less for the full cache window after the save finishes
        # loading.  Only cache sessions whose manager/team identity is closed;
        # confirmed unemployment is carried by the application state instead.
        complete = bool(cached) and all(
            list(item.get("managed_team_refs") or []) for item in cached
        )
        if complete and scanned_at and now - scanned_at < HUMAN_MANAGER_CACHE_SECONDS:
            return cached
    sessions = _discover_human_managers_direct(
        reader, allow_heap_fallback=allow_heap_fallback,
    )
    with _RUNTIME_CACHE_LOCK:
        state["human_manager_sessions"] = list(sessions)
        state["human_manager_scanned_at"] = now
    return sessions


def discover_human_manager_session(
    reader: Reader, fixture_addresses: list[int], preferred_manager_id: int | None = None,
) -> dict[str, Any] | None:
    sessions = discover_human_managers(
        reader, fixture_addresses, preferred_manager_id=preferred_manager_id,
    )
    if preferred_manager_id:
        return next(
            (item for item in sessions if int(item["manager_id"]) == int(preferred_manager_id)),
            None,
        )
    return sessions[0] if len(sessions) == 1 else None


def _update_fixture_address_cache(
    reader: Reader, scanned: list[int], *, scanned_at: float | None = None,
) -> list[int]:
    """Merge a partial live scan without retaining deallocated fixture objects."""
    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        scope = state.get("scope")
        previous = set(state.get("fixture_addresses") or ())
    discovered = {int(address) for address in scanned if int(address) > 0}
    candidates = sorted(previous - discovered)
    if candidates:
        retained = set(read_fixture_snapshots_at_addresses(reader, candidates)[0])
        discovered.update(retained)
    merged = sorted(discovered)
    with _RUNTIME_CACHE_LOCK:
        if state.get("scope") == scope:
            state["fixture_addresses"] = merged
            state["fixture_scanned_at"] = (
                monotonic() if scanned_at is None else float(scanned_at)
            )
    return merged


def cached_fixture_addresses(
    reader: Reader, *, force_scan: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[int], bool]:
    state = _runtime_state(reader)
    now = monotonic()
    with _RUNTIME_CACHE_LOCK:
        addresses = list(state["fixture_addresses"])
        fresh = bool(
            addresses
            and not force_scan
            and now - float(state.get("fixture_scanned_at") or 0.0)
            < FIXTURE_ADDRESS_CACHE_SECONDS
        )
    if fresh:
        return addresses, True
    scanned, _bytes_scanned = scan_fixture_addresses(reader, cancel_check=cancel_check)
    return _update_fixture_address_cache(
        reader, scanned, scanned_at=now,
    ), False


def cached_generic_results(
    reader: Reader, game_date: str, *, force_scan: bool = False, incremental: bool = False,
    addresses_override: list[int] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read and cache FM's global FIXTURE_RESULT archive for this game date."""
    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        if not force_scan and state["result_game_date"] == game_date:
            return list(state["generic_results"]), True
        known_addresses = list(state["result_addresses"])
    if addresses_override is not None:
        addresses = list(addresses_override)
    elif incremental and known_addresses and not force_scan:
        addresses = known_addresses
    else:
        addresses, _bytes_scanned = scan_result_addresses(reader, cancel_check=cancel_check)
    spans = result_region_spans(reader, addresses)
    archive_work_total = max(1, len(addresses) * 2)
    fingerprints, _fingerprint_bytes = read_result_fingerprints_at_addresses(
        reader, addresses,
        progress=(
            lambda completed, _total: progress_callback(
                completed, archive_work_total,
            )
            if progress_callback else None
        ),
        cancel_check=cancel_check,
    )
    parsed: list[dict[str, Any]] = []
    for index, (address, raw) in enumerate(fingerprints.items(), start=1):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        item = parse_completed_result_from_raw(reader, address, raw)
        if item is not None:
            parsed.append(item)
        if progress_callback and (index == len(fingerprints) or index % 32 == 0):
            progress_callback(
                min(archive_work_total, len(addresses) + index),
                archive_work_total,
            )
    scanned = deduplicate_results(parsed)
    if progress_callback:
        progress_callback(archive_work_total, archive_work_total)
    with _RUNTIME_CACHE_LOCK:
        state["result_addresses"] = addresses
        state["result_region_spans"] = spans
        state["result_fingerprints"] = fingerprints
        state["result_pool_rescanned_at"] = monotonic()
        state["result_game_date"] = game_date
        state["generic_results"] = scanned
    return scanned, False


def result_region_spans(reader: Reader, addresses: list[int]) -> list[tuple[int, int]]:
    """Return the small set of live memory regions that contain result objects."""
    ordered = sorted(set(addresses))
    if not ordered:
        return []
    spans: list[tuple[int, int]] = []
    index = 0
    regions = sorted(iter_readable_regions(reader.process), key=lambda item: item.base_address)
    for region in regions:
        if region.type != MEM_PRIVATE:
            continue
        start = region.base_address
        end = start + region.size
        while index < len(ordered) and ordered[index] < start:
            index += 1
        if index >= len(ordered):
            break
        if ordered[index] < end:
            spans.append((start, region.size))
            while index < len(ordered) and ordered[index] < end:
                index += 1
    return spans


def probe_live_completed_results(
    required_keys: set[tuple[str, int, int, int]],
    *,
    fixture_hints: dict[tuple[str, int, int, int], int] | None = None,
) -> dict[str, Any]:
    """Read changed objects in cached result pools for the shadow settler."""
    if not required_keys:
        return {"results": [], "changed_objects": 0, "bytes_scanned": 0, "region_count": 0}
    with open_supported_reader() as (_process_path, layout, module, reader):
        return probe_live_completed_results_with_reader(
            reader, required_keys, fixture_hints=fixture_hints,
        )


def probe_live_completed_results_with_reader(
    reader: Reader,
    required_keys: set[tuple[str, int, int, int]],
    *,
    allow_expensive_rescan: bool = True,
    fixture_hints: dict[tuple[str, int, int, int], int] | None = None,
) -> dict[str, Any]:
    """Reader-reusing variant for the high-frequency FM24 capture loop."""
    if not required_keys:
        return {"results": [], "changed_objects": 0, "bytes_scanned": 0, "region_count": 0}
    hints = {}
    for key, raw_address in (fixture_hints or {}).items():
        if key not in required_keys:
            continue
        try:
            address = int(str(raw_address or "0"), 0)
        except (TypeError, ValueError):
            continue
        if address > 0:
            hints[key] = address
    hinted_results = recover_fixture_hint_results(reader, hints) if hints else []
    hinted_results = enrich_halftime_results(reader, hinted_results, required_keys)

    def settlement_matches(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        matched = {
            _result_identity(item): item
            for item in rows
            if _result_identity(item) in required_keys
        }
        matched.update({_result_identity(item): item for item in hinted_results})
        return list(matched.values())

    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        spans = list(state.get("result_region_spans") or [])
        known_addresses = list(state.get("result_addresses") or [])
        previous_fingerprints = dict(state.get("result_fingerprints") or {})
        cached_results = list(state.get("generic_results") or [])
        last_pool_rescan = float(state.get("result_pool_rescanned_at") or 0.0)
    cached_results = normalize_two_legged_results_from_fixture_hints(
        reader, cached_results, fixture_hints,
    )
    cached_results = enrich_halftime_results(reader, cached_results, required_keys)
    cached_matches = [item for item in cached_results if _result_identity(item) in required_keys]
    pool_rescan_seconds = (
        RESULT_POOL_RESCAN_SECONDS
        if allow_expensive_rescan else LIVE_RESULT_POOL_RESCAN_SECONDS
    )
    if not spans:
        # FM may not have created its result pool when FMODD first starts.
        # Re-discover the pool after the normal rescan interval instead of
        # keeping an empty region cache for the lifetime of the process.
        if monotonic() - last_pool_rescan < pool_rescan_seconds:
            return {
                "results": settlement_matches(cached_matches),
                "changed_objects": 0,
                "bytes_scanned": 0,
                "region_count": 0,
            }
        if not allow_expensive_rescan:
            pooled = read_fixture_pool_addresses(reader)
            if pooled is None or not pooled[1]:
                with _RUNTIME_CACHE_LOCK:
                    state["result_pool_rescanned_at"] = monotonic()
                return {
                    "results": settlement_matches(cached_matches),
                    "changed_objects": 0,
                    "bytes_scanned": int(pooled[2]) if pooled else 0,
                    "region_count": 0,
                }
            addresses = list(pooled[1])
            bytes_scanned = int(pooled[2])
        else:
            addresses, bytes_scanned = scan_result_addresses(reader)
        spans = result_region_spans(reader, addresses)
        fingerprints, fingerprint_bytes = read_result_fingerprints_at_addresses(
            reader, addresses,
        )
        discovered = deduplicate_results([
            item for address, raw in fingerprints.items()
            if (item := parse_completed_result_from_raw(reader, address, raw))
        ])
        discovered = normalize_two_legged_results_from_fixture_hints(
            reader, discovered, fixture_hints,
        )
        merged = enrich_halftime_results(
            reader, deduplicate_results([*cached_results, *discovered]), required_keys,
        )
        with _RUNTIME_CACHE_LOCK:
            state["result_addresses"] = addresses
            state["result_region_spans"] = spans
            state["result_fingerprints"] = fingerprints
            state["generic_results"] = merged
            state["merged_result_game_date"] = None
            state["result_pool_rescanned_at"] = monotonic()
        return {
            "results": settlement_matches(merged),
            "changed_objects": len(fingerprints),
            "bytes_scanned": bytes_scanned + fingerprint_bytes,
            "region_count": len(spans),
        }
    full_pool_rescan = (
        not known_addresses
        or monotonic() - last_pool_rescan >= pool_rescan_seconds
    )
    if full_pool_rescan and allow_expensive_rescan:
        fingerprints, bytes_scanned = scan_result_fingerprints_in_spans(reader, spans)
    elif full_pool_rescan:
        pooled = read_fixture_pool_addresses(reader)
        pooled_addresses = list(pooled[1]) if pooled is not None else []
        candidate_addresses = sorted(set(known_addresses) | set(pooled_addresses))
        fingerprints, fingerprint_bytes = read_result_fingerprints_at_addresses(
            reader, candidate_addresses,
        )
        bytes_scanned = (int(pooled[2]) if pooled else 0) + fingerprint_bytes
    else:
        fingerprints, bytes_scanned = read_result_fingerprints_at_addresses(
            reader, known_addresses,
        )
    changed_addresses = [
        address for address, fingerprint in fingerprints.items()
        if previous_fingerprints.get(address) != fingerprint
    ]
    changed_results = deduplicate_results([
        item for address in changed_addresses
        if (
            (raw := fingerprints.get(address)) is not None
            and (item := parse_completed_result_from_raw(reader, address, raw))
        )
    ])
    changed_results = normalize_two_legged_results_from_fixture_hints(
        reader, changed_results, fixture_hints,
    )
    merged = enrich_halftime_results(
        reader, deduplicate_results([*cached_results, *changed_results]), required_keys,
    )
    with _RUNTIME_CACHE_LOCK:
        state["result_addresses"] = (
            sorted(fingerprints)
            if full_pool_rescan
            else sorted(set(known_addresses) | set(fingerprints))
        )
        if full_pool_rescan:
            state["result_region_spans"] = result_region_spans(
                reader, list(state["result_addresses"]),
            )
        state["result_fingerprints"] = fingerprints
        state["generic_results"] = merged
        if changed_addresses:
            state["merged_result_game_date"] = None
        if full_pool_rescan:
            state["result_pool_rescanned_at"] = monotonic()
    matches = settlement_matches(merged)
    return {
        "results": matches,
        "changed_objects": len(changed_addresses),
        "bytes_scanned": bytes_scanned,
        "region_count": len(spans),
    }


def fixture_result_candidates(
    reader: Reader,
    game_date: str,
    *,
    force_scan: bool = False,
    known_result_addresses: set[int] | None = None,
    known_result_keys: set[tuple[str, int, int, int]] | None = None,
    known_result_items: dict[
        tuple[str, int, int, int], dict[str, Any]
    ] | None = None,
    performance: dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    fixture_snapshots_override: dict[int, bytes] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    yield_memory_lock: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Recover completed results attached directly to finished fixtures.

    FM normally keeps completed matches in the result-object pool, but some
    knockout ties that finish in extra time or penalties only retain the
    result through the fixture's ``result_or_state`` pointer.  Try both known
    result layouts here; parsers still validate teams, dates, scores and the
    completion outcome before accepting a candidate.
    """
    # Normal refreshes reuse the fixture list because another heap sweep would
    # duplicate work. A user-requested deep recovery may explicitly rebuild it
    # to escape stale fixture/result pointers retained by the current session.
    if fixture_snapshots_override is None:
        addresses, _cache_hit = cached_fixture_addresses(
            reader, force_scan=force_scan,
        )
        snapshots, header_bytes = read_fixture_snapshots_at_addresses(reader, addresses)
        snapshots_reused = False
    else:
        snapshots = dict(fixture_snapshots_override)
        header_bytes = 0
        snapshots_reused = True
    known_results = set(known_result_addresses or ())
    known_keys = set(known_result_keys or ())
    known_items = dict(known_result_items or {})
    # The generic result scan already validated these result objects and the
    # caller supplies their complete identities.  Build an address lookup so
    # fixture-link recovery can avoid re-reading competition/team objects for
    # every historical fixture that points at an already-known result.
    known_items_by_result_address: dict[int, dict[str, Any]] = {}
    for item in known_items.values():
        try:
            result_address = int(str(item.get("result_address") or "0"), 0)
        except (AttributeError, TypeError, ValueError):
            result_address = 0
        if result_address > 0 and result_address in known_results:
            known_items_by_result_address[result_address] = item
    recovered: list[dict[str, Any]] = []
    pointer_candidates = 0
    skipped_known = 0
    fast_skipped_known = 0
    normalized_two_legged = 0
    stage_cache: dict[tuple[int, int], dict[str, Any] | None] = {}
    snapshot_total = len(snapshots)
    for snapshot_index, (address, raw) in enumerate(snapshots.items(), start=1):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        home_pointer, away_pointer, result_pointer, competition_pointer = struct.unpack_from(
            "<QQQQ", raw, 0x08,
        )
        match_date = decode_date(struct.unpack_from("<I", raw, 0x4C)[0])
        # Count the header as soon as its cheap, already-prefetched fields have
        # been inspected.  This keeps the UI moving even when the subsequent
        # native object validation for one fixture is slow.
        if progress_callback:
            progress_callback(snapshot_index, snapshot_total)
        # A full refresh can inspect tens of thousands of historical fixture
        # headers.  Give foreground writes (for example a canteen plan
        # change) a chance to run between native reads instead of holding the
        # process mutex for the entire result-link pass.
        if yield_memory_lock and snapshot_index % 256 == 0:
            yield_memory_lock()
        if not match_date or match_date.isoformat() >= game_date or not result_pointer:
            continue
        pointer_candidates += 1
        known_item = known_items_by_result_address.get(result_pointer)
        if known_item is not None:
            # This pointer is the exact object accepted by the generic result
            # scan.  The fixture header still matters for two-legged knockout
            # normalization, but parsing it requires no additional FM reads.
            skipped_known += 1
            fast_skipped_known += 1
            if getattr(reader, "layout", None) is not None:
                fixture = parse_fixture_from_raw(
                    reader, address, raw, validate_references=False,
                )
                # A live result slot can be recycled after a fixture header is
                # retained.  The date check prevents applying a two-legged
                # normalization from an unrelated stale header while still
                # avoiding the expensive competition/team identity reads.
                if fixture and fixture.match_date.isoformat() == str(
                    known_item.get("date") or ""
                ):
                    normalized = normalize_two_legged_fixture_result(
                        reader, fixture, known_item, stage_cache=stage_cache,
                    )
                    if normalized != known_item:
                        recovered.append(normalized)
                        normalized_two_legged += 1
            continue
        competition = reader.competition(competition_pointer)
        home = reader.team(home_pointer)
        away = reader.team(away_pointer)
        if not competition or not home or not away:
            continue
        expected = (
            match_date.isoformat(),
            int(competition["id"]),
            int(home["id"]),
            int(away["id"]),
        )
        fixture = None
        if result_pointer in known_results and expected in known_keys:
            skipped_known += 1
            known_item = known_items.get(expected)
            if known_item and getattr(reader, "layout", None) is not None:
                fixture = parse_fixture_from_raw(
                    reader, address, raw, validate_references=False,
                )
            if fixture and known_item:
                normalized = normalize_two_legged_fixture_result(
                    reader, fixture, known_item, stage_cache=stage_cache,
                )
                if normalized != known_item:
                    recovered.append(normalized)
                    normalized_two_legged += 1
            continue
        for parser in (parse_completed_result, parse_season_completed_result):
            item = (
                parser(reader, result_pointer, competition_override=competition)
                if parser is parse_season_completed_result
                else parser(reader, result_pointer)
            )
            if not item:
                continue
            identity = (
                item.get("date"),
                int((item.get("competition") or {}).get("id")),
                int((item.get("home_team") or {}).get("id")),
                int((item.get("away_team") or {}).get("id")),
            )
            if identity == expected:
                if getattr(reader, "layout", None) is not None:
                    fixture = parse_fixture_from_raw(
                        reader, address, raw, validate_references=False,
                    )
                item = {
                    **item,
                    "result_source": (
                        "fixture_result_pointer"
                        if parser is parse_completed_result
                        else "persistent_fixture_pointer"
                    ),
                }
                if fixture:
                    normalized = normalize_two_legged_fixture_result(
                        reader, fixture, item, stage_cache=stage_cache,
                    )
                    if normalized != item:
                        normalized_two_legged += 1
                    item = normalized
                recovered.append(item)
                break
    if progress_callback:
        progress_callback(snapshot_total, snapshot_total)
    if performance is not None:
        performance.update({
            "result_fixture_headers": len(snapshots),
            "result_fixture_header_bytes": int(header_bytes),
            "result_fixture_headers_reused": int(snapshots_reused),
            "result_fixture_pointer_candidates": pointer_candidates,
            "result_fixture_pointer_skipped_known": skipped_known,
            "result_fixture_pointer_fast_skipped_known": fast_skipped_known,
            "result_fixture_pointer_recovered": len(recovered),
            "result_fixture_two_legged_normalized": normalized_two_legged,
        })
    return deduplicate_results(recovered)


def recover_fixture_hint_results(
    reader: Reader,
    fixture_hints: dict[tuple[str, int, int, int], int],
) -> list[dict[str, Any]]:
    """Recover missing results through fixture addresses captured at bet time."""
    recovered: list[dict[str, Any]] = []
    stage_cache: dict[tuple[int, int], dict[str, Any] | None] = {}
    for expected, address in fixture_hints.items():
        fixture = parse_fixture(reader, int(address))
        if not fixture or not fixture.result_or_state:
            continue
        competition = reader.competition(fixture.competition_season)
        home = reader.team(fixture.home_team)
        away = reader.team(fixture.away_team)
        if not competition or not home or not away:
            continue
        actual = (
            fixture.match_date.isoformat(), int(competition["id"]),
            int(home["id"]), int(away["id"]),
        )
        if actual != expected:
            continue
        for parser in (parse_completed_result, parse_season_completed_result):
            item = (
                parser(
                    reader, fixture.result_or_state,
                    competition_override=competition,
                )
                if parser is parse_season_completed_result
                else parser(reader, fixture.result_or_state)
            )
            if not item:
                continue
            try:
                result_date = date.fromisoformat(str(item["date"]))
                teams_match = (
                    int(item["home_team"]["id"]), int(item["away_team"]["id"]),
                ) == expected[2:]
                # Persistent score records can be dated one day after their
                # owning fixture. This is safe only here, after the bet-time
                # fixture object itself matched the full expected identity.
                result_date_matches = abs(
                    (result_date - date.fromisoformat(expected[0])).days
                ) <= 1
            except (KeyError, TypeError, ValueError):
                continue
            if not teams_match or not result_date_matches:
                continue
            source_result_date = str(item["date"])
            item = {
                **item,
                "date": expected[0],
                "competition": {**competition, "id": expected[1]},
                **(
                    {"source_result_date": source_result_date}
                    if source_result_date != expected[0] else {}
                ),
                "result_source": (
                    "fixture_result_pointer"
                    if parser is parse_completed_result
                    else "persistent_fixture_pointer"
                ),
                "fixture_address": hex(int(address)),
                "settlement_verified": True,
                "settlement_evidence": "bet_fixture_result_pointer",
            }
            recovered.append(normalize_two_legged_fixture_result(
                reader, fixture, item, stage_cache=stage_cache,
            ))
            break
    return deduplicate_results(recovered)


def cached_completed_results(
    reader: Reader, game_date: str, save_instance_id: str | None = None, *,
    force_scan: bool = False, incremental: bool = False,
    force_fixture_scan: bool = False,
    result_addresses_override: list[int] | None = None,
    performance: dict[str, Any] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    fixture_snapshots_override: dict[int, bytes] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    yield_memory_lock: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Merge the large result archive once per FM game date."""
    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        if not force_scan and state["merged_result_game_date"] == game_date:
            return list(state["merged_results"]), True
    phase_started = perf_counter()
    scanned, _generic_cache_hit = cached_generic_results(
        reader, game_date, force_scan=force_scan, incremental=incremental,
        addresses_override=result_addresses_override, cancel_check=cancel_check,
        progress_callback=(
            lambda completed, total: progress_callback(
                "archive", completed, total,
            )
            if progress_callback else None
        ),
    )
    if progress_callback:
        progress_callback("archive", len(scanned), len(scanned))
    if performance is not None:
        performance["results_archive_ms"] = round(
            (perf_counter() - phase_started) * 1000, 1,
        )
    with _RUNTIME_CACHE_LOCK:
        known_result_addresses = set(state.get("result_addresses") or ())
    phase_started = perf_counter()
    fixture_results = fixture_result_candidates(
        reader, game_date, force_scan=force_fixture_scan,
        known_result_addresses=known_result_addresses,
        known_result_keys={_result_identity(item) for item in scanned},
        known_result_items={_result_identity(item): item for item in scanned},
        performance=performance, cancel_check=cancel_check,
        fixture_snapshots_override=fixture_snapshots_override,
        progress_callback=(
            lambda completed, total: progress_callback("fixtures", completed, total)
            if progress_callback else None
        ),
        yield_memory_lock=yield_memory_lock,
    )
    if performance is not None:
        performance["results_fixture_links_ms"] = round(
            (perf_counter() - phase_started) * 1000, 1,
        )
    phase_started = perf_counter()
    results = resolve_daily_team_conflicts(
        merge_result_history(merge_verified_results([*scanned, *fixture_results]), save_instance_id)
    )
    if performance is not None:
        performance["results_merge_ms"] = round(
            (perf_counter() - phase_started) * 1000, 1,
        )
    with _RUNTIME_CACHE_LOCK:
        state["merged_results"] = results
        state["merged_result_game_date"] = game_date
    return results, False


def remember_completed_results(reader: Reader, game_date: str, results: list[dict[str, Any]]) -> None:
    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        state["merged_results"] = results
        state["merged_result_game_date"] = game_date


def recover_persistent_results(
    reader: Reader, game_date: str, missing: set[tuple[str, int, int, int]],
    *, force: bool = False,
) -> list[dict[str, Any]]:
    """Search and retain exact persistent results for unresolved settlement keys.

    The season-result sweep is expensive, so the previous implementation
    remembered only that a key had been checked.  That made the corroborating
    result disappear for 60 seconds even though its native object was still
    valid, preventing it from meeting an archive result discovered by a later
    live poll.  Retain the rows as well; the settlement binder still re-reads
    each native address before granting authority.
    """
    state = _runtime_state(reader)
    now = monotonic()
    with _RUNTIME_CACHE_LOCK:
        if state["persistent_checked_game_date"] != game_date:
            state["persistent_checked_game_date"] = game_date
            state["persistent_checked_keys"] = {}
            state["persistent_results_by_key"] = {}
        checked = state["persistent_checked_keys"]
        cached_by_key = state.setdefault("persistent_results_by_key", {})
        unchecked = set(missing) if force else {
            key for key in missing
            if now - float(checked.get(key, 0.0)) >= 60.0
        }
    if not unchecked:
        return [
            dict(item)
            for key in missing
            for item in cached_by_key.get(key, [])
        ]
    candidates = []
    for address in scan_season_result_addresses(reader)[0]:
        item = parse_season_completed_result(reader, address)
        if item:
            candidates.append(item)
    by_exact: dict[
        tuple[str, int, int, int],
        dict[tuple[int, int], dict[str, Any]],
    ] = defaultdict(dict)
    for item in candidates:
        key = _result_identity(item)
        score = (int(item["home_goals"]), int(item["away_goals"]))
        by_exact[key].setdefault(score, item)
    newly_recovered: dict[
        tuple[str, int, int, int], list[dict[str, Any]],
    ] = {}
    for key in unchecked:
        exact_scores = by_exact.get(key, {})
        if exact_scores:
            # FM commonly retains duplicate native objects for one result.
            # Preserve one row per distinct score so disagreement is surfaced
            # as a conflict instead of being hidden by dictionary overwrite.
            newly_recovered[key] = list(exact_scores.values())
            continue
        try:
            expected_date = date.fromisoformat(key[0])
        except ValueError:
            continue
        nearby = []
        for item in candidates:
            identity = _result_identity(item)
            if identity[2:] != key[2:]:
                continue
            try:
                distance = abs(
                    (date.fromisoformat(identity[0]) - expected_date).days
                )
            except ValueError:
                continue
            if distance <= 1:
                nearby.append((distance, item))
        if not nearby:
            continue
        nearest_distance = min(distance for distance, _item in nearby)
        nearest = [
            item for distance, item in nearby if distance == nearest_distance
        ]
        if len(nearest) != 1:
            continue
        item = nearest[0]
        source_result_date = str(item["date"])
        newly_recovered[key] = [{
            **item,
            "date": key[0],
            "competition": {**item["competition"], "id": key[1]},
            **(
                {"source_result_date": source_result_date}
                if source_result_date != key[0] else {}
            ),
            "result_source": "persistent_competition_alias",
        }]
    with _RUNTIME_CACHE_LOCK:
        state["persistent_checked_keys"].update({key: now for key in unchecked})
        cache = state.setdefault("persistent_results_by_key", {})
        for key in unchecked:
            cache[key] = [dict(item) for item in newly_recovered.get(key, [])]
        return [
            dict(item)
            for key in missing
            for item in cache.get(key, [])
        ]


def _elo_expected_score(rating: float, opponent: float, advantage: float = 0.0) -> float:
    return 1.0 / (1.0 + 10.0 ** ((opponent - rating - advantage) / 400.0))


def cached_elo_context(
    reader: Reader, results: list[dict[str, Any]],
) -> tuple[
    dict[tuple[tuple[str, int, int, int], int], float],
    dict[tuple[int, int], float],
    dict[tuple[int, int], int],
    bool,
]:
    """Build opponent-quality corrections from results without any extra FM reads."""
    ordered: list[dict[str, Any]] = []
    signature_rows = []
    for item in results:
        try:
            identity = _result_identity(item)
            home_goals = int(item["home_goals"])
            away_goals = int(item["away_goals"])
        except (KeyError, TypeError, ValueError):
            continue
        ordered.append(item)
        signature_rows.append((*identity, home_goals, away_goals))
    ordered.sort(key=_result_identity)
    signature = hash(tuple(sorted(signature_rows)))
    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        if state.get("elo_signature") == signature:
            return (
                dict(state["elo_adjustments"]), dict(state["elo_ratings"]),
                dict(state["elo_match_counts"]), True,
            )

    ratings: dict[tuple[int, int], float] = {}
    match_counts: dict[tuple[int, int], int] = {}
    adjustments: dict[tuple[tuple[str, int, int, int], int], float] = {}
    for item in ordered:
        identity = _result_identity(item)
        competition_id = int(identity[1])
        home_id, away_id = int(identity[2]), int(identity[3])
        home_key, away_key = (competition_id, home_id), (competition_id, away_id)
        home_rating = ratings.get(home_key, ELO_INITIAL_RATING)
        away_rating = ratings.get(away_key, ELO_INITIAL_RATING)
        home_count = match_counts.get(home_key, 0)
        away_count = match_counts.get(away_key, 0)
        home_confidence = min(away_count / ELO_CONFIDENCE_MATCHES, 1.0)
        away_confidence = min(home_count / ELO_CONFIDENCE_MATCHES, 1.0)
        adjustments[(identity, home_id)] = round(
            clamp(
                (away_rating - ELO_INITIAL_RATING) / ELO_OPPONENT_SCALE,
                -ELO_OPPONENT_CORRECTION_CAP, ELO_OPPONENT_CORRECTION_CAP,
            ) * home_confidence,
            4,
        )
        adjustments[(identity, away_id)] = round(
            clamp(
                (home_rating - ELO_INITIAL_RATING) / ELO_OPPONENT_SCALE,
                -ELO_OPPONENT_CORRECTION_CAP, ELO_OPPONENT_CORRECTION_CAP,
            ) * away_confidence,
            4,
        )
        expected_home = _elo_expected_score(
            home_rating, away_rating, ELO_HOME_ADVANTAGE,
        )
        home_goals, away_goals = int(item["home_goals"]), int(item["away_goals"])
        actual_home = 1.0 if home_goals > away_goals else 0.5 if home_goals == away_goals else 0.0
        change = ELO_UPDATE_K * (actual_home - expected_home)
        ratings[home_key] = home_rating + change
        ratings[away_key] = away_rating - change
        match_counts[home_key] = home_count + 1
        match_counts[away_key] = away_count + 1

    with _RUNTIME_CACHE_LOCK:
        state["elo_signature"] = signature
        state["elo_adjustments"] = dict(adjustments)
        state["elo_ratings"] = dict(ratings)
        state["elo_match_counts"] = dict(match_counts)
    return adjustments, ratings, match_counts, False


def team_form_signature(
    past: list[dict[str, Any]], team_id: int,
    elo_adjustments: dict[tuple[tuple[str, int, int, int], int], float],
) -> str:
    recent = sorted(past, key=lambda item: item["date"], reverse=True)[:RECENT_MATCH_LIMIT]
    payload = [
        (
            *_result_identity(item), int(item["home_goals"]), int(item["away_goals"]),
            round(float(elo_adjustments.get((_result_identity(item), team_id), 0.0)), 4),
        )
        for item in recent
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def competition_baseline_signature(
    results: list[dict[str, Any]], competition_id: int, game_date: str,
) -> str:
    sample = sorted(
        (
            item for item in results
            if int(item["competition"]["id"]) == int(competition_id)
            and item["date"] < game_date
        ),
        key=lambda item: item["date"], reverse=True,
    )[:300]
    payload = [
        (*_result_identity(item), int(item["home_goals"]), int(item["away_goals"]))
        for item in sample
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def reusable_snapshot_model_cache(
    snapshot: dict[str, Any] | None, save_instance_id: str | None, game_date: str,
    *, process_id: int | None = None, module_base: int | None = None,
) -> tuple[dict[tuple[int, int, str, str], dict[str, Any]], dict[tuple[int, str], dict[str, Any]]]:
    """Reuse profiles across game dates when their live/input signatures match."""
    previous = snapshot or {}
    cache = previous.get("model_cache") or {}
    if (
        int(cache.get("version") or 0) != MODEL_CACHE_VERSION
        or str(previous.get("model_version") or "") != MODEL_VERSION
        or not save_identity_matches(
            str(previous.get("save_instance_id") or ""), str(save_instance_id or ""),
        )
        or process_id is not None
        and int(cache.get("process_id") or 0) != int(process_id)
        or module_base is not None
        and str(cache.get("module_base") or "").casefold() != hex(int(module_base)).casefold()
    ):
        return {}, {}
    profiles: dict[tuple[int, int, str, str], dict[str, Any]] = {}
    for item in cache.get("profiles") or []:
        try:
            key = (
                int(str(item["team_address"]), 16), int(item["team_id"]),
                str(item["form_signature"]), str(item["roster_signature"]),
            )
            profile = item.get("profile")
            if isinstance(profile, dict):
                profiles[key] = dict(profile)
        except (KeyError, TypeError, ValueError):
            continue
    baselines: dict[tuple[int, str], dict[str, Any]] = {}
    for item in cache.get("baselines") or []:
        try:
            key = (int(item["competition_id"]), str(item["result_signature"]))
            baseline = item.get("baseline")
            if isinstance(baseline, dict):
                baselines[key] = dict(baseline)
        except (KeyError, TypeError, ValueError):
            continue
    return profiles, baselines


def cached_team_profile(
    reader: Reader, game_date: str, team_address: int, team_id: int, past: list[dict[str, Any]],
    elo_adjustments: dict[tuple[tuple[str, int, int, int], int], float],
    persisted: dict[tuple[int, int, str, str], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    state = _runtime_state(reader)
    form_signature = team_form_signature(past, team_id, elo_adjustments)
    roster_snapshot = getattr(reader, "roster_model_snapshot", None)
    if callable(roster_snapshot):
        roster_signature, squad = roster_snapshot(team_address)
    else:
        roster_signature = reader.roster_model_signature(team_address)
        squad = None
    reusable_key = (team_address, team_id, form_signature, roster_signature)
    key = (game_date, *reusable_key)
    with _RUNTIME_CACHE_LOCK:
        profile = state["profiles"].get(key)
    if profile is not None:
        return profile, True, {
            "form_signature": form_signature, "roster_signature": roster_signature,
        }
    profile = (persisted or {}).get(reusable_key)
    if profile is not None:
        profile = dict(profile)
        with _RUNTIME_CACHE_LOCK:
            state["profiles"][key] = profile
        return profile, True, {
            "form_signature": form_signature, "roster_signature": roster_signature,
        }
    profile = team_profile(
        reader, team_address, team_id, past, elo_adjustments, squad=squad,
    )
    with _RUNTIME_CACHE_LOCK:
        state["profiles"][key] = profile
    return profile, False, {
        "form_signature": form_signature, "roster_signature": roster_signature,
    }


def enrich_competition_format_team_profiles(
    reader: Reader,
    competition_formats: list[dict[str, Any]],
    game_date: str,
    by_team: dict[int, list[dict[str, Any]]],
    elo_adjustments: dict[tuple[tuple[str, int, int, int], int], float],
    profiles: dict[tuple[int, int], dict[str, Any]],
    profile_metadata: dict[tuple[int, int], dict[str, Any]],
    persisted_profiles: dict[tuple[int, int, str, str], dict[str, Any]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> int:
    """Populate complete cup fields independently of the fixture date window."""
    cache_hits = 0
    for format_row in competition_formats:
        opening_teams = format_row.get("opening_teams") or []
        expected = int(format_row.get("opening_slot_count") or 0)
        round_counts = [
            int(value) for value in format_row.get("knockout_round_tie_counts") or []
            if int(value) >= 0
        ]
        terminal_stage = bool(format_row.get("has_terminal_cup_stage"))
        provisional_group_stage = bool(
            not terminal_stage
            and str(format_row.get("opening_stage_type") or "") == "group"
            and expected >= 16
        )
        if (
            not format_row.get("opening_field_complete")
            or not (terminal_stage or provisional_group_stage)
            or expected < 2 or expected > 256
            or len(opening_teams) != expected
            or terminal_stage
            and next((value for value in reversed(round_counts) if value > 0), 0) != 1
        ):
            continue
        for team in opening_teams:
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            team_id = int(team.get("id") or 0)
            try:
                team_address = int(str(team.get("address") or "0"), 16)
            except (TypeError, ValueError):
                team_address = 0
            if not team_id or not team_address:
                continue
            key = (team_address, team_id)
            if key not in profiles:
                profile, hit, metadata = cached_team_profile(
                    reader, game_date, team_address, team_id,
                    by_team.get(team_id, []), elo_adjustments,
                    persisted=persisted_profiles,
                )
                profiles[key] = profile
                profile_metadata[key] = metadata
                cache_hits += int(hit)
            team["profile"] = dict(profiles[key])
    return cache_hits


def cached_competition_baseline(
    reader: Reader, results: list[dict[str, Any]], competition_id: int, game_date: str,
    persisted: dict[tuple[int, str], dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    state = _runtime_state(reader)
    result_signature = competition_baseline_signature(results, competition_id, game_date)
    reusable_key = (competition_id, result_signature)
    key = (game_date, *reusable_key)
    with _RUNTIME_CACHE_LOCK:
        baseline = state["baselines"].get(key)
    if baseline is not None:
        return baseline, True, result_signature
    baseline = (persisted or {}).get(reusable_key)
    if baseline is not None:
        baseline = dict(baseline)
        with _RUNTIME_CACHE_LOCK:
            state["baselines"][key] = baseline
        return baseline, True, result_signature
    baseline = competition_goal_baseline(results, competition_id, game_date)
    with _RUNTIME_CACHE_LOCK:
        state["baselines"][key] = baseline
    return baseline, False, result_signature

WOMENS_MARKERS = ("女足", "女子", "women", "woman", "femin", "frauen")
NATIONAL_MARKERS = (
    "世界杯", "欧洲杯", "亚洲杯", "美洲杯", "非洲国家杯", "金杯", "国家联赛",
    "世预赛", "欧预赛", "亚洲预选赛", "国际友谊赛", "欧国联", "非洲杯", "美加联",
    "world cup", "nations league",
)

# The national-team schedule is deliberately narrow. Other continental cups,
# friendlies and regional Nations Leagues are not part of the betting catalogue.
ALLOWED_NATIONAL_MARKERS = (
    "世界杯", "世预赛", "欧洲杯", "欧洲足球锦标赛", "欧国联", "美洲杯", "南美足球锦标赛", "亚洲杯",
    "东南亚杯", "西亚杯", "非洲杯", "阿拉伯杯", "大洋洲杯", "加勒比海杯", "欧美杯",
    "中北美及加勒比国家联赛", "中北美洲及加勒比海地区国家联赛", "金杯赛",
    "world cup", "euro", "nations league", "copa america", "asian cup",
    "africa cup", "african cup", "arab cup", "gold cup", "oceania cup", "caribbean cup", "asean cup",
)

# Qualifiers remain valid ordinary match markets, but they do not have a
# single competition champion.  Championship discovery uses this classifier
# without changing the ordinary betting catalogue.  Name matching is only a
# fallback for native competitions whose metadata has no dedicated flag.
NATIONAL_QUALIFICATION_MARKERS = (
    "预选赛", "资格赛", "预选", "qualifier", "qualification", "qualifying",
)
NATIONAL_QUALIFICATION_COMPETITION_IDS = {100102, 100103}

CONTINENTAL_CLUB_MARKERS = (
    "欧冠", "欧女冠", "欧洲冠军联赛", "欧联", "欧协联", "欧洲协会联赛",
    "亚冠", "亚洲冠军联赛", "亚足联冠军", "亚洲足联冠军",
    "北美冠军", "北美洲冠军", "中北美冠军", "中北美洲冠军",
    "中北美及加勒比海冠军", "中北美洲及加勒比海冠军",
    "非洲冠军联赛", "非洲足联冠军", "南美解放者杯", "南美杯",
    "大洋洲冠军联赛", "洲际俱乐部",
    "uefa champions league", "uefa europa league", "uefa conference league",
    "europa conference league", "afc champions league", "concacaf champions",
    "caf champions league", "copa libertadores", "copa sudamericana",
    "ofc champions league", "north american champions league", "liga de campeones",
)
CLUB_CUP_MARKERS = ("联赛杯", "杯", "cup", "trophy")
DOMESTIC_LEAGUE_MARKERS = (
    "联赛", "league", "championship", "bundesliga", "serie ",
    "liga", "eredivisie", "premier", "大联盟",
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def competition_name(competition: dict[str, Any]) -> str:
    return competition.get("short_name") or competition.get("name") or str(competition.get("id", ""))


def has_resolved_team_name(team: dict[str, Any]) -> bool:
    name = str(team.get("short_name") or team.get("name") or "").strip()
    return bool(name) and not bool(re.fullmatch(r"国家队\s+\d+", name))


def canonical_team(team: dict[str, Any]) -> dict[str, Any]:
    canonical_name = NATION_NAMES.get(int(team.get("id", 0)))
    return {**team, "name": canonical_name} if canonical_name else team


def is_womens_competition(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in WOMENS_MARKERS)


def is_national_competition(name: str) -> bool:
    lowered = name.lower()
    if "俱乐部" in lowered:
        return False
    if any(marker in lowered for marker in ALLOWED_NATIONAL_MARKERS):
        return True
    return False


def is_national_qualification_competition(
    name: str, competition_id: int | None = None,
) -> bool:
    """Return whether a competition is a national-team qualification event."""
    try:
        if int(competition_id or 0) in NATIONAL_QUALIFICATION_COMPETITION_IDS:
            return True
    except (TypeError, ValueError):
        pass
    lowered = str(name or "").casefold()
    if "俱乐部" in lowered or not any(
        marker.casefold() in lowered for marker in NATIONAL_QUALIFICATION_MARKERS
    ):
        return False
    # A generic club playoff/qualification name should not be affected.  The
    # national marker list is the native-name fallback when competition type
    # metadata is incomplete.
    return is_national_competition(name)


def club_competition_kind(name: str) -> str:
    lowered = str(name or "").casefold()
    if any(marker.casefold() in lowered for marker in CONTINENTAL_CLUB_MARKERS):
        return "cup"
    if any(marker.casefold() in lowered for marker in CLUB_CUP_MARKERS):
        return "cup"
    return "league" if any(
        marker.casefold() in lowered for marker in DOMESTIC_LEAGUE_MARKERS
    ) else "cup"


def is_runtime_generated_competition_id(competition_id: int) -> bool:
    """Return whether FM may reuse this save-local competition ID."""
    return str(int(competition_id)).startswith("2000")


def competition_metadata(
    competition_id: int, name: str | None = None, *, national_teams: bool = False,
) -> dict[str, Any] | None:
    supplied_name = str(name or "").strip()
    fixed_metadata = (
        None if is_runtime_generated_competition_id(competition_id)
        else WATCHED_COMPETITIONS.get(competition_id)
    )
    id_only_name = bool(
        not supplied_name
        or supplied_name == str(competition_id)
        or bool(re.fullmatch(r"\d+", supplied_name))
    )
    unresolved_name = bool(
        fixed_metadata is None and id_only_name
    )
    if unresolved_name and national_teams:
        return {
            "id": competition_id,
            "name": "国家队比赛",
            "kind": "national",
            "reputation": 84,
        }
    if unresolved_name:
        return {
            "id": competition_id,
            "name": "友谊赛",
            "kind": "friendly",
            "reputation": 0,
        }
    display_name = (
        fixed_metadata[0]
        if id_only_name and fixed_metadata is not None
        else supplied_name or (fixed_metadata or (str(competition_id), "cup", 50))[0]
    )
    if is_womens_competition(display_name):
        return None
    if national_teams:
        reputation = 96 if "世界杯" in display_name else 91 if any(term in display_name for term in ("欧洲杯", "美洲杯", "非洲杯", "亚洲杯")) else 84
        return {"id": competition_id, "name": display_name, "kind": "national", "reputation": reputation}
    if fixed_metadata is not None:
        stored_name, kind, reputation = fixed_metadata
        return {"id": competition_id, "name": display_name or stored_name, "kind": kind, "reputation": reputation}
    if not is_national_competition(display_name):
        lowered = display_name.casefold()
        kind = club_competition_kind(display_name)
        reputation = 88 if any(marker in lowered for marker in ("英格兰冠军", "championship")) else 60
        return {"id": competition_id, "name": display_name, "kind": kind, "reputation": reputation}
    reputation = 96 if "世界杯" in display_name else 91 if any(term in display_name for term in ("欧洲杯", "美洲杯")) else 86
    return {"id": competition_id, "name": display_name, "kind": "national", "reputation": reputation}


def competition_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    kind_order = {"league": 0, "cup": 1, "national": 2, "friendly": 3}
    return (kind_order.get(item["kind"], 3), -int(item["reputation"]), item["name"])


def is_famous_competition(meta: dict[str, Any]) -> bool:
    if int(meta.get("id") or 0) in FAMOUS_COMPETITION_IDS:
        return True
    name = str(meta.get("name") or "").casefold()
    if str(meta.get("kind") or "") == "national":
        return True
    return any(
        marker.casefold() in name
        for marker in (*FAMOUS_COMPETITION_MARKERS, *CONTINENTAL_CLUB_MARKERS)
    )


def filter_fixture_rows_for_odds(
    fixture_rows: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]],
    competition_scope: str, requested_end: Any,
    managed_competition_ids: set[int] | None = None,
    managed_team_ids: set[int] | None = None,
) -> list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    in_window = [row for row in fixture_rows if row[0].match_date <= requested_end]
    if competition_scope == "all":
        return in_window
    managed_teams = {int(value) for value in (managed_team_ids or set())}
    involves_managed_team = lambda row: bool(
        {int(row[2]["id"]), int(row[3]["id"])} & managed_teams
    )
    if competition_scope == "famous":
        allowed = set(managed_competition_ids or ())
        return [
            row for row in in_window
            if (
                is_famous_competition(row[1])
                or int(row[1]["id"]) in allowed
                or involves_managed_team(row)
            )
        ]
    allowed = set(managed_competition_ids or ())
    return [
        row for row in in_window
        if int(row[1]["id"]) in allowed or involves_managed_team(row)
    ]


def favorite_schedule_scope_from_fixture_headers(
    reader: Reader,
    fixtures: list[Any],
    favorite_team_ids: set[int],
    season_start: str,
    season_end: str,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[set[int], set[int]]:
    """Resolve favorite-team competition seasons without expanding every fixture.

    Fixture headers already contain team and competition-season pointers.  Read
    only the small UID prefix for each unique team, validate the handful of
    favorite matches as real club objects, then expand only their competition
    seasons.  This keeps unrelated fixture teams out of the expensive object
    and roster paths while retaining a lightweight whole-schedule check for new
    cup draws.
    """
    favorite_ids = {
        int(value) for value in favorite_team_ids if int(value) > 0
    }
    if not favorite_ids:
        return set(), set()
    candidates = [
        fixture for fixture in fixtures
        if season_start <= fixture.match_date.isoformat() <= season_end
    ]
    team_addresses = {
        int(address)
        for fixture in candidates
        for address in (fixture.home_team, fixture.away_team)
        if int(address) > 0
    }
    snapshots = reader._fixed_size_snapshots(
        team_addresses, ENTITY_UID + 4,
    )
    uid_by_address = {
        address: int(struct.unpack_from("<I", raw, ENTITY_UID)[0])
        for address, raw in snapshots.items()
        if len(raw) >= ENTITY_UID + 4
    }
    favorite_addresses: set[int] = set()
    for address, uid in uid_by_address.items():
        if uid not in favorite_ids:
            continue
        team = reader.team(address)
        if (
            team
            and int(team.get("id") or 0) == uid
            and str(team.get("team_type") or "club") == "club"
        ):
            favorite_addresses.add(address)

    season_addresses: set[int] = set()
    total = len(candidates)
    for index, fixture in enumerate(candidates, start=1):
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        if {
            int(fixture.home_team), int(fixture.away_team),
        } & favorite_addresses:
            season_addresses.add(int(fixture.competition_season or 0))
        if progress_callback:
            progress_callback(index, total)
    season_addresses.discard(0)

    competition_ids: set[int] = set()
    for season_address in season_addresses:
        competition = reader.competition(season_address)
        competition_id = int((competition or {}).get("id") or 0)
        if competition_id > 0:
            competition_ids.add(competition_id)
    return season_addresses, competition_ids


def hidden_competition_seasons_from_fixture_headers(
    reader: Reader,
    fixtures: list[Any],
    hidden_competition_ids: set[int],
    cancel_check: Callable[[], bool] | None = None,
) -> set[int]:
    """Map blacklisted competition IDs to live season pointers before team reads."""
    hidden_ids = {
        int(value) for value in hidden_competition_ids if int(value) > 0
    }
    if not hidden_ids:
        return set()
    season_addresses = sorted({
        int(fixture.competition_season or 0)
        for fixture in fixtures
        if int(fixture.competition_season or 0) > 0
    })
    if cancel_check and cancel_check():
        raise RuntimeError("refresh cancelled")
    expected_vtable = reader.module_base + reader.layout.competition_vtable_rva
    season_snapshots = reader._fixed_size_snapshots(season_addresses, 0x20)
    actual_by_season: dict[int, int] = {}
    direct_snapshots: dict[int, bytes] = {}
    for season_address, raw in season_snapshots.items():
        if len(raw) < 0x20:
            continue
        if int(struct.unpack_from("<Q", raw, 0)[0]) == expected_vtable:
            actual_by_season[season_address] = season_address
            direct_snapshots[season_address] = raw
            continue
        nested = int(struct.unpack_from("<Q", raw, 0x18)[0])
        if nested > 0:
            actual_by_season[season_address] = nested
    nested_addresses = {
        actual for season, actual in actual_by_season.items()
        if season not in direct_snapshots
    }
    actual_snapshots = (
        reader._fixed_size_snapshots(nested_addresses, ENTITY_UID + 4)
        if nested_addresses else {}
    )
    hidden_seasons: set[int] = set()
    for season_address, actual in actual_by_season.items():
        raw = direct_snapshots.get(season_address) or actual_snapshots.get(actual)
        if (
            not raw
            or len(raw) < ENTITY_UID + 4
            or int(struct.unpack_from("<Q", raw, 0)[0]) != expected_vtable
        ):
            continue
        competition_id = int(struct.unpack_from("<I", raw, ENTITY_UID)[0])
        if competition_id in hidden_ids:
            hidden_seasons.add(season_address)
    return hidden_seasons


def fixture_is_open(
    fixture: Any,
    game_date: Any,
    game_minutes: int | None = None,
) -> bool:
    """Return whether a fixture is still at or before its scheduled kickoff."""
    if fixture.match_date < game_date:
        return False
    if (
        fixture.match_date == game_date
        and game_minutes is not None
        and fixture.kickoff_minutes is not None
        and int(fixture.kickoff_minutes) < int(game_minutes)
    ):
        return False
    return True


def cached_managed_schedule_competitions(
    reader: Reader, fixture_addresses: list[int],
    fixture_rows: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]],
    results: list[dict[str, Any]], managed_teams: list[dict[str, Any]],
    save_instance_id: str | None, season_start: str, season_end: str,
    cancel_check: Callable[[], bool] | None = None,
    fixture_snapshots: dict[int, bytes] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
) -> tuple[set[int], bool]:
    managed_club_ids = {
        int(item.get("id") or 0)
        for item in managed_teams
        if str(item.get("team_type") or "club") == "club"
    }
    if not managed_club_ids:
        return set(), True
    key = (str(save_instance_id or ""), tuple(sorted(managed_club_ids)), season_start, season_end)
    state = _runtime_state(reader)
    with _RUNTIME_CACHE_LOCK:
        cached = state["managed_schedule_competitions"].get(key)
    cache_hit = cached is not None
    competition_ids = set(cached or ())
    if not cache_hit:
        for result in results:
            if not season_start <= str(result.get("date") or "") <= season_end:
                continue
            result_teams = {
                int((result.get("home_team") or {}).get("id") or 0),
                int((result.get("away_team") or {}).get("id") or 0),
            }
            if result_teams & managed_club_ids:
                competition_ids.add(int((result.get("competition") or {}).get("id") or 0))
        snapshots = fixture_snapshots
        if snapshots is None:
            snapshots, _header_bytes = read_fixture_snapshots_at_addresses(
                reader, fixture_addresses,
            )
        team_uid_cache: dict[int, int] = {}

        def team_uid(address: int) -> int:
            if address not in team_uid_cache:
                team_uid_cache[address] = int(
                    reader.u32(address + ENTITY_UID) or 0
                )
            return team_uid_cache[address]

        total_addresses = len(fixture_addresses)
        for index, address in enumerate(fixture_addresses, start=1):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            fixture = parse_fixture_from_raw(
                reader, address, snapshots.get(address),
                validate_references=False,
            )
            if not fixture or not season_start <= fixture.match_date.isoformat() <= season_end:
                if progress_callback:
                    progress_callback(index, total_addresses)
                continue
            fixture_teams = {
                team_uid(fixture.home_team), team_uid(fixture.away_team),
            }
            if not fixture_teams & managed_club_ids:
                if progress_callback:
                    progress_callback(index, total_addresses)
                continue
            home = reader.team(fixture.home_team)
            away = reader.team(fixture.away_team)
            if (
                not home or not away
                or {int(home["id"]), int(away["id"])} != fixture_teams
            ):
                if progress_callback:
                    progress_callback(index, total_addresses)
                continue
            competition = reader.competition(fixture.competition_season)
            if competition and int(competition.get("id") or 0) > 0:
                competition_ids.add(int(competition["id"]))
            if progress_callback:
                progress_callback(index, total_addresses)
    # Newly drawn cup fixtures are already present in the current short window;
    # merge them without repeating the full-season discovery scan.
    for _fixture, meta, home, away in fixture_rows:
        if {int(home["id"]), int(away["id"])} & managed_club_ids:
            competition_ids.add(int(meta["id"]))
    competition_ids.discard(0)
    with _RUNTIME_CACHE_LOCK:
        state["managed_schedule_competitions"][key] = sorted(competition_ids)
    return competition_ids, cache_hit


def collapse_adjacent_duplicate_fixtures(
    rows: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Canonicalize cloned FM fixture objects, including rescheduled copies.

    FM keeps the same fixture in several heap-owned views. After a one-day
    reschedule, an older clone can retain the former date. The team and
    competition object pointers plus the native round/slot remain stable, so
    use those fields as the fixture identity and retain the newest adjacent
    date. Gaps longer than one day remain separate to avoid hiding a replay.
    """
    grouped: dict[tuple[int, int, int, int], list[Any]] = defaultdict(list)
    for row in rows:
        fixture, competition, _home, _away = row
        identity = (
            int(competition["id"]), int(fixture.home_team),
            int(fixture.away_team), int(fixture.round_or_slot),
        )
        grouped[identity].append(row)

    retained = []
    for group in grouped.values():
        versions = sorted(group, key=lambda item: (item[0].match_date, item[0].address))
        cluster = [versions[0]]
        for row in versions[1:]:
            if (row[0].match_date - cluster[-1][0].match_date).days <= 1:
                cluster.append(row)
                continue
            retained.append(cluster[-1])
            cluster = [row]
        retained.append(cluster[-1])
    return sorted(retained, key=lambda item: (item[0].match_date, item[0].address))


def read_upcoming_fixture_snapshot(
    days: int = 14, *, competition_scope: str = "all",
    visible_competition_ids: set[int] | None = None,
    managed_team_ids: set[int] | None = None,
    hidden_competition_ids: set[int] | None = None,
    game_minutes: int | None = None,
) -> dict[str, Any]:
    """Build a lightweight schedule fingerprint from already discovered fixtures.

    This normally reparses known fixture objects. The address cache expires on
    a bounded interval so newly allocated fixtures are eventually discovered
    without sweeping process memory on every probe.
    """
    if days not in {3, 7, 14}:
        raise ValueError(f"unsupported odds window: {days}")
    if competition_scope not in ODDS_SCOPE_OPTIONS:
        raise ValueError(f"unknown competition scope: {competition_scope}")
    allowed_competitions = {int(value) for value in (visible_competition_ids or set())}
    managed_ids = {int(value) for value in (managed_team_ids or set())}
    hidden_ids = {int(value) for value in (hidden_competition_ids or set())}
    with open_supported_reader() as (_process_path, _layout, _module, reader):
        game_date = decode_date(read_layout_game_date_code(reader))
        if not game_date:
            raise RuntimeError("game date unavailable")
        end_date = game_date + timedelta(days=days)
        fixture_addresses, cache_hit = cached_fixture_addresses(reader, force_scan=False)
        fixture_snapshots, _header_bytes = read_fixture_snapshots_at_addresses(
            reader, fixture_addresses,
        )
        fixture_rows = []
        for address in fixture_addresses:
            fixture = parse_fixture_from_raw(
                reader, address, fixture_snapshots.get(address),
                validate_references=False,
            )
            if not fixture or not fixture_is_open(fixture, game_date, game_minutes) or fixture.match_date > end_date:
                continue
            competition = reader.competition(fixture.competition_season)
            if int((competition or {}).get("id") or 0) in hidden_ids:
                continue
            home = reader.team(fixture.home_team)
            away = reader.team(fixture.away_team)
            if not competition or not home or not away:
                continue
            if not has_resolved_team_name(home) or not has_resolved_team_name(away):
                continue
            meta = competition_metadata(
                competition["id"], competition_name(competition),
                national_teams=(
                    home.get("team_type") == "national"
                    and away.get("team_type") == "national"
                ),
            )
            if not meta:
                continue
            competition_id = int(meta["id"])
            team_ids = {int(home["id"]), int(away["id"])}
            if (
                competition_scope == "famous"
                and not is_famous_competition(meta)
                and competition_id not in allowed_competitions
                and not team_ids & managed_ids
            ):
                continue
            if (
                competition_scope in {"favorite_schedule", "managed_schedule"}
                and competition_id not in allowed_competitions
                and not team_ids & managed_ids
            ):
                continue
            fixture_rows.append((fixture, meta, home, away))
        rows = sorted({
            (
                fixture.match_date.isoformat(), int(meta["id"]),
                int(home["id"]), int(away["id"]),
                int(fixture.kickoff_minutes) if fixture.kickoff_minutes is not None else -1,
            )
            for fixture, meta, home, away in collapse_adjacent_duplicate_fixtures(fixture_rows)
        })
        encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        return {
            "game_date": game_date.isoformat(),
            "fingerprint": hashlib.sha256(encoded).hexdigest(),
            "fixtures": rows,
            "cache_hit": cache_hit,
        }


def quote_decimal_odds(value: float, *, asian: bool = False) -> float:
    """Return the executable price shared by every betting market."""
    del asian  # Retained for call-site compatibility; every market uses one ladder.
    clipped = max(1.01, float(value))
    increment = (
        10.0 if clipped > 100.0
        else 5.0 if clipped > 50.0
        else 1.0 if clipped > 10.0
        else 0.5 if clipped > 5.0
        else 0.01
    )
    tick = Decimal(str(increment))
    units = (Decimal(str(clipped)) / tick).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP,
    )
    quoted = float(units * tick)
    return max(1.01, round(quoted, 2))


def fair_odds(weights: tuple[float, float, float]) -> float | None:
    win, push, loss = weights
    return quote_decimal_odds(1.0 + loss / win, asian=True) if win > 0 else None


def decimal_odds(probability: float, *, asian: bool = False) -> float | None:
    return quote_decimal_odds(1 / probability, asian=asian) if probability > 0 else None


def exact_goals_odds(probability: float) -> float:
    fair = 1 / probability if probability > 0 else 1.01
    return quote_decimal_odds(fair / EXACT_GOALS_ODDS_DIVISOR)


def exact_score_grid_mass(matrix: list[list[float]], max_goals: int = 5) -> float:
    """Probability mass of the full-time score grid actually offered (0..max_goals)."""
    return sum(
        matrix[home][away]
        for home in range(max_goals + 1)
        for away in range(max_goals + 1)
    )


def exact_score_divisor(
    covered_mass: float,
    zero_probability: float,
    target_return_rate: float = EXACT_SCORE_TARGET_RETURN_RATE,
) -> float:
    """Divisor for non-zero full-time score cells that makes the covered 0-5
    grid return `target_return_rate` while 0-0 keeps EXACT_GOALS_ODDS_DIVISOR."""
    nonzero_mass = covered_mass - zero_probability
    return (
        1.0 / target_return_rate
        - EXACT_GOALS_ODDS_DIVISOR * zero_probability
    ) / nonzero_mass


def bookmaker_prices(
    probabilities: dict[str, float], margin: float, *, asian: bool = False,
) -> dict[str, float | None]:
    """Allocate an exact overround with a mild favourite-longshot bias."""
    weights = {key: max(value, 0.0) ** MARGIN_WEIGHT_POWER for key, value in probabilities.items()}
    total_weight = sum(weights.values())
    return {
        key: decimal_odds(
            value + margin * weights[key] / total_weight, asian=asian,
        )
        for key, value in probabilities.items()
    }


def bookmaker_weighted_odds(weights: tuple[float, float, float], margin: float = TWO_WAY_MARGIN) -> float | None:
    win, push, loss = weights
    active = win + loss
    if active <= 0 or win <= 0:
        return None
    win_probability = win / active
    quoted = bookmaker_prices(
        {"win": win_probability, "loss": 1 - win_probability},
        margin,
        asian=True,
    )
    return quoted["win"]


def match_outcome(home_goals: int, away_goals: int) -> str:
    return "home" if home_goals > away_goals else "away" if away_goals > home_goals else "draw"


def half_time_markets(home_xg: float, away_xg: float) -> dict[str, Any]:
    """Price first-half and half-time/full-time markets from one joint model."""
    half_xg = {
        "home": round(home_xg * FIRST_HALF_GOAL_SHARE, 3),
        "away": round(away_xg * FIRST_HALF_GOAL_SHARE, 3),
    }
    second_xg = {
        "home": max(0.01, home_xg - half_xg["home"]),
        "away": max(0.01, away_xg - half_xg["away"]),
    }
    half_matrix = score_matrix(half_xg["home"], half_xg["away"])
    second_matrix = score_matrix(second_xg["home"], second_xg["away"], rho=0.0)
    half_probabilities = {"home": 0.0, "draw": 0.0, "away": 0.0}
    second_probabilities = {"home": 0.0, "draw": 0.0, "away": 0.0}
    for half_home, row in enumerate(half_matrix):
        for half_away, probability in enumerate(row):
            half_probabilities[match_outcome(half_home, half_away)] += probability

    half_full_probabilities = {
        f"{half}_{full}": 0.0
        for half in ("home", "draw", "away")
        for full in ("home", "draw", "away")
    }
    second_difference_probabilities: dict[int, float] = defaultdict(float)
    half_total_probabilities: dict[int, float] = defaultdict(float)
    second_total_probabilities: dict[int, float] = defaultdict(float)
    for half_home, half_row in enumerate(half_matrix):
        for half_away, half_probability in enumerate(half_row):
            half_total_probabilities[half_home + half_away] += half_probability
    for second_home, second_row in enumerate(second_matrix):
        for second_away, second_probability in enumerate(second_row):
            second_probabilities[match_outcome(second_home, second_away)] += second_probability
            second_difference_probabilities[second_home - second_away] += second_probability
            second_total_probabilities[second_home + second_away] += second_probability
    for half_home, half_row in enumerate(half_matrix):
        for half_away, half_probability in enumerate(half_row):
            half_outcome = match_outcome(half_home, half_away)
            half_difference = half_home - half_away
            for second_difference, second_probability in second_difference_probabilities.items():
                full_difference = half_difference + second_difference
                full_outcome = "home" if full_difference > 0 else "away" if full_difference < 0 else "draw"
                half_full_probabilities[f"{half_outcome}_{full_outcome}"] += (
                    half_probability * second_probability
                )

    highest_scoring_half_probabilities = {"first": 0.0, "second": 0.0, "equal": 0.0}
    for half_total, half_probability in half_total_probabilities.items():
        for second_total, second_probability in second_total_probabilities.items():
            code = "first" if half_total > second_total else "second" if second_total > half_total else "equal"
            highest_scoring_half_probabilities[code] += half_probability * second_probability
    half_handicap, half_home_odds, half_away_odds = choose_asian_handicap(half_matrix)
    half_total, half_over_odds, half_under_odds = choose_half_total_line(half_matrix)
    second_handicap, second_home_odds, second_away_odds = choose_asian_handicap(second_matrix)
    second_total, second_over_odds, second_under_odds = choose_half_total_line(second_matrix)
    half_casino_1x2 = bookmaker_prices(
        half_probabilities, HALF_MARKET_ONE_X_TWO_MARGIN,
    )
    second_casino_1x2 = bookmaker_prices(
        second_probabilities, HALF_MARKET_ONE_X_TWO_MARGIN,
    )
    half_handicap_options = asian_handicap_options(
        half_matrix, half_handicap, margin=HALF_MARKET_TWO_WAY_MARGIN,
        equivalent_1x2_prices=half_casino_1x2,
    )
    second_handicap_options = asian_handicap_options(
        second_matrix, second_handicap, margin=HALF_MARKET_TWO_WAY_MARGIN,
        equivalent_1x2_prices=second_casino_1x2,
    )
    half_main_handicap = next(item for item in half_handicap_options if item["is_main"])
    second_main_handicap = next(item for item in second_handicap_options if item["is_main"])

    return {
        "xg": half_xg,
        "fair_1x2": {
            key: decimal_odds(probability) for key, probability in half_probabilities.items()
        },
        "casino_1x2": half_casino_1x2,
        "asian_handicap": {
            "home_line": half_handicap,
            "home_odds": half_main_handicap["home_odds"],
            "fair_home_odds": half_home_odds,
            "away_line": -half_handicap,
            "away_odds": half_main_handicap["away_odds"],
            "fair_away_odds": half_away_odds,
        },
        "handicap_options": half_handicap_options,
        "total_goals": {
            "line": half_total,
            "over_odds": bookmaker_weighted_odds(
                total_weights(half_matrix, half_total, True), HALF_MARKET_TWO_WAY_MARGIN,
            ),
            "fair_over_odds": half_over_odds,
            "under_odds": bookmaker_weighted_odds(
                total_weights(half_matrix, half_total, False), HALF_MARKET_TWO_WAY_MARGIN,
            ),
            "fair_under_odds": half_under_odds,
        },
        "total_options": asian_total_options(
            half_matrix, half_total, minimum_line=0.25,
            margin=HALF_MARKET_TWO_WAY_MARGIN,
        ),
        "fair_half_full": {
            key: decimal_odds(probability)
            for key, probability in half_full_probabilities.items()
        },
        "half_full": bookmaker_prices(half_full_probabilities, HALF_FULL_MARGIN),
        "fair_highest_scoring_half": {
            key: decimal_odds(probability)
            for key, probability in highest_scoring_half_probabilities.items()
        },
        "highest_scoring_half": bookmaker_prices(
            highest_scoring_half_probabilities, HIGHEST_SCORING_HALF_MARGIN,
        ),
        "second_half": {
            "xg": {"home": round(second_xg["home"], 3), "away": round(second_xg["away"], 3)},
            "fair_1x2": {
                key: decimal_odds(probability)
                for key, probability in second_probabilities.items()
            },
            "casino_1x2": second_casino_1x2,
            "asian_handicap": {
                "home_line": second_handicap,
                "home_odds": second_main_handicap["home_odds"],
                "fair_home_odds": second_home_odds,
                "away_line": -second_handicap,
                "away_odds": second_main_handicap["away_odds"],
                "fair_away_odds": second_away_odds,
            },
            "handicap_options": second_handicap_options,
            "total_goals": {
                "line": second_total,
                "over_odds": bookmaker_weighted_odds(
                    total_weights(second_matrix, second_total, True),
                    HALF_MARKET_TWO_WAY_MARGIN,
                ),
                "fair_over_odds": second_over_odds,
                "under_odds": bookmaker_weighted_odds(
                    total_weights(second_matrix, second_total, False),
                    HALF_MARKET_TWO_WAY_MARGIN,
                ),
                "fair_under_odds": second_under_odds,
            },
            "total_options": asian_total_options(
                second_matrix, second_total, minimum_line=0.25,
                margin=HALF_MARKET_TWO_WAY_MARGIN,
            ),
        },
    }


def _asian_handicap_candidates(matrix: list[list[float]]) -> list[float]:
    """Build quarter-goal handicap candidates from the score matrix support."""
    maximum_home_goals = max(0, len(matrix) - 1)
    maximum_away_goals = max(0, max((len(row) for row in matrix), default=1) - 1)
    return [
        step / 4
        for step in range(-maximum_home_goals * 4, maximum_away_goals * 4 + 1)
    ]


def choose_asian_handicap(matrix: list[list[float]]) -> tuple[float, float | None, float | None]:
    candidates = _asian_handicap_candidates(matrix)
    line = min(candidates, key=lambda value: abs(asian_weights(matrix, value, "home")[0] + asian_weights(matrix, value, "home")[1] / 2 - 0.5))
    return line, fair_odds(asian_weights(matrix, line, "home")), fair_odds(asian_weights(matrix, line, "away"))


def asian_handicap_options(
    matrix: list[list[float]], main_line: float | None = None, *, bookmaker: bool = True,
    margin: float = TWO_WAY_MARGIN,
    equivalent_1x2_prices: dict[str, float | None] | None = None,
) -> list[dict[str, float | None]]:
    """Price the main Asian handicap and the two adjacent quarter-lines on each side."""
    if main_line is None:
        main_line = choose_asian_handicap(matrix)[0]
    main_step = int(round(float(main_line) * 4))
    start = main_step - 2
    options = []
    for step in range(start, start + 5):
        line = step / 4
        home_weights = asian_weights(matrix, line, "home")
        away_weights = asian_weights(matrix, line, "away")
        home_odds = (
            bookmaker_weighted_odds(home_weights, margin) if bookmaker else fair_odds(home_weights)
        )
        away_odds = (
            bookmaker_weighted_odds(away_weights, margin) if bookmaker else fair_odds(away_weights)
        )
        # Winning at -0.5 and winning the match are the same settlement event.
        # Keep one executable quote after margin allocation and ladder rounding.
        if equivalent_1x2_prices:
            if line == -0.5 and equivalent_1x2_prices.get("home") is not None:
                home_odds = equivalent_1x2_prices["home"]
            if line == 0.5 and equivalent_1x2_prices.get("away") is not None:
                away_odds = equivalent_1x2_prices["away"]
        options.append({
            "home_line": line,
            "home_odds": home_odds,
            "fair_home_odds": fair_odds(home_weights),
            "away_line": -line,
            "away_odds": away_odds,
            "fair_away_odds": fair_odds(away_weights),
            "is_main": step == main_step,
        })
    return options


def _total_line_candidates(
    matrix: list[list[float]], minimum_line: float,
) -> list[float]:
    """Build quarter-goal candidates from the score matrix support."""
    maximum_home_goals = max(0, len(matrix) - 1)
    maximum_away_goals = max(0, max((len(row) for row in matrix), default=1) - 1)
    minimum_step = int(round(float(minimum_line) * 4))
    maximum_step = max(minimum_step, (maximum_home_goals + maximum_away_goals) * 4)
    return [step / 4 for step in range(minimum_step, maximum_step + 1)]


def choose_total_line(matrix: list[list[float]]) -> tuple[float, float | None, float | None]:
    candidates = _total_line_candidates(matrix, 1.5)
    line = min(candidates, key=lambda value: abs(total_weights(matrix, value, True)[0] + total_weights(matrix, value, True)[1] / 2 - 0.5))
    return line, fair_odds(total_weights(matrix, line, True)), fair_odds(total_weights(matrix, line, False))


def choose_half_total_line(matrix: list[list[float]]) -> tuple[float, float | None, float | None]:
    candidates = _total_line_candidates(matrix, 0.5)
    line = min(candidates, key=lambda value: abs(total_weights(matrix, value, True)[0] + total_weights(matrix, value, True)[1] / 2 - 0.5))
    return line, fair_odds(total_weights(matrix, line, True)), fair_odds(total_weights(matrix, line, False))


def asian_total_options(
    matrix: list[list[float]], main_line: float | None = None, *, bookmaker: bool = True,
    minimum_line: float = 1.0, margin: float = TWO_WAY_MARGIN,
) -> list[dict[str, float | bool | None]]:
    """Price the main total and the two adjacent quarter-lines on each side."""
    if main_line is None:
        main_line = choose_total_line(matrix)[0]
    main_step = int(round(float(main_line) * 4))
    minimum_step = int(round(minimum_line * 4))
    start = max(minimum_step, main_step - 2)
    options = []
    for step in range(start, start + 5):
        line = step / 4
        over_weights = total_weights(matrix, line, True)
        under_weights = total_weights(matrix, line, False)
        options.append({
            "line": line,
            "over_odds": (
                bookmaker_weighted_odds(over_weights, margin) if bookmaker else fair_odds(over_weights)
            ),
            "fair_over_odds": fair_odds(over_weights),
            "under_odds": (
                bookmaker_weighted_odds(under_weights, margin) if bookmaker else fair_odds(under_weights)
            ),
            "fair_under_odds": fair_odds(under_weights),
            "is_main": step == main_step,
        })
    return options


def team_total_options(
    matrix: list[list[float]], side: str, *, bookmaker: bool = True,
) -> list[dict[str, float | bool | None]]:
    maximum_goals = (
        max(0, len(matrix) - 1)
        if side == "home"
        else max(0, max((len(row) for row in matrix), default=1) - 1)
    )
    candidates = [step / 4 for step in range(1, maximum_goals * 4 + 1)]
    main_line = min(candidates, key=lambda value: abs(
        team_total_weights(matrix, value, side, True)[0]
        + team_total_weights(matrix, value, side, True)[1] / 2 - 0.5
    ))
    main_step = int(round(main_line * 4))
    start = max(1, main_step - 2)
    options = []
    for step in range(start, start + 5):
        line = step / 4
        over_weights = team_total_weights(matrix, line, side, True)
        under_weights = team_total_weights(matrix, line, side, False)
        options.append({
            "line": line,
            "over_odds": (
                bookmaker_weighted_odds(over_weights, TEAM_TOTAL_MARGIN)
                if bookmaker else fair_odds(over_weights)
            ),
            "fair_over_odds": fair_odds(over_weights),
            "under_odds": (
                bookmaker_weighted_odds(under_weights, TEAM_TOTAL_MARGIN)
                if bookmaker else fair_odds(under_weights)
            ),
            "fair_under_odds": fair_odds(under_weights),
            "is_main": step == main_step,
        })
    return options


def _binary_yes_price(probability: float, margin: float) -> float | None:
    return bookmaker_prices(
        {"yes": probability, "no": 1.0 - probability}, margin,
    )["yes"]


def additional_score_markets(matrix: list[list[float]]) -> dict[str, Any]:
    outcome_probabilities = {"home": 0.0, "draw": 0.0, "away": 0.0}
    winning_margin_probabilities = {
        "home_1": 0.0, "home_2": 0.0, "home_3_plus": 0.0,
        "draw": 0.0,
        "away_1": 0.0, "away_2": 0.0, "away_3_plus": 0.0,
    }
    home_clean_sheet = away_clean_sheet = 0.0
    home_win_to_nil = away_win_to_nil = 0.0
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            outcome_probabilities[match_outcome(home_goals, away_goals)] += probability
            difference = home_goals - away_goals
            margin_code = (
                "draw" if difference == 0
                else f"home_{difference}" if difference in {1, 2}
                else "home_3_plus" if difference >= 3
                else f"away_{-difference}" if difference in {-1, -2}
                else "away_3_plus"
            )
            winning_margin_probabilities[margin_code] += probability
            if away_goals == 0:
                home_clean_sheet += probability
                if home_goals > 0:
                    home_win_to_nil += probability
            if home_goals == 0:
                away_clean_sheet += probability
                if away_goals > 0:
                    away_win_to_nil += probability

    double_chance_probabilities = {
        "home_draw": outcome_probabilities["home"] + outcome_probabilities["draw"],
        "home_away": outcome_probabilities["home"] + outcome_probabilities["away"],
        "draw_away": outcome_probabilities["draw"] + outcome_probabilities["away"],
    }
    clean_sheet_probabilities = {
        "home_yes": home_clean_sheet,
        "home_no": 1.0 - home_clean_sheet,
        "away_yes": away_clean_sheet,
        "away_no": 1.0 - away_clean_sheet,
    }
    win_to_nil_probabilities = {
        "home": home_win_to_nil,
        "away": away_win_to_nil,
    }
    return {
        "fair_double_chance": {
            key: decimal_odds(probability)
            for key, probability in double_chance_probabilities.items()
        },
        "double_chance": {
            key: _binary_yes_price(probability, DOUBLE_CHANCE_MARGIN)
            for key, probability in double_chance_probabilities.items()
        },
        "fair_winning_margin": {
            key: decimal_odds(probability)
            for key, probability in winning_margin_probabilities.items()
        },
        "winning_margin": bookmaker_prices(
            winning_margin_probabilities, WINNING_MARGIN_MARGIN,
        ),
        "fair_clean_sheet": {
            key: decimal_odds(probability)
            for key, probability in clean_sheet_probabilities.items()
        },
        "clean_sheet": {
            key: _binary_yes_price(probability, CLEAN_SHEET_MARGIN)
            for key, probability in clean_sheet_probabilities.items()
        },
        "fair_win_to_nil": {
            key: decimal_odds(probability)
            for key, probability in win_to_nil_probabilities.items()
        },
        "win_to_nil": {
            key: _binary_yes_price(probability, CLEAN_SHEET_MARGIN)
            for key, probability in win_to_nil_probabilities.items()
        },
    }


def _score_difference_probabilities(matrix: list[list[float]]) -> dict[int, float]:
    output: dict[int, float] = defaultdict(float)
    for home_goals, row in enumerate(matrix):
        for away_goals, probability in enumerate(row):
            output[home_goals - away_goals] += probability
    return dict(output)


def knockout_market_prices(
    match: dict[str, Any], context: dict[str, Any],
) -> dict[str, Any] | None:
    """Price advancement and decision method from the remaining tie states."""
    home_id = int(match["home"]["id"])
    away_id = int(match["away"]["id"])
    tie_home_id = home_id if context["orientation"] == "first" else away_id
    tie_away_id = away_id if context["orientation"] == "first" else home_id
    home_xg = float(match["xg"]["home"])
    away_xg = float(match["xg"]["away"])
    current_matrix = context.get("current_score_matrix")
    if not isinstance(current_matrix, list) or not current_matrix:
        current_matrix = score_matrix(home_xg, away_xg)
    current_diff = _score_difference_probabilities(current_matrix)
    regulation: dict[int, float] = defaultdict(float)
    if int(context["leg_count"]) == 1:
        regulation.update(current_diff)
        deciding_home_xg, deciding_away_xg = home_xg, away_xg
        tie_home_is_deciding_home = True
    elif int(context["leg_index"]) == 1:
        first_xg = context.get("first_leg_xg") or {"home": home_xg, "away": away_xg}
        second_xg = context.get("second_leg_xg") or {"home": away_xg, "away": home_xg}
        first_diff = current_diff if context.get("current_score_matrix") else (
            _score_difference_probabilities(
                score_matrix(float(first_xg["home"]), float(first_xg["away"]))
            )
        )
        return_diff = _score_difference_probabilities(
            score_matrix(float(second_xg["home"]), float(second_xg["away"]))
        )
        for first_difference, first_probability in first_diff.items():
            for return_difference, return_probability in return_diff.items():
                regulation[first_difference - return_difference] += (
                    first_probability * return_probability
                )
        deciding_home_xg = float(second_xg["home"])
        deciding_away_xg = float(second_xg["away"])
        tie_home_is_deciding_home = False
    else:
        first_home = context.get("first_home_goals")
        first_away = context.get("first_away_goals")
        if first_home is None or first_away is None:
            first_xg = context.get("first_leg_xg") or {"home": away_xg, "away": home_xg}
            second_xg = context.get("second_leg_xg") or {"home": home_xg, "away": away_xg}
            first_diff = _score_difference_probabilities(
                score_matrix(float(first_xg["home"]), float(first_xg["away"]))
            )
            current_diff = _score_difference_probabilities(
                score_matrix(float(second_xg["home"]), float(second_xg["away"]))
            )
            for first_difference, first_probability in first_diff.items():
                for return_difference, return_probability in current_diff.items():
                    regulation[first_difference - return_difference] += (
                        first_probability * return_probability
                    )
        else:
            initial_difference = int(first_home) - int(first_away)
            for difference, probability in current_diff.items():
                regulation[initial_difference - difference] += probability
        second_xg = context.get("second_leg_xg") or {"home": home_xg, "away": away_xg}
        deciding_home_xg = float(second_xg["home"])
        deciding_away_xg = float(second_xg["away"])
        tie_home_is_deciding_home = False

    extra_diff_display = _score_difference_probabilities(
        score_matrix(max(0.01, deciding_home_xg / 3), max(0.01, deciding_away_xg / 3), rho=0.0)
    )
    extra_diff_tie = {
        (difference if tie_home_is_deciding_home else -difference): probability
        for difference, probability in extra_diff_display.items()
    }
    tie_home_penalty_probability = clamp(
        (
            deciding_home_xg / (deciding_home_xg + deciding_away_xg)
            if tie_home_is_deciding_home else
            deciding_away_xg / (deciding_home_xg + deciding_away_xg)
        ),
        0.42, 0.58,
    )
    methods = {
        f"{tie_home_id}_regular": 0.0,
        f"{tie_away_id}_regular": 0.0,
        f"{tie_home_id}_extra_time": 0.0,
        f"{tie_away_id}_extra_time": 0.0,
        f"{tie_home_id}_penalties": 0.0,
        f"{tie_away_id}_penalties": 0.0,
    }
    extra_time_probability = penalties_probability = 0.0
    for difference, probability in regulation.items():
        if difference > 0:
            methods[f"{tie_home_id}_regular"] += probability
            continue
        if difference < 0:
            methods[f"{tie_away_id}_regular"] += probability
            continue
        extra_time_probability += probability
        for extra_difference, extra_probability in extra_diff_tie.items():
            combined = probability * extra_probability
            if extra_difference > 0:
                methods[f"{tie_home_id}_extra_time"] += combined
            elif extra_difference < 0:
                methods[f"{tie_away_id}_extra_time"] += combined
            else:
                penalties_probability += combined
                methods[f"{tie_home_id}_penalties"] += combined * tie_home_penalty_probability
                methods[f"{tie_away_id}_penalties"] += combined * (1 - tie_home_penalty_probability)
    advancement = {
        str(tie_home_id): sum(value for key, value in methods.items() if key.startswith(f"{tie_home_id}_")),
        str(tie_away_id): sum(value for key, value in methods.items() if key.startswith(f"{tie_away_id}_")),
    }
    method_prices = bookmaker_prices(methods, KNOCKOUT_METHOD_MARGIN)
    settlement = context["settlement_fixture"]
    first_leg = None
    if (
        int(context["leg_count"]) == 2
        and context.get("first_home_goals") is not None
        and context.get("first_away_goals") is not None
    ):
        first_leg = {
            "home_id": tie_home_id,
            "away_id": tie_away_id,
            "home_goals": int(context["first_home_goals"]),
            "away_goals": int(context["first_away_goals"]),
        }
    return {
        "leg_index": int(context["leg_index"]),
        "leg_count": int(context["leg_count"]),
        "pricing_context": {
            key: dict(context[key])
            for key in ("first_leg_xg", "second_leg_xg")
            if isinstance(context.get(key), dict)
        },
        "first_leg": first_leg,
        "settlement_fixture_date": settlement["date"],
        "settlement_home_id": int(settlement["home_id"]),
        "settlement_away_id": int(settlement["away_id"]),
        "settlement_kickoff_minutes": settlement.get("kickoff_minutes"),
        "settlement_kickoff_time": settlement.get("kickoff_time"),
        "fair_advance": {key: decimal_odds(value) for key, value in advancement.items()},
        "advance": bookmaker_prices(advancement, KNOCKOUT_ADVANCE_MARGIN),
        "fair_extra_time": {
            "yes": decimal_odds(extra_time_probability),
            "no": decimal_odds(1 - extra_time_probability),
        },
        "extra_time": bookmaker_prices(
            {"yes": extra_time_probability, "no": 1 - extra_time_probability},
            KNOCKOUT_BINARY_MARGIN,
        ),
        "fair_penalties": {
            "yes": decimal_odds(penalties_probability),
            "no": decimal_odds(1 - penalties_probability),
        },
        "penalties": bookmaker_prices(
            {"yes": penalties_probability, "no": 1 - penalties_probability},
            KNOCKOUT_BINARY_MARGIN,
        ),
        "fair_advance_method": {
            key: decimal_odds(value) for key, value in methods.items()
        },
        "advance_method": method_prices,
    }


def ensure_match_market_catalog(match: dict[str, Any]) -> bool:
    """Upgrade cached match rows to the current line ladders and derived markets."""
    try:
        home_xg = float(match["xg"]["home"])
        away_xg = float(match["xg"]["away"])
    except (KeyError, TypeError, ValueError):
        return False
    matrix = score_matrix(home_xg, away_xg)
    btts = 1 - sum(matrix[0]) - sum(row[0] for row in matrix) + matrix[0][0]
    odd_goals = sum(
        probability for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
        if (home_goals + away_goals) % 2 == 1
    )
    no_goal = matrix[0][0]
    total_xg = home_xg + away_xg
    first_score_probabilities = {
        "home": (1 - no_goal) * home_xg / total_xg,
        "away": (1 - no_goal) * away_xg / total_xg,
        "none": no_goal,
    }
    handicap, fair_home_handicap, fair_away_handicap = choose_asian_handicap(matrix)
    total, fair_over, fair_under = choose_total_line(matrix)
    handicap_options = asian_handicap_options(
        matrix, handicap,
        equivalent_1x2_prices=match.get("casino_1x2"),
    )
    total_options = asian_total_options(matrix, total)
    main_handicap = next(item for item in handicap_options if item["is_main"])
    main_total = next(item for item in total_options if item["is_main"])
    match["asian_handicap"] = {
        "home_line": handicap,
        "home_odds": main_handicap["home_odds"],
        "fair_home_odds": fair_home_handicap,
        "away_line": -handicap,
        "away_odds": main_handicap["away_odds"],
        "fair_away_odds": fair_away_handicap,
    }
    match["handicap_options"] = handicap_options
    match["total_goals"] = {
        "line": total,
        "over_odds": main_total["over_odds"],
        "fair_over_odds": fair_over,
        "under_odds": main_total["under_odds"],
        "fair_under_odds": fair_under,
    }
    match["total_options"] = total_options
    match["team_total_options"] = {
        "home": team_total_options(matrix, "home"),
        "away": team_total_options(matrix, "away"),
    }
    match["fair_btts"] = {"yes": decimal_odds(btts), "no": decimal_odds(1 - btts)}
    match["btts"] = bookmaker_prices({"yes": btts, "no": 1 - btts}, BTTS_MARGIN)
    match["fair_goal_parity"] = {
        "odd": decimal_odds(odd_goals), "even": decimal_odds(1 - odd_goals),
    }
    match["goal_parity"] = bookmaker_prices(
        {"odd": odd_goals, "even": 1 - odd_goals}, GOAL_PARITY_MARGIN,
    )
    match["fair_first_score"] = {
        key: decimal_odds(probability)
        for key, probability in first_score_probabilities.items()
    }
    match["first_score"] = bookmaker_prices(
        first_score_probabilities, FIRST_SCORE_MARGIN,
    )
    match["first_score"]["none"] = exact_goals_odds(no_goal)
    match["half_time"] = half_time_markets(home_xg, away_xg)
    match.update(additional_score_markets(matrix))
    match["market_catalog_state"] = "full"
    match["markets_loaded"] = True
    return True


def normalize_match_market_catalog_state(match: dict[str, Any]) -> str:
    state = str(match.get("market_catalog_state") or "")
    if state not in {"core", "full"}:
        state = (
            "full"
            if match.get("half_time") and match.get("handicap_options")
            else "core"
        )
    match["market_catalog_state"] = state
    match["markets_loaded"] = state == "full"
    return state


def team_profile(
    reader: Reader, team_address: int, team_id: int, past: list[dict[str, Any]],
    elo_adjustments: dict[tuple[tuple[str, int, int, int], int], float] | None = None,
    *, squad: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    squad = reader.roster(team_address) if squad is None else squad
    return build_team_profile(squad, team_id, past, elo_adjustments)


def model_confidence(
    home: dict[str, Any], away: dict[str, Any],
) -> dict[str, Any]:
    def profile_score(profile: dict[str, Any]) -> float:
        recent = clamp(float(profile.get("recent_coverage") or 0.0), 0.0, 1.0)
        if profile.get("ca_source") != "squad":
            return clamp(0.10 + 0.25 * recent, 0.0, 1.0)
        roster = clamp(
            float(profile.get("candidate_players") or 0) / PRICED_PLAYER_COUNT,
            0.0, 1.0,
        )
        positions = clamp(float(profile.get("position_coverage") or 0.0), 0.0, 1.0)
        return clamp(0.55 * roster + 0.20 * positions + 0.25 * recent, 0.0, 1.0)

    score = min(profile_score(home), profile_score(away))
    label = "high" if score >= 0.85 else "medium" if score >= 0.50 else "low"
    return {"label": label, "score": round(score, 3)}


def shrink_competition_home_away_split(
    base_home: float, base_away: float,
) -> tuple[float, float]:
    """Keep the competition goal level without treating its full split as venue edge.

    Competition samples can confound home advantage with seeding and fixture
    assignment.  The team profiles already price those strength differences, so
    retaining the raw split would count part of the same signal twice.
    """
    average = (float(base_home) + float(base_away)) / 2.0
    weight = COMPETITION_HOME_AWAY_SPLIT_WEIGHT
    return (
        average + weight * (float(base_home) - average),
        average + weight * (float(base_away) - average),
    )


def expected_goals(
    home: dict[str, Any],
    away: dict[str, Any],
    base_home: float,
    base_away: float,
    *,
    neutral_site: bool = False,
    context_log_edge: float = 0.0,
    home_standings: dict[str, Any] | None = None,
    away_standings: dict[str, Any] | None = None,
    home_reputation: int | None = None,
    away_reputation: int | None = None,
) -> tuple[float, float]:
    base_home, base_away = shrink_competition_home_away_split(
        base_home, base_away,
    )
    # A 20-point squad-strength gap is roughly one full competitive tier in FM.
    raw_strength_gap = float(home["squad_strength"]) - float(away["squad_strength"])
    strength_gap = clamp(raw_strength_gap, -50.0, 50.0)
    # CA remains the most stable pre-match signal, but the edge cap stops one
    # dominant input from overwhelming combined form, morale, ELO and reputation.
    strength_log_edge = clamp(
        STRENGTH_LOG_COEFFICIENT * strength_gap,
        -STRENGTH_LOG_EDGE_CAP, STRENGTH_LOG_EDGE_CAP,
    )
    # Preserve the calibrated linear range while avoiding a discontinuous
    # plateau for very extreme mismatches.  The tail is deliberately small
    # and capped, so form/ELO/reputation cannot be overwhelmed by one rating.
    extreme_gap = math.copysign(
        min(max(abs(raw_strength_gap) - EXTREME_STRENGTH_GAP_THRESHOLD, 0.0),
            EXTREME_STRENGTH_GAP_TAIL_CAP),
        raw_strength_gap,
    )
    strength_log_edge = clamp(
        strength_log_edge + EXTREME_STRENGTH_LOG_COEFFICIENT * extreme_gap,
        -STRENGTH_LOG_EDGE_CAP, STRENGTH_LOG_EDGE_CAP,
    )

    home_goal_difference = home.get("recent_adjusted_goal_difference")
    if home_goal_difference is None:
        home_goal_difference = (home["recent_goals_for"] or 0) - (home["recent_goals_against"] or 0)
    away_goal_difference = away.get("recent_adjusted_goal_difference")
    if away_goal_difference is None:
        away_goal_difference = (away["recent_goals_for"] or 0) - (away["recent_goals_against"] or 0)
    def recent_coverage(profile: dict[str, Any]) -> float:
        if profile.get("recent_coverage") is not None:
            return clamp(float(profile["recent_coverage"]), 0.0, 1.0)
        matches = min(max(int(profile.get("recent_matches") or 0), 0), RECENT_MATCH_LIMIT)
        return sum(RECENT_MATCH_WEIGHTS[:matches])

    form_sample = min(recent_coverage(home), recent_coverage(away))
    form_log_edge = clamp(
        RECENT_FORM_COEFFICIENT * (home_goal_difference - away_goal_difference) * form_sample,
        -RECENT_FORM_LOG_EDGE_CAP, RECENT_FORM_LOG_EDGE_CAP,
    )

    morale_gap = home["candidate_20_morale_raw"] - away["candidate_20_morale_raw"]
    morale_log_edge = clamp(
        0.004 * morale_gap, -MORALE_LOG_EDGE_CAP, MORALE_LOG_EDGE_CAP,
    )
    standings_home_goal_adjustment = 0.0
    standings_away_goal_adjustment = 0.0
    standings_elo_log_edge = 0.0
    if home_standings and away_standings:
        league_average = float(home_standings.get("league_average_goals") or 0.0)
        if league_average > 0:
            def shrunk_ratio(profile: dict[str, Any], key: str) -> float:
                played = max(int(profile.get("played") or 0), 0)
                goals = max(float(profile.get(key) or 0.0), 0.0)
                rate = (
                    goals + STANDINGS_PRIOR_MATCHES * league_average
                ) / (played + STANDINGS_PRIOR_MATCHES)
                return clamp(rate / league_average, 0.50, 2.00)

            home_attack = shrunk_ratio(home_standings, "goals_for")
            home_defence = shrunk_ratio(home_standings, "goals_against")
            away_attack = shrunk_ratio(away_standings, "goals_for")
            away_defence = shrunk_ratio(away_standings, "goals_against")
            standings_home_goal_adjustment = clamp(
                STANDINGS_GOAL_LOG_COEFFICIENT
                * 0.5 * (math.log(home_attack) + math.log(away_defence)),
                -STANDINGS_GOAL_LOG_ADJUSTMENT_CAP,
                STANDINGS_GOAL_LOG_ADJUSTMENT_CAP,
            )
            standings_away_goal_adjustment = clamp(
                STANDINGS_GOAL_LOG_COEFFICIENT
                * 0.5 * (math.log(away_attack) + math.log(home_defence)),
                -STANDINGS_GOAL_LOG_ADJUSTMENT_CAP,
                STANDINGS_GOAL_LOG_ADJUSTMENT_CAP,
            )
        elo_confidence = min(
            int(home_standings.get("elo_matches") or 0),
            int(away_standings.get("elo_matches") or 0),
            ELO_CONFIDENCE_MATCHES,
        ) / ELO_CONFIDENCE_MATCHES
        standings_elo_log_edge = clamp(
            STANDINGS_ELO_LOG_COEFFICIENT
            * (float(home_standings.get("elo") or ELO_INITIAL_RATING)
               - float(away_standings.get("elo") or ELO_INITIAL_RATING))
            * elo_confidence,
            -STANDINGS_ELO_LOG_EDGE_CAP,
            STANDINGS_ELO_LOG_EDGE_CAP,
        )
    reputation_log_edge = 0.0
    if (
        home_reputation is not None and away_reputation is not None
        and 1 <= int(home_reputation) <= 10000
        and 1 <= int(away_reputation) <= 10000
    ):
        reputation_log_edge = clamp(
            TEAM_REPUTATION_LOG_COEFFICIENT
            * math.log(float(home_reputation) / float(away_reputation)),
            -TEAM_REPUTATION_LOG_EDGE_CAP,
            TEAM_REPUTATION_LOG_EDGE_CAP,
        )
    total_edge = (
        strength_log_edge + form_log_edge + morale_log_edge
        + standings_elo_log_edge + reputation_log_edge + context_log_edge
    )
    low_ca_goal_log_adjustment = 0.0
    if home.get("ca_source") == "squad" and away.get("ca_source") == "squad":
        stronger_team_ca = max(
            float(
                home.get("weighted_raw_ca")
                or home.get("candidate_20_ca")
                or LOW_CA_GOAL_THRESHOLD
            ),
            float(
                away.get("weighted_raw_ca")
                or away.get("candidate_20_ca")
                or LOW_CA_GOAL_THRESHOLD
            ),
        )
        low_ca_goal_log_adjustment = clamp(
            (LOW_CA_GOAL_THRESHOLD - stronger_team_ca) * LOW_CA_GOAL_LOG_COEFFICIENT,
            0.0, LOW_CA_GOAL_LOG_EDGE_CAP,
        )
    recent_total_goal_log_adjustment = 0.0
    recent_total_sum = 0.0
    recent_total_weight = 0.0
    for profile in (home, away):
        coverage = recent_coverage(profile)
        goals_for = profile.get("recent_goals_for")
        goals_against = profile.get("recent_goals_against")
        if coverage and goals_for is not None and goals_against is not None:
            recent_total_sum += (float(goals_for) + float(goals_against)) * coverage
            recent_total_weight += coverage
    if recent_total_weight:
        recent_total_average = recent_total_sum / recent_total_weight
        recent_total_confidence = min(recent_total_weight / 2.0, 1.0)
        competition_total = base_home + base_away
        recent_total_goal_log_adjustment = clamp(
            RECENT_TOTAL_GOALS_LOG_COEFFICIENT
            * (recent_total_average - competition_total)
            * recent_total_confidence,
            -RECENT_TOTAL_GOALS_LOG_ADJUSTMENT_CAP,
            RECENT_TOTAL_GOALS_LOG_ADJUSTMENT_CAP,
        )
    if neutral_site:
        neutral_base = (base_home + base_away) / 2
        base_home = neutral_base
        base_away = neutral_base
    goal_level_log_adjustment = (
        GOAL_LEVEL_LOG_ADJUSTMENT
        + low_ca_goal_log_adjustment
        + recent_total_goal_log_adjustment
    )
    return (
        round(max(0.20, base_home * math.exp(
            goal_level_log_adjustment + total_edge + standings_home_goal_adjustment
        )), 3),
        round(max(0.20, base_away * math.exp(
            goal_level_log_adjustment - total_edge + standings_away_goal_adjustment
        )), 3),
    )


def build_match_odds(
    home_team: dict[str, Any], away_team: dict[str, Any], home: dict[str, Any], away: dict[str, Any],
    base_home: float, base_away: float, competition_id: int | None = None, fixture_date: Any = None,
    home_standings: dict[str, Any] | None = None,
    away_standings: dict[str, Any] | None = None,
    include_extended_markets: bool = True,
) -> dict[str, Any]:
    neutral_site = competition_id in WORLD_CUP_FINALS_IDS
    context_log_edge = 0.0
    if neutral_site and getattr(fixture_date, "year", None) == 2026:
        if home_team["id"] in WORLD_CUP_2026_HOST_IDS:
            context_log_edge += 0.18
        if away_team["id"] in WORLD_CUP_2026_HOST_IDS:
            context_log_edge -= 0.18
    home_xg, away_xg = expected_goals(
        home,
        away,
        base_home,
        base_away,
        neutral_site=neutral_site,
        context_log_edge=context_log_edge,
        home_standings=home_standings,
        away_standings=away_standings,
        home_reputation=home_team.get("reputation"),
        away_reputation=away_team.get("reputation"),
    )
    matrix = score_matrix(home_xg, away_xg)
    home_win = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix)) if h > a)
    draw = sum(matrix[i][i] for i in range(len(matrix)))
    away_win = 1 - home_win - draw
    fair_1x2 = {"home": decimal_odds(home_win), "draw": decimal_odds(draw), "away": decimal_odds(away_win)}
    casino_1x2 = bookmaker_prices({"home": home_win, "draw": draw, "away": away_win}, ONE_X_TWO_MARGIN)
    confidence = model_confidence(home, away)
    base = {
        "home": {
            "id": home_team["id"], "name": home_team["short_name"] or home_team["name"],
            "address": home_team.get("address"),
            "reputation": home_team.get("reputation"),
            "profile": {**home, "standings": home_standings} if home_standings else home,
        },
        "away": {
            "id": away_team["id"], "name": away_team["short_name"] or away_team["name"],
            "address": away_team.get("address"),
            "reputation": away_team.get("reputation"),
            "profile": {**away, "standings": away_standings} if away_standings else away,
        },
        "xg": {"home": home_xg, "away": away_xg},
        "fair_1x2": fair_1x2,
        "casino_1x2": casino_1x2,
        "pricing_mode": "casino",
        "confidence": confidence["label"],
        "confidence_score": confidence["score"],
        "market_catalog_state": "full" if include_extended_markets else "core",
    }
    if not include_extended_markets:
        return base

    btts = 1 - sum(matrix[0]) - sum(row[0] for row in matrix) + matrix[0][0]
    odd_goals = sum(
        probability for home_goals, row in enumerate(matrix)
        for away_goals, probability in enumerate(row)
        if (home_goals + away_goals) % 2 == 1
    )
    no_goal = matrix[0][0]
    total_xg = home_xg + away_xg
    first_score_probabilities = {
        "home": (1 - no_goal) * home_xg / total_xg,
        "away": (1 - no_goal) * away_xg / total_xg,
        "none": no_goal,
    }
    first_score_prices = bookmaker_prices(first_score_probabilities, FIRST_SCORE_MARGIN)
    first_score_prices["none"] = exact_goals_odds(no_goal)
    handicap, home_handicap_odds, away_handicap_odds = choose_asian_handicap(matrix)
    total, over_odds, under_odds = choose_total_line(matrix)
    total_weights_pair = (total_weights(matrix, total, True), total_weights(matrix, total, False))
    handicap_options = asian_handicap_options(
        matrix, handicap,
        equivalent_1x2_prices=casino_1x2,
    )
    main_handicap = next(item for item in handicap_options if item["is_main"])
    half_time = half_time_markets(home_xg, away_xg)
    base.update({
        "asian_handicap": {
            "home_line": handicap,
            "home_odds": main_handicap["home_odds"],
            "fair_home_odds": home_handicap_odds,
            "away_line": -handicap,
            "away_odds": main_handicap["away_odds"],
            "fair_away_odds": away_handicap_odds,
        },
        "handicap_options": handicap_options,
        "total_goals": {
            "line": total,
            "over_odds": bookmaker_weighted_odds(total_weights_pair[0]),
            "fair_over_odds": over_odds,
            "under_odds": bookmaker_weighted_odds(total_weights_pair[1]),
            "fair_under_odds": under_odds,
        },
        "total_options": asian_total_options(matrix, total),
        "team_total_options": {
            "home": team_total_options(matrix, "home"),
            "away": team_total_options(matrix, "away"),
        },
        "fair_btts": {"yes": decimal_odds(btts), "no": decimal_odds(1 - btts)},
        "btts": bookmaker_prices({"yes": btts, "no": 1 - btts}, BTTS_MARGIN),
        "fair_goal_parity": {"odd": decimal_odds(odd_goals), "even": decimal_odds(1 - odd_goals)},
        "goal_parity": bookmaker_prices(
            {"odd": odd_goals, "even": 1 - odd_goals}, GOAL_PARITY_MARGIN
        ),
        "fair_first_score": {
            key: decimal_odds(probability) for key, probability in first_score_probabilities.items()
        },
        "first_score": first_score_prices,
        **additional_score_markets(matrix),
        "half_time": half_time,
    })
    return base


def discover_upcoming_competitions(days: int = 7) -> list[dict[str, Any]]:
    with open_supported_reader() as (_process_path, _layout, _module, reader):
        game_date = decode_date(read_layout_game_date_code(reader))
        if not game_date:
            raise RuntimeError("game date unavailable")
        game_minutes = decode_kickoff_minutes(read_layout_game_date_code(reader))
        grouped: dict[tuple[int, str, str], int] = {}
        seen_fixtures: set[tuple[int, str, int, int]] = set()
        for address in scan_fixture_addresses(reader)[0]:
            fixture = parse_fixture(reader, address)
            if (
                not fixture
                or not fixture_is_open(fixture, game_date, game_minutes)
                or fixture.match_date > game_date + timedelta(days=days)
            ):
                continue
            competition = reader.competition(fixture.competition_season)
            home = reader.team(fixture.home_team)
            away = reader.team(fixture.away_team)
            if not competition or not home or not away:
                continue
            name = competition["short_name"] or competition["name"] or str(competition["id"])
            fixture_key = (competition["id"], fixture.match_date.isoformat(), home["id"], away["id"])
            if fixture_key in seen_fixtures:
                continue
            seen_fixtures.add(fixture_key)
            key = (competition["id"], name, fixture.match_date.isoformat())
            grouped[key] = grouped.get(key, 0) + 1
    earliest: dict[int, dict[str, Any]] = {}
    for (competition_id, name, date_text), count in grouped.items():
        item = {"id": competition_id, "name": name, "date": date_text, "matches": count}
        if competition_id not in earliest or date_text < earliest[competition_id]["date"]:
            earliest[competition_id] = item
    return sorted(earliest.values(), key=lambda item: (item["date"], item["name"], item["id"]))


def read_completed_results(
    required_keys: set[tuple[str, int, int, int]] | None = None,
    *,
    fixture_hints: dict[tuple[str, int, int, int], int] | None = None,
    deep_recovery: bool = False,
    preferred_save_id: str | None = None,
    preferred_manager_id: int | None = None,
    known_manager_sessions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return the validated final results currently retained in FM memory."""
    with open_supported_reader() as (_process_path, layout, module, reader):
        game_date = decode_date(read_layout_game_date_code(reader))
        game_date_text = game_date.isoformat() if game_date else ""
        session = discover_manager_session(reader, game_date) if game_date else None
        identity = read_savegame_identity(reader)
        equivalent_stable_key = None
        if layout.key == "fm24":
            if preferred_save_id:
                save_name, _candidates = read_cached_save_name(
                    reader, module, identity,
                )
                stable_key = stable_save_key(layout, identity, save_name)
                previous_name = confirmed_save_name(preferred_save_id)
                if (
                    stable_key
                    and save_name
                    and previous_name
                    and str(save_name).casefold() != str(previous_name).casefold()
                    and _fm24_manager_survives_save_name_change(
                        reader, preferred_manager_id, known_manager_sessions,
                    )
                ):
                    stable_key = None
            else:
                stable_key, equivalent_stable_key, save_name = fm24_save_evidence(
                    layout, reader, module, identity, discover_managers=True,
                )
        else:
            save_name = None
            stable_key = stable_save_key(layout, identity, save_name)
        save_instance_id = layout_save_identity(
            layout,
            resolve_save_identity(
                session.get("game_instance_id") if session else None,
                preferred_manager_id,
                preferred_id=preferred_save_id,
                stable_id=stable_key,
                equivalent_stable_id=equivalent_stable_key,
                namespace=layout.key,
            ),
        )
        results, cache_hit = cached_completed_results(
            reader, game_date_text, save_instance_id,
            force_fixture_scan=deep_recovery,
        )
        required = set(required_keys or ())
        missing = required - {_result_identity(item) for item in results}
        hinted = recover_fixture_hint_results(
            reader,
            {
                key: address for key, address in (fixture_hints or {}).items()
                if key in missing
            },
        )
        if hinted:
            results = resolve_daily_team_conflicts(merge_result_history(
                merge_verified_results([*results, *hinted]), save_instance_id,
            ))
            remember_completed_results(reader, game_date_text, results)
        missing = required - {_result_identity(item) for item in results}
        if cache_hit and missing:
            results, _cache_hit = cached_completed_results(
                reader, game_date_text, save_instance_id, force_scan=True,
                force_fixture_scan=deep_recovery,
            )
        missing = {
            key for key in required - {_result_identity(item) for item in results}
            if game_date and key[0] < game_date.isoformat()
        }
        if missing:
            recovered = recover_persistent_results(
                reader, game_date_text, missing, force=deep_recovery,
            )
            if recovered:
                results = resolve_daily_team_conflicts(merge_result_history(
                    merge_verified_results([*results, *recovered]), save_instance_id,
                ))
                remember_completed_results(reader, game_date_text, results)
        results = enrich_halftime_results(reader, results, required)
        remember_completed_results(reader, game_date_text, results)
    save_result_history(
        results, save_instance_id, game_date_text,
        protected_result_keys=required,
    )
    return results


def read_game_date() -> str:
    with open_supported_reader() as (_process_path, layout, module, reader):
        value = decode_date(read_layout_game_date_code(reader))
        if not value or value == date(1900, 1, 1):
            raise RuntimeError("game date unavailable")
        return value.isoformat()


def read_game_clock_from_reader(reader: Reader) -> dict[str, Any]:
    code = read_layout_game_date_code(reader)
    game_date = decode_date(code)
    if not game_date or game_date == date(1900, 1, 1):
        raise RuntimeError("game date unavailable")
    minutes = decode_kickoff_minutes(code)
    if minutes is None:
        minutes = 0
    return {
        "date": game_date.isoformat(),
        "time": f"{minutes // 60:02d}:{minutes % 60:02d}",
        "minutes": minutes,
    }


def read_game_clock() -> dict[str, Any]:
    with open_supported_reader() as (_process_path, _layout, _module, reader):
        return read_game_clock_from_reader(reader)


def read_connection_context(
    *, preferred_save_id: str | None = None,
    preferred_manager_id: int | None = None,
    known_manager_sessions: list[dict[str, Any]] | None = None,
    selected_process: tuple[int, str, Any] | None = None,
) -> dict[str, Any]:
    """Read the save/player identity without scanning fixtures or building odds.

    This is the connection handshake used before startup odds refresh.  It
    publishes only identity fields whose native objects were revalidated in
    the current read session; fixture, result and model work stays outside the
    connection critical path.
    """
    started = perf_counter()
    with open_supported_reader(selected_process) as (
        _process_path, layout, module, reader,
    ):
        clock = read_game_clock_from_reader(reader)
        game_date = date.fromisoformat(str(clock["date"]))
        stable_identity = read_savegame_identity(reader)
        sessions = discover_human_managers(
            reader, [], preferred_manager_id=preferred_manager_id,
            known_manager_sessions=known_manager_sessions,
            allow_expensive_direct_scan=False,
        )
        has_managed_team_refs = any(
            isinstance(item, dict)
            and bool(item.get("managed_team_refs"))
            for item in sessions
        )
        if not has_managed_team_refs:
            # Some supported installations do not expose a usable database
            # index or complete manager/team links during the initial
            # handshake.  XGP relies on dynamic root resolution, so its cheap
            # lookup can briefly find the manager before the managed-team
            # relation is available.  Keep the indexed fast path, then use the
            # existing bounded fallback when no verified team reference was
            # found.  The fallback retains the same build and object checks.
            sessions = discover_human_managers(
                reader, [], preferred_manager_id=preferred_manager_id,
                known_manager_sessions=known_manager_sessions,
                allow_expensive_direct_scan=True,
            )
        equivalent_stable_key = None
        if layout.key == "fm24":
            # A first-time FM24 local-save provider search can take several
            # seconds or find nothing for network saves.  Validate an existing
            # address here and defer any heap discovery to startup odds work.
            stable_save_name, _cached_candidates = read_cached_save_name(
                reader, module, stable_identity,
            )
            stable_key = stable_save_key(layout, stable_identity, stable_save_name)
            previous_name = confirmed_save_name(preferred_save_id)
            if (
                preferred_save_id
                and preferred_manager_id
                and stable_key
                and stable_save_name
                and previous_name
                and str(stable_save_name).casefold() != str(previous_name).casefold()
                and any(
                    int(item.get("manager_id") or item.get("id") or 0)
                    == int(preferred_manager_id)
                    for item in sessions if isinstance(item, dict)
                )
            ):
                # The FM24 local provider renames a network save when its
                # human manager changes clubs. A surviving manager UID is the
                # continuity evidence; retain the existing career/account.
                stable_key = None
        else:
            stable_save_name = None
            stable_key = stable_save_key(layout, stable_identity, None)
        save_context_key = stable_key or fm24_network_save_key(layout, sessions)
        save_context_key, equivalent_stable_key = anchored_save_context_evidence(
            layout, save_context_key, equivalent_stable_key, preferred_save_id,
        )
        # If the save provider and network-session evidence are both
        # temporarily unavailable, keep the manager as a continuity hint.
        # ``resolve_career`` only reuses an existing career when that manager
        # belongs to exactly one career, so this does not merge ambiguous
        # same-manager save copies.  Without this hint a cold connection can
        # create a new provisional ``career-*`` scope and force a full odds
        # rebuild even though the previous career is still known.
        identity_manager_id = int(preferred_manager_id or 0)
        if not identity_manager_id and len(sessions) == 1:
            identity_manager_id = int(sessions[0].get("manager_id") or 0)
        canonical_save_id = layout_save_identity(
            layout,
            resolve_save_identity(
                None, identity_manager_id or None, preferred_id=preferred_save_id,
                stable_id=save_context_key,
                equivalent_stable_id=equivalent_stable_key,
                namespace=layout.key,
            ),
        )
        if not canonical_save_id:
            raise RuntimeError("无法确认当前存档身份")

        preferred_context_matches = bool(
            preferred_save_id
            and save_identity_matches(canonical_save_id, preferred_save_id)
        )
        effective_manager_id = int(
            (
                preferred_manager_id
                if preferred_context_matches else None
            )
            or stored_selected_manager_id(canonical_save_id)
            or 0
        )
        selected_session = next(
            (
                item for item in sessions
                if effective_manager_id
                and int(item.get("manager_id") or 0) == effective_manager_id
            ),
            None,
        )
        if selected_session is None and len(sessions) == 1 and not effective_manager_id:
            selected_session = sessions[0]
            effective_manager_id = int(selected_session.get("manager_id") or 0)

        manager_options = []
        for item in sessions:
            refs = [dict(ref) for ref in item.get("managed_team_refs") or []]
            manager_options.append({
                "id": int(item.get("manager_id") or 0),
                "name": item.get("manager_name") or f"经理 {item.get('manager_id')}",
                "manager_address": item.get("manager_address"),
                "teams": [
                    {
                        "id": int(ref.get("team_id") or 0),
                        "team_type": str(ref.get("team_type") or "club"),
                        "address": ref.get("address"),
                        "club_address": ref.get("club_address"),
                        "manager_address": ref.get("manager_address"),
                    }
                    for ref in refs if int(ref.get("team_id") or 0) > 0
                ],
                "managed_team_refs": refs,
            })

        managed_teams: list[dict[str, Any]] = []
        manager = None
        if selected_session is not None:
            selected_manager_id = int(
                selected_session.get("manager_id") or 0
            )
            manager_address = str(selected_session.get("manager_address") or "")
            for ref in selected_session.get("managed_team_refs") or []:
                team_id = int(ref.get("team_id") or ref.get("id") or 0)
                team_type = str(ref.get("team_type") or "club")
                try:
                    address = int(str(ref.get("address") or "0"), 0)
                except (TypeError, ValueError):
                    address = 0
                team = reader.team(address) if address else None
                if (
                    not team
                    or int(team.get("id") or 0) != team_id
                    or str(team.get("team_type") or "club") != team_type
                ):
                    # Epic FM24 has no fixed database-root probe, so manager
                    # discovery can briefly retain a team address from before
                    # FM rebuilt the object. Re-resolve by stable manager/team
                    # identity before declaring the connection manager-less.
                    team = resolve_managed_team_by_identity(
                        reader, team_id, team_type, selected_manager_id,
                        ref.get("manager_address") or manager_address,
                        scan_manager=True,
                    )
                if not team:
                    continue
                managed_teams.append({
                    "id": int(team["id"]),
                    "name": team.get("short_name") or team.get("name"),
                    "team_type": str(team.get("team_type") or "club"),
                    "source": "native_database_index",
                    "address": team.get("address"),
                    "club_address": team.get("club_address"),
                    "manager_address": team.get("manager_address") or manager_address,
                })
            managed_teams.sort(key=lambda item: (
                item.get("team_type") != "club", int(item.get("id") or 0),
            ))
            manager_address = manager_address or str(
                (managed_teams[0] if managed_teams else {}).get("manager_address") or ""
            )
            try:
                manager_pointer = int(manager_address, 0)
            except (TypeError, ValueError):
                manager_pointer = 0
            manager = {
                "id": int(selected_session.get("manager_id") or 0),
                "name": selected_session.get("manager_name")
                or f"经理 {selected_session.get('manager_id')}",
                "team_id": int((managed_teams[0] if managed_teams else {}).get("id") or 0)
                or None,
                "team_name": (managed_teams[0] if managed_teams else {}).get("name"),
                "team_type": (managed_teams[0] if managed_teams else {}).get("team_type"),
                "game_instance_id": canonical_save_id,
                "source": "native_database_index",
                "manager_address": manager_address or None,
                "person_address": (
                    hex(manager_pointer + int(layout.manager_person_offset))
                    if manager_pointer else None
                ),
                "coaching_license": (
                    _manager_coaching_license(
                        reader, manager_pointer + int(layout.manager_person_offset),
                    ) if manager_pointer else None
                ),
            }

        remember_save_name(canonical_save_id, stable_save_name)
        managed_team = next(
            (item for item in managed_teams if item.get("team_type") == "club"),
            managed_teams[0] if managed_teams else None,
        )
        return {
            # Save/date evidence alone does not verify the player context.  If
            # manager discovery is temporarily empty, startup odds must repeat
            # the full identity path instead of reusing this partial handshake.
            "connection_context_verified": bool(sessions),
            # A connected FM24 session may not expose the local save-name
            # provider even though its canonical identity already matches the
            # previously verified save.  Keep the safety gate for a first
            # connection, but do not block an unchanged, verified session
            # merely because the optional name probe returned no value.
            "save_identity_discovery_deferred": bool(
                layout.key == "fm24"
                and not stable_save_name
                and not preferred_context_matches
            ),
            "source": f"read-only {layout.display_name} process memory",
            "game_layout": str(layout.key),
            "game_version": str(layout.game_version or layout.display_name),
            "game_date": game_date.isoformat(),
            "game_time": str(clock.get("time") or "00:00"),
            "save_instance_id": canonical_save_id,
            "save_name": confirmed_save_name(canonical_save_id),
            "stable_savegame_id": (
                stable_identity.get("savegame_id") if stable_identity else None
            ),
            "session_nonce": (
                stable_identity.get("session_nonce") if stable_identity else None
            ),
            "manager": manager,
            "manager_options": manager_options,
            "selected_manager_id": int((manager or {}).get("id") or 0) or None,
            "manager_selection_required": bool(
                len(sessions) > 1 and selected_session is None
            ),
            "unemployed_manager_detected": bool(manager and not managed_teams),
            "managed_team": managed_team,
            "managed_teams": managed_teams,
            "connection_performance": {
                "total_ms": round((perf_counter() - started) * 1000, 1),
                "manager_count": len(sessions),
                "session_generation": int(
                    getattr(reader, "session_generation", 0) or 0
                ),
            },
        }


def read_manager_context(
    manager_id: int, *, preferred_save_id: str | None = None,
    known_session: dict[str, Any] | None = None,
    known_manager_sessions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh only the selected human manager and managed-team identities."""
    manager_id = int(manager_id)
    if manager_id <= 0:
        raise ValueError("invalid manager id")
    with open_supported_reader() as (_process_path, layout, module, reader):
        game_date = decode_date(read_layout_game_date_code(reader))
        if not game_date:
            raise RuntimeError("game date unavailable")
        stable_identity = read_savegame_identity(reader)
        equivalent_stable_key = None
        if layout.key == "fm24":
            if preferred_save_id:
                stable_save_name, _candidates = read_cached_save_name(
                    reader, module, stable_identity,
                )
                stable_key = stable_save_key(
                    layout, stable_identity, stable_save_name,
                )
                previous_name = confirmed_save_name(preferred_save_id)
                if (
                    stable_key
                    and stable_save_name
                    and previous_name
                    and str(stable_save_name).casefold() != str(previous_name).casefold()
                    and _fm24_manager_survives_save_name_change(
                        reader, manager_id,
                        [known_session] if known_session else None,
                    )
                ):
                    # A local FM24 save label follows the current club. Do
                    # not resolve a new career just because that label changed.
                    stable_key = None
            else:
                stable_key, equivalent_stable_key, stable_save_name = fm24_save_evidence(
                    layout, reader, module, stable_identity,
                    discover_managers=True,
                )
        else:
            stable_save_name = None
            stable_key = stable_save_key(layout, stable_identity, None)
        stable_key, equivalent_stable_key = anchored_save_context_evidence(
            layout, stable_key, equivalent_stable_key, preferred_save_id,
        )
        canonical_save_id = layout_save_identity(
            layout,
            resolve_save_identity(
                None, manager_id, preferred_id=preferred_save_id,
                stable_id=stable_key,
                equivalent_stable_id=equivalent_stable_key,
                namespace=layout.key,
            ),
        )
        fixture_addresses: list[int] = []
        fixture_cache_hit = True
        if known_session and int(
            known_session.get("manager_id") or known_session.get("id") or 0
        ) == manager_id:
            # Treat manager options as hints.  Another human manager can change
            # jobs while this account is inactive, so verify the manager UID,
            # team UID and reverse manager link before reusing its assignments.
            session, sessions = _revalidate_known_human_manager_session(
                reader, manager_id, dict(known_session),
                known_manager_sessions=known_manager_sessions,
            )
            if session is None:
                fixture_addresses, fixture_cache_hit = cached_fixture_addresses(reader)
                sessions = discover_human_managers(
                    reader, fixture_addresses, preferred_manager_id=manager_id,
                )
                session = next(
                    (
                        item for item in sessions
                        if int(item.get("manager_id") or 0) == manager_id
                    ),
                    None,
                )
        else:
            fixture_addresses, fixture_cache_hit = cached_fixture_addresses(reader)
            sessions = discover_human_managers(
                reader, fixture_addresses, preferred_manager_id=manager_id,
            )
            session = next(
                (item for item in sessions if int(item.get("manager_id") or 0) == manager_id),
                None,
            )
        if session is None:
            raise RuntimeError("所选经理已失效，请重新读取存档")

        managed_teams: list[dict[str, Any]] = []
        for ref in session.get("managed_team_refs") or []:
            team_id = int(ref.get("team_id") or 0)
            team_type = str(ref.get("team_type") or "club")
            try:
                address = int(str(ref.get("address") or "0"), 16)
            except (TypeError, ValueError):
                address = 0
            team = reader.team(address) if address else None
            if not team or int(team.get("id") or 0) != team_id or str(team.get("team_type") or "club") != team_type:
                team = resolve_managed_team_by_identity(
                    reader, team_id, team_type, manager_id,
                    ref.get("manager_address"), scan_manager=True,
                )
            if not team:
                team = resolve_managed_team(
                    reader, team_id, team_type, fixture_addresses, [],
                )
            if not team:
                continue
            team_name = team.get("short_name") or team.get("name")
            if not team_name:
                continue
            managed_teams.append({
                "id": int(team["id"]),
                "name": team_name,
                "team_type": team.get("team_type") or team_type,
                "source": "human_manager_context_refresh",
                "address": team.get("address"),
                "club_address": team.get("club_address"),
                "manager_address": team.get("manager_address"),
            })
        managed_team = next(
            (item for item in managed_teams if item.get("team_type") == "club"),
            managed_teams[0] if managed_teams else None,
        )
        manager_address = (
            (managed_team or {}).get("manager_address")
            or session.get("manager_address")
        )
        try:
            person_address = (
                int(str(manager_address or "0"), 16) + reader.layout.manager_person_offset
            )
        except (TypeError, ValueError):
            person_address = 0
        manager = {
            "id": manager_id,
            "name": session.get("manager_name") or f"经理 {manager_id}",
            "team_id": int((managed_team or {}).get("id") or 0) or None,
            "team_name": (managed_team or {}).get("name"),
            "team_type": (managed_team or {}).get("team_type"),
            "game_instance_id": canonical_save_id,
            "source": "human_manager_context_refresh",
            "manager_address": manager_address,
            "person_address": hex(person_address) if person_address else None,
            "coaching_license": (
                _manager_coaching_license(reader, person_address)
                if person_address else None
            ),
        }
        return {
            "game_date": game_date.isoformat(),
            "save_instance_id": canonical_save_id,
            "manager": manager,
            "manager_options": [
                {
                    "id": int(item["manager_id"]),
                    "name": item.get("manager_name") or f"经理 {item['manager_id']}",
                    "manager_address": item.get("manager_address"),
                    "teams": [
                        {
                            "id": int(ref["team_id"]),
                            "team_type": ref.get("team_type"),
                            "address": ref.get("address"),
                            "club_address": ref.get("club_address"),
                            "manager_address": ref.get("manager_address"),
                        }
                        for ref in item.get("managed_team_refs", [])
                    ],
                    "managed_team_refs": [dict(ref) for ref in item.get("managed_team_refs", [])],
                }
                for item in sessions
            ],
            "selected_manager_id": manager_id,
            "manager_selection_required": False,
            "unemployed_manager_detected": not bool(managed_teams),
            "managed_team": managed_team,
            "managed_teams": managed_teams,
            "fixture_address_cache_hit": fixture_cache_hit,
        }


_FM26_MATCH_PLAYBACK_CACHE_LOCK = RLock()
_FM26_MATCH_PLAYBACK_CACHE: dict[int, dict[str, Any]] = {}
_FM26_MATCH_SETUP_CONTROLLER_OFFSET = 0x550
_FM26_MATCH_SETUP_VIEWABLE_TYPE_OFFSET = 0x518
_FM26_MATCH_CONTROLLER_SETUP_OFFSET = 0x108
_FM26_MATCH_CONTROLLER_NATIVE_OBJECT_OFFSET = 0x10
_FM26_MATCH_CONTROLLER_KICKED_OFF_OFFSET = 0x11B
_FM26_MATCH_SETUP_SMALL_REGION_SIZE = 0x40000
_FM26_MATCH_SETUP_UNITY_REGION_SIZE = 0x1000000
_FM26_MATCH_SETUP_REGION_RESCAN_SECONDS = 2.0
_FM26_MATCH_SETUP_FULL_RESCAN_SECONDS = 60.0


def _read_il2cpp_c_string(reader: Reader, address: int | None) -> str | None:
    if not address:
        return None
    raw = reader.bytes(int(address), 96)
    if not raw:
        return None
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _il2cpp_class_identity(
    reader: Reader, class_address: int | None,
) -> tuple[str | None, str | None]:
    if not class_address:
        return None, None
    return (
        _read_il2cpp_c_string(reader, reader.ptr(int(class_address) + 0x10)),
        _read_il2cpp_c_string(reader, reader.ptr(int(class_address) + 0x18)),
    )


def _validate_fm26_match_setup(
    reader: Reader, setup: int, setup_class: int,
) -> dict[str, Any] | None:
    if reader.ptr(setup) != setup_class:
        return None
    controller = reader.ptr(setup + _FM26_MATCH_SETUP_CONTROLLER_OFFSET)
    if not controller or reader.ptr(
        controller + _FM26_MATCH_CONTROLLER_SETUP_OFFSET
    ) != setup:
        return None
    controller_class = reader.ptr(controller)
    if _il2cpp_class_identity(reader, controller_class) != (
        "MatchPlaybackController", "FM.Match",
    ):
        return None
    native_object = reader.ptr(
        controller + _FM26_MATCH_CONTROLLER_NATIVE_OBJECT_OFFSET
    )
    viewable_type = reader.u8(setup + _FM26_MATCH_SETUP_VIEWABLE_TYPE_OFFSET)
    kicked_off = reader.u8(controller + _FM26_MATCH_CONTROLLER_KICKED_OFF_OFFSET)
    if viewable_type is None or kicked_off is None:
        return None
    # MatchPlaybackController is a live MonoBehaviour while the viewer page is
    # open.  MatchKickedOff remains false throughout the pre-match viewer and
    # therefore cannot be an activity requirement.  Unity clears m_CachedPtr
    # when the page controller is destroyed, which separates retained managed
    # setup objects from the currently visible viewer.
    active = bool(native_object)
    return {
        "setup": setup,
        "controller": controller,
        "active": active,
        "spectator": bool(active and viewable_type == 1),
        "viewable_match_type": viewable_type,
        "match_kicked_off": kicked_off == 1,
    }


def _scan_fm26_match_setups(
    reader: Reader,
    setup_class: int,
    regions: tuple[tuple[int, int], ...] = (),
    *,
    include_large_regions: bool = False,
) -> tuple[tuple[int, ...], tuple[tuple[int, int], ...]]:
    needle = struct.pack("<Q", setup_class)
    if regions:
        scan_regions = regions
    else:
        readable = [
            region for region in iter_readable_regions(reader.process)
            if region.type == MEM_PRIVATE
            and (region.protect & 0xFF) == PAGE_READWRITE
        ]
        small_regions = sorted(
            (
                (int(region.base_address), int(region.size))
                for region in readable
                if 0 < region.size <= _FM26_MATCH_SETUP_SMALL_REGION_SIZE
            ),
        )
        unity_regions = (
            sorted(
                (
                    (int(region.base_address), int(region.size))
                    for region in readable
                    if region.size == _FM26_MATCH_SETUP_UNITY_REGION_SIZE
                ),
                reverse=True,
            )
            if include_large_regions else []
        )
        scan_regions = tuple(small_regions + unity_regions)
    candidates: set[int] = set()
    matched_regions = []
    for base, size in scan_regions:
        raw = reader.bytes(base, size)
        if not raw:
            continue
        found_in_region = False
        offset = raw.find(needle)
        while offset >= 0:
            if offset % 8 == 0:
                setup = base + offset
                state = _validate_fm26_match_setup(
                    reader, setup, setup_class,
                )
                if state is not None:
                    candidates.add(setup)
                    found_in_region = True
                    if state and state["active"] and state["spectator"]:
                        matched_regions.append((base, size))
                        return tuple(sorted(candidates)), tuple(matched_regions)
            offset = raw.find(needle, offset + 8)
        if found_in_region:
            matched_regions.append((base, size))
    return tuple(sorted(candidates)), tuple(matched_regions)


def _read_fm26_match_playback_state(
    reader: Reader,
    layout: Any,
    *,
    allow_discovery: bool,
    discovery_signature: tuple[Any, ...] = (),
    allow_large_discovery: bool = False,
) -> dict[str, Any]:
    pid = int(getattr(reader.process, "pid", 0) or 0)
    with _FM26_MATCH_PLAYBACK_CACHE_LOCK:
        cached = dict(_FM26_MATCH_PLAYBACK_CACHE.get(pid) or {})
    if cached.get("reader_module_base") != int(reader.module_base):
        cached = {}

    if allow_discovery:
        module_name = getattr(layout, "match_playback_module_name", None)
        expected_timestamp = getattr(
            layout, "match_playback_module_timestamp", None,
        )
        expected_size = getattr(layout, "match_playback_module_image_size", None)
        type_info_rva = getattr(layout, "match_setup_type_info_rva", None)
        game_assembly = find_module(reader.process, module_name) if module_name else None
        if (
            game_assembly
            and expected_timestamp is not None
            and expected_size is not None
            and type_info_rva is not None
            and game_assembly.size == expected_size
        ):
            pe_offset = reader.u32(game_assembly.base_address + 0x3C)
            timestamp = (
                reader.u32(game_assembly.base_address + pe_offset + 8)
                if pe_offset is not None else None
            )
            setup_class = reader.ptr(game_assembly.base_address + type_info_rva)
            cache_key = (
                game_assembly.base_address, game_assembly.size,
                timestamp, setup_class,
            )
            if timestamp == expected_timestamp and setup_class:
                now = monotonic()
                if cached.get("key") != cache_key:
                    cached = {
                        "key": cache_key,
                        "reader_module_base": int(reader.module_base),
                        "setup_class": setup_class,
                        "candidates": (),
                        "regions": (),
                        "last_region_scan": 0.0,
                        "last_full_scan": 0.0,
                        "discovery_signature": (),
                    }
                current_states = [
                    state
                    for setup in tuple(cached.get("candidates") or ())
                    if (state := _validate_fm26_match_setup(
                        reader, int(setup), setup_class,
                    )) is not None
                ]
                has_active = any(state["active"] for state in current_states)
                cached_regions = tuple(cached.get("regions") or ())
                region_rescan_due = bool(
                    cached_regions and not has_active
                    and now - float(cached.get("last_region_scan") or 0.0)
                    >= _FM26_MATCH_SETUP_REGION_RESCAN_SECONDS
                )
                full_scan_due = bool(
                    not has_active and (
                        not cached.get("last_full_scan")
                        or tuple(cached.get("discovery_signature") or ())
                        != tuple(discovery_signature)
                        or now - float(cached.get("last_full_scan") or 0.0)
                        >= _FM26_MATCH_SETUP_FULL_RESCAN_SECONDS
                    )
                )
                if region_rescan_due:
                    candidates, regions = _scan_fm26_match_setups(
                        reader,
                        setup_class,
                        cached_regions,
                    )
                    cached["candidates"] = candidates
                    if regions:
                        cached["regions"] = regions
                    cached["last_region_scan"] = now
                    has_active = any(
                        bool(state and state["active"])
                        for setup in candidates
                        if (state := _validate_fm26_match_setup(
                            reader, setup, setup_class,
                        )) is not None
                    )
                if full_scan_due and not has_active:
                    candidates, regions = _scan_fm26_match_setups(
                        reader,
                        setup_class,
                        include_large_regions=allow_large_discovery,
                    )
                    cached["candidates"] = candidates
                    cached["regions"] = regions
                    cached["last_region_scan"] = now
                    cached["last_full_scan"] = now
                    cached["discovery_signature"] = tuple(discovery_signature)
                with _FM26_MATCH_PLAYBACK_CACHE_LOCK:
                    _FM26_MATCH_PLAYBACK_CACHE.clear()
                    _FM26_MATCH_PLAYBACK_CACHE[pid] = dict(cached)

    setup_class = int(cached.get("setup_class") or 0)
    candidates = tuple(cached.get("candidates") or ())
    valid = [
        state
        for setup in candidates
        if (state := _validate_fm26_match_setup(reader, int(setup), setup_class))
        is not None
    ] if setup_class else []
    active = next((state for state in valid if state["active"]), None)
    selected = active or (valid[0] if valid else None)
    return {
        "available": bool(cached),
        "active": bool(active),
        "spectator": bool(active and active["spectator"]),
        "candidate_count": len(candidates),
        "valid_candidate_count": len(valid),
        "viewable_match_type": (
            selected["viewable_match_type"] if selected else None
        ),
        "match_kicked_off": bool(selected and selected["match_kicked_off"]),
    }


def read_match_engine_state_from_reader(
    reader: Reader,
    layout: Any | None = None,
    module_base: int | None = None,
    *,
    allow_spectator_discovery: bool = False,
    allow_large_match_playback_discovery: bool = False,
) -> dict[str, Any]:
    """Read match state through an existing validated process handle."""
    layout = layout or reader.layout
    module_base = int(module_base if module_base is not None else reader.module_base)
    if layout.key == "fm24":
        pointer_rva = layout.match_session_pointer_rva
        vtable_rva = layout.match_session_vtable_rva
        if pointer_rva is None or vtable_rva is None:
            return {
                "active": False,
                "known": False,
                "phase": None,
                "mode": None,
                "reason": f"{layout.display_name} match-session layout is unavailable",
            }
        manager = reader.ptr(module_base + pointer_rva)
        if not manager or reader.ptr(manager) != module_base + vtable_rva:
            return {
                "active": False,
                "known": False,
                "phase": None,
                "mode": None,
                "reason": "FM24 match-session manager unavailable",
            }
        # MATCH_SESSION_MANAGER owns a vector of active native match
        # sessions at +0x08. It contains one entry while the match viewer
        # is running and is empty again after leaving the match.
        # Read the vector as one structure instead of using Reader.ptr():
        # a valid empty std::vector may store three null pointers, while
        # Reader.ptr() intentionally converts a null pointer to None.
        vector = reader.bytes(manager + 0x08, 0x18)
        if not vector or len(vector) != 0x18:
            return {
                "active": False,
                "known": False,
                "phase": None,
                "mode": None,
                "reason": "FM24 match-session vector unavailable",
            }
        header = decode_vector_header(
            vector, 8, 4096, 4096, allow_null_empty=True,
        )
        if header is None:
            return {
                "active": False,
                "known": False,
                "phase": None,
                "mode": None,
                "reason": "FM24 match-session vector is invalid",
            }
        session_count = header[3]
        spectator_session_count = 0
        game_session_vtable_rva = getattr(
            layout, "game_match_session_vtable_rva", None,
        )
        if session_count > 0 and game_session_vtable_rva is not None:
            session_slots = reader.bytes(header[0], session_count * 8)
            if session_slots and len(session_slots) == session_count * 8:
                for (session,) in struct.iter_unpack("<Q", session_slots):
                    if (
                        not session
                        or reader.ptr(session)
                        != module_base + int(game_session_vtable_rva)
                    ):
                        continue
                    viewer_vector = reader.bytes(session + 0x28, 0x18)
                    viewer_header = (
                        decode_vector_header(
                            viewer_vector, 8, 4096, 4096,
                            allow_null_empty=True,
                        )
                        if viewer_vector and len(viewer_vector) == 0x18
                        else None
                    )
                    if viewer_header is not None and viewer_header[3] > 0:
                        spectator_session_count += 1
        return {
            "active": session_count > 0,
            "known": True,
            "phase": None,
            "mode": None,
            "session_count": session_count,
            "spectator": spectator_session_count > 0,
            "spectator_session_count": spectator_session_count,
            "detector": "native_match_session_vector",
        }
    if layout.key != "fm26":
        return {
            "active": False,
            "known": False,
            "phase": None,
            "mode": None,
            "reason": f"{layout.display_name} match-engine flags are not mapped yet",
        }
    phase_rva = layout.match_engine_phase_rva
    mode_rva = layout.match_engine_mode_rva
    phase = reader.u32(module_base + phase_rva) if phase_rva is not None else None
    mode = reader.u32(module_base + mode_rva) if mode_rva is not None else None
    if layout.distribution == "xgp":
        if mode is None:
            return {
                "active": False, "known": False, "phase": phase, "mode": mode,
                "reason": "XGP match mode unavailable",
            }
        return {
            "active": mode == 4,
            "known": True,
            "phase": phase,
            "mode": mode,
            "detector": "xgp_match_mode",
        }
    state_rva = layout.match_engine_active_state_rva
    if state_rva is None:
        return {
            "active": False,
            "known": False,
            "phase": phase,
            "mode": mode,
            "state": None,
            "reason": "FM26 match runtime state is unavailable",
        }
    state_before = reader.u8(module_base + state_rva)
    state_after = reader.u8(module_base + state_rva)
    if state_before is None or state_after is None:
        return {
            "active": False,
            "known": False,
            "phase": phase,
            "mode": mode,
            "state": state_after,
            "reason": "FM26 match runtime state cannot be read",
        }
    if state_before != state_after:
        return {
            "active": False,
            "known": False,
            "phase": phase,
            "mode": mode,
            "state": state_after,
            "reason": "FM26 match runtime state changed during read",
        }
    if state_after in {4, 6}:
        return {
            "active": True,
            "known": True,
            "spectator": False,
            "phase": phase,
            "mode": mode,
            "state": state_after,
            "detector": "native_match_runtime_state",
        }
    if phase is None or mode is None:
        return {
            "active": False,
            "known": False,
            "phase": phase,
            "mode": mode,
            "state": state_after,
            "reason": "FM26 match phase or mode cannot be read",
        }
    if (phase, mode) != (0, 4):
        return {
            "active": False,
            "known": True,
            "spectator": False,
            "phase": phase,
            "mode": mode,
            "state": state_after,
            "processing_fixture_count": 0,
            "detector": "native_match_runtime_state",
        }
    pointer_rva = layout.play_fixture_manager_pointer_rva
    vtable_rva = layout.play_fixture_manager_vtable_rva
    manager = (
        reader.ptr(module_base + pointer_rva)
        if pointer_rva is not None else None
    )
    if (
        not manager
        or vtable_rva is None
        or reader.ptr(manager) != module_base + vtable_rva
    ):
        return {
            "active": False,
            "known": False,
            "phase": phase,
            "mode": mode,
            "state": state_after,
            "reason": "FM26 play-fixture manager cannot be validated",
        }
    queue_counts = []
    for offset in (0x20, 0x38):
        header = decode_vector_header(
            reader.bytes(manager + offset, 0x18),
            8,
            4096,
            4096,
            allow_null_empty=True,
        )
        if header is None:
            return {
                "active": False,
                "known": False,
                "phase": phase,
                "mode": mode,
                "state": state_after,
                "reason": "FM26 play-fixture processing queue is invalid",
            }
        queue_counts.append(header[3])
    processing_count = sum(queue_counts)
    spectator = state_after == 0 and processing_count > 0
    return {
        "active": processing_count > 0,
        "known": True,
        "spectator": spectator,
        "phase": phase,
        "mode": mode,
        "state": state_after,
        "processing_fixture_count": processing_count,
        "processing_queue_counts": queue_counts,
        "detector": "native_match_runtime_and_processing_queues",
    }


def read_match_engine_state() -> dict[str, Any]:
    """Read the build-specific native match-engine state."""
    with open_supported_reader() as (_process_path, layout, module, reader):
        return read_match_engine_state_from_reader(
            reader, layout=layout, module_base=module.base_address,
        )


def read_game_runtime_state(
    *,
    discover_match_playback: bool = False,
    discover_large_match_playback: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the game clock and match state through one process selection."""
    with open_supported_reader() as (_process_path, layout, module, reader):
        return (
            read_game_clock_from_reader(reader),
            read_match_engine_state_from_reader(
                reader, layout=layout, module_base=module.base_address,
                allow_spectator_discovery=discover_match_playback,
                allow_large_match_playback_discovery=(
                    discover_large_match_playback
                ),
            ),
        )


def read_save_identity(
    *, preferred_save_id: str | None = None,
    preferred_manager_id: int | None = None,
    known_manager_sessions: list[dict[str, Any]] | None = None,
) -> str | None:
    """Return the active career instance ID without generating fixtures or odds."""
    with open_supported_reader() as (_process_path, layout, module, reader):
        identity = read_savegame_identity(reader)
        equivalent_stable_key = None
        if layout.key == "fm24":
            if preferred_save_id:
                save_name, _candidates = read_cached_save_name(
                    reader, module, identity,
                )
                stable_key = stable_save_key(layout, identity, save_name)
                previous_name = confirmed_save_name(preferred_save_id)
                if (
                    stable_key
                    and save_name
                    and previous_name
                    and str(save_name).casefold() != str(previous_name).casefold()
                    and _fm24_manager_survives_save_name_change(
                        reader, preferred_manager_id, known_manager_sessions,
                    )
                ):
                    # FM24 renames a network save after a club change. Keep
                    # the durable career identity; the manager refresh below
                    # will publish the new club and save label.
                    return str(preferred_save_id)
                if not stable_key and known_manager_sessions is not None:
                    known_ids = {
                        int(item.get("manager_id") or item.get("id") or 0)
                        for item in known_manager_sessions
                        if int(item.get("manager_id") or item.get("id") or 0) > 0
                    }
                    validated = _validated_known_human_manager_sessions(
                        reader, known_manager_sessions,
                    )
                    if preferred_manager_id and any(
                        int(item.get("manager_id") or 0)
                        == int(preferred_manager_id)
                        for item in validated
                    ):
                        return str(preferred_save_id)

                    # The previously confirmed manager objects disappeared.
                    # This is the meaningful point at which a local provider
                    # discovery is worth its heap-scan cost: a changed local
                    # save name is strong switch evidence, while a network
                    # career still falls back to re-discovering human managers.
                    save_name, _candidates = read_save_name(
                        reader, module, identity,
                    )
                    stable_key = stable_save_key(layout, identity, save_name)
                    if not stable_key:
                        sessions = discover_human_managers(
                            reader, [],
                            preferred_manager_id=preferred_manager_id,
                            allow_expensive_direct_scan=True,
                        )
                        observed_ids = {
                            int(item.get("manager_id") or 0)
                            for item in sessions
                            if int(item.get("manager_id") or 0) > 0
                        }
                        # A surviving human-manager UID proves that this is
                        # still the same network career even if jobs, teams or
                        # the complete manager set changed.
                        if known_ids & observed_ids:
                            return str(preferred_save_id)
                        stable_key = fm24_network_save_key(layout, sessions)
            else:
                stable_key, equivalent_stable_key, save_name = fm24_save_evidence(
                    layout, reader, module, identity, discover_managers=False,
                )
            if not stable_key and preferred_save_id:
                # Without enough current manager evidence, a weak network
                # context must retain the last confirmed career. A transient
                # read failure is not permission to create an empty account.
                return str(preferred_save_id)
            if not stable_key:
                return None
        else:
            save_name = None
            stable_key = stable_save_key(layout, identity, None)
        resolved = layout_save_identity(
            layout,
            resolve_save_identity(
                None, None, stable_id=stable_key,
                equivalent_stable_id=equivalent_stable_key,
                namespace=layout.key,
            ),
        )
        remember_save_name(resolved, save_name)
        return resolved


def read_verified_results() -> list[dict[str, Any]]:
    if not RESULT_OVERRIDES_PATH.exists():
        return []
    try:
        payload = json.loads(RESULT_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("results", payload) if isinstance(payload, dict) else payload
    return [
        {
            **item,
            "settlement_verified": True,
            "settlement_evidence": "manual_verified_result_override",
        }
        for item in rows if isinstance(item, dict)
    ]


def merge_verified_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge results absent from FM's generic result pool, preferring verified rows."""
    merged = {}
    for item in [*results, *read_verified_results()]:
        try:
            key = _result_identity(item)
        except (KeyError, TypeError, ValueError):
            continue
        merged[key] = item
    return deduplicate_results(list(merged.values()))


def result_history_path(save_instance_id: str | None) -> Path | None:
    return owned_result_history_path(save_instance_id)


def legacy_result_history_path(save_instance_id: str | None) -> Path | None:
    return owned_legacy_result_history_path(save_instance_id)


def _result_history_owner(save_instance_id: str | None) -> ResultHistoryOwner:
    return ResultHistoryOwner(
        save_instance_id=save_instance_id,
        durable_path=result_history_path(save_instance_id),
        legacy_path=legacy_result_history_path(save_instance_id),
    )


def read_result_history(save_instance_id: str | None) -> list[dict[str, Any]]:
    return _result_history_owner(save_instance_id).load()


def read_result_goal_events(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read one still-live result event vector only when a user needs it."""
    if isinstance(result.get("goal_events"), list):
        return deepcopy(result["goal_events"])
    if str(result.get("result_source") or "").lower() not in {
        "fixture_result_archive", "fixture_result_pointer",
    }:
        return None
    try:
        address = int(str(result.get("result_address") or "0"), 0)
    except (TypeError, ValueError):
        return None
    if address <= 0:
        return None
    with open_supported_reader() as (_path, _layout, _module, reader):
        current = parse_completed_result(reader, address)
        if not result_snapshot_matches(result, current):
            return None
        events = parse_goal_events(reader, address)
    return deepcopy(events) if isinstance(events, list) else None


def enrich_halftime_results(
    reader: Reader,
    results: list[dict[str, Any]],
    required_keys: set[tuple[str, int, int, int]],
) -> list[dict[str, Any]]:
    """Attach half-time scores only to results currently needed for settlement."""
    enriched = []
    for item in results:
        item = sanitize_result_details(item)
        if (
            _result_identity(item) not in required_keys
            or result_details_consistent(item)
        ):
            enriched.append(item)
            continue
        try:
            address = int(str(item.get("result_address") or "0"), 16)
        except ValueError:
            address = 0
        # A persistent BASIC_SCORELINE address is not a FIXTURE_RESULT.  Its
        # bytes can coincidentally satisfy parts of the event parser and then
        # attach another match's short-lived detail vector.  Only archive
        # results own the layout described by result_event_*.
        detail_source = str(item.get("result_source") or "").lower()
        eligible = bool(address) and detail_source in {
            "fixture_result_archive", "fixture_result_pointer",
        }
        current_before = parse_completed_result(reader, address) if eligible else None
        summary = (
            parse_result_event_summary(
                reader,
                address,
                expected_home_goals=int(item["home_goals"]),
                expected_away_goals=int(item["away_goals"]),
            )
            if result_snapshot_matches(item, current_before)
            else None
        )
        current_after = parse_completed_result(reader, address) if summary is not None else None
        candidate = (
            sanitize_result_details({**item, **summary})
            if summary is not None else None
        )
        if (
            candidate is None
            or not result_snapshot_matches(item, current_after)
        ):
            enriched.append(item)
        else:
            enriched.append(candidate)
    return enriched


MATCH_INTELLIGENCE_RESULT_SOURCES = {
    "fixture_result_archive",
    "fixture_result_pointer",
    "persistent_season_record",
    "persistent_fixture_pointer",
    "persistent_competition_alias",
    "basic_scoreline",
}


def hidden_future_result_candidates(
    reader: Reader,
    results: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    game_date_text: str,
) -> list[dict[str, Any]]:
    """Build the private 7F intelligence pool from results hidden in normal history.

    The public result page intentionally excludes every result dated today or
    later.  Only rows that also match a currently open fixture are eligible for
    sale, so an old/stale result cannot be attached by team names or dates alone.
    """
    open_matches: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    for match in matches:
        try:
            key = (
                str(match["fixture_date"]), int(match["competition_id"]),
                int(match["home"]["id"]), int(match["away"]["id"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        open_matches[key] = match

    # ``deduplicate_results`` intentionally keeps different scorelines for the
    # same fixture identity so the normal history merger can surface a
    # conflict.  That is unsafe for intelligence: the purchase stores one
    # scoreline under the fixture id, so iterating those rows would make the
    # result depend on cache/source ordering.  Collapse equal evidence and
    # fail closed when the same fixture has contradictory final scores.
    results_by_key: dict[
        tuple[str, int, int, int], list[dict[str, Any]]
    ] = defaultdict(list)
    conflict_keys: set[tuple[str, int, int, int]] = set()
    for result in results:
        try:
            key = _result_identity(result)
        except (KeyError, TypeError, ValueError):
            continue
        if key[0] < game_date_text or key not in open_matches:
            continue
        if result.get("result_conflict"):
            conflict_keys.add(key)
            continue
        try:
            int(result["home_goals"])
            int(result["away_goals"])
        except (KeyError, TypeError, ValueError):
            continue
        source = str(result.get("result_source") or result.get("source") or "").lower()
        if source not in MATCH_INTELLIGENCE_RESULT_SOURCES:
            continue
        results_by_key[key].append(result)

    candidates: list[dict[str, Any]] = []
    for key, evidence in results_by_key.items():
        if key in conflict_keys:
            continue
        score_pairs = {
            (int(item["home_goals"]), int(item["away_goals"]))
            for item in evidence
        }
        if len(score_pairs) != 1:
            # A stale/reused result object or two unmerged sources disagree.
            # Do not sell either value until the result pipeline resolves it.
            continue
        result = evidence[0]
        home_goals, away_goals = next(iter(score_pairs))
        match = open_matches[key]
        candidate = {
            "fixture_id": "|".join(map(str, key)),
            "fixture_date": key[0],
            "kickoff_minutes": match.get("kickoff_minutes"),
            "kickoff_time": match.get("kickoff_time"),
            "competition_id": key[1],
            "competition_name": str(match.get("competition_name") or result["competition"].get("name") or key[1]),
            "home": dict(match.get("home") or result["home_team"]),
            "away": dict(match.get("away") or result["away_team"]),
            "home_goals": home_goals,
            "away_goals": away_goals,
            "result_source": str(
                result.get("result_source") or result.get("source") or ""
            ).lower(),
            "available_tiers": ["outcome", "total_goals", "score"],
        }
        for field in (
            "winner_side", "decided_by", "after_extra_time_home_goals",
            "after_extra_time_away_goals", "penalty_shootout_home_goals",
            "penalty_shootout_away_goals",
        ):
            if result.get(field) is not None:
                candidate[field] = result[field]

        candidates.append(candidate)
    return sorted(candidates, key=lambda item: (
        str(item["fixture_date"]),
        int(item.get("kickoff_minutes") or 24 * 60),
        str(item.get("competition_name") or ""),
        str((item.get("home") or {}).get("name") or ""),
    ))


def open_match_intelligence_targets(
    matches: list[dict[str, Any]],
    game_date_text: str,
    game_minutes: int,
) -> tuple[
    list[dict[str, Any]],
    set[tuple[str, int, int, int]],
    dict[tuple[str, int, int, int], int],
]:
    """Return exact open-fixture identities eligible for private intelligence.

    The scheduled kickoff minute remains eligible, matching the normal market
    clock boundary.  Once the verified game clock moves past kickoff, the
    fixture is removed even if a stale market snapshot still contains it.
    """
    eligible: list[dict[str, Any]] = []
    keys: set[tuple[str, int, int, int]] = set()
    fixture_hints: dict[tuple[str, int, int, int], int] = {}
    for match in matches:
        try:
            fixture_date = str(match["fixture_date"])
            kickoff = int(match["kickoff_minutes"])
            key = (
                fixture_date,
                int(match["competition_id"]),
                int(match["home"]["id"]),
                int(match["away"]["id"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if fixture_date < game_date_text or (
            fixture_date == game_date_text and kickoff < int(game_minutes)
        ):
            continue
        eligible.append(match)
        keys.add(key)
        try:
            address = int(str(match.get("fixture_address") or "0"), 0)
        except (TypeError, ValueError):
            address = 0
        if address > 0:
            fixture_hints[key] = address
    return eligible, keys, fixture_hints


def probe_match_intelligence_candidates(
    matches: list[dict[str, Any]],
    game_date_text: str,
    game_minutes: int,
) -> dict[str, Any]:
    """Probe pre-generated results without rebuilding odds or scanning the heap.

    This reuses the verified result-address cache, bounded fingerprints and
    fixture-result pointers maintained by the normal refresh pipeline.  Once
    bounded result regions are known, their normal ten-second rescan can find
    archive-only slots that are absent from the native fixture pool.  An empty
    cache still avoids starting a new whole-heap scan from this worker.
    """
    eligible, required_keys, fixture_hints = open_match_intelligence_targets(
        matches, game_date_text, game_minutes,
    )
    if not required_keys:
        return {
            "candidates": [], "changed_objects": 0,
            "bytes_scanned": 0, "region_count": 0,
        }
    with open_supported_reader() as (_process_path, _layout, _module, reader):
        state = _runtime_state(reader)
        with _RUNTIME_CACHE_LOCK:
            allow_bounded_region_rescan = bool(
                state.get("result_region_spans")
            )
        probe = probe_live_completed_results_with_reader(
            reader,
            required_keys,
            allow_expensive_rescan=allow_bounded_region_rescan,
            fixture_hints=fixture_hints,
        )
        candidates = hidden_future_result_candidates(
            reader,
            list(probe.get("results") or []),
            eligible,
            game_date_text,
        )
    return {**probe, "candidates": candidates}


def _result_priority(item: dict[str, Any]) -> tuple[int, int]:
    source = str(item.get("source") or item.get("result_source") or "").lower()
    verified = int("verified" in source or "club schedule" in source)
    try:
        address = int(str(item.get("result_address", "0")), 16)
    except ValueError:
        address = 0
    return verified, address


def resolve_daily_team_conflicts(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        by_date[str(item.get("date", ""))].append(item)
    resolved = []
    for date_text in sorted(by_date):
        occupied: set[int] = set()
        for item in sorted(by_date[date_text], key=_result_priority, reverse=True):
            teams = {int(item["home_team"]["id"]), int(item["away_team"]["id"])}
            if occupied & teams:
                continue
            resolved.append(item)
            occupied.update(teams)
    return sorted(resolved, key=_result_identity)


def merge_result_history(results: list[dict[str, Any]], save_instance_id: str | None) -> list[dict[str, Any]]:
    return _result_history_owner(save_instance_id).merge(
        results,
        load_existing=read_result_history,
    )


def save_result_history(
    results: list[dict[str, Any]],
    save_instance_id: str | None,
    game_date_text: str | None = None,
    protected_result_keys: set[tuple[str, int, int, int]] | None = None,
) -> None:
    _result_history_owner(save_instance_id).save(
        results,
        game_date_text,
        protected_result_keys,
        merge_results=merge_result_history,
    )


def competition_goal_baseline(
    results: list[dict[str, Any]], competition_id: int, game_date_text: str, limit: int = 300
) -> dict[str, Any]:
    sample = sorted(
        (
            item for item in results
            if item["competition"]["id"] == competition_id and item["date"] < game_date_text
        ),
        key=lambda item: item["date"],
        reverse=True,
    )[:limit]
    # Shrink sparse competitions toward a conservative cross-league prior.
    prior_matches = 40
    home_goals = sum(item["home_goals"] for item in sample)
    away_goals = sum(item["away_goals"] for item in sample)
    return {
        "home": round((home_goals + prior_matches * 1.45) / (len(sample) + prior_matches), 3),
        "away": round((away_goals + prior_matches * 1.25) / (len(sample) + prior_matches), 3),
        "sample": len(sample),
        "prior_matches": prior_matches,
    }


def standings_model_profiles(
    season_league_results: list[dict[str, Any]],
) -> dict[tuple[int, int], dict[str, Any]]:
    """Expose current-season table form to the odds model without extra FM reads."""
    profiles: dict[tuple[int, int], dict[str, Any]] = {}
    for table in build_standings(season_league_results):
        rows = table.get("teams") or []
        total_played = sum(max(int(row.get("played") or 0), 0) for row in rows)
        league_average = (
            sum(max(float(row.get("goals_for") or 0.0), 0.0) for row in rows) / total_played
            if total_played else 0.0
        )
        for row in rows:
            profiles[(int(table["competition_id"]), int(row["team_id"]))] = {
                **row,
                "league_average_goals": round(league_average, 4),
            }
    return profiles


def season_window(game_date: datetime | Any) -> tuple[str, str]:
    value = game_date.date() if isinstance(game_date, datetime) else game_date
    start_year = value.year if value.month >= 7 else value.year - 1
    return f"{start_year}-07-01", f"{start_year + 1}-06-30"


def competition_season_starts(
    game_date: datetime | Any,
    competition_formats: list[dict[str, Any]],
    results: list[dict[str, Any]] | None = None,
) -> dict[int, str]:
    from tools.championship_odds import FAMOUS_LEAGUES

    value = game_date.date() if isinstance(game_date, datetime) else game_date
    oldest_supported = value - timedelta(days=370)
    cross_year_start = date(
        value.year if value.month >= 7 else value.year - 1, 7, 1,
    )

    def stale_cross_year_start(candidate: date) -> bool:
        return candidate.month >= 7 and candidate < cross_year_start

    starts: dict[int, str] = {}
    results_by_competition: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for result in results or []:
        try:
            competition_id = int((result.get("competition") or {}).get("id") or 0)
            result_date = date.fromisoformat(str(result.get("date") or ""))
        except (TypeError, ValueError):
            continue
        if competition_id > 0 and oldest_supported <= result_date < value:
            results_by_competition[competition_id].append(result)
    for item in competition_formats:
        competition_id = int(item.get("competition_id") or 0)
        configured_calendar = str(
            (FAMOUS_LEAGUES.get(competition_id) or {}).get("calendar") or ""
        )
        if configured_calendar == "cross_year":
            continue
        if configured_calendar == "calendar_year":
            starts[competition_id] = f"{value.year}-01-01"
            continue
        participant_ids = {
            int(team.get("id") or 0)
            for team in item.get("opening_teams") or []
            if int(team.get("id") or 0) > 0
        }
        competition_results = results_by_competition.get(competition_id) or []
        try:
            season_address = int(
                str(item.get("competition_season_address") or "0"), 0,
            )
        except (TypeError, ValueError):
            season_address = 0
        if season_address and competition_results:
            addressed_results = []
            matching_results = []
            for result in competition_results:
                try:
                    result_address = int(str(
                        (result.get("competition") or {}).get("address") or "0"
                    ), 0)
                except (TypeError, ValueError):
                    result_address = 0
                if not result_address:
                    continue
                addressed_results.append(result)
                if result_address == season_address:
                    matching_results.append(result)
            if addressed_results:
                competition_results = matching_results
        if participant_ids and competition_results:
            current_dates: list[date] = []
            outside_dates: list[date] = []
            for result in competition_results:
                try:
                    result_date = date.fromisoformat(str(result["date"]))
                    team_ids = {
                        int((result.get(side) or {}).get("id") or 0)
                        for side in ("home_team", "away_team")
                    }
                except (KeyError, TypeError, ValueError):
                    continue
                (current_dates if team_ids <= participant_ids else outside_dates).append(result_date)
            cutoff = max(outside_dates, default=None)
            eligible = sorted(day for day in current_dates if cutoff is None or day > cutoff)
            if eligible:
                inferred_start = eligible[0]
                if cutoff is None:
                    unique_dates = sorted(set(eligible))
                    for previous, current in zip(unique_dates, unique_dates[1:]):
                        gap = (current - previous).days
                        cross_year_break = previous.month in {4, 5, 6, 7} and current.month in {6, 7, 8, 9, 10}
                        calendar_year_break = previous.month in {10, 11, 12} and current.month in {1, 2, 3, 4, 5}
                        if gap >= 35 and (cross_year_break or calendar_year_break):
                            inferred_start = current
                if not stale_cross_year_start(inferred_start):
                    starts[competition_id] = inferred_start.isoformat()
                    continue
        if not item.get("season_fixture_bounds_verified"):
            continue
        try:
            first_fixture = date.fromisoformat(str(item.get("first_fixture_date") or ""))
        except ValueError:
            continue
        if competition_id <= 0:
            continue
        if (
            oldest_supported <= first_fixture <= value
            and not stale_cross_year_start(first_fixture)
        ):
            # Calendar-year leagues can begin before the July cross-year fallback.
            starts[competition_id] = first_fixture.isoformat()
        elif first_fixture > value and first_fixture.month <= 6:
            # A discovered next season beginning in the first half of the year
            # still identifies the active competition as calendar-year.
            starts[competition_id] = f"{value.year}-01-01"
    return starts


def generate_all_odds(
    days: int = 14,
    required_result_keys: set[tuple[str, int, int, int]] | None = None,
    *,
    refresh_mode: str = "full",
    previous_snapshot: dict[str, Any] | None = None,
    preferred_save_id: str | None = None,
    preferred_manager: dict[str, Any] | None = None,
    preferred_managed_teams: list[dict[str, Any]] | None = None,
    preferred_manager_options: list[dict[str, Any]] | None = None,
    preferred_manager_id: int | None = None,
    competition_scope: str = "all",
    favorite_team_ids: set[int] | None = None,
    hidden_competition_ids: set[int] | None = None,
    force_fixture_scan: bool = False,
    force_fixture_heap_scan: bool = False,
    cancel_check: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    championship_enabled: bool = True,
    discover_future_league_seasons: bool = False,
    connection_context_verified: bool = False,
    yield_memory_lock: Callable[[], None] | None = None,
) -> dict[str, Any]:
    if refresh_mode not in {"full", "fast"}:
        raise ValueError(f"unknown refresh mode: {refresh_mode}")
    if competition_scope not in ODDS_SCOPE_OPTIONS:
        raise ValueError(f"unknown competition scope: {competition_scope}")
    if days not in {3, 7, 14}:
        raise ValueError(f"unsupported odds window: {days}")
    fast_refresh = refresh_mode == "fast"
    started_at = perf_counter()
    last_progress_emit = 0.0

    def publish_progress(
        phase: str, text: str, completed: int | None = None,
        total: int | None = None, *, force: bool = False,
    ) -> None:
        nonlocal last_progress_emit
        if progress_callback is None:
            return
        now = perf_counter()
        if not force and now - last_progress_emit < 0.2:
            return
        payload: dict[str, Any] = {"phase": phase, "text": text}
        if completed is not None:
            payload["completed"] = max(0, int(completed))
        if total is not None:
            payload["total"] = max(0, int(total))
        try:
            progress_callback(payload)
            last_progress_emit = now
        except Exception:
            # Diagnostics and UI progress must never make a refresh fail.
            pass

    publish_progress("open_reader", "正在连接 Football Manager", force=True)
    with open_supported_reader() as (process_path, layout, module, reader):
        game_date_code = read_layout_game_date_code(reader)
        game_date = decode_date(game_date_code)
        if not game_date:
            raise RuntimeError("game date unavailable")
        game_date_text = game_date.isoformat()
        game_minutes = decode_kickoff_minutes(game_date_code)
        phase_started = perf_counter()
        stable_identity = read_savegame_identity(reader)
        # FM24 exposes its local-save provider later than the core date and
        # fixture structures on some loads.  Resolve it after the human-manager
        # scan below, matching the order used by tools that reliably attach to
        # this build.  FM26 keeps its direct stable-ID/name path unchanged.
        stable_save_name, _save_name_candidates = (
            read_save_name(reader, module, stable_identity)
            if layout.key == "fm26" else (None, [])
        )
        stable_key = stable_save_key(layout, stable_identity, stable_save_name)
        save_identity_ms = round((perf_counter() - phase_started) * 1000, 1)
        preliminary_save_id = (
            layout_save_identity(
                layout,
                resolve_save_identity(
                    None, None,
                    preferred_id=preferred_save_id,
                    stable_id=stable_key,
                    namespace=layout.key,
                ),
            )
            if layout.key == "fm26" and stable_key and preferred_save_id else None
        )
        reuse_confirmed_manager_context = bool(
            preferred_manager
            and preferred_managed_teams
            and (
                connection_context_verified
                or (
                    previous_snapshot
                    and (
                        (
                            layout.key == "fm26"
                            and save_identity_matches(preliminary_save_id, preferred_save_id)
                        )
                        or (
                            layout.key == "fm24"
                            and previous_snapshot.get("connection_context_verified")
                            and save_identity_matches(
                                str(previous_snapshot.get("save_instance_id") or ""),
                                preferred_save_id,
                            )
                        )
                    )
                )
            )
        )
        phase_started = perf_counter()
        stable_scope = None
        if stable_identity:
            stable_scope = f"save-{stable_identity['savegame_id']}"
            if stable_identity.get("session_nonce"):
                stable_scope += f":session-{stable_identity['session_nonce']}"
        telemetry_session = None
        if not reuse_confirmed_manager_context:
            telemetry_session = discover_manager_session(
                reader, game_date, stable_scope=stable_scope, cancel_check=cancel_check,
            )
        timings: dict[str, float] = {
            "save_identity_ms": save_identity_ms,
            "manager_session_ms": round((perf_counter() - phase_started) * 1000, 1),
            "manager_session_reused": int(reuse_confirmed_manager_context),
        }

        catalog = {
            competition_id: competition_metadata(competition_id, details[0])
            for competition_id, details in WATCHED_COMPETITIONS.items()
        }
        catalog = {key: value for key, value in catalog.items() if value}
        catalog[-1] = {"id": -1, "name": "国家队比赛", "kind": "national", "reputation": 100, "aggregate": True}
        fixture_rows = []
        fixture_filter_counts: dict[str, int] = defaultdict(int)
        seen_fixtures: set[tuple[str, int, int, int]] = set()
        phase_started = perf_counter()
        publish_progress("fixture_scan", "正在发现赛程与赛果对象", force=True)
        pre_scanned_result_addresses: list[int] | None = None
        fixture_scan_bytes = 0
        if fast_refresh:
            fixture_addresses, fixture_cache_hit = cached_fixture_addresses(
                reader, force_scan=force_fixture_scan, cancel_check=cancel_check,
            )
            fixture_scan_bytes = int(getattr(reader, "last_fixture_scan_bytes", 0))
        else:
            fixture_addresses, pre_scanned_result_addresses, fixture_scan_bytes = (
                scan_fixture_and_result_addresses(
                    reader, cancel_check=cancel_check,
                    use_pool=not force_fixture_heap_scan,
                )
            )
            state = _runtime_state(reader)
            fixture_addresses = _update_fixture_address_cache(
                reader, fixture_addresses,
            )
            with _RUNTIME_CACHE_LOCK:
                state["result_addresses"] = list(pre_scanned_result_addresses)
            fixture_cache_hit = False
        fixture_scan_mode = (
            str((previous_snapshot or {}).get("fixture_scan_mode") or "incremental")
            if fast_refresh and fixture_cache_hit else
            str(getattr(reader, "last_fixture_scan_mode", "incremental"))
        )
        fixture_pool_source = getattr(reader, "fixture_pool_source", None)
        timings["fixture_scan_bytes"] = int(fixture_scan_bytes)
        timings["fixture_pool_source"] = str(fixture_pool_source or "none")
        header_started = perf_counter()
        fixture_snapshots, fixture_header_bytes = read_fixture_snapshots_at_addresses(
            reader,
            fixture_addresses,
            progress=lambda completed, total: publish_progress(
                "fixture_headers", "正在批量读取赛程头", completed, total,
            ),
            cancel_check=cancel_check,
        )
        timings["fixture_header_bytes"] = int(fixture_header_bytes)
        timings["fixture_headers_ms"] = round(
            (perf_counter() - header_started) * 1000, 1,
        )
        publish_progress(
            "fixture_headers", "正在批量读取赛程头",
            len(fixture_addresses), len(fixture_addresses), force=True,
        )
        # Competition formats own standings as well as outright markets. Keep
        # their ordinary discovery window independent of the outright-market
        # setting, and allow the standings page to request a wider one-off
        # search for a newly created season before it reaches the odds window.
        fixture_discovery_days = max(days, CHAMPIONSHIP_DISCOVERY_DAYS)
        known_season_addresses = {
            int(str(item.get("competition_season_address") or "0"), 0)
            for item in (previous_snapshot or {}).get("competition_formats") or []
            if str(item.get("competition_season_address") or "").startswith("0x")
        }
        future_season_candidates: dict[int, Any] = {}
        season_fixture_bounds: dict[int, tuple[date, date]] = {}

        def append_fixture_row(fixture: Any, competition: dict[str, Any] | None = None) -> bool:
            competition = competition or reader.competition(fixture.competition_season)
            home_team = reader.team(fixture.home_team)
            away_team = reader.team(fixture.away_team)
            if not competition:
                fixture_filter_counts["missing_competition"] += 1
                return False
            if not home_team or not away_team:
                fixture_filter_counts["missing_team_object"] += 1
                return False
            if not has_resolved_team_name(home_team) or not has_resolved_team_name(away_team):
                fixture_filter_counts["unresolved_team_name"] += 1
                return False
            meta = competition_metadata(
                competition["id"], competition_name(competition),
                national_teams=(
                    home_team.get("team_type") == "national"
                    and away_team.get("team_type") == "national"
                ),
            )
            if not meta:
                fixture_filter_counts["excluded_competition"] += 1
                return False
            key = (fixture.match_date.isoformat(), meta["id"], home_team["id"], away_team["id"])
            if key in seen_fixtures:
                fixture_filter_counts["duplicate"] += 1
                return False
            seen_fixtures.add(key)
            catalog[meta["id"]] = meta
            fixture_rows.append((fixture, meta, home_team, away_team))
            fixture_filter_counts["accepted"] += 1
            return True

        fixture_total = len(fixture_addresses)
        parsed_fixtures = []
        for fixture_index, address in enumerate(fixture_addresses, start=1):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            publish_progress(
                "fixture_filter", "正在筛选盘口日期窗口",
                fixture_index, fixture_total,
            )
            fixture = parse_fixture_from_raw(
                reader, address, fixture_snapshots.get(address),
                validate_references=False,
            )
            if not fixture:
                fixture_filter_counts["parse_failed"] += 1
                continue
            fixture_filter_counts["parsed"] += 1
            parsed_fixtures.append(fixture)
            season_address = int(fixture.competition_season or 0)
            if season_address:
                previous_bounds = season_fixture_bounds.get(season_address)
                season_fixture_bounds[season_address] = (
                    min(previous_bounds[0], fixture.match_date)
                    if previous_bounds else fixture.match_date,
                    max(previous_bounds[1], fixture.match_date)
                    if previous_bounds else fixture.match_date,
                )
        publish_progress(
            "fixture_filter", "正在筛选盘口日期窗口",
            fixture_total, fixture_total, force=True,
        )

        favorite_scope_season_addresses: set[int] = set()
        favorite_scope_competition_ids: set[int] = set()
        if competition_scope == "favorite_schedule":
            favorite_scope_started = perf_counter()
            favorite_season_start, favorite_season_end = season_window(game_date)
            favorite_season_end = max(
                favorite_season_end,
                (game_date + timedelta(days=fixture_discovery_days)).isoformat(),
            )
            if (
                (championship_enabled and not fast_refresh)
                or discover_future_league_seasons
            ):
                favorite_season_end = max(
                    favorite_season_end,
                    (game_date + timedelta(days=370)).isoformat(),
                )
            publish_progress(
                "schedule_scope", "正在建立收藏赛事索引",
                0, len(parsed_fixtures), force=True,
            )
            (
                favorite_scope_season_addresses,
                favorite_scope_competition_ids,
            ) = favorite_schedule_scope_from_fixture_headers(
                reader,
                parsed_fixtures,
                set(favorite_team_ids or set()),
                favorite_season_start,
                favorite_season_end,
                cancel_check=cancel_check,
                progress_callback=lambda completed, total: publish_progress(
                    "schedule_scope", "正在建立收藏赛事索引",
                    completed, total,
                ),
            )
            timings["favorite_schedule_scope_ms"] = round(
                (perf_counter() - favorite_scope_started) * 1000, 1,
            )
            timings["favorite_schedule_seasons"] = len(
                favorite_scope_season_addresses
            )
            timings["favorite_schedule_competitions"] = len(
                favorite_scope_competition_ids
            )
            publish_progress(
                "schedule_scope", "收藏赛事索引建立完成",
                len(parsed_fixtures), len(parsed_fixtures), force=True,
            )

        hidden_scope_candidates = (
            [
                fixture for fixture in parsed_fixtures
                if int(fixture.competition_season or 0)
                in favorite_scope_season_addresses
            ]
            if competition_scope == "favorite_schedule"
            else parsed_fixtures
        )
        hidden_scope_season_addresses = (
            hidden_competition_seasons_from_fixture_headers(
                reader,
                hidden_scope_candidates,
                set(hidden_competition_ids or set()),
                cancel_check=cancel_check,
            )
        )
        timings["hidden_competition_seasons"] = len(
            hidden_scope_season_addresses
        )

        for fixture in parsed_fixtures:
            season_address = int(fixture.competition_season or 0)
            if (
                competition_scope == "favorite_schedule"
                and season_address not in favorite_scope_season_addresses
            ):
                fixture_filter_counts["scope_excluded"] += 1
                continue
            if season_address in hidden_scope_season_addresses:
                fixture_filter_counts["hidden_competition"] += 1
                continue
            if not fixture_is_open(fixture, game_date, game_minutes):
                fixture_filter_counts["already_started_or_past"] += 1
                continue
            if fixture.match_date > game_date + timedelta(days=fixture_discovery_days):
                fixture_filter_counts["beyond_window"] += 1
                if (
                    (
                        (championship_enabled and not fast_refresh)
                        or discover_future_league_seasons
                    )
                    and season_address not in known_season_addresses
                    and fixture.match_date <= game_date + timedelta(days=370)
                    and (
                        season_address not in future_season_candidates
                        or fixture.match_date
                        < future_season_candidates[season_address].match_date
                    )
                ):
                    future_season_candidates[season_address] = fixture
                continue
            append_fixture_row(fixture)

        discovered_representative_seasons = 0
        discovered_competition_ids = {
            int(meta.get("id") or 0)
            for _fixture, meta, _home, _away in fixture_rows
            if int(meta.get("id") or 0) > 0
        }
        for fixture in sorted(
            future_season_candidates.values(), key=lambda item: item.match_date,
        ):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            competition = reader.competition(fixture.competition_season)
            competition_id = int((competition or {}).get("id") or 0)
            if not competition_id or competition_id in discovered_competition_ids:
                continue
            if append_fixture_row(fixture, competition):
                discovered_representative_seasons += 1
                discovered_competition_ids.add(competition_id)
        fixture_rows = collapse_adjacent_duplicate_fixtures(fixture_rows)
        knockout_contexts = annotate_knockout_fixture_rows(reader, fixture_rows)
        competition_formats = native_competition_format_snapshots(
            reader, fixture_rows, season_fixture_bounds, season_fixtures=parsed_fixtures,
        )
        timings["known_competition_seasons_discovered"] = (
            discovered_representative_seasons
        )
        timings["fixtures_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        timings["fixture_candidates"] = fixture_total
        timings["fixture_filter_counts"] = {
            **dict(sorted(fixture_filter_counts.items())),
            "before_scope_filter": len(fixture_rows),
        }
        timings["fixture_window_rows"] = len(fixture_rows)

        phase_started = perf_counter()
        requested_manager_id = preferred_manager_id or int(
            (preferred_manager or {}).get("id") or 0
        )
        # Cached sessions are only hints: discover_human_managers revalidates
        # the manager vtable/UID, team UID and reverse reference before reuse.
        known_manager_options = list(
            preferred_manager_options
            if preferred_manager_options is not None
            else (previous_snapshot or {}).get("manager_options") or []
        )
        publish_progress(
            "manager_identity", "正在校验当前经理和执教队伍",
            0, len(fixture_addresses), force=True,
        )
        human_manager_sessions = discover_human_managers(
            reader,
            fixture_addresses,
            preferred_manager_id=requested_manager_id or None,
            known_manager_sessions=(
                known_manager_options
                if (
                    known_manager_options
                    and preferred_manager
                    and preferred_managed_teams
                ) else None
            ),
            fixture_snapshots=fixture_snapshots,
            progress_callback=lambda phase, completed, total: publish_progress(
                "manager_identity",
                (
                    "正在从赛程头汇总球队地址"
                    if phase == "fixtures" else "正在校验球队经理关系"
                ),
                completed, total,
            ),
            cancel_check=cancel_check,
        )
        publish_progress(
            "manager_identity", "当前经理和执教队伍校验完成",
            1, 1, force=True,
        )
        publish_progress("save_identity", "正在确认当前存档身份", force=True)
        timings["human_manager_reused"] = int(
            bool(human_manager_sessions)
            and all(
                item.get("source") == "verified_snapshot_manager"
                for item in human_manager_sessions
            )
        )
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        equivalent_stable_key = None
        if layout.key == "fm24":
            save_name_started = perf_counter()
            if connection_context_verified and preferred_save_id:
                # The connection handshake has already anchored this live FM24
                # session. Repeating provider/name discovery here can fall back
                # to a full private-heap scan and starve every local HTTP route.
                stable_save_name = confirmed_save_name(preferred_save_id)
                timings["save_identity_reused"] = 1
            else:
                stable_key, equivalent_stable_key, stable_save_name = fm24_save_evidence(
                    layout, reader, module, stable_identity,
                    manager_sessions=human_manager_sessions,
                )
                timings["save_identity_reused"] = 0
            timings["save_identity_ms"] = round(
                (perf_counter() - save_name_started) * 1000, 1,
            )
        save_context_key = stable_key or fm24_network_save_key(layout, human_manager_sessions)
        save_context_key, equivalent_stable_key = anchored_save_context_evidence(
            layout, save_context_key, equivalent_stable_key, preferred_save_id,
        )
        canonical_save_id = layout_save_identity(
            layout,
            resolve_save_identity(
                telemetry_session.get("game_instance_id") if telemetry_session else None,
                None,
                preferred_id=preferred_save_id,
                stable_id=save_context_key,
                equivalent_stable_id=equivalent_stable_key,
                namespace=layout.key,
            ),
        )
        effective_manager_id = preferred_manager_id or stored_selected_manager_id(canonical_save_id)
        selected_human_session = next(
            (
                item for item in human_manager_sessions
                if effective_manager_id and int(item["manager_id"]) == int(effective_manager_id)
            ),
            None,
        )
        if (
            selected_human_session is None
            and len(human_manager_sessions) == 1
            and not effective_manager_id
        ):
            selected_human_session = human_manager_sessions[0]
        human_session = selected_human_session
        manager_session = human_session or telemetry_session
        if human_session:
            preferred_manager_matches = bool(
                preferred_manager
                and int(preferred_manager.get("id") or 0) == int(human_session["manager_id"])
            )
            identity_preference = (
                canonical_save_id or preferred_save_id
                if save_context_key or preferred_manager_matches
                else None
            )
            canonical_save_id = layout_save_identity(
                layout,
                resolve_save_identity(
                    telemetry_session.get("game_instance_id") if telemetry_session else None,
                    human_session.get("manager_id"),
                    preferred_id=identity_preference,
                    stable_id=save_context_key,
                    equivalent_stable_id=equivalent_stable_key,
                    namespace=layout.key,
                ),
            )
        remember_save_name(canonical_save_id, stable_save_name)
        if human_session and canonical_save_id:
            manager_session = {
                **human_session,
                "game_instance_id": canonical_save_id,
                "source": (
                    "human_manager_object+save_telemetry"
                    if telemetry_session else "human_manager_object"
                ),
            }
        elif telemetry_session and canonical_save_id:
            manager_session = {**telemetry_session, "game_instance_id": canonical_save_id}
        if human_manager_sessions and not manager_session:
            manager_session = {
                **human_manager_sessions[0],
                "game_instance_id": canonical_save_id,
                "source": "human_manager_selection_required",
            }
        confirmed_session = None
        if (
            preferred_save_id and canonical_save_id == preferred_save_id
            and preferred_manager and preferred_managed_teams
        ):
            previous_refs = [
                {
                    "team_id": int(team.get("id") or 0),
                    "team_type": str(team.get("team_type") or "club"),
                    "address": team.get("address"),
                    "club_address": team.get("club_address"),
                    "manager_address": team.get("manager_address") or preferred_manager.get("manager_address"),
                    "confirmed_manager_id": int(preferred_manager.get("id") or 0),
                }
                for team in preferred_managed_teams if int(team.get("id") or 0) > 0
            ]
            if previous_refs:
                primary_ref = next(
                    (item for item in previous_refs if item["team_type"] == "club"), previous_refs[0],
                )
                confirmed_session = {
                    "game_instance_id": canonical_save_id,
                    "team_id": primary_ref["team_id"],
                    "team_type": primary_ref["team_type"],
                    "manager_id": int(preferred_manager.get("id") or 0),
                    "managed_team_refs": previous_refs,
                    "source": "confirmed_same_save_fallback",
                }
                if not human_session and effective_manager_id:
                    manager_session = confirmed_session
                else:
                    manager_session = manager_session or confirmed_session
        set_active_save_id(canonical_save_id)
        timings["human_manager_ms"] = round((perf_counter() - phase_started) * 1000, 1)

        phase_started = perf_counter()
        publish_progress("managed_teams", "正在读取当前执教队伍资料", force=True)
        managed_team = None
        managed_teams = []
        manager = None
        manager_person = None
        manager_details = None
        if manager_session and manager_session.get("team_id"):
            session_refs = []
            session_sources = (
                (human_session,)
                if human_session
                else (telemetry_session, confirmed_session)
            )
            for source_session in session_sources:
                if not source_session:
                    continue
                refs = source_session.get("managed_team_refs") or [source_session]
                for ref in refs:
                    identity = (int(ref.get("team_id") or 0), str(ref.get("team_type") or "club"))
                    if identity[0] and not any(
                        (int(item.get("team_id") or 0), str(item.get("team_type") or "club")) == identity
                        for item in session_refs
                    ):
                        session_refs.append(dict(ref))
            session_refs.sort(key=lambda item: (str(item.get("team_type")) != "club", int(item.get("team_id") or 0)))
            for ref in session_refs:
                try:
                    ref_address = int(str(ref.get("address") or "0"), 16)
                except (TypeError, ValueError):
                    ref_address = 0
                team = reader.team(ref_address) if ref_address else None
                if not team or int(team.get("id") or 0) != int(ref["team_id"]):
                    identity_manager_id = int(
                        ref.get("confirmed_manager_id") or manager_session.get("manager_id") or 0
                    )
                    team = (
                        resolve_managed_team_by_identity(
                            reader, int(ref["team_id"]), str(ref["team_type"]),
                            identity_manager_id, ref.get("manager_address"),
                        )
                        if identity_manager_id else None
                    )
                    if not team:
                        team = resolve_managed_team(
                            reader,
                            int(ref["team_id"]),
                            str(ref["team_type"]),
                            fixture_addresses,
                            fixture_rows,
                        )
                    if not team and identity_manager_id:
                        team = resolve_managed_team_by_identity(
                            reader, int(ref["team_id"]), str(ref["team_type"]),
                            identity_manager_id, ref.get("manager_address"), scan_manager=True,
                        )
                if not team:
                    continue
                confirmed_manager_id = int(ref.get("confirmed_manager_id") or 0)
                if confirmed_manager_id:
                    live_manager = int(str(team.get("manager_address") or "0"), 16)
                    if (
                        not _human_manager_matches(reader, live_manager, confirmed_manager_id)
                    ):
                        continue
                team_name = team.get("short_name") or team.get("name")
                if team_name:
                    managed_teams.append({
                        "id": team["id"],
                        "name": team_name,
                        "team_type": team.get("team_type"),
                        "source": manager_session["source"],
                        "address": team.get("address"),
                        "club_address": team.get("club_address"),
                        "manager_address": team.get("manager_address"),
                    })
                manager_address = int(team.get("manager_address", "0x0"), 16)
                if manager_address and not manager_details:
                    person = manager_address + reader.layout.manager_person_offset
                    manager_id = reader.u32(person + ENTITY_UID)
                    session_manager_id = int(manager_session.get("manager_id") or 0)
                    if (
                        not manager_id
                        and session_manager_id
                        and _human_manager_matches(reader, manager_address, session_manager_id)
                    ):
                        manager_id = session_manager_id
                    if manager_id:
                        manager_person = person
                        manager_details = {
                            "id": manager_id,
                            "team_id": team["id"],
                            "team_name": team_name,
                            "team_type": team.get("team_type"),
                            "game_instance_id": manager_session["game_instance_id"],
                            "source": "current_save_team_manager",
                            "manager_address": team.get("manager_address"),
                            "person_address": hex(person),
                            "coaching_license": _manager_coaching_license(
                                reader, person,
                            ),
                        }
            managed_team = next(
                (item for item in managed_teams if item.get("team_type") == "club"),
                managed_teams[0] if managed_teams else None,
            )
        timings["managed_team_ms"] = round((perf_counter() - phase_started) * 1000, 1)

        phase_started = perf_counter()
        result_performance: dict[str, Any] = {}
        save_instance_id = canonical_save_id
        publish_progress("result_archive", "正在读取历史赛果", force=True)
        results, result_cache_hit = cached_completed_results(
            reader, game_date_text, save_instance_id,
            force_scan=not fast_refresh, incremental=fast_refresh,
            result_addresses_override=pre_scanned_result_addresses,
            performance=result_performance,
            cancel_check=cancel_check,
            fixture_snapshots_override=fixture_snapshots,
            progress_callback=lambda phase, completed, total: publish_progress(
                "result_archive" if phase == "archive" else "result_fixture_links",
                "正在读取历史赛果" if phase == "archive" else "正在核对赛程关联赛果",
                completed, total,
            ),
            yield_memory_lock=yield_memory_lock,
        )
        results = annotate_result_fixture_stages(reader, results, parsed_fixtures)
        if cancel_check and cancel_check():
            raise RuntimeError("refresh cancelled")
        results = enrich_halftime_results(
            reader, results, set(required_result_keys or ()),
        )
        remember_completed_results(reader, game_date_text, results)
        retained_league_formats = (
            native_retained_league_format_snapshots(
                reader,
                list((previous_snapshot or {}).get("competition_formats") or []),
                game_date_text,
            )
            if (
                previous_snapshot
                and str(previous_snapshot.get("save_instance_id") or "")
                == str(save_instance_id or "")
            ) else []
        )
        competition_formats.extend(retained_league_formats)
        result_competition_formats = native_result_competition_format_snapshots(
            reader, results, season_fixture_bounds,
        )
        competition_formats.extend(result_competition_formats)
        timings["result_competition_formats_observed"] = len(
            result_competition_formats
        )
        timings["retained_league_formats_revalidated"] = len(
            retained_league_formats
        )
        timings["results_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        timings.update(result_performance)
        if not fast_refresh:
            state = _runtime_state(reader)
            with _RUNTIME_CACHE_LOCK:
                state["profiles"] = {
                    key: value for key, value in state["profiles"].items()
                    if key[0] != game_date_text
                }
                state["baselines"] = {
                    key: value for key, value in state["baselines"].items()
                    if key[0] != game_date_text
                }
        for result in results:
            competition = result["competition"]
            meta = competition_metadata(
                competition["id"], competition_name(competition),
                national_teams=(
                    int(result["home_team"]["id"]) in NATION_NAMES
                    and int(result["away_team"]["id"]) in NATION_NAMES
                ),
            )
            if meta:
                catalog[meta["id"]] = meta

        observed_format_count = len(competition_formats)
        competition_formats = persistent_competition_format_snapshots(
            competition_formats,
            previous_snapshot,
            save_instance_id,
            game_date_text,
        )
        timings["competition_formats_observed"] = observed_format_count
        timings["competition_formats_retained"] = max(
            0, len(competition_formats) - observed_format_count,
        )

        scope_managed_teams = list(managed_teams)
        if (
            not scope_managed_teams
            and preferred_save_id
            and canonical_save_id == preferred_save_id
        ):
            scope_managed_teams = list(preferred_managed_teams or [])
        managed_team_ids = {
            int(item.get("id") or 0) for item in scope_managed_teams
            if int(item.get("id") or 0) > 0
        }
        scope_team_ids = managed_team_ids
        scope_teams = scope_managed_teams
        schedule_timing_key = "managed_schedule"
        if competition_scope == "favorite_schedule":
            scope_team_ids = {
                int(value) for value in (favorite_team_ids or set())
                if int(value) > 0
            }
            scope_teams = [
                {"id": team_id, "team_type": "club"}
                for team_id in sorted(scope_team_ids)
            ]
            schedule_timing_key = "favorite_schedule"
        scope_competition_ids: set[int] = set()
        if competition_scope == "favorite_schedule":
            scope_competition_ids = set(favorite_scope_competition_ids)
            timings["favorite_schedule_cache_hit"] = 0
        elif competition_scope in {"managed_schedule", "famous"}:
            schedule_started = perf_counter()
            season_start, season_end = season_window(game_date)
            scope_competition_ids, schedule_cache_hit = cached_managed_schedule_competitions(
                reader, fixture_addresses, fixture_rows, results, scope_teams,
                save_instance_id, season_start, season_end, cancel_check,
                fixture_snapshots=fixture_snapshots,
                progress_callback=lambda completed, total: publish_progress(
                    "schedule_scope", "正在确认收藏球队所属赛事",
                    completed, total,
                ),
            )
            timings[f"{schedule_timing_key}_ms"] = round((perf_counter() - schedule_started) * 1000, 1)
            timings[f"{schedule_timing_key}_cache_hit"] = int(schedule_cache_hit)
        fixture_rows_before_scope = len(fixture_rows)
        requested_window_end = game_date + timedelta(days=days)
        fixture_filter_counts["requested_window_excluded"] = sum(
            1 for fixture, _meta, _home, _away in fixture_rows
            if fixture.match_date > requested_window_end
        )
        fixture_rows = filter_fixture_rows_for_odds(
            fixture_rows, competition_scope,
            requested_window_end, scope_competition_ids,
            scope_team_ids,
        )
        fixture_filter_counts["scope_filter_excluded"] = max(
            0, fixture_rows_before_scope - len(fixture_rows),
        )
        timings["fixture_filter_counts"] = {
            **dict(sorted(fixture_filter_counts.items())),
            "before_scope_filter": fixture_rows_before_scope,
            "after_scope_filter": len(fixture_rows),
        }

        start_date, end_date = season_window(game_date)
        season_starts = competition_season_starts(
            game_date, competition_formats, results,
        )
        season_league_results = []
        for result in results:
            meta = catalog.get(int((result.get("competition") or {}).get("id") or 0))
            if not meta or meta.get("kind") != "league":
                continue
            competition_start = season_starts.get(int(meta["id"]), start_date)
            if not competition_start <= str(result.get("date") or "") < game_date_text:
                continue
            season_league_results.append({
                "date": result["date"],
                "competition_id": meta["id"],
                "competition_name": meta["name"],
                "competition_kind": "league",
                "home_team": result["home_team"],
                "away_team": result["away_team"],
                "home_goals": result["home_goals"],
                "away_goals": result["away_goals"],
                **{
                    key: result[key]
                    for key in (
                        "competition_stage_index", "competition_group_index",
                    )
                    if result.get(key) is not None
                },
            })
        standings_profiles = standings_model_profiles(season_league_results)

        elo_started = perf_counter()
        elo_adjustments, _elo_ratings, _elo_match_counts, elo_cache_hit = cached_elo_context(
            reader, results,
        )
        timings["elo_ms"] = round((perf_counter() - elo_started) * 1000, 1)
        timings["elo_cache_hit"] = int(elo_cache_hit)

        by_team: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in results:
            if item["date"] >= game_date_text:
                continue
            by_team[item["home_team"]["id"]].append(item)
            by_team[item["away_team"]["id"]].append(item)

        persisted_profiles, persisted_baselines = reusable_snapshot_model_cache(
            previous_snapshot, save_instance_id, game_date_text,
            process_id=int(reader.process.pid), module_base=int(reader.module_base),
        )
        baselines: dict[int, dict[str, Any]] = {}
        baseline_signatures: dict[int, str] = {}
        profiles: dict[tuple[int, int], dict[str, Any]] = {}
        profile_metadata: dict[tuple[int, int], dict[str, Any]] = {}
        matches = []
        profile_cache_hits = 0
        baseline_cache_hits = 0
        slow_team_profiles: list[dict[str, Any]] = []
        phase_started = perf_counter()
        model_reads_before = int(reader.read_calls)
        model_requested_before = int(reader.requested_bytes)
        if championship_enabled:
            publish_progress(
                "championship_profiles", "正在准备杯赛参赛球队画像",
                force=True,
            )
            profile_cache_hits += enrich_competition_format_team_profiles(
                reader,
                competition_formats,
                game_date_text,
                by_team,
                elo_adjustments,
                profiles,
                profile_metadata,
                persisted_profiles=persisted_profiles,
                cancel_check=cancel_check,
            )
        fixture_team_inputs: dict[
            tuple[int, int], tuple[dict[str, Any], list[dict[str, Any]]]
        ] = {}
        for fixture, _meta, home_team, away_team in fixture_rows:
            fixture_team_inputs.setdefault(
                (int(fixture.home_team), int(home_team["id"])),
                (home_team, by_team[home_team["id"]]),
            )
            fixture_team_inputs.setdefault(
                (int(fixture.away_team), int(away_team["id"])),
                (away_team, by_team[away_team["id"]]),
            )
        pending_team_inputs = [
            (key, value) for key, value in fixture_team_inputs.items()
            if key not in profiles
        ]
        team_profile_total = len(pending_team_inputs)
        for team_profile_index, (key, (team, past)) in enumerate(
            pending_team_inputs, start=1,
        ):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            publish_progress(
                "team_profiles",
                f"正在读取球队阵容：{team.get('short_name') or team.get('name') or team['id']}",
                team_profile_index - 1, team_profile_total, force=True,
            )
            profile_started = perf_counter()
            profile, hit, metadata = cached_team_profile(
                reader, game_date_text, key[0], key[1], past,
                elo_adjustments, persisted=persisted_profiles,
            )
            profiles[key] = profile
            profile_metadata[key] = metadata
            profile_cache_hits += int(hit)
            profile_ms = round((perf_counter() - profile_started) * 1000, 1)
            if profile_ms >= 100:
                slow_team_profiles.append({
                    "team_id": int(team["id"]),
                    "team_name": str(team.get("name") or ""),
                    "elapsed_ms": profile_ms,
                    "cache_hit": bool(hit),
                })
            publish_progress(
                "team_profiles", "正在批量读取球队阵容",
                team_profile_index, team_profile_total, force=True,
            )
        model_total = len(fixture_rows)
        competition_reputations: dict[int, int | None] = {}
        for model_index, (fixture, meta, home_team, away_team) in enumerate(
            fixture_rows, start=1,
        ):
            if cancel_check and cancel_check():
                raise RuntimeError("refresh cancelled")
            if meta["id"] not in baselines:
                baseline, hit, result_signature = cached_competition_baseline(
                    reader, results, meta["id"], game_date_text,
                    persisted=persisted_baselines,
                )
                baselines[meta["id"]] = baseline
                baseline_signatures[meta["id"]] = result_signature
                baseline_cache_hits += int(hit)
            home_key = (fixture.home_team, home_team["id"])
            away_key = (fixture.away_team, away_team["id"])
            if home_key not in profiles:
                profile_started = perf_counter()
                profile, hit, metadata = cached_team_profile(
                    reader, game_date_text, fixture.home_team, home_team["id"],
                    by_team[home_team["id"]], elo_adjustments,
                    persisted=persisted_profiles,
                )
                profiles[home_key] = profile
                profile_metadata[home_key] = metadata
                profile_cache_hits += int(hit)
                profile_ms = round((perf_counter() - profile_started) * 1000, 1)
                if profile_ms >= 100:
                    slow_team_profiles.append({
                        "team_id": int(home_team["id"]),
                        "team_name": str(home_team.get("name") or ""),
                        "elapsed_ms": profile_ms,
                        "cache_hit": bool(hit),
                    })
            if away_key not in profiles:
                profile_started = perf_counter()
                profile, hit, metadata = cached_team_profile(
                    reader, game_date_text, fixture.away_team, away_team["id"],
                    by_team[away_team["id"]], elo_adjustments,
                    persisted=persisted_profiles,
                )
                profiles[away_key] = profile
                profile_metadata[away_key] = metadata
                profile_cache_hits += int(hit)
                profile_ms = round((perf_counter() - profile_started) * 1000, 1)
                if profile_ms >= 100:
                    slow_team_profiles.append({
                        "team_id": int(away_team["id"]),
                        "team_name": str(away_team.get("name") or ""),
                        "elapsed_ms": profile_ms,
                        "cache_hit": bool(hit),
                    })
            baseline = baselines[meta["id"]]
            row = build_match_odds(
                home_team,
                away_team,
                profiles[home_key],
                profiles[away_key],
                baseline["home"],
                baseline["away"],
                meta["id"],
                fixture.match_date,
                standings_profiles.get((meta["id"], home_team["id"])),
                standings_profiles.get((meta["id"], away_team["id"])),
                include_extended_markets=False,
            )
            season_address = int(fixture.competition_season or 0)
            if season_address not in competition_reputations:
                competition_reputations[season_address] = native_competition_reputation(
                    reader, season_address,
                )
            native_reputation = competition_reputations[season_address]
            row.update({
                "fixture_address": hex(fixture.address),
                "fixture_date": fixture.match_date.isoformat(),
                "kickoff_minutes": fixture.kickoff_minutes,
                "kickoff_time": (
                    f"{fixture.kickoff_minutes // 60:02d}:{fixture.kickoff_minutes % 60:02d}"
                    if fixture.kickoff_minutes is not None else None
                ),
                "competition_id": meta["id"],
                "competition_name": meta["name"],
                "competition_kind": meta["kind"],
                "competition_reputation": native_reputation or meta["reputation"],
                "competition_reputation_native": native_reputation,
                "competition_stage_index": fixture.stage_index,
                "competition_group_index": fixture.group_index,
            })
            knockout_context = knockout_contexts.get(int(fixture.address))
            if knockout_context:
                row["_knockout_context"] = dict(knockout_context)
            matches.append(row)
            publish_progress(
                "team_models", "正在生成比赛盘口",
                model_index, model_total,
            )
        publish_progress(
            "team_models", "正在生成比赛盘口",
            model_total, model_total, force=True,
        )
        tie_leg_xg: dict[int, dict[str, dict[str, float]]] = defaultdict(dict)
        for row in matches:
            context = row.get("_knockout_context")
            if context:
                tie_leg_xg[int(context["tie_address"])][str(context["orientation"])] = dict(row["xg"])
        for row in matches:
            context = row.pop("_knockout_context", None)
            if not context:
                continue
            leg_xg = tie_leg_xg[int(context["tie_address"])]
            context["first_leg_xg"] = leg_xg.get("first")
            context["second_leg_xg"] = leg_xg.get("second")
            knockout = knockout_market_prices(row, context)
            if knockout:
                row["knockout"] = knockout
            row["competition_round"] = competition_round_metadata(context)
            row["competition_match_role"] = str(
                context.get("competition_match_role") or "knockout"
            )
            final_metadata = competition_final_metadata(row, context)
            if final_metadata:
                row["competition_final"] = final_metadata
        timings["model_ms"] = round((perf_counter() - phase_started) * 1000, 1)
        timings["model_memory_read_calls"] = int(reader.read_calls) - model_reads_before
        timings["model_memory_requested_bytes"] = (
            int(reader.requested_bytes) - model_requested_before
        )
        timings["slow_team_profiles"] = sorted(
            slow_team_profiles,
            key=lambda item: float(item["elapsed_ms"]),
            reverse=True,
        )[:10]

        matches.sort(key=lambda item: (
            item["fixture_date"],
            item["kickoff_minutes"] if item["kickoff_minutes"] is not None else 24 * 60,
            *competition_sort_key({"kind": item["competition_kind"], "reputation": item["competition_reputation"], "name": item["competition_name"]}),
            item["home"]["name"], item["away"]["name"],
        ))
        match_intelligence_results = hidden_future_result_candidates(
            reader, results, matches, game_date_text,
        )
        season_results = []
        settlement_results = []
        settlement_keys = set(required_result_keys or ())
        for result in results:
            if result["date"] >= game_date_text:
                continue
            if not has_resolved_team_name(result["home_team"]) or not has_resolved_team_name(result["away_team"]):
                continue
            meta = catalog.get(result["competition"]["id"])
            if not meta:
                continue
            display_result = {
                "date": result["date"],
                "competition_id": meta["id"],
                "competition_name": meta["name"],
                "competition_kind": meta["kind"],
                "home": canonical_team(result["home_team"]),
                "away": canonical_team(result["away_team"]),
                "home_goals": result["home_goals"],
                "away_goals": result["away_goals"],
            }
            for field in RESULT_KICKOFF_FIELDS:
                if result.get(field) is not None:
                    display_result[field] = result[field]
            for field in (
                "competition_stage_index", "competition_group_index",
            ):
                if result.get(field) is not None:
                    display_result[field] = int(result[field])
            # Preserve a verified native event vector when a result source
            # supplied one.  Older/cached rows simply omit this optional field.
            if isinstance(result.get("goal_events"), list):
                display_result["goal_events"] = result["goal_events"]
            if result.get("half_home_goals") is not None and result.get("half_away_goals") is not None:
                display_result["half_home_goals"] = int(result["half_home_goals"])
                display_result["half_away_goals"] = int(result["half_away_goals"])
            if result.get("first_scoring_team") in {"home", "away", "none"}:
                display_result["first_scoring_team"] = result["first_scoring_team"]
            for field in (
                "after_extra_time_home_goals", "after_extra_time_away_goals",
                "penalty_shootout_home_goals", "penalty_shootout_away_goals",
                "home_outcome_code", "away_outcome_code", "winner_side", "decided_by",
            ):
                if result.get(field) is not None:
                    display_result[field] = result[field]
            display_result.update(result_knockout_identity(
                str(meta["kind"]), result, display_result,
            ))
            competition_start = season_starts.get(int(meta["id"]), start_date)
            if competition_start <= result["date"] <= end_date:
                season_results.append(display_result)
            if _result_identity(result) in settlement_keys:
                if display_result.get("advanced_team_id") is None:
                    knockout_decision = fixture_result_knockout_decision(
                        reader, fixture_addresses, result,
                    )
                    if knockout_decision:
                        display_result.update(knockout_decision)
                settlement_result = dict(display_result)
                for field in (
                    "settlement_verified", "settlement_evidence",
                    "settlement_sources", "result_source", "result_address",
                    "fixture_address",
                ):
                    if result.get(field) is not None:
                        settlement_result[field] = result[field]
                settlement_results.append(settlement_result)
        season_results.sort(key=lambda item: (item["date"], item["competition_name"], item["home"]["name"]), reverse=True)

        if manager_person and manager_details:
            # Keep the manager ID for team/contract lookup, but do not read a
            # display name from the unstable person-name memory layout.
            manager = {**manager_details, "source": "current_team_manager"}
        elif managed_teams and preferred_manager and preferred_save_id == canonical_save_id:
            manager = {
                **preferred_manager,
                "source": "confirmed_same_save_fallback",
                "manager_address": managed_teams[0].get("manager_address") or preferred_manager.get("manager_address"),
            }
        elif manager_session:
            manager_address = str(manager_session.get("manager_address") or "")
            try:
                manager_pointer = int(manager_address or "0", 0)
            except (TypeError, ValueError):
                manager_pointer = 0
            manager_id = int(manager_session.get("manager_id") or 0)
            if manager_id > 0:
                manager = {
                    "id": manager_id,
                    "name": manager_session.get("manager_name") or f"经理 {manager_id}",
                    "team_id": None,
                    "team_name": None,
                    "team_type": None,
                    "game_instance_id": canonical_save_id,
                    "source": "human_manager_without_team",
                    "manager_address": manager_address or None,
                    "person_address": (
                        hex(manager_pointer + int(reader.layout.manager_person_offset))
                        if manager_pointer else None
                    ),
                    "coaching_license": (
                        _manager_coaching_license(
                            reader,
                            manager_pointer + int(reader.layout.manager_person_offset),
                        ) if manager_pointer else None
                    ),
                }
        timings.update(reader.read_metrics())

    publish_progress("result_history", "正在保存赛果历史", force=True)
    save_result_history(
        results, save_instance_id, game_date_text,
        protected_result_keys=set(required_result_keys or ()),
    )
    competitions = sorted(catalog.values(), key=competition_sort_key)
    counts = defaultdict(int)
    for match in matches:
        counts[match["competition_id"]] += 1
    for item in competitions:
        item["upcoming_matches"] = (
            sum(1 for match in matches if match["competition_kind"] == "national")
            if item.get("aggregate") else counts[item["id"]]
        )
    publish_progress("odds_complete", "盘口读取完成，正在校验", force=True)
    return {
        "model_version": MODEL_VERSION,
        "game_layout": layout.key,
        "game_version": layout.display_name,
        "pricing_mode": "casino",
        "available_pricing_modes": ["casino", "fair"],
        "market_margins": {
            "1x2": ONE_X_TWO_MARGIN,
            "asian_handicap": TWO_WAY_MARGIN,
            "totals": TWO_WAY_MARGIN,
            "team_totals": TEAM_TOTAL_MARGIN,
            "half_time_1x2": HALF_MARKET_ONE_X_TWO_MARGIN,
            "half_time_asian_handicap": HALF_MARKET_TWO_WAY_MARGIN,
            "half_time_totals": HALF_MARKET_TWO_WAY_MARGIN,
            "second_half_1x2": HALF_MARKET_ONE_X_TWO_MARGIN,
            "second_half_asian_handicap": HALF_MARKET_TWO_WAY_MARGIN,
            "second_half_totals": HALF_MARKET_TWO_WAY_MARGIN,
            "btts": BTTS_MARGIN,
            "double_chance": DOUBLE_CHANCE_MARGIN,
            "clean_sheet": CLEAN_SHEET_MARGIN,
            "win_to_nil": CLEAN_SHEET_MARGIN,
            "first_score": FIRST_SCORE_MARGIN,
            "goal_parity": GOAL_PARITY_MARGIN,
            "half_full": HALF_FULL_MARGIN,
            "winning_margin": WINNING_MARGIN_MARGIN,
            "highest_scoring_half": HIGHEST_SCORING_HALF_MARGIN,
            "exact_score_target_return_rate": EXACT_SCORE_TARGET_RETURN_RATE,
            "exact_score_return_rate_band": list(EXACT_SCORE_RETURN_RATE_BAND),
            "exact_goals_divisor": EXACT_GOALS_ODDS_DIVISOR,
        },
        "strength_model": {
            "scope": "clubs_and_national_teams",
            "ca_curve": "linear: nonlinear_ca = ca",
            "star_player_count": STAR_PLAYER_COUNT,
            "star_weight": STAR_CA_WEIGHT,
            "other_starter_count": OTHER_STARTER_COUNT,
            "other_starter_weight": OTHER_STARTER_CA_WEIGHT,
            "bench_player_count": BENCH_PLAYER_COUNT,
            "bench_weight": BENCH_CA_WEIGHT,
            "priced_player_count": PRICED_PLAYER_COUNT,
            "partial_roster_policy": "renormalize_occupied_slot_weights",
            "raw_ca_weight": RAW_CA_WEIGHT,
            "conditioned_ca_weight": CONDITIONED_CA_WEIGHT,
            "strength_log_coefficient": STRENGTH_LOG_COEFFICIENT,
            "strength_log_edge_cap": STRENGTH_LOG_EDGE_CAP,
            "competition_home_away_split_weight": COMPETITION_HOME_AWAY_SPLIT_WEIGHT,
            "recent_form_coefficient": RECENT_FORM_COEFFICIENT,
            "recent_form_log_edge_cap": RECENT_FORM_LOG_EDGE_CAP,
            "morale_log_edge_cap": MORALE_LOG_EDGE_CAP,
            "recent_total_goals_log_coefficient": RECENT_TOTAL_GOALS_LOG_COEFFICIENT,
            "recent_total_goals_log_adjustment_cap": RECENT_TOTAL_GOALS_LOG_ADJUSTMENT_CAP,
            "recent_match_limit": RECENT_MATCH_LIMIT,
            "recent_match_weights": [round(value, 6) for value in RECENT_MATCH_WEIGHTS],
            "elo_initial_rating": ELO_INITIAL_RATING,
            "elo_update_k": ELO_UPDATE_K,
            "elo_home_advantage": ELO_HOME_ADVANTAGE,
            "elo_opponent_scale": ELO_OPPONENT_SCALE,
            "elo_opponent_correction_cap": ELO_OPPONENT_CORRECTION_CAP,
            "elo_confidence_matches": ELO_CONFIDENCE_MATCHES,
            "standings_prior_matches": STANDINGS_PRIOR_MATCHES,
            "standings_goal_log_coefficient": STANDINGS_GOAL_LOG_COEFFICIENT,
            "standings_goal_log_adjustment_cap": STANDINGS_GOAL_LOG_ADJUSTMENT_CAP,
            "standings_elo_log_coefficient": STANDINGS_ELO_LOG_COEFFICIENT,
            "standings_elo_log_edge_cap": STANDINGS_ELO_LOG_EDGE_CAP,
            "team_reputation_log_coefficient": TEAM_REPUTATION_LOG_COEFFICIENT,
            "team_reputation_log_edge_cap": TEAM_REPUTATION_LOG_EDGE_CAP,
            "goal_level_log_adjustment": GOAL_LEVEL_LOG_ADJUSTMENT,
            "low_ca_goal_threshold": LOW_CA_GOAL_THRESHOLD,
            "low_ca_goal_log_coefficient": LOW_CA_GOAL_LOG_COEFFICIENT,
            "low_ca_goal_log_edge_cap": LOW_CA_GOAL_LOG_EDGE_CAP,
            "dixon_coles_rho": DIXON_COLES_RHO,
        },
        "calibration_reference": "OddsLab World Cup 2026 closing consensus, 2026-06-11 to 2026-06-25",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": f"read-only {layout.display_name} process memory",
        "game_date": game_date_text,
        "fixture_scan_mode": fixture_scan_mode,
        "odds_scope": competition_scope,
        "odds_days": days,
        "hidden_competition_ids": sorted({
            int(value) for value in (hidden_competition_ids or set())
            if int(value) > 0
        }),
        "game_time": (
            f"{decode_kickoff_minutes(game_date_code) // 60:02d}:{decode_kickoff_minutes(game_date_code) % 60:02d}"
            if decode_kickoff_minutes(game_date_code) is not None else "00:00"
        ),
        "window_end": (game_date + timedelta(days=days)).isoformat(),
        "save_instance_id": canonical_save_id,
        "save_name": confirmed_save_name(canonical_save_id),
        "stable_savegame_id": stable_identity.get("savegame_id") if stable_identity else None,
        "session_nonce": stable_identity.get("session_nonce") if stable_identity else None,
        "manager": manager,
        "manager_options": [
            {
                "id": int(item["manager_id"]),
                "name": item.get("manager_name") or f"经理 {item['manager_id']}",
                "manager_address": item.get("manager_address"),
                "teams": [
                    {
                        "id": int(ref["team_id"]),
                        "team_type": ref.get("team_type"),
                        "address": ref.get("address"),
                        "club_address": ref.get("club_address"),
                        "manager_address": ref.get("manager_address"),
                    }
                    for ref in item.get("managed_team_refs", [])
                ],
                "managed_team_refs": [dict(ref) for ref in item.get("managed_team_refs", [])],
            }
            for item in human_manager_sessions
        ],
        "selected_manager_id": int((manager or {}).get("id") or (manager_session or {}).get("manager_id") or 0) or None,
        "manager_selection_required": bool(len(human_manager_sessions) > 1 and not selected_human_session),
        "unemployed_manager_detected": bool(
            manager and not managed_teams and human_session
        ),
        "managed_team": managed_team,
        "managed_teams": managed_teams,
        "season_start": start_date,
        "season_end": end_date,
        "competition_season_starts": season_starts,
        "competitions": competitions,
        "competition_formats": competition_formats,
        "competition_goal_baselines": baselines,
        "model_cache": {
            "version": MODEL_CACHE_VERSION,
            "game_date": game_date_text,
            "process_id": int(reader.process.pid),
            "module_base": hex(int(reader.module_base)),
            "profiles": [
                {
                    "team_address": hex(team_address),
                    "team_id": team_id,
                    **profile_metadata[(team_address, team_id)],
                    "profile": profile,
                }
                for (team_address, team_id), profile in profiles.items()
            ],
            "baselines": [
                {
                    "competition_id": competition_id,
                    "result_signature": baseline_signatures[competition_id],
                    "baseline": baseline,
                }
                for competition_id, baseline in baselines.items()
            ],
        },
        "matches": matches,
        # This private payload is removed by LocalOddsState before the public
        # snapshot is saved or published. It contains the facts sold by 7F.
        "_match_intelligence_results": match_intelligence_results,
        "season_results": season_results,
        "settlement_results": settlement_results,
        "performance": {
            **timings,
            "total_ms": round((perf_counter() - started_at) * 1000, 1),
            "fixture_address_cache_hit": fixture_cache_hit,
            "result_cache_hit": result_cache_hit,
            "refresh_mode": refresh_mode,
            "profile_cache_hits": profile_cache_hits,
            "baseline_cache_hits": baseline_cache_hits,
        },
    }


def generate_odds(competition_id: int = 102429, days: int = 7) -> dict[str, Any]:
    with open_supported_reader() as (_process_path, layout, _module, reader):
        game_date = decode_date(read_layout_game_date_code(reader))
        if not game_date:
            raise RuntimeError("game date unavailable")
        fixture_candidates = []
        for address in scan_fixture_addresses(reader)[0]:
            fixture = parse_fixture(reader, address)
            if not fixture or not game_date <= fixture.match_date <= game_date + timedelta(days=days):
                continue
            competition = reader.competition(fixture.competition_season)
            home = reader.team(fixture.home_team)
            away = reader.team(fixture.away_team)
            if competition and home and away and competition["id"] == competition_id:
                fixture_candidates.append((fixture, competition, home, away))
        if not fixture_candidates:
            raise RuntimeError(f"no upcoming fixtures for competition {competition_id} in the next {days} days")
        fixture_date = min(item[0].match_date for item in fixture_candidates)
        fixtures = [item for item in fixture_candidates if item[0].match_date == fixture_date]
        unique = {}
        for fixture, competition, home, away in fixtures:
            unique.setdefault((home["id"], away["id"]), (fixture, competition, home, away))
        fixtures = list(unique.values())

        results = merge_verified_results(deduplicate_results([item for address in scan_result_addresses(reader)[0] if (item := parse_completed_result(reader, address))]))
        elo_adjustments, _elo_ratings, _elo_match_counts, _elo_cache_hit = cached_elo_context(
            reader, results,
        )
        by_team: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for item in results:
            if item["date"] >= game_date.isoformat():
                continue
            by_team[item["home_team"]["id"]].append(item)
            by_team[item["away_team"]["id"]].append(item)
        baseline = competition_goal_baseline(results, competition_id, game_date.isoformat())
        base_home = baseline["home"]
        base_away = baseline["away"]

        rows = []
        for fixture, competition, home_team, away_team in sorted(fixtures, key=lambda item: (item[2]["name"] or "", item[3]["name"] or "")):
            home = team_profile(
                reader, fixture.home_team, home_team["id"],
                by_team[home_team["id"]], elo_adjustments,
            )
            away = team_profile(
                reader, fixture.away_team, away_team["id"],
                by_team[away_team["id"]], elo_adjustments,
            )
            home_xg, away_xg = expected_goals(
                home, away, base_home, base_away,
                home_reputation=home_team.get("reputation"),
                away_reputation=away_team.get("reputation"),
            )
            matrix = score_matrix(home_xg, away_xg)
            home_win = sum(matrix[h][a] for h in range(len(matrix)) for a in range(len(matrix)) if h > a)
            draw = sum(matrix[i][i] for i in range(len(matrix)))
            away_win = 1 - home_win - draw
            btts = 1 - sum(matrix[0]) - sum(row[0] for row in matrix) + matrix[0][0]
            handicap, home_handicap_odds, away_handicap_odds = choose_asian_handicap(matrix)
            total, over_odds, under_odds = choose_total_line(matrix)
            fair_1x2 = {"home": decimal_odds(home_win), "draw": decimal_odds(draw), "away": decimal_odds(away_win)}
            handicap_options = asian_handicap_options(matrix, handicap, bookmaker=False)
            total_options = asian_total_options(matrix, total, bookmaker=False)
            confidence = model_confidence(home, away)
            rows.append({
                "home": {"id": home_team["id"], "name": home_team["short_name"] or home_team["name"], "profile": home},
                "away": {"id": away_team["id"], "name": away_team["short_name"] or away_team["name"], "profile": away},
                "xg": {"home": home_xg, "away": away_xg},
                "fair_1x2": fair_1x2,
                "asian_handicap": {"home_line": handicap, "home_odds": home_handicap_odds, "away_line": -handicap, "away_odds": away_handicap_odds},
                "handicap_options": handicap_options,
                "total_goals": {"line": total, "over_odds": over_odds, "under_odds": under_odds},
                "total_options": total_options,
                "btts": {"yes": decimal_odds(btts), "no": decimal_odds(1 - btts)},
                "confidence": confidence["label"],
                "confidence_score": confidence["score"],
            })

    return {
        "model_version": MODEL_VERSION,
        "game_layout": layout.key,
        "game_version": layout.display_name,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": f"read-only {layout.display_name} process memory",
        "game_date": game_date.isoformat(),
        "fixture_date": fixture_date.isoformat(),
        "competition_id": competition_id,
        "competition_name": (fixtures[0][1]["short_name"] or fixtures[0][1]["name"]) if fixtures else str(competition_id),
        "market_scope": "90-minute fair odds; no bookmaker margin",
        "model_note": "Team strength blends the best 13 and best 20 available effective-CA players. Expected goals use a CA-gap response, opponent-Elo-adjusted recent goal difference, morale adjustments, a competition-specific scoring baseline, and Dixon-Coles low-score correction. Cup rotation, historical xG, historical lineups, formations and exact registration eligibility are not yet included.",
        "competition_goal_baseline": baseline,
        "matches": rows,
    }


def save_odds(output: dict[str, Any], output_path: Path | None = None) -> Path:
    game_date = datetime.fromisoformat(output["game_date"])
    output_path = output_path or Path("data") / "odds" / f"fm26_cup_preliminary_{game_date:%Y%m%d}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Memory-only preliminary cup odds")
    parser.add_argument("--competition-id", type=int, default=102429)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = generate_odds(args.competition_id)
    output_path = save_odds(output, args.output)
    print(json.dumps({"output": str(output_path.resolve()), "matches": len(output["matches"]), "baseline": output["competition_goal_baseline"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
