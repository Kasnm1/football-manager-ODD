"""Persist generation-time youth plans for acquired clubs."""

from __future__ import annotations

import json
import hashlib
import re
import secrets
import struct
import threading
import zlib
from datetime import date, datetime, timezone
from typing import Any

from fm_collector.win32 import open_process, write_process_memory
from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root
from tools.club_reader import (
    _address,
    _context_addresses,
    _manager_primary_nation,
    _name,
    _player,
    _remote_free_block,
    _remote_malloc_block,
    _write_person_display_name,
    _validated_player_person,
)
from tools.database_index import (
    DatabaseIndex, database_index_for_reader, resolve_team_club,
)
from tools.initial_data_audit import TEAM_CLUB, Reader, decode_date, select_process_layout


SCHEMA_VERSION = 12
SUPPORTED_GAMES = {"fm24", "fm26"}
GOLDEN_GENERATION_SUPPORTED_GAMES = {"fm24", "fm26"}
PLAN_KINDS = {"golden_generation", "academy_son"}
GOLDEN_GENERATION_YEARLY_LIMIT = 10
GOLDEN_GENERATION_MAX_PLAYERS = GOLDEN_GENERATION_YEARLY_LIMIT
ACADEMY_SON_YEARLY_LIMIT = 1
ACADEMY_SON_CA_RANGE = (85, 105)
ACADEMY_SON_PA_RANGE = (130, 199)
ACADEMY_SON_CA_PRICES = {
    (85, 105): 10_000_000,
    (105, 120): 50_000_000,
    (120, 135): 200_000_000,
    (135, 150): 1_000_000_000,
    (50, 150): 10_000_000,
}
GOLDEN_GENERATION_PA_PRICES = {
    (110, 140): 5_000_000,
    (120, 150): 15_000_000,
    (130, 160): 50_000_000,
    (140, 170): 150_000_000,
    (150, 180): 500_000_000,
    (160, 190): 1_500_000_000,
    (170, 200): 5_000_000_000,
    # Keep already-purchased plans valid after switching to FM negative-PA bands.
    (130, 140): 5_000_000,
    (140, 150): 25_000_000,
    (150, 160): 50_000_000,
    (160, 170): 250_000_000,
    (170, 180): 500_000_000,
    (180, 190): 5_000_000_000,
    (190, 200): 50_000_000_000,
}
ACADEMY_SON_PA_PRICES = {
    (110, 140): 10_000_000,
    (120, 150): 30_000_000,
    (130, 160): 100_000_000,
    (140, 170): 300_000_000,
    (150, 180): 1_000_000_000,
    (160, 190): 3_000_000_000,
    (170, 200): 10_000_000_000,
    (120, 200): 500_000_000,
    (80, 200): 500_000_000,
    # Legacy fixed-width bands remain accepted for active saved plans.
    (130, 140): 10_000_000,
    (140, 150): 50_000_000,
    (150, 160): 100_000_000,
    (160, 170): 500_000_000,
    (170, 180): 1_000_000_000,
    (180, 190): 10_000_000_000,
    (190, 200): 100_000_000_000,
}
ACADEMY_SON_LEGACY_PA_PRICES = {
    (110, 140): 10_000_000,
    (120, 150): 50_000_000,
    (130, 160): 100_000_000,
    (140, 170): 500_000_000,
    (150, 180): 1_000_000_000,
    (160, 190): 10_000_000_000,
    (170, 200): 100_000_000_000,
    (120, 200): 500_000_000,
    (80, 200): 500_000_000,
    (130, 140): 10_000_000,
    (140, 150): 50_000_000,
    (150, 160): 100_000_000,
    (160, 170): 500_000_000,
    (170, 180): 1_000_000_000,
    (180, 190): 10_000_000_000,
    (190, 200): 100_000_000_000,
}
ACADEMY_SON_RANDOM_CA_BAND = (50, 150)
ACADEMY_SON_RANDOM_PA_BAND = (120, 200)
ACADEMY_SON_LEGACY_RANDOM_PA_BAND = (80, 200)
ACADEMY_SON_GENERATION_SLOTS = 10
ACADEMY_SON_NEW_PLAN_SLOTS = 4
ACADEMY_SON_NAME_PATTERN = re.compile(r"[A-Za-z]{2,30}\Z")
ACADEMY_SON_POSITIONS = (
    "GK", "SW", "DL", "DC", "DR", "DM", "ML", "MC", "MR",
    "AML", "AMC", "AMR", "ST", "WBL", "WBR",
)
ACADEMY_SON_PRIMARY_POSITION_RATING = 20
ACADEMY_SON_SECONDARY_POSITION_RATING = 12
ACADEMY_SON_OTHER_POSITION_RATING = 1
YOUTH_CONTRACT_TYPE = 3
YOUTH_COHORT_STABLE_OBSERVATIONS = 3
YOUTH_COHORT_STABLE_SECONDS = 5.0
RELATION_OBJECT_PERSON = 3
RELATION_PERMANENT = 79
RELATION_LIKES_PERSON = 1
RELATION_REASON_PARENT = 1
RELATION_REASON_CHILD = 3
RELATION_LEVEL = 100
RELATION_FORMED_AT_CLUB = 72
_LOCK = threading.RLock()


def _invalidate_reader_after_write(reader: Reader) -> None:
    """Discard prefetched process pages before a write-back verification."""
    invalidate = getattr(reader, "invalidate_prefetch", None)
    if callable(invalidate):
        invalidate()
    string_cache = getattr(reader, "string_cache", None)
    if hasattr(string_cache, "clear"):
        string_cache.clear()


def _attach_database_index(reader: Reader) -> DatabaseIndex | None:
    """Attach a session or request-local UID directory to a Reader.

    Several youth-plan paths open a short-lived Reader directly because they
    may run while a writable transaction is being prepared.  Without this
    adapter, ``resolve_team_club`` can only see the persisted address hint and
    loses the FMRTE-style UID relocation after a save reload.  A live session
    directory remains preferred; constructing a request-local directory is a
    bounded compatibility fallback and failures leave the old validated hint
    path available.
    """
    directory = database_index_for_reader(reader)
    if directory is None:
        try:
            directory = DatabaseIndex(reader)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None
    try:
        reader.database_index_provider = lambda **_kwargs: directory
    except (AttributeError, TypeError):
        pass
    return directory


def _academy_son_write_mismatches(
    reader: Reader, *, person: int, name_fields: tuple[int, int, int, int],
    player: int, layout: Any, positions_address: int,
    nationality_address: int, target_display_name: str,
    target_given_name: str, manager_last_name: str, target_ca: int | None,
    target_pa: int, target_positions: bytes | None,
    manager_nationality: int,
) -> dict[str, Any]:
    actual = {
        "display_name": _name(reader, person),
        "given_name": reader.fm_nested_string_at(name_fields[0]),
        "surname": reader.fm_nested_string_at(name_fields[1]),
        "common_name_reference": int(reader.ptr(name_fields[2]) or 0),
        "pa": int(reader.u16(player + layout.player_pa_offset) or 0),
        "nationality_reference": int(reader.ptr(nationality_address) or 0),
    }
    expected = {
        "display_name": target_display_name,
        "given_name": target_given_name,
        "surname": manager_last_name,
        "common_name_reference": 0,
        "pa": int(target_pa),
        "nationality_reference": int(manager_nationality),
    }
    if target_ca is not None:
        actual["ca"] = int(reader.u16(player + layout.player_ca_offset) or 0)
        expected["ca"] = int(target_ca)
    if target_positions is not None:
        actual["positions"] = reader.bytes(
            positions_address, len(ACADEMY_SON_POSITIONS),
        )
        expected["positions"] = target_positions
    return {
        key: {"expected": expected[key], "actual": actual[key]}
        for key in expected
        if actual[key] != expected[key]
    }


def _roll_low_biased_value(
    minimum: int, maximum: int, *, one_percent_maximum: bool = False,
) -> int:
    if minimum >= maximum:
        return int(minimum)
    if one_percent_maximum and secrets.randbelow(100) == 0:
        return int(maximum)
    upper = maximum - 1 if one_percent_maximum else maximum
    span = upper - minimum + 1
    return int(minimum + min(secrets.randbelow(span), secrets.randbelow(span)))


def _roll_academy_son_abilities(config: dict[str, Any]) -> tuple[int, int]:
    min_ca = int(config["min_ca"])
    max_ca = min(int(config["max_ca"]), int(config["max_pa"]) - 1)
    if (min_ca, int(config["max_ca"])) == ACADEMY_SON_RANDOM_CA_BAND:
        target_ca = _roll_low_biased_value(min_ca, max_ca)
    else:
        target_ca = secrets.randbelow(max_ca - min_ca + 1) + min_ca
    target_pa = _roll_academy_son_pa(config, minimum=target_ca + 1)
    return target_ca, target_pa


def _roll_academy_son_pa(
    config: dict[str, Any], *, minimum: int = 0,
) -> int:
    configured = (int(config["min_pa"]), int(config["max_pa"]))
    min_pa = max(configured[0], int(minimum))
    max_pa = configured[1]
    if min_pa > max_pa:
        raise ValueError("儿子原生 CA 已达到所选 PA 档位上限")
    if configured in {
        ACADEMY_SON_RANDOM_PA_BAND, ACADEMY_SON_LEGACY_RANDOM_PA_BAND,
    }:
        return _roll_low_biased_value(
            min_pa, max_pa, one_percent_maximum=True,
        )
    return secrets.randbelow(max_pa - min_pa + 1) + min_pa


def _plans_path(scope_id: str) -> Path:
    return save_data_root(scope_id or None) / "world" / "youth_intake_plans.json"


def _empty_payload() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "plans": {},
        "son_usage": {},
        "golden_generation_usage": {},
    }


def _mark_golden_hook_generation_completed(
    plan: dict[str, Any], configured: int,
) -> bool:
    """Keep a Hook-complete round active until the final roster is verified."""
    if plan.get("generation_completed") and plan.get("status") == "waiting":
        return False
    configured = max(0, int(configured))
    plan["hook_remaining_count"] = 0
    plan["generation_completed"] = True
    plan["remaining_count"] = max(
        0,
        configured - len(plan.get("processed_player_ids") or []),
    )
    plan["status"] = "waiting"
    plan.pop("completed_at", None)
    plan["verification_result"] = {
        "expected_count": configured,
        "verified_count": len(plan.get("processed_player_ids") or []),
        "hook_applied_count": configured,
        "hook_assessment": "generation_hook_completed_pending_roster",
    }
    plan["last_result"] = {
        "message": (
            f"生成期 Hook 已作用于 {configured} 名青训球员，"
            "正在等待正式名单核验"
        ),
        "applied_count": len(plan.get("processed_player_ids") or []),
    }
    return True


def _load(scope_id: str) -> dict[str, Any]:
    payload = load_document(
        "youth_intake_plans", None, scope_id, legacy_path=_plans_path(scope_id),
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("plans"), dict):
        return _empty_payload()
    try:
        previous_schema = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        previous_schema = 0
    changed = previous_schema != SCHEMA_VERSION
    payload["schema_version"] = SCHEMA_VERSION
    if not isinstance(payload.get("son_usage"), dict):
        payload["son_usage"] = {}
        changed = True
    if not isinstance(payload.get("golden_generation_usage"), dict):
        payload["golden_generation_usage"] = {}
        changed = True
    for plan in payload.get("plans", {}).values():
        kind = str(plan.get("kind") or "")
        armed_year = str(plan.get("armed_game_date") or "")[:4]
        if previous_schema < 10 and plan.get("status") in {"armed", "waiting"}:
            for field in (
                "locked_candidate_ids", "cohort_locked_at",
                "candidate_observation_ids", "candidate_eligible_observation_ids",
                "candidate_observation_changed_at", "candidate_observation_count",
                "selected_player_id", "selected_player_mode",
            ):
                if field in plan:
                    plan.pop(field, None)
                    changed = True
            plan["cohort_status"] = "waiting"
        if (
            kind in PLAN_KINDS
            and plan.get("status") in {"armed", "waiting"}
            and plan.get("execution_mode") != "hybrid"
        ):
            plan["execution_mode"] = "hybrid"
            changed = True
        if kind == "golden_generation":
            config = normalize_youth_plan_config(kind, plan.get("config"))
            if plan.get("config") != config:
                plan["config"] = config
                changed = True
            if (
                previous_schema < 12
                and plan.get("status") in {"armed", "waiting"}
                and int(
                    (plan.get("verification_result") or {}).get(
                        "consumed_by_academy_son"
                    ) or 0
                ) > 0
            ):
                verification = dict(plan.get("verification_result") or {})
                verification.pop("consumed_by_academy_son", None)
                verification.pop("shortfall", None)
                plan["verification_result"] = verification
                plan["remaining_count"] = max(
                    0,
                    int(config["count"])
                    - len(plan.get("processed_player_ids") or []),
                )
                for field in (
                    "locked_candidate_ids", "cohort_locked_at",
                    "cohort_shortfall", "candidate_observation_ids",
                    "candidate_eligible_observation_ids",
                    "candidate_observation_changed_at",
                    "candidate_observation_count",
                ):
                    plan.pop(field, None)
                plan["cohort_status"] = "waiting"
                changed = True
            if (
                previous_schema < 11
                and plan.get("status") == "completed"
                and not (plan.get("ability_results") or [])
                and not (plan.get("processed_player_ids") or [])
                and str(
                    (plan.get("verification_result") or {}).get(
                        "hook_assessment"
                    ) or ""
                ) == "generation_hook_completed"
            ):
                changed = _mark_golden_hook_generation_completed(
                    plan, int(config["count"]),
                ) or changed
            if "hook_remaining_count" not in plan:
                plan["hook_remaining_count"] = max(
                    0,
                    min(
                        int(config["count"]),
                        int(plan.get("remaining_count", config["count"])),
                    ),
                )
                changed = True
            if (
                plan.get("status") in {"armed", "waiting"}
                and plan.get("execution_mode") == "hybrid"
                and not plan.get("roster_recovery_only")
                and int(plan.get("hook_remaining_count") or 0) == 0
            ):
                changed = _mark_golden_hook_generation_completed(
                    plan, int(config["count"]),
                ) or changed
            if armed_year.isdigit():
                usage = payload["golden_generation_usage"].setdefault(armed_year, [])
                if not any(
                    str(row.get("armed_at") or "") == str(plan.get("armed_at") or "")
                    for row in usage if isinstance(row, dict)
                ):
                    usage.append({
                        "team_id": int(plan.get("team_id") or 0),
                        "count": int((plan.get("config") or {}).get("count") or 0),
                        "armed_at": str(plan.get("armed_at") or ""),
                        "armed_game_date": str(plan.get("armed_game_date") or ""),
                    })
                    changed = True
            continue
        if kind != "academy_son":
            continue
        raw_config = dict(plan.get("config") or {})
        if (
            int(raw_config.get("min_ca", 0)) == 150
            and int(raw_config.get("max_ca", 0)) == 165
        ):
            raw_config.update({"min_ca": 135, "max_ca": 150})
        config = normalize_youth_plan_config("academy_son", raw_config)
        if plan.get("config") != config:
            plan["config"] = config
            changed = True
        if config["preserve_ca"]:
            if not (
                int(config["min_pa"])
                <= int(plan.get("target_pa") or 0)
                <= int(config["max_pa"])
            ):
                plan["target_pa"] = _roll_academy_son_pa(config)
                changed = True
            if "target_ca" in plan:
                plan.pop("target_ca", None)
                changed = True
        elif not (
            int(config["min_ca"])
            <= int(plan.get("target_ca") or 0)
            <= int(config["max_ca"])
            and int(config["min_pa"])
            <= int(plan.get("target_pa") or 0)
            <= int(config["max_pa"])
            and int(plan.get("target_pa") or 0) > int(plan.get("target_ca") or 0)
        ):
            plan["target_ca"], plan["target_pa"] = _roll_academy_son_abilities(config)
            changed = True
        if "native_applied" not in plan:
            plan["native_applied"] = False
            changed = True
        generation_completed = bool(
            plan.get("generation_completed")
            or plan.get("native_applied")
            or plan.get("status") == "completed"
        )
        if plan.get("generation_completed") is not generation_completed:
            plan["generation_completed"] = generation_completed
            changed = True
        if not 0 <= int(plan.get("target_slot", -1)) < ACADEMY_SON_GENERATION_SLOTS:
            plan["target_slot"] = (
                zlib.crc32(str(plan.get("armed_at") or "").encode("utf-8"))
                % ACADEMY_SON_GENERATION_SLOTS
            )
            changed = True
        if armed_year.isdigit():
            usage = payload["son_usage"].setdefault(armed_year, [])
            if not any(
                str(row.get("armed_at") or "") == str(plan.get("armed_at") or "")
                for row in usage if isinstance(row, dict)
            ):
                usage.append({
                    "team_id": int(plan.get("team_id") or 0),
                    "armed_at": str(plan.get("armed_at") or ""),
                    "armed_game_date": str(plan.get("armed_game_date") or ""),
                })
                changed = True
    if changed:
        _save(scope_id, payload)
    return payload


def _save(scope_id: str, payload: dict[str, Any]) -> None:
    save_document(
        "youth_intake_plans", payload, scope_id, legacy_path=_plans_path(scope_id),
    )


def _plan_key(team_id: int, kind: str) -> str:
    return f"{int(team_id)}:{kind}"


def _reconcile_academy_son_usage_year(
    payload: dict[str, Any], year: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Make the current-year son quota agree with its active plan record.

    Older cancellation paths could delete the plan without rolling back the
    separate yearly usage row. A row without a same-year active academy-son
    plan is not actionable or cancellable, so it must not consume the quota.
    """
    year = str(year or "")
    yearly = payload.setdefault("son_usage", {})
    existing = [
        dict(row) for row in yearly.get(year, [])
        if isinstance(row, dict)
    ]
    plans = sorted(
        (
            plan for plan in (payload.get("plans") or {}).values()
            if isinstance(plan, dict)
            and plan.get("kind") == "academy_son"
            and plan.get("status") in {"armed", "waiting"}
            and str(plan.get("armed_game_date") or "")[:4] == year
            and int(plan.get("team_id") or 0) > 0
        ),
        key=lambda plan: str(plan.get("armed_at") or ""),
    )
    remaining = list(existing)
    reconciled: list[dict[str, Any]] = []
    for plan in plans:
        team_id = int(plan.get("team_id") or 0)
        armed_at = str(plan.get("armed_at") or "")
        match_index = next((
            index for index, row in enumerate(remaining)
            if int(row.get("team_id") or 0) == team_id
            and str(row.get("armed_at") or "") == armed_at
        ), None)
        row = remaining.pop(match_index) if match_index is not None else {}
        row.update({
            "team_id": team_id,
            "armed_at": armed_at,
            "armed_game_date": str(plan.get("armed_game_date") or ""),
        })
        name = str((plan.get("config") or {}).get("name") or "")
        if name:
            row["name"] = name
        reconciled.append(row)
    changed = reconciled != existing
    if changed:
        if reconciled:
            yearly[year] = reconciled
        else:
            yearly.pop(year, None)
    return reconciled, changed


def _reconcile_golden_generation_usage_year(
    payload: dict[str, Any], team_id: int, year: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Make the displayed quota agree with the currently active plan."""
    team_id = int(team_id)
    year = str(year or "")
    yearly = payload.setdefault("golden_generation_usage", {})
    existing = [
        dict(row) for row in yearly.get(year, [])
        if isinstance(row, dict)
    ]
    plan = (payload.get("plans") or {}).get(
        _plan_key(team_id, "golden_generation"),
    )
    plan_matches = (
        isinstance(plan, dict)
        and plan.get("kind") == "golden_generation"
        and plan.get("status") in {"armed", "waiting"}
        and int(plan.get("team_id") or 0) == team_id
    )
    armed_at = str(plan.get("armed_at") or "") if plan_matches else ""
    expected = {
        "team_id": team_id,
        "count": int((plan.get("config") or {}).get("count") or 0),
        "armed_at": armed_at,
        "armed_game_date": str(plan.get("armed_game_date") or ""),
    } if plan_matches else None
    retained: list[dict[str, Any]] = []
    matched = False
    for row in existing:
        if int(row.get("team_id") or 0) != team_id:
            retained.append(row)
            continue
        if (
            expected is not None and not matched
            and str(row.get("armed_at") or "") == armed_at
        ):
            row.update(expected)
            retained.append(row)
            matched = True
    if expected is not None and not matched:
        retained.append(expected)
    changed = retained != existing
    if changed:
        if retained:
            yearly[year] = retained
        else:
            yearly.pop(year, None)
    return retained, changed


def normalize_youth_plan_config(kind: str, config: dict[str, Any] | None) -> dict[str, Any]:
    kind = str(kind or "").strip()
    raw = dict(config or {})
    if kind == "golden_generation":
        count = int(raw.get("count", 1))
        min_pa = int(raw.get("min_pa", 110))
        max_pa = int(raw.get("max_pa", 140))
        if not 1 <= count <= GOLDEN_GENERATION_MAX_PLAYERS:
            raise ValueError(f"黄金一代人数必须在 1 至 {GOLDEN_GENERATION_MAX_PLAYERS} 人之间")
        if (min_pa, max_pa) not in GOLDEN_GENERATION_PA_PRICES:
            raise ValueError("黄金一代 PA 必须选择预设档位")
        normalized = {
            "count": count,
            "min_pa": min_pa,
            "max_pa": max_pa,
        }
        return normalized
    if kind == "academy_son":
        count = int(raw.get("count", 1))
        if count != 1:
            raise ValueError("儿子历练计划固定为 1 人")
        name = str(raw.get("name") or "").strip()
        preserve_ca = bool(raw.get("preserve_ca")) or (
            "min_ca" not in raw and "max_ca" not in raw
        )
        min_pa = int(raw.get("min_pa", 110))
        max_pa = int(raw.get("max_pa", 140))
        primary_position = str(raw.get("primary_position") or "").strip().upper()
        secondary_position = str(raw.get("secondary_position") or "").strip().upper()
        min_ca = int(raw.get("min_ca", ACADEMY_SON_CA_RANGE[0]))
        max_ca = int(raw.get("max_ca", ACADEMY_SON_CA_RANGE[1]))
        legacy = (
            not preserve_ca
            and not name
            and (min_ca, max_ca) == ACADEMY_SON_CA_RANGE
            and (min_pa, max_pa) == ACADEMY_SON_PA_RANGE
        )
        if name and not ACADEMY_SON_NAME_PATTERN.fullmatch(name):
            raise ValueError("儿子名字必须为 2 至 30 个英文字母")
        if not preserve_ca and not legacy and (
            min_ca, max_ca
        ) not in ACADEMY_SON_CA_PRICES:
            raise ValueError("儿子 CA 必须选择预设档位")
        if not legacy and (min_pa, max_pa) not in ACADEMY_SON_PA_PRICES:
            raise ValueError("儿子 PA 必须选择预设档位")
        if not preserve_ca and not legacy and min_ca >= max_pa:
            raise ValueError("儿子 PA 档位必须能够生成高于 CA 的数值")
        if primary_position or secondary_position:
            if primary_position not in ACADEMY_SON_POSITIONS:
                raise ValueError("儿子主位置无效")
            if (
                secondary_position
                and secondary_position not in ACADEMY_SON_POSITIONS
            ):
                raise ValueError("儿子副位置无效")
            if secondary_position and primary_position == secondary_position:
                raise ValueError("儿子主位置和副位置不能相同")
        normalized = {
            "count": 1,
            "name": name,
            "preserve_ca": preserve_ca,
            "min_pa": min_pa,
            "max_pa": max_pa,
            "primary_position": primary_position,
            "secondary_position": secondary_position,
        }
        if not preserve_ca:
            normalized.update({"min_ca": min_ca, "max_ca": max_ca})
        return normalized
    raise ValueError("未知的青训计划")


def golden_generation_price(config: dict[str, Any] | None) -> int:
    normalized = normalize_youth_plan_config("golden_generation", config)
    unit_price = GOLDEN_GENERATION_PA_PRICES[
        (normalized["min_pa"], normalized["max_pa"])
    ]
    return int(unit_price * normalized["count"])


def academy_son_price(config: dict[str, Any] | None) -> int:
    normalized = normalize_youth_plan_config("academy_son", config)
    if not normalized["primary_position"]:
        raise ValueError("请选择儿子的主位置")
    pa_price = ACADEMY_SON_PA_PRICES[
        (normalized["min_pa"], normalized["max_pa"])
    ]
    if normalized["preserve_ca"]:
        return int(pa_price)
    return int(ACADEMY_SON_LEGACY_PA_PRICES[
        (normalized["min_pa"], normalized["max_pa"])
    ] + ACADEMY_SON_CA_PRICES[
        (normalized["min_ca"], normalized["max_ca"])
    ])


def golden_generation_son_overlap_count(
    golden_plan: dict[str, Any], son_plan: dict[str, Any],
) -> int:
    """Compatibility shim: academy sons never consume golden-plan slots."""
    del golden_plan, son_plan
    return 0


def _parse_game_date(value: str | date | None) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError as error:
        raise ValueError("当前游戏日期不可用，请先刷新存档") from error


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    internal_fields = {
        "baseline_player_ids", "baseline_loaned_out_ids",
        "global_player_baseline_ids",
        "processed_player_ids",
        "candidate_observation_ids", "candidate_eligible_observation_ids",
        "locked_candidate_ids",
        "planned_ability_targets", "planned_ability_originals",
        "selected_player_id",
    }
    verification = dict(plan.get("verification_result") or {})
    shortfall_recovery_available = bool(
        plan.get("status") == "completed_with_refund"
        and int(verification.get("observed_count") or 0)
        > int(verification.get("eligible_count") or 0)
    )
    return {
        key: value for key, value in plan.items()
        if key not in internal_fields
    } | {
        "baseline_count": len(plan.get("baseline_player_ids") or []),
        "processed_count": len(plan.get("processed_player_ids") or []),
        "candidate_count": len(
            plan.get("locked_candidate_ids")
            or plan.get("candidate_observation_ids")
            or []
        ),
        "shortfall_recovery_available": shortfall_recovery_available,
    }


def youth_plans_for_team(scope_id: str, team_id: int) -> dict[str, Any]:
    with _LOCK:
        plans = _load(scope_id).get("plans") or {}
        return {
            kind: _public_plan(plans[_plan_key(team_id, kind)])
            for kind in PLAN_KINDS
            if _plan_key(team_id, kind) in plans
        }


def academy_son_year_status(
    scope_id: str, game_date: str | date | None,
) -> dict[str, Any]:
    current = _parse_game_date(game_date)
    year = str(current.year)
    with _LOCK:
        payload = _load(scope_id)
        usage, changed = _reconcile_academy_son_usage_year(payload, year)
        if changed:
            _save(scope_id, payload)
    used = min(ACADEMY_SON_YEARLY_LIMIT, len(usage))
    return {
        "year": current.year,
        "limit": ACADEMY_SON_YEARLY_LIMIT,
        "used": used,
        "remaining": max(0, ACADEMY_SON_YEARLY_LIMIT - used),
        "placements": usage,
    }


def golden_generation_year_status(
    scope_id: str, team_id: int, game_date: str | date | None,
) -> dict[str, Any]:
    current = _parse_game_date(game_date)
    year = str(current.year)
    with _LOCK:
        payload = _load(scope_id)
        yearly_usage, changed = _reconcile_golden_generation_usage_year(
            payload, team_id, year,
        )
        if changed:
            _save(scope_id, payload)
        usage = [
            dict(row)
            for row in yearly_usage
            if isinstance(row, dict)
            and int(row.get("team_id") or 0) == int(team_id)
        ]
    used = min(
        GOLDEN_GENERATION_YEARLY_LIMIT,
        sum(max(0, int(row.get("count") or 0)) for row in usage),
    )
    return {
        "year": current.year,
        "team_id": int(team_id),
        "limit": GOLDEN_GENERATION_YEARLY_LIMIT,
        "used": used,
        "remaining": max(0, GOLDEN_GENERATION_YEARLY_LIMIT - used),
        "placements": usage,
    }


def active_youth_generation_plans(scope_id: str) -> list[dict[str, Any]]:
    with _LOCK:
        plans = list((_load(scope_id).get("plans") or {}).values())
    result: list[dict[str, Any]] = []
    active_sons = sorted(
        (
            plan for plan in plans
            if plan.get("kind") == "academy_son"
            and plan.get("status") in {"armed", "waiting"}
        ),
        key=lambda plan: str(plan.get("armed_at") or ""), reverse=True,
    )
    selected_son = active_sons[0] if active_sons else None
    for plan in plans:
        kind = str(plan.get("kind") or "")
        if plan.get("status") not in {"armed", "waiting"}:
            continue
        if plan.get("roster_recovery_only"):
            continue
        armed_at = str(plan.get("armed_at") or "")
        row = {
            "team_id": int(plan.get("team_id") or 0),
            "team_address": plan.get("team_address"),
            "kind": kind,
            "token": zlib.crc32(armed_at.encode("utf-8")),
            "execution_mode": str(
                plan.get("execution_mode") or "generation_hook"
            ),
        }
        if kind == "golden_generation":
            config = normalize_youth_plan_config(kind, plan.get("config"))
            remaining = max(0, min(
                int(config["count"]),
                int(plan.get("hook_remaining_count", config["count"])),
            ))
            row.update({
                "count": remaining,
                "min_pa": int(config["min_pa"]),
                "max_pa": int(config["max_pa"]),
            })
        elif kind == "academy_son":
            if plan is not selected_son:
                continue
            config = normalize_youth_plan_config(kind, plan.get("config"))
            row["count"] = 1
            row["roster_finalize_only"] = bool(config["preserve_ca"])
            if not row["roster_finalize_only"]:
                row["ca"] = int(plan.get("target_ca") or 0)
                row["pa"] = int(plan.get("target_pa") or 0)
                row["slot"] = int(plan.get("target_slot") or 0)
                row["applied"] = 1 if plan.get("native_applied") else 0
                row["generation_completed"] = bool(
                    plan.get("generation_completed") or plan.get("native_applied")
                )
        else:
            continue
        result.append(row)
    return result


def active_golden_generation_plans(scope_id: str) -> list[dict[str, Any]]:
    return [
        row for row in active_youth_generation_plans(scope_id)
        if row.get("kind") == "golden_generation"
    ]


def record_youth_generation_progress(
    scope_id: str, status: dict[str, Any],
) -> int:
    targets = status.get("targets") or []
    progress = {
        (int(row.get("team_id") or 0), int(row.get("token") or 0) & 0xFFFFFFFF):
        max(0, int(row.get("remaining") or 0))
        for row in targets
        if int(row.get("team_id") or 0) > 0
    }
    son_target = status.get("son_target") or {}
    son_key = (
        int(son_target.get("team_id") or 0),
        int(son_target.get("token") or 0) & 0xFFFFFFFF,
    )
    son_applied = max(0, int(status.get("son_applied_count") or 0))
    changed = 0
    with _LOCK:
        payload = _load(scope_id)
        for plan in (payload.get("plans") or {}).values():
            kind = str(plan.get("kind") or "")
            if (
                kind not in PLAN_KINDS
                or plan.get("status") not in {"armed", "waiting"}
            ):
                continue
            key = (
                int(plan.get("team_id") or 0),
                zlib.crc32(str(plan.get("armed_at") or "").encode("utf-8")),
            )
            if kind == "academy_son":
                if key != son_key or son_applied < 1:
                    continue
                plan["generation_completed"] = True
                if not status.get("son_finalized"):
                    if not plan.get("native_applied") or plan.get("status") != "waiting":
                        plan["native_applied"] = True
                        plan["status"] = "waiting"
                        changed += 1
                    continue
                plan["remaining_count"] = 0
                plan["status"] = "completed"
                plan["completed_at"] = datetime.now(timezone.utc).isoformat()
                plan["last_result"] = {
                    "message": "儿子历练计划已生效，CA/PA 已按专属范围写入",
                    "applied_count": 1,
                    "player": status.get("son_player"),
                }
                changed += 1
                continue
            if key not in progress:
                continue
            configured = int(normalize_youth_plan_config(
                "golden_generation", plan.get("config"),
            )["count"])
            remaining = min(configured, progress[key])
            if plan.get("execution_mode") == "hybrid":
                if int(plan.get("hook_remaining_count", configured)) != remaining:
                    plan["hook_remaining_count"] = remaining
                    changed += 1
                if remaining == 0 and _mark_golden_hook_generation_completed(
                    plan, configured,
                ):
                    changed += 1
                continue
            if int(plan.get("remaining_count", configured)) != remaining:
                plan["remaining_count"] = remaining
                changed += 1
            if remaining == 0 and plan.get("status") != "completed":
                plan["status"] = "completed"
                plan["completed_at"] = datetime.now(timezone.utc).isoformat()
                plan["last_result"] = {
                    "message": f"生成期 PA 计划已作用于 {configured} 名青训球员",
                    "applied_count": configured,
                }
                changed += 1
        if changed:
            _save(scope_id, payload)
    return changed


def record_golden_generation_progress(
    scope_id: str, targets: list[dict[str, Any]],
) -> int:
    return record_youth_generation_progress(scope_id, {"targets": targets})


def disarm_youth_plan(
    scope_id: str, team_id: int, kind: str, *, rollback_usage: bool = False,
) -> None:
    with _LOCK:
        payload = _load(scope_id)
        removed = payload["plans"].pop(_plan_key(team_id, kind), None)
        if rollback_usage and removed and kind in {"academy_son", "golden_generation"}:
            year = str(removed.get("armed_game_date") or "")[:4]
            usage_key = (
                "son_usage" if kind == "academy_son"
                else "golden_generation_usage"
            )
            usage = (payload.get(usage_key) or {}).get(year)
            if isinstance(usage, list):
                armed_at = str(removed.get("armed_at") or "")
                for index in range(len(usage) - 1, -1, -1):
                    row = usage[index]
                    if (
                        isinstance(row, dict)
                        and int(row.get("team_id") or 0) == int(team_id)
                        and str(row.get("armed_at") or "") == armed_at
                    ):
                        usage.pop(index)
                        break
        _save(scope_id, payload)


def complete_youth_plan_shortfall(
    scope_id: str, team_id: int, kind: str, *, shortfall: int,
    eligible_count: int, observed_count: int, refund: dict[str, Any],
) -> dict[str, Any]:
    """Close a stable but undersized intake after its missing slots are refunded."""
    kind = str(kind or "").strip()
    if kind not in PLAN_KINDS:
        raise ValueError("未知的青训计划")
    with _LOCK:
        payload = _load(scope_id)
        plan = (payload.get("plans") or {}).get(_plan_key(team_id, kind))
        if not plan:
            raise ValueError("青训计划不存在")
        if plan.get("status") == "completed_with_refund":
            return _public_plan(plan)
        if plan.get("status") not in {"armed", "waiting"}:
            raise RuntimeError("青训计划已经结算，不能重复退款")
        config = normalize_youth_plan_config(kind, plan.get("config"))
        expected_count = int(config["count"])
        missing_count = min(expected_count, max(1, int(shortfall)))
        fulfilled_count = max(0, expected_count - missing_count)
        verification = dict(plan.get("verification_result") or {})
        verification.pop("shortfall_settlement_pending", None)
        verification.update({
            "expected_count": expected_count,
            "verified_count": fulfilled_count,
            "eligible_count": max(0, int(eligible_count)),
            "observed_count": max(0, int(observed_count)),
            "shortfall": missing_count,
            "refunded_count": missing_count,
            "refund_total": float(refund.get("total") or 0),
        })
        plan["verification_result"] = verification
        plan["shortfall_result"] = {
            "expected_count": expected_count,
            "fulfilled_count": fulfilled_count,
            "missing_count": missing_count,
            "eligible_count": max(0, int(eligible_count)),
            "observed_count": max(0, int(observed_count)),
        }
        plan["refund_result"] = dict(refund)
        plan["remaining_count"] = 0
        if kind == "golden_generation":
            plan["hook_remaining_count"] = 0
        else:
            plan["generation_completed"] = True
        plan["status"] = "completed_with_refund"
        plan["completed_at"] = datetime.now(timezone.utc).isoformat()
        plan["last_result"] = {
            "message": (
                f"正式青训名单人数不足：计划 {expected_count} 人，"
                f"完成 {fulfilled_count} 人；缺少 {missing_count} 人的费用已退款"
            ),
            "applied_count": fulfilled_count,
            "missing_count": missing_count,
            "refund_total": float(refund.get("total") or 0),
        }
        _save(scope_id, payload)
        return _public_plan(plan)


def complete_golden_son_overlap_refund(
    scope_id: str, team_id: int, *, refund: dict[str, Any],
) -> dict[str, Any]:
    """Reclassify one untouched golden result as a son-consumed refunded slot."""
    with _LOCK:
        payload = _load(scope_id)
        plans = payload.get("plans") or {}
        golden = plans.get(_plan_key(team_id, "golden_generation"))
        son = plans.get(_plan_key(team_id, "academy_son"))
        if not golden or not son:
            raise ValueError("同俱乐部儿子与小妖计划不完整")
        verification = dict(golden.get("verification_result") or {})
        if (
            golden.get("status") == "completed_with_refund"
            and int(verification.get("consumed_by_academy_son") or 0) > 0
        ):
            return _public_plan(golden)
        if golden.get("status") != "completed":
            raise RuntimeError("小妖计划尚未完成，不能执行重叠补偿退款")
        if golden_generation_son_overlap_count(golden, son) != 1:
            raise RuntimeError("未确认儿子占用了同队小妖 Hook 名额")
        results = list(golden.get("ability_results") or [])
        removable_index = next(
            (
                index for index in range(len(results) - 1, -1, -1)
                if int(results[index].get("player_id") or results[index].get("id") or 0) > 0
                and int(results[index].get("pa_before") or 0)
                == int(results[index].get("pa") or 0)
            ),
            None,
        )
        if removable_index is None:
            raise RuntimeError("没有可安全取消登记且无需回滚 PA 的小妖球员")
        removed = results.pop(removable_index)
        removed_id = int(removed.get("player_id") or removed.get("id") or 0)
        processed = {
            int(value) for value in golden.get("processed_player_ids") or []
            if int(value) > 0
        }
        processed.discard(removed_id)
        golden["processed_player_ids"] = sorted(processed)
        golden["processed_count"] = len(processed)
        golden["ability_results"] = results
        for field in ("planned_ability_targets", "planned_ability_originals"):
            values = dict(golden.get(field) or {})
            values.pop(str(removed_id), None)
            golden[field] = values
        config = normalize_youth_plan_config(
            "golden_generation", golden.get("config"),
        )
        verification.update({
            "expected_count": int(config["count"]),
            "verified_count": len(processed),
            "consumed_by_academy_son": 1,
            "shortfall": 1,
            "refunded_count": 1,
            "refund_total": float(refund.get("total") or 0),
            "removed_unmodified_player_id": removed_id,
        })
        golden["verification_result"] = verification
        golden["shortfall_result"] = {
            "expected_count": int(config["count"]),
            "fulfilled_count": len(processed),
            "missing_count": 1,
            "consumed_by_academy_son": 1,
        }
        golden["refund_result"] = dict(refund)
        golden["remaining_count"] = 0
        golden["status"] = "completed_with_refund"
        golden["completed_at"] = datetime.now(timezone.utc).isoformat()
        golden["last_result"] = {
            "message": (
                f"儿子占用 1 个小妖名额；已确认 {len(processed)} 人，"
                "缺少 1 人的费用已退款"
            ),
            "applied_count": len(processed),
            "missing_count": 1,
            "refund_total": float(refund.get("total") or 0),
        }
        _save(scope_id, payload)
        return _public_plan(golden)


def reopen_misclassified_youth_shortfall(
    scope_id: str, team_id: int, kind: str,
) -> dict[str, Any]:
    """Reopen a refunded plan whose observed intake was rejected by the old gate.

    The original payment was already refunded, so this is a compensating roster-only
    recovery. It reuses the pre-intake baseline and never reinstalls the generation
    Hook. The user must explicitly run final-roster verification afterwards.
    """
    kind = str(kind or "").strip()
    if kind not in PLAN_KINDS:
        raise ValueError("未知的青训计划")
    with _LOCK:
        payload = _load(scope_id)
        plan = (payload.get("plans") or {}).get(_plan_key(team_id, kind))
        if not plan or plan.get("status") != "completed_with_refund":
            raise ValueError("当前没有可恢复的已退款青训计划")
        verification = dict(plan.get("verification_result") or {})
        observed_count = max(0, int(verification.get("observed_count") or 0))
        eligible_count = max(0, int(verification.get("eligible_count") or 0))
        if observed_count <= eligible_count:
            raise ValueError("该计划没有旧候选门槛误判证据，不能自动恢复")
        config = normalize_youth_plan_config(kind, plan.get("config"))
        processed = {
            int(value) for value in plan.get("processed_player_ids") or []
            if int(value) > 0
        }
        remaining = max(0, int(config["count"]) - len(processed))
        if remaining <= 0:
            raise ValueError("该青训计划已经完成，无需恢复")
        now = datetime.now(timezone.utc).isoformat()
        plan["prior_shortfall_result"] = dict(plan.get("shortfall_result") or {})
        plan["prior_refund_result"] = dict(plan.get("refund_result") or {})
        plan["status"] = "waiting"
        plan["roster_recovery_only"] = True
        plan["recovery_reason"] = "legacy_formed_at_club_candidate_gate"
        plan["reopened_at"] = now
        plan["remaining_count"] = remaining
        plan["cohort_status"] = "waiting"
        plan["cohort_shortfall"] = 0
        plan["candidate_observation_count"] = 0
        plan["last_result"] = {
            "message": "旧候选门槛曾排除已观察到的本届新人；请手动核验并补写",
            "applied_count": len(processed),
            "remaining_count": remaining,
        }
        for field in (
            "completed_at", "shortfall_result", "refund_result",
            "locked_candidate_ids", "cohort_locked_at",
            "candidate_observation_ids", "candidate_eligible_observation_ids",
            "candidate_observation_changed_at", "cohort_observed_count",
        ):
            plan.pop(field, None)
        _save(scope_id, payload)
        return _public_plan(plan)


def remove_youth_plans_for_teams(
    scope_id: str, team_ids: set[int] | list[int] | tuple[int, ...],
) -> list[dict[str, Any]]:
    """Remove plans and yearly usage for clubs no longer owned at this save date."""
    targets = {
        int(team_id) for team_id in team_ids
        if int(team_id) > 0
    }
    if not targets:
        return []
    with _LOCK:
        payload = _load(scope_id)
        plans = payload.get("plans") or {}
        removed = [
            dict(plan) for plan in plans.values()
            if int(plan.get("team_id") or 0) in targets
        ]
        payload["plans"] = {
            key: plan for key, plan in plans.items()
            if int(plan.get("team_id") or 0) not in targets
        }
        usage_changed = False
        for usage_key in ("son_usage", "golden_generation_usage"):
            yearly = payload.get(usage_key) or {}
            for year, rows in list(yearly.items()):
                if not isinstance(rows, list):
                    continue
                retained = [
                    row for row in rows
                    if not isinstance(row, dict)
                    or int(row.get("team_id") or 0) not in targets
                ]
                if len(retained) == len(rows):
                    continue
                usage_changed = True
                if retained:
                    yearly[year] = retained
                else:
                    yearly.pop(year, None)
        if removed or usage_changed:
            _save(scope_id, payload)
    return [_public_plan(plan) for plan in removed]


def _movement_snapshot(
    reader: Reader, person: int, team_address: int,
) -> dict[str, Any]:
    """Read only the movement fields needed by youth candidate filtering."""
    fm24 = reader.layout.key == "fm24"
    contract_pointer_offset = 0xC8 if fm24 else 0xA8
    contract_size = 0xC0 if fm24 else 0xC8
    contract_type_offset = 0xB3 if fm24 else 0xC3
    joined_date_offset = 0x14C if fm24 else 0x11C
    signed_date_offset = 0x44 if fm24 else 0x4C
    loan_pointer_offset = 0xD0 if fm24 else 0xB0
    contract = int(reader.ptr(person + contract_pointer_offset) or 0)
    if not contract:
        raise RuntimeError("球员主合同指针为空")
    raw_contract = reader.bytes(contract, contract_size)
    if (
        not raw_contract
        or len(raw_contract) != contract_size
        or struct.unpack_from("<Q", raw_contract, 0x08)[0] != person
        or not struct.unpack_from("<Q", raw_contract, 0x10)[0]
    ):
        raise RuntimeError("球员主合同身份校验失败")
    joined = decode_date(int(reader.u32(person + joined_date_offset) or 0))
    signed = decode_date(
        struct.unpack_from("<I", raw_contract, signed_date_offset)[0]
    )
    loan_container = int(reader.ptr(person + loan_pointer_offset) or 0)
    loan_contract = int(reader.ptr(loan_container) or 0) if loan_container else 0
    loan: dict[str, Any] | None = None
    expected_loan_rva = getattr(reader.layout, "loan_contract_vtable_rva", None)
    if loan_contract and expected_loan_rva is not None:
        loan_target = int(reader.ptr(loan_contract + 0x10) or 0)
        if (
            reader.ptr(loan_contract) == reader.module_base + int(expected_loan_rva)
            and reader.ptr(loan_contract + 0x08) == person
            and loan_target
        ):
            loan = {
                "team_address": hex(loan_target),
                "is_loaned_out": loan_target != int(team_address),
            }
    return {
        "contract_type": int(raw_contract[contract_type_offset]),
        "joined_club_date": joined.isoformat() if joined else None,
        "signed_date": signed.isoformat() if signed else None,
        "loan": loan,
        "is_loaned_out": bool(loan and loan.get("is_loaned_out")),
    }


def _roster_snapshot(
    reader: Reader, team_id: int, team_address: Any, game_date: date,
    include_movement: bool = False,
) -> list[dict[str, Any]]:
    try:
        # A plan can survive a save reload, so use the stable Team UID to
        # relocate the live Team/Club pair before reading its roster.  The
        # saved address is only a current-session hint validated by the
        # resolver, never an authoritative persistent pointer.
        resolved = resolve_team_club(reader, _address(team_address), int(team_id))
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise RuntimeError("俱乐部地址已失效，请重新扫描俱乐部") from error
    team = int(resolved.team_address)
    club = int(resolved.club_address)
    linked_teams = [team]
    linked_header = reader.bytes(club + 0x18, 24) if club else None
    if linked_header and len(linked_header) == 24:
        begin, end, capacity = struct.unpack("<QQQ", linked_header)
        if (
            begin and begin <= end <= capacity and not (end - begin) % 8
            and 1 <= (end - begin) // 8 <= 16
        ):
            candidates = []
            for candidate in reader.ptr_array(begin, (end - begin) // 8):
                if (
                    candidate
                    and reader.ptr(candidate) == reader.module_base + reader.layout.team_vtable_rva
                    and reader.ptr(candidate + TEAM_CLUB) == club
                ):
                    candidates.append(int(candidate))
            if team in candidates:
                linked_teams = list(dict.fromkeys(candidates))
    players_by_id: dict[int, dict[str, Any]] = {}
    for linked_team in linked_teams:
        for summary in reader.roster(linked_team):
            address = _address(summary.get("address"))
            player = _player(
                reader, address, game_date=game_date, roster_summary=summary,
            ) if address else None
            if player and int(player.get("id") or 0) > 0:
                person = _validated_player_person(reader, address, int(player["id"]))
                relationships = reader.ptr(
                    person + int(reader.layout.person_relationships_offset)
                ) if reader.layout.person_relationships_offset is not None else 0
                header = reader.bytes(relationships, 24) if relationships else None
                formed_at_club = False
                if header and len(header) == 24:
                    relation_begin, relation_end, relation_capacity = struct.unpack(
                        "<QQQ", header,
                    )
                    if (
                        relation_begin and relation_begin <= relation_end <= relation_capacity
                        and not (relation_end - relation_begin) % 16
                        and relation_end - relation_begin <= 16 * 512
                    ):
                        raw_relations = reader.bytes(
                            relation_begin, relation_end - relation_begin,
                        ) or b""
                        if len(raw_relations) == relation_end - relation_begin:
                            formed_at_club = any(
                                struct.unpack_from("<Q", raw_relations, position)[0] == club
                                and raw_relations[position + 10] == 1
                                and raw_relations[position + 11] == RELATION_FORMED_AT_CLUB
                                for position in range(0, len(raw_relations), 16)
                            )
                player["formed_at_club"] = formed_at_club
                if include_movement:
                    try:
                        player["movement"] = _movement_snapshot(
                            reader, person, linked_team,
                        )
                    except Exception as error:
                        # Movement data is a classification aid, not a reason to
                        # discard an otherwise identity-validated roster row.
                        player["movement"] = {
                            "read_error": str(error),
                        }
                players_by_id[int(player["id"])] = player
    players = list(players_by_id.values())
    return players


def _fresh_youth_player_targets(
    reader: Reader, directory: DatabaseIndex, plan: dict[str, Any],
) -> dict[int, tuple[int, int, str]]:
    """Recheck post-plan Person objects individually for explicit manual search.

    A transiently unreadable sample can exclude a whole vtable group from the
    normal player index. Do not reuse that classification for manual recovery:
    refresh the pointer table, exclude the saved global baseline, then validate
    each remaining outer Player and its UID. This scans a registered table,
    never unbounded process memory, and does not relax Club ownership checks.
    """
    if plan.get("global_player_baseline_complete") is not True:
        return {}
    _invalidate_reader_after_write(reader)
    addresses = directory.addresses("person", force=True)
    headers = reader._fixed_size_snapshots(addresses, 0x10)
    excluded = {
        int(value) for value in (
            list(plan.get("global_player_baseline_ids") or [])
            + list(plan.get("baseline_player_ids") or [])
            + list(plan.get("processed_player_ids") or [])
        )
    }
    layouts = (
        ("actual_player", reader.layout.player_person_offset,
         reader.layout.actual_player_vtable_rvas),
        ("actual_player_and_non_player",
         reader.layout.player_and_non_player_person_offset,
         reader.layout.player_and_non_player_vtable_rvas),
    )
    targets: dict[int, tuple[int, int, str]] = {}
    ambiguous: set[int] = set()
    for person in addresses:
        raw = headers.get(person)
        if not raw or len(raw) < 0x10:
            continue
        uid = struct.unpack_from("<I", raw, 0x0C)[0]
        if not uid or uid in excluded or uid in ambiguous:
            continue
        matches = []
        for kind, offset, vtables in layouts:
            if offset is None or person <= int(offset):
                continue
            player = int(person) - int(offset)
            if int(reader.ptr(player) or 0) not in {
                reader.module_base + int(rva) for rva in vtables
            }:
                continue
            try:
                if _validated_player_person(reader, player, uid) == person:
                    matches.append((int(person), player, kind))
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
        if len(matches) != 1:
            continue
        target = matches[0]
        if uid in targets and targets[uid] != target:
            targets.pop(uid)
            ambiguous.add(uid)
        else:
            targets[uid] = target
    return targets


def _primary_contract_youth_candidates(
    reader: Reader, directory: DatabaseIndex | None,
    plan: dict[str, Any], team_id: int, team_address: Any, game_date: date,
    *, fresh_targets: dict[int, tuple[int, int, str]] | None = None,
) -> list[dict[str, Any]]:
    """Find post-plan players owned by the target Club but absent from rosters.

    Some clubs have no persistent youth squad.  Their intake candidates can
    already have a validated primary contract with the first team while still
    living outside every roster exposed by ``Club+0x18``.  Restrict this
    fallback to the complete post-plan global UID delta, then validate the
    contract's Person back-reference and Team -> Club ownership before exposing
    a candidate.  Roster membership remains the preferred source.
    """
    if directory is None or plan.get("global_player_baseline_complete") is not True:
        return []
    try:
        targets = directory.player_targets() if fresh_targets is None else fresh_targets
        resolved = resolve_team_club(
            reader, _address(team_address), int(team_id),
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return []
    excluded = {
        int(value)
        for value in (
            list(plan.get("global_player_baseline_ids") or [])
            + list(plan.get("processed_player_ids") or [])
        )
        if int(value) > 0
    }
    pending = [
        (int(uid), int(person), int(player))
        for uid, (person, player, _kind) in targets.items()
        if int(uid) > 0 and int(uid) not in excluded
        and int(person) > 0 and int(player) > 0
    ]
    if not pending:
        return []
    contract_offset = 0xC8 if reader.layout.key == "fm24" else 0xA8
    snapshot_reader = getattr(reader, "_fixed_size_snapshots", None)
    person_snapshots = (
        snapshot_reader(
            [person for _uid, person, _player_address in pending],
            contract_offset + 8,
        )
        if callable(snapshot_reader) else {}
    )
    contract_by_person: dict[int, int] = {}
    for _uid, person, _player_address in pending:
        raw = person_snapshots.get(person)
        contract = (
            struct.unpack_from("<Q", raw, contract_offset)[0]
            if raw and len(raw) >= contract_offset + 8
            else int(reader.ptr(person + contract_offset) or 0)
        )
        if contract:
            contract_by_person[person] = int(contract)
    contract_snapshots = (
        snapshot_reader(list(set(contract_by_person.values())), 0x18)
        if callable(snapshot_reader) and contract_by_person else {}
    )
    expected_team_vtable = (
        int(reader.module_base) + int(reader.layout.team_vtable_rva)
    )
    target_club = int(resolved.club_address)
    players: list[dict[str, Any]] = []
    for uid, person, player_address in pending:
        contract = contract_by_person.get(person, 0)
        if not contract:
            continue
        raw_contract = contract_snapshots.get(contract)
        contract_person = (
            struct.unpack_from("<Q", raw_contract, 0x08)[0]
            if raw_contract and len(raw_contract) >= 0x18
            else int(reader.ptr(contract + 0x08) or 0)
        )
        contract_team = (
            struct.unpack_from("<Q", raw_contract, 0x10)[0]
            if raw_contract and len(raw_contract) >= 0x18
            else int(reader.ptr(contract + 0x10) or 0)
        )
        if (
            contract_person != person
            or not contract_team
            or int(reader.ptr(contract_team) or 0) != expected_team_vtable
            or int(reader.u32(contract_team + 0x0C) or 0) <= 0
            or int(reader.ptr(contract_team + TEAM_CLUB) or 0) != target_club
        ):
            continue
        try:
            _validated_player_person(reader, player_address, uid)
            row = _player(reader, player_address, game_date=game_date)
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if not row or int(row.get("id") or 0) != uid:
            continue
        row["candidate_source"] = "primary_contract"
        row["formed_at_club"] = None
        try:
            row["movement"] = _movement_snapshot(
                reader, person, int(contract_team),
            )
        except Exception as error:
            row["movement"] = {"read_error": str(error)}
        if bool((row.get("movement") or {}).get("is_loaned_out")):
            continue
        players.append(row)
    return players


def _youth_candidate_source_players(
    reader: Reader, plan: dict[str, Any], team_id: int,
    team_address: Any, game_date: date, *, deep_search: bool = False,
) -> list[dict[str, Any]]:
    """Merge roster evidence with independently validated contract ownership."""
    directory = _attach_database_index(reader)
    roster_error = None
    try:
        roster_players = _roster_snapshot(
            reader, team_id, team_address, game_date, True,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        # A stale linked roster must not prevent the contract fallback. If that
        # also finds nothing, preserve the technical error instead of refunding.
        roster_error = error
        roster_players = []
    fresh_targets = None
    if deep_search and directory is not None:
        fresh_targets = _fresh_youth_player_targets(reader, directory, plan)
    contract_players = _primary_contract_youth_candidates(
        reader, directory, plan, team_id, team_address, game_date,
        fresh_targets=fresh_targets,
    )
    players_by_id = {
        int(row.get("id") or 0): row
        for row in roster_players
        if int(row.get("id") or 0) > 0
    }
    for row in contract_players:
        players_by_id.setdefault(int(row["id"]), row)
    if roster_error is not None and not players_by_id:
        raise roster_error
    return list(players_by_id.values())


def read_youth_roster_snapshot(
    team_id: int, team_address: Any, game_date: str | date,
) -> dict[str, Any]:
    current = _parse_game_date(game_date)
    pid, _path, layout = select_process_layout()
    if layout.key not in SUPPORTED_GAMES:
        raise RuntimeError("当前 Football Manager 版本尚未适配青训计划")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        try:
            reader.module = module
        except AttributeError:
            pass
        directory = _attach_database_index(reader)
        if directory is None:
            raise RuntimeError("无法建立原生球队/球员 UID 目录，青训计划未创建")
        try:
            # Use the same native Person table as world-player search, without
            # its display filters or sampled player-group classification. The
            # persisted historical field name remains compatible; all existing
            # Person UIDs form a safe superset of the existing player baseline.
            global_player_ids = list(directory.person_uids_snapshot())
            if not global_player_ids:
                raise RuntimeError("empty native Person UID baseline")
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise RuntimeError("无法建立全球球员 UID 基线，青训计划未创建") from error
        players = _roster_snapshot(
            reader, team_id, team_address, current, True,
        )
    return {
        "game_key": layout.key,
        "players": players,
        "global_player_ids": global_player_ids,
        "global_player_baseline_complete": True,
    }


def arm_youth_plan(
    scope_id: str, team_id: int, team_address: Any, kind: str,
    config: dict[str, Any] | None, game_date: str | date,
    *, payment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = str(kind or "").strip()
    normalized = normalize_youth_plan_config(kind, config)
    snapshot = read_youth_roster_snapshot(team_id, team_address, game_date)
    if snapshot["game_key"] not in GOLDEN_GENERATION_SUPPORTED_GAMES:
        raise RuntimeError("当前 Football Manager 版本尚未适配青训生成")
    now = datetime.now(timezone.utc).isoformat()
    plan = {
        "team_id": int(team_id),
        "team_address": str(team_address),
        "kind": kind,
        "game_key": snapshot["game_key"],
        "armed_game_date": _parse_game_date(game_date).isoformat(),
        "armed_at": now,
        "status": "armed",
        "execution_mode": "hybrid",
        "config": normalized,
        "baseline_player_ids": sorted({
            int(row.get("id") or 0) for row in snapshot["players"]
            if int(row.get("id") or 0) > 0
        }),
        "baseline_loaned_out_ids": sorted({
            int(row.get("id") or 0) for row in snapshot["players"]
            if int(row.get("id") or 0) > 0
            and bool((row.get("movement") or {}).get("is_loaned_out"))
        }),
        "global_player_baseline_ids": sorted({
            int(value) for value in snapshot.get("global_player_ids") or []
            if int(value) > 0
        }),
        "global_player_baseline_complete": (
            snapshot.get("global_player_baseline_complete") is True
        ),
        "processed_player_ids": [],
        "cohort_status": "waiting",
        "last_result": None,
    }
    plan["remaining_count"] = int(normalized["count"])
    if kind == "golden_generation":
        plan["hook_remaining_count"] = int(normalized["count"])
    if kind == "academy_son":
        if normalized["preserve_ca"]:
            plan["target_pa"] = _roll_academy_son_pa(normalized)
        else:
            plan["target_ca"], plan["target_pa"] = _roll_academy_son_abilities(
                normalized,
            )
        plan["target_slot"] = secrets.randbelow(ACADEMY_SON_NEW_PLAN_SLOTS)
        plan["native_applied"] = False
        plan["generation_completed"] = False
    if payment:
        plan["payment"] = {
            **{
                key: float(payment.get(key) or 0)
                for key in ("bank", "wallet", "total")
            },
            "transaction_id": str(payment.get("transaction_id") or ""),
        }
    with _LOCK:
        payload = _load(scope_id)
        if kind == "academy_son":
            year = str(_parse_game_date(game_date).year)
            usage, usage_changed = _reconcile_academy_son_usage_year(
                payload, year,
            )
            if usage_changed:
                _save(scope_id, payload)
            if len(usage) >= ACADEMY_SON_YEARLY_LIMIT:
                raise ValueError(f"{year} 年唯一的儿子历练名额已经用完")
            active_son = next(
                (
                    existing for existing in payload["plans"].values()
                    if existing.get("kind") == "academy_son"
                    and existing.get("status") in {"armed", "waiting"}
                ),
                None,
            )
            if active_son:
                raise ValueError("当前已有儿子历练计划，请先取消原计划")
            for key, existing in list(payload["plans"].items()):
                if existing.get("kind") == "academy_son":
                    payload["plans"].pop(key, None)
            usage.append({
                "team_id": int(team_id),
                "armed_at": now,
                "armed_game_date": plan["armed_game_date"],
                "name": str(normalized.get("name") or ""),
            })
            payload["son_usage"][year] = usage
        elif kind == "golden_generation":
            year = str(_parse_game_date(game_date).year)
            existing = payload["plans"].get(_plan_key(team_id, kind))
            if existing and existing.get("status") in {"armed", "waiting"}:
                raise ValueError("该俱乐部已有进行中的小妖青训计划")
            usage, usage_changed = _reconcile_golden_generation_usage_year(
                payload, team_id, year,
            )
            if usage_changed:
                _save(scope_id, payload)
            used = sum(
                max(0, int(row.get("count") or 0))
                for row in usage
                if isinstance(row, dict)
                and int(row.get("team_id") or 0) == int(team_id)
            )
            if used + int(normalized["count"]) > GOLDEN_GENERATION_YEARLY_LIMIT:
                raise ValueError(
                    f"该俱乐部 {year} 年的小妖青训最多可安排 "
                    f"{GOLDEN_GENERATION_YEARLY_LIMIT} 人"
                )
            usage.append({
                "team_id": int(team_id),
                "count": int(normalized["count"]),
                "armed_at": now,
                "armed_game_date": plan["armed_game_date"],
            })
            payload["golden_generation_usage"][year] = usage
        payload["plans"][_plan_key(team_id, kind)] = plan
        _save(scope_id, payload)
    return _public_plan(plan)


def _relation_record(target_person: int, reason: int) -> bytes:
    return struct.pack("<QH", int(target_person), int(reason)) + bytes([
        RELATION_OBJECT_PERSON,
        RELATION_LIKES_PERSON,
        RELATION_LEVEL,
        RELATION_PERMANENT,
        0,
        0xFF,
    ])


def build_parent_child_relation_records(
    manager_person: int, child_person: int,
) -> tuple[bytes, bytes]:
    """Return child->manager and manager->child records for regression tests."""
    return (
        _relation_record(manager_person, RELATION_REASON_PARENT),
        _relation_record(child_person, RELATION_REASON_CHILD),
    )


def _write_relation(
    process: Any, reader: Reader, source_person: int, target_person: int, reason: int,
) -> dict[str, Any]:
    offset = reader.layout.person_relationships_offset
    relationships = reader.ptr(source_person + int(offset)) if offset is not None else 0
    header = reader.bytes(relationships, 24) if relationships else None
    if not relationships or not header or len(header) != 24:
        raise RuntimeError("无法读取人物关系容器")
    begin, end, capacity = struct.unpack("<QQQ", header)
    record = _relation_record(target_person, reason)
    if begin == end == capacity == 0:
        allocated, free_address = _remote_malloc_block(process, len(record))
        try:
            write_process_memory(process, allocated, record)
            new_header = struct.pack(
                "<QQQ", allocated, allocated + len(record),
                allocated + len(record),
            )
            write_process_memory(process, relationships, new_header)
            if (
                reader.bytes(allocated, len(record)) != record
                or reader.bytes(relationships, 24) != new_header
            ):
                raise RuntimeError("空父子关系容器首次写入校验失败")
        except Exception:
            write_process_memory(process, relationships, header)
            _remote_free_block(process, free_address, allocated)
            raise
        return {
            "mode": "reallocated", "address": allocated,
            "original": b"", "header_address": relationships,
            "original_header": header, "allocated": allocated,
            "free_address": free_address,
        }
    if (
        not begin or begin > end or end > capacity or (end - begin) % 16
        or end - begin > 16 * 512 or capacity - end > 16 * 512
    ):
        raise RuntimeError("人物关系容器结构无效")
    existing = reader.bytes(begin, end - begin) or b""
    if len(existing) != end - begin:
        raise RuntimeError("无法完整读取人物关系记录")
    for position in range(0, len(existing), 16):
        row = existing[position:position + 16]
        if (
            struct.unpack_from("<Q", row)[0] == int(target_person)
            and struct.unpack_from("<H", row, 8)[0] == int(reason)
            and row[10] == RELATION_OBJECT_PERSON
            and row[11] == RELATION_LIKES_PERSON
        ):
            address = begin + position
            write_process_memory(process, address, record)
            if reader.bytes(address, 16) != record:
                write_process_memory(process, address, row)
                raise RuntimeError("父子关系记录写入校验失败")
            return {
                "mode": "updated", "address": address,
                "original": row, "header_address": relationships,
                "allocated": 0, "free_address": 0,
            }
    if capacity - end >= 16:
        write_process_memory(process, end, record)
        write_process_memory(process, relationships + 8, struct.pack("<Q", end + 16))
        if reader.bytes(end, 16) != record or reader.ptr(relationships + 8) != end + 16:
            write_process_memory(process, relationships, header)
            raise RuntimeError("父子关系记录追加校验失败")
        return {
            "mode": "appended", "address": end,
            "original": b"", "header_address": relationships,
            "original_header": header, "allocated": 0, "free_address": 0,
        }
    allocated, free_address = _remote_malloc_block(process, len(existing) + 16)
    try:
        write_process_memory(process, allocated, existing + record)
        write_process_memory(
            process, relationships,
            struct.pack("<QQQ", allocated, allocated + len(existing) + 16, allocated + len(existing) + 16),
        )
        if reader.bytes(allocated + len(existing), 16) != record:
            raise RuntimeError("父子关系扩容写入校验失败")
    except Exception:
        write_process_memory(process, relationships, header)
        _remote_free_block(process, free_address, allocated)
        raise
    return {
        "mode": "reallocated", "address": allocated + len(existing),
        "original": b"", "header_address": relationships,
        "original_header": header, "allocated": allocated,
        "free_address": free_address,
    }


def _rollback_relation(process: Any, undo: dict[str, Any]) -> None:
    mode = undo.get("mode")
    if mode == "updated":
        write_process_memory(process, int(undo["address"]), undo["original"])
    elif mode in {"appended", "reallocated"}:
        write_process_memory(
            process, int(undo["header_address"]), undo["original_header"],
        )
    if mode == "reallocated":
        _remote_free_block(
            process, int(undo.get("free_address") or 0), int(undo.get("allocated") or 0),
        )


def _new_youth_candidates(
    plan: dict[str, Any], players: list[dict[str, Any]], *,
    require_formed_at_club: bool = False,
    exclude_movement: bool = True,
) -> list[dict[str, Any]]:
    baseline_ids = list(plan.get("baseline_player_ids") or [])
    if plan.get("global_player_baseline_complete") is True:
        baseline_ids += list(plan.get("global_player_baseline_ids") or [])
    excluded = {
        int(value) for value in (
            baseline_ids
            + list(plan.get("processed_player_ids") or [])
        )
    }
    return [
        row for row in players
        if int(row.get("id") or 0) not in excluded
        and (
            not exclude_movement
            or _movement_exclusion_reason(plan, row) is None
        )
        and (
            not require_formed_at_club
            or row.get("formed_at_club") is True
        )
    ]


def _candidate_order_key(armed_at: str, player_id: int) -> bytes:
    return hashlib.sha256(
        f"{armed_at}:{int(player_id)}".encode("utf-8")
    ).digest()


def _youth_generation_started(plan: dict[str, Any], kind: str) -> bool:
    """Return whether the generation phase has produced observable progress."""
    if plan.get("generation_completed") or plan.get("native_applied"):
        return True
    if kind != "golden_generation":
        return False
    configured = int(normalize_youth_plan_config(
        kind, plan.get("config"),
    )["count"])
    return int(plan.get("hook_remaining_count", configured)) < configured


def _movement_exclusion_reason(
    plan: dict[str, Any], row: dict[str, Any],
) -> str | None:
    player_id = int(row.get("id") or 0)
    if player_id in {
        int(value) for value in plan.get("baseline_loaned_out_ids") or []
    }:
        return "loan_return"
    movement = row.get("movement")
    if not isinstance(movement, dict):
        return None
    loan = movement.get("loan")
    if isinstance(loan, dict) and not bool(loan.get("is_loaned_out")):
        return "incoming_loan"
    return None


def _stable_random_candidates(
    candidates: list[dict[str, Any]], armed_at: str, count: int,
) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda row: _candidate_order_key(
            armed_at, int(row.get("id") or 0),
        ),
    )[:max(0, int(count))]


def _hook_ability_candidate(
    candidates: list[dict[str, Any]], target_ca: int, target_pa: int,
) -> dict[str, Any] | None:
    return next(
        (
            candidate for candidate in candidates
            if int(candidate.get("ca") or 0) == int(target_ca)
            and int(candidate.get("pa") or 0) == int(target_pa)
        ),
        None,
    )


def _candidate_has_primary_position(
    candidate: dict[str, Any], primary_position: str,
) -> bool:
    target = str(primary_position or "").strip().upper()
    if not target:
        return False
    declared = {
        str(value or "").strip().upper()
        for value in candidate.get("primary_positions") or []
        if str(value or "").strip()
    }
    if declared:
        return target in declared
    ratings = {
        str(label or "").strip().upper(): int(value or 0)
        for label, value in dict(candidate.get("position_ratings") or {}).items()
        if str(label or "").strip()
    }
    highest = max(ratings.values(), default=0)
    return highest > 0 and ratings.get(target) == highest


def _automatic_son_candidate(
    candidates: list[dict[str, Any]], *, armed_at: str, target_slot: int,
    target_ca: int, target_pa: int, primary_position: str,
    fallback_mode: str,
) -> tuple[dict[str, Any], str]:
    ordered = sorted(
        candidates,
        key=lambda candidate: _candidate_order_key(
            armed_at, int(candidate.get("id") or 0),
        ),
    )
    if not ordered:
        raise ValueError("没有可用的儿子候选")
    hook_match = _hook_ability_candidate(ordered, target_ca, target_pa)
    if hook_match is not None:
        return hook_match, "hook_ability_match"
    position_matches = [
        candidate for candidate in ordered
        if _candidate_has_primary_position(candidate, primary_position)
    ]
    if position_matches:
        return (
            position_matches[int(target_slot) % len(position_matches)],
            "primary_position_match",
        )
    return ordered[int(target_slot) % len(ordered)], str(fallback_mode)


def _generated_player_names(players: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    for player in players:
        player_id = int(player.get("id") or player.get("player_id") or 0)
        if player_id > 0 and player_id in seen:
            continue
        name = str(
            player.get("name") or player.get("player_name")
            or (f"球员 {player_id}" if player_id > 0 else "青训球员")
        ).strip()
        if name:
            names.append(name)
        if player_id > 0:
            seen.add(player_id)
    return names


def _youth_completion_message(
    kind: str, players: list[dict[str, Any]],
) -> str:
    names = _generated_player_names(players)
    if kind == "academy_son":
        return f"儿子 {names[0] if names else '青训球员'} 已生成"
    return f"小妖 {'、'.join(names) if names else '青训球员'} 已生成"


def _parse_utc_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable_youth_candidate_batch(
    scope_id: str, team_id: int, kind: str, plan: dict[str, Any],
    candidates: list[dict[str, Any]], required_count: int,
    *, observation_candidates: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]] | None, str | None, dict[str, int] | None]:
    """Persist and lock a quiet final-roster cohort before automatic writes."""
    rows_by_id = {
        int(row.get("id") or 0): row
        for row in candidates
        if int(row.get("id") or 0) > 0
    }
    candidate_ids = sorted(rows_by_id)
    observation_ids = sorted({
        int(row.get("id") or 0)
        for row in (
            candidates if observation_candidates is None else observation_candidates
        )
        if int(row.get("id") or 0) > 0
    })
    now = datetime.now(timezone.utc)
    locked_ids: list[int] = []
    with _LOCK:
        payload = _load(scope_id)
        stored = (payload.get("plans") or {}).get(_plan_key(team_id, kind))
        if (
            not stored
            or stored.get("armed_at") != plan.get("armed_at")
            or stored.get("status") not in {"armed", "waiting"}
        ):
            raise RuntimeError("青训计划已发生变化")
        locked_ids = sorted({
            int(value) for value in stored.get("locked_candidate_ids") or []
            if int(value) > 0
        })
        if stored.get("cohort_status") == "insufficient":
            # Older automatic checks could persist an undersized cohort before the
            # actual intake month. Reopen that observation so later youth can join.
            locked_ids = []
            stored.pop("locked_candidate_ids", None)
            stored.pop("cohort_locked_at", None)
            stored.pop("cohort_shortfall", None)
        if not locked_ids:
            observed_ids = sorted({
                int(value)
                for value in stored.get("candidate_observation_ids") or []
                if int(value) > 0
            })
            observed_eligible_ids = sorted({
                int(value)
                for value in stored.get("candidate_eligible_observation_ids") or []
                if int(value) > 0
            })
            if (
                observed_ids != observation_ids
                or observed_eligible_ids != candidate_ids
            ):
                stored["candidate_observation_ids"] = observation_ids
                stored["candidate_eligible_observation_ids"] = candidate_ids
                stored["candidate_observation_count"] = 1
                stored["candidate_observation_changed_at"] = now.isoformat()
                stored["cohort_status"] = "observing"
                _save(scope_id, payload)
                return None, "已发现新增青训候选，正在等待正式名单稳定", None
            observations = min(
                YOUTH_COHORT_STABLE_OBSERVATIONS,
                max(1, int(stored.get("candidate_observation_count") or 1)) + 1,
            )
            changed_at = _parse_utc_timestamp(
                stored.get("candidate_observation_changed_at")
            ) or now
            stored["candidate_observation_count"] = observations
            if not observation_ids:
                stored["cohort_status"] = "observing"
                _save(scope_id, payload)
                return None, "尚未观察到本批正式新增青训人员", None
            stable_seconds = max(0.0, (now - changed_at).total_seconds())
            if (
                observations < YOUTH_COHORT_STABLE_OBSERVATIONS
                or stable_seconds < YOUTH_COHORT_STABLE_SECONDS
            ):
                stored["cohort_status"] = "observing"
                _save(scope_id, payload)
                return None, "正式青训名单仍在生成，等待候选集合稳定", None
            if len(candidate_ids) < max(1, int(required_count)):
                shortfall = max(1, int(required_count)) - len(candidate_ids)
                # A quiet, undersized cohort is not proof that the seasonal intake
                # has finished. Keep observing and let only an explicit manual
                # verification turn this provisional shortfall into a refund.
                stored["cohort_status"] = "observed_shortfall"
                stored["cohort_observed_count"] = len(observation_ids)
                stored.pop("locked_candidate_ids", None)
                stored.pop("cohort_locked_at", None)
                stored.pop("cohort_shortfall", None)
                _save(scope_id, payload)
                return [rows_by_id[player_id] for player_id in candidate_ids], None, {
                    "eligible_count": len(candidate_ids),
                    "observed_count": len(observation_ids),
                    "shortfall": shortfall,
                }
            locked_ids = candidate_ids
            stored["locked_candidate_ids"] = locked_ids
            stored["cohort_locked_at"] = now.isoformat()
            stored["cohort_status"] = "locked"
            _save(scope_id, payload)
    missing = [player_id for player_id in locked_ids if player_id not in rows_by_id]
    if missing:
        return None, "等待已锁定的正式青训球员重新出现在俱乐部名单", None
    return [rows_by_id[player_id] for player_id in locked_ids], None, None


def _reserve_son_selection(
    scope_id: str, team_id: int, plan: dict[str, Any], player_id: int,
    selection_mode: str,
) -> None:
    with _LOCK:
        payload = _load(scope_id)
        stored = (payload.get("plans") or {}).get(
            _plan_key(team_id, "academy_son")
        )
        if not stored or stored.get("armed_at") != plan.get("armed_at"):
            raise RuntimeError("儿子历练计划已发生变化")
        existing = int(stored.get("selected_player_id") or 0)
        existing_mode = str(stored.get("selected_player_mode") or "")
        if (
            existing and existing != int(player_id)
            and existing_mode != "manual_selection"
        ):
            raise RuntimeError("儿子候选已锁定为另一名球员")
        stored["selected_player_id"] = int(player_id)
        stored["selected_player_mode"] = str(selection_mode)
        stored["selection_reserved_at"] = datetime.now(timezone.utc).isoformat()
        _save(scope_id, payload)


def _reserve_golden_ability_targets(
    scope_id: str, team_id: int, plan: dict[str, Any],
    selected: list[dict[str, Any]], current_values: dict[int, int],
    current_ca_values: dict[int, int], config: dict[str, Any],
) -> tuple[dict[int, int], dict[int, int]]:
    with _LOCK:
        payload = _load(scope_id)
        stored = (payload.get("plans") or {}).get(
            _plan_key(team_id, "golden_generation")
        )
        if (
            not stored
            or stored.get("armed_at") != plan.get("armed_at")
            or stored.get("execution_mode") not in {"hybrid", "roster_finalize"}
        ):
            raise RuntimeError("小妖青训计划已发生变化")
        targets = {
            int(key): int(value)
            for key, value in (stored.get("planned_ability_targets") or {}).items()
            if str(key).isdigit()
        }
        originals = {
            int(key): int(value)
            for key, value in (stored.get("planned_ability_originals") or {}).items()
            if str(key).isdigit()
        }
        for row in selected:
            player_id = int(row.get("id") or 0)
            minimum_target = max(
                int(config["min_pa"]), int(current_ca_values[player_id]) + 1,
            )
            if minimum_target > int(config["max_pa"]):
                raise RuntimeError(
                    f"球员 {player_id} 的 CA 已达到所选 PA 档位上限"
                )
            if player_id not in targets:
                current = int(current_values[player_id])
                targets[player_id] = (
                    current
                    if minimum_target <= current <= int(config["max_pa"])
                    else secrets.randbelow(
                        int(config["max_pa"]) - minimum_target + 1
                    ) + minimum_target
                )
            if not minimum_target <= targets[player_id] <= int(config["max_pa"]):
                raise RuntimeError("已保存的小妖 PA 目标超出计划档位")
            originals.setdefault(player_id, int(current_values[player_id]))
        stored["planned_ability_targets"] = {
            str(key): value for key, value in sorted(targets.items())
        }
        stored["planned_ability_originals"] = {
            str(key): value for key, value in sorted(originals.items())
        }
        stored["ability_targets_reserved_at"] = datetime.now(timezone.utc).isoformat()
        _save(scope_id, payload)
    return targets, originals


def youth_plan_candidates(
    scope_id: str, team_id: int, team_address: Any, kind: str,
    game_date: str | date,
) -> list[dict[str, Any]]:
    """Return verified final-roster candidates for a manual plan fallback."""
    kind = str(kind or "").strip()
    if kind != "academy_son":
        raise ValueError("只有儿子历练需要手动选择青训球员")
    with _LOCK:
        payload = _load(scope_id)
        plan = dict(
            payload.get("plans", {}).get(_plan_key(team_id, kind)) or {}
        )
    if not plan or plan.get("status") not in {"armed", "waiting"}:
        raise ValueError("当前没有可手动处理的儿子历练计划")
    current = _parse_game_date(game_date)
    pid, _path, layout = select_process_layout()
    if layout.key != plan.get("game_key") or layout.key not in SUPPORTED_GAMES:
        raise RuntimeError("儿子历练计划所属游戏版本与当前连接不一致")
    with open_process(pid) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        try:
            reader.module = module
        except AttributeError:
            pass
        players = _youth_candidate_source_players(
            reader, plan, team_id, team_address, current,
        )
    roster_candidates = _new_youth_candidates(
        plan, players,
        exclude_movement=False,
    )
    candidates = [
        row for row in roster_candidates
        if _movement_exclusion_reason(plan, row) is None
    ]
    return [
        {
            "id": int(row["id"]),
            "name": str(row.get("name") or f"球员 {int(row['id'])}"),
            "age": int(row["age"]),
            "ca": int(row.get("ca") or 0),
            "pa": int(row.get("pa") or 0),
        }
        for row in candidates
    ]


def _apply_academy_sons(
    process: Any, reader: Reader, plan: dict[str, Any], candidates: list[dict[str, Any]],
    *, manager_id: int, manager_team_id: int, manager_team_address: Any,
    manager_address: Any,
) -> list[dict[str, Any]]:
    _manager, manager_person, _team = _context_addresses(
        reader, manager_id, manager_team_id, manager_team_address, manager_address,
    )
    selected = sorted(
        candidates,
        key=lambda row: (int(row.get("pa") or 0), int(row.get("ca") or 0), -int(row["id"])),
        reverse=True,
    )[:int(plan["config"]["count"])]
    applied: list[dict[str, Any]] = []
    undo_stack: list[dict[str, Any]] = []
    try:
        for row in selected:
            player = _address(row.get("address"))
            child_person = _validated_player_person(reader, player, int(row["id"]))
            child_undo = _write_relation(
                process, reader, child_person, manager_person, RELATION_REASON_PARENT,
            )
            undo_stack.append(child_undo)
            parent_undo = _write_relation(
                process, reader, manager_person, child_person, RELATION_REASON_CHILD,
            )
            undo_stack.append(parent_undo)
            applied.append({
                "id": int(row["id"]), "name": row.get("name"),
                "ca": row.get("ca"), "pa": row.get("pa"),
            })
    except Exception:
        for undo in reversed(undo_stack):
            try:
                _rollback_relation(process, undo)
            except Exception:
                pass
        raise
    return applied


def _has_parent_relation(
    reader: Reader, child_person: int, manager_person: int,
) -> bool:
    offset = reader.layout.person_relationships_offset
    relationships = reader.ptr(child_person + offset) if offset is not None else 0
    header = reader.bytes(relationships, 24) if relationships else None
    if not header or len(header) != 24:
        return False
    begin, end, capacity = struct.unpack("<QQQ", header)
    if (
        not begin or begin > end or end > capacity
        or (end - begin) % 16 or end - begin > 16 * 4096
    ):
        return False
    return any(
        reader.ptr(address) == manager_person
        and reader.u16(address + 8) == RELATION_REASON_PARENT
        for address in range(begin, end, 16)
    )


def finalize_academy_son_attributes(
    scope_id: str, team_id: int, team_address: Any, game_date: str | date,
    *, manager_id: int, manager_team_id: int,
    manager_team_address: Any = 0, manager_address: Any = 0,
    allow_random_fallback: bool = False,
    fallback_requires_formed_at_club: bool = False,
    require_stable_cohort: bool = False,
    allow_shortfall: bool = False,
    deep_search: bool = False,
) -> dict[str, Any]:
    with _LOCK:
        payload = _load(scope_id)
        plan = dict(
            payload.get("plans", {}).get(_plan_key(team_id, "academy_son")) or {}
        )
    if not plan or plan.get("status") not in {"armed", "waiting"}:
        return {"finalized": False, "reason": "儿子历练计划当前不活动"}
    target_pa = int(plan.get("target_pa") or 0)
    config = normalize_youth_plan_config("academy_son", plan.get("config"))
    preserve_ca = bool(config["preserve_ca"])
    target_ca = None if preserve_ca else int(plan.get("target_ca") or 0)
    configured_given_name = str(config.get("name") or "")
    primary_position = str(config.get("primary_position") or "")
    secondary_position = str(config.get("secondary_position") or "")
    target_positions = None
    if primary_position:
        target_positions = bytearray(
            [ACADEMY_SON_OTHER_POSITION_RATING] * len(ACADEMY_SON_POSITIONS)
        )
        target_positions[ACADEMY_SON_POSITIONS.index(primary_position)] = (
            ACADEMY_SON_PRIMARY_POSITION_RATING
        )
        if secondary_position:
            target_positions[ACADEMY_SON_POSITIONS.index(secondary_position)] = (
                ACADEMY_SON_SECONDARY_POSITION_RATING
            )
        target_positions = bytes(target_positions)
    pa_target_valid = int(config["min_pa"]) <= target_pa <= int(config["max_pa"])
    ca_target_valid = preserve_ca or (
        int(config["min_ca"]) <= int(target_ca or 0) <= int(config["max_ca"])
        and int(target_ca or 0) < target_pa
    )
    if not pa_target_valid or not ca_target_valid:
        raise RuntimeError("儿子 CA/PA 档位结果无效")
    current = _parse_game_date(game_date)
    pid, _path, layout = select_process_layout()
    if layout.key != plan.get("game_key") or layout.key not in SUPPORTED_GAMES:
        raise RuntimeError("儿子历练计划所属游戏版本与当前连接不一致")
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        try:
            reader.module = module
        except AttributeError:
            pass
        players = _youth_candidate_source_players(
            reader, plan, team_id, team_address, current,
            deep_search=deep_search,
        )
        roster_candidates = _new_youth_candidates(
            plan, players, require_formed_at_club=False,
            exclude_movement=False,
        )
        candidates = [
            row for row in roster_candidates
            if _movement_exclusion_reason(plan, row) is None
        ]
        if allow_shortfall and not candidates and not _youth_generation_started(
            plan, "academy_son",
        ):
            return {
                "finalized": False,
                "reason": "尚未观察到本批青训生成，计划继续等待",
                "remaining": 1,
            }
        movement_excluded_count = len(roster_candidates) - len(candidates)
        fallback_candidates = (
            [row for row in candidates if row.get("formed_at_club") is True]
            if fallback_requires_formed_at_club else candidates
        )
        _manager, manager_person, _team = _context_addresses(
            reader, manager_id, manager_team_id,
            manager_team_address, manager_address,
        )
        manager_last_name_field = (
            manager_person + int(layout.person_last_name_offset)
        )
        manager_last_name_reference = int(
            reader.ptr(manager_last_name_field) or 0
        )
        manager_last_name = str(
            reader.fm_nested_string_at(manager_last_name_field) or ""
        ).strip()
        if not manager_last_name_reference or not manager_last_name:
            raise RuntimeError("玩家经理姓氏不可用，无法让儿子继承姓氏")
        sons = []
        for row in candidates:
            player = _address(row.get("address"))
            try:
                person = _validated_player_person(reader, player, int(row["id"]))
            except (RuntimeError, ValueError):
                continue
            if _has_parent_relation(reader, person, manager_person):
                sons.append((row, player, person))
        selection_mode = "native_relation"
        relation_preexisting = False
        relation_undo_stack: list[dict[str, Any]] = []
        selected_player_id = (
            0
            if str(plan.get("selected_player_mode") or "") == "manual_selection"
            else int(plan.get("selected_player_id") or 0)
        )
        if selected_player_id > 0:
            row = next(
                (
                    candidate for candidate in candidates
                    if int(candidate.get("id") or 0) == selected_player_id
                ),
                None,
            )
            if row is None:
                return {
                    "finalized": False,
                    "reason": "等待已锁定的儿子候选重新进入正式青训名单",
                }
            player = _address(row.get("address"))
            person = _validated_player_person(reader, player, int(row["id"]))
            relation_preexisting = _has_parent_relation(
                reader, person, manager_person,
            )
            selection_mode = str(
                plan.get("selected_player_mode") or "automatic_cohort"
            )
            _reserve_son_selection(
                scope_id, team_id, plan, int(row["id"]), selection_mode,
            )
            if not _has_parent_relation(reader, person, manager_person):
                try:
                    relation_undo_stack.append(_write_relation(
                        process, reader, person, manager_person,
                        RELATION_REASON_PARENT,
                    ))
                    relation_undo_stack.append(_write_relation(
                        process, reader, manager_person, person,
                        RELATION_REASON_CHILD,
                    ))
                except Exception:
                    for undo in reversed(relation_undo_stack):
                        try:
                            _rollback_relation(process, undo)
                        except Exception:
                            pass
                    raise
        elif not sons and not allow_random_fallback:
            return {"finalized": False, "reason": "等待原生儿子进入青训名单"}
        elif sons:
            row, player, person = max(
                sons, key=lambda item: int(item[0].get("id") or 0),
            )
            relation_preexisting = True
            _reserve_son_selection(
                scope_id, team_id, plan, int(row["id"]), selection_mode,
            )
        else:
            target_slot = int(plan.get("target_slot") or 0)
            if require_stable_cohort:
                stable_candidates, reason, shortfall = _stable_youth_candidate_batch(
                    scope_id, team_id, "academy_son", plan,
                    fallback_candidates, 1,
                    observation_candidates=candidates,
                )
                if stable_candidates is None:
                    return {"finalized": False, "reason": reason}
                if shortfall:
                    return {
                        "finalized": False,
                        "reason": "正式青训名单已稳定，但没有符合条件的儿子候选",
                        **shortfall,
                    }
                row, selection_mode = _automatic_son_candidate(
                    stable_candidates,
                    armed_at=str(plan.get("armed_at") or ""),
                    target_slot=target_slot,
                    target_ca=int(target_ca or -1),
                    target_pa=target_pa,
                    primary_position=primary_position,
                    fallback_mode="automatic_cohort",
                )
            else:
                ordered = sorted(
                    fallback_candidates,
                    key=lambda candidate: _candidate_order_key(
                        str(plan.get("armed_at") or ""),
                        int(candidate.get("id") or 0),
                    ),
                )
                if allow_shortfall and not ordered:
                    return {
                        "finalized": False,
                        "reason": "已按用户要求核验正式名单，但没有符合条件的儿子候选",
                        "eligible_count": 0,
                        "observed_count": len(candidates),
                        "shortfall": 1,
                    }
                if target_slot >= len(ordered) and not allow_shortfall:
                    return {
                        "finalized": False,
                        "reason": f"等待本批第 {target_slot + 1} 名青训候选进入名单",
                    }
                row, selection_mode = _automatic_son_candidate(
                    ordered,
                    armed_at=str(plan.get("armed_at") or ""),
                    target_slot=target_slot,
                    target_ca=int(target_ca or -1),
                    target_pa=target_pa,
                    primary_position=primary_position,
                    fallback_mode="random_fallback",
                )
            player = _address(row.get("address"))
            person = _validated_player_person(reader, player, int(row["id"]))
            _reserve_son_selection(
                scope_id, team_id, plan, int(row["id"]), selection_mode,
            )
            try:
                relation_undo_stack.append(_write_relation(
                    process, reader, person, manager_person,
                    RELATION_REASON_PARENT,
                ))
                relation_undo_stack.append(_write_relation(
                    process, reader, manager_person, person,
                    RELATION_REASON_CHILD,
                ))
            except Exception:
                for undo in reversed(relation_undo_stack):
                    try:
                        _rollback_relation(process, undo)
                    except Exception:
                        pass
                raise
        original_ca = int(reader.u16(player + layout.player_ca_offset) or 0)
        original_pa = int(reader.u16(player + layout.player_pa_offset) or 0)
        if preserve_ca:
            minimum_pa = max(int(config["min_pa"]), original_ca + 1)
            if minimum_pa > int(config["max_pa"]):
                raise RuntimeError("儿子原生 CA 已达到所选 PA 档位上限")
            if not minimum_pa <= target_pa <= int(config["max_pa"]):
                target_pa = _roll_academy_son_pa(
                    config, minimum=original_ca + 1,
                )
                with _LOCK:
                    latest = _load(scope_id)
                    stored = latest.get("plans", {}).get(
                        _plan_key(team_id, "academy_son")
                    )
                    if (
                        not stored
                        or stored.get("armed_at") != plan.get("armed_at")
                    ):
                        raise RuntimeError("儿子历练计划已发生变化")
                    stored["target_pa"] = target_pa
                    _save(scope_id, latest)
            target_ca = original_ca
        positions_offset = getattr(layout, "player_positions_offset", None)
        nationality_offset = getattr(layout, "person_nationality_offset", None)
        if positions_offset is None or nationality_offset is None:
            raise RuntimeError("当前游戏布局缺少儿子位置或第一国籍字段")
        positions_address = player + int(positions_offset)
        original_positions = reader.bytes(
            positions_address, len(ACADEMY_SON_POSITIONS),
        )
        if (
            not original_positions
            or len(original_positions) != len(ACADEMY_SON_POSITIONS)
            or any(value > 20 for value in original_positions)
        ):
            raise RuntimeError("儿子原始位置数据校验失败")
        nationality_address = person + int(nationality_offset)
        original_nationality = int(reader.ptr(nationality_address) or 0)
        manager_nationality, manager_nationality_id = _manager_primary_nation(
            reader, module.base_address, int(manager_id), manager_address,
        )
        name_fields = (
            person + int(layout.person_first_name_offset),
            person + int(layout.person_last_name_offset),
            person + int(layout.person_common_name_offset),
            person + int(layout.person_full_name_offset),
        )
        name_pointers = [int(reader.ptr(address) or 0) for address in name_fields]
        current_name = str(_name(reader, person) or "")
        current_given_name = str(
            reader.fm_nested_string_at(name_fields[0]) or ""
        ).strip()
        current_last_name = str(
            reader.fm_nested_string_at(name_fields[1]) or ""
        ).strip()
        target_given_name = str(
            configured_given_name
            or current_given_name
            or current_name
            or row.get("name")
            or "Youth"
        ).strip()
        target_display_name = " ".join(
            part for part in (target_given_name, manager_last_name) if part
        )
        repaired_fields = []
        if not relation_preexisting:
            repaired_fields.append("parent_relation")
        if current_given_name != target_given_name:
            repaired_fields.append("name")
        if (
            current_last_name != manager_last_name
            or name_pointers[2] != 0
        ):
            repaired_fields.append("surname")
        if not preserve_ca and original_ca != target_ca:
            repaired_fields.append("ca")
        if original_pa != target_pa:
            repaired_fields.append("pa")
        if target_positions is not None and original_positions != target_positions:
            repaired_fields.append("positions")
        if original_nationality != manager_nationality:
            repaired_fields.append("nationality")
        allocated = 0
        free_address = 0
        try:
            given_name_reference = name_pointers[0]
            if (
                configured_given_name
                or not given_name_reference
                or current_given_name != target_given_name
            ):
                allocated, free_address = _write_person_display_name(
                    process, name_fields, target_given_name.encode("utf-8"),
                )
                _invalidate_reader_after_write(reader)
                given_name_reference = int(reader.ptr(name_fields[2]) or 0)
                if (
                    not given_name_reference
                    or reader.fm_nested_string_at(name_fields[2])
                    != target_given_name
                ):
                    raise RuntimeError("儿子名字写入准备校验失败")
                write_process_memory(
                    process, name_fields[3], struct.pack("<Q", name_pointers[3]),
                )
            mismatches: dict[str, Any] = {}
            for _attempt in range(2):
                write_process_memory(
                    process, name_fields[0],
                    struct.pack("<Q", given_name_reference),
                )
                write_process_memory(
                    process, name_fields[1],
                    struct.pack("<Q", manager_last_name_reference),
                )
                write_process_memory(
                    process, name_fields[2], struct.pack("<Q", 0),
                )
                if not preserve_ca:
                    write_process_memory(
                        process, player + layout.player_ca_offset,
                        struct.pack("<H", int(target_ca or 0)),
                    )
                write_process_memory(
                    process, player + layout.player_pa_offset,
                    struct.pack("<H", target_pa),
                )
                if target_positions is not None:
                    write_process_memory(
                        process, positions_address, target_positions,
                    )
                write_process_memory(
                    process, nationality_address,
                    struct.pack("<Q", manager_nationality),
                )
                _invalidate_reader_after_write(reader)
                mismatches = _academy_son_write_mismatches(
                    reader, person=person, name_fields=name_fields,
                    player=player, layout=layout,
                    positions_address=positions_address,
                    nationality_address=nationality_address,
                    target_display_name=target_display_name,
                    target_given_name=target_given_name,
                    manager_last_name=manager_last_name,
                    target_ca=None if preserve_ca else target_ca,
                    target_pa=target_pa,
                    target_positions=target_positions,
                    manager_nationality=manager_nationality,
                )
                if not mismatches:
                    break
            if mismatches:
                fields = "、".join(sorted(mismatches))
                raise RuntimeError(f"儿子字段写入校验失败：{fields}")
            with _LOCK:
                latest = _load(scope_id)
                stored = latest.get("plans", {}).get(
                    _plan_key(team_id, "academy_son")
                )
                if (
                    not stored
                    or stored.get("armed_at") != plan.get("armed_at")
                ):
                    raise RuntimeError("儿子历练计划已发生变化")
                processed = {
                    int(value) for value in stored.get("processed_player_ids") or []
                }
                processed.add(int(row["id"]))
                stored["processed_player_ids"] = sorted(processed)
                stored["native_applied"] = True
                stored["generation_completed"] = True
                stored["remaining_count"] = 0
                stored["status"] = "completed"
                stored["completed_at"] = datetime.now(timezone.utc).isoformat()
                stored["ability_result"] = {
                    "player_id": int(row["id"]),
                    "player_name_before": row.get("name"),
                    "player_name": target_display_name,
                    "given_name": target_given_name,
                    "surname_before": current_last_name or None,
                    "surname": manager_last_name,
                    "ca_before": original_ca,
                    "ca": original_ca if preserve_ca else target_ca,
                    "pa_before": original_pa,
                    "pa": target_pa,
                    "positions_before": list(original_positions),
                    "primary_position": primary_position or None,
                    "primary_position_rating": (
                        ACADEMY_SON_PRIMARY_POSITION_RATING
                        if primary_position else None
                    ),
                    "secondary_position": secondary_position or None,
                    "secondary_position_rating": (
                        ACADEMY_SON_SECONDARY_POSITION_RATING
                        if secondary_position else None
                    ),
                    "nationality_before": original_nationality,
                    "nationality_id": manager_nationality_id,
                    "selection_mode": selection_mode,
                    "target_slot": int(plan.get("target_slot") or 0),
                }
                stored["verification_result"] = {
                    "expected_count": 1,
                    "verified_count": 1,
                    "hook_generation_observed": bool(
                        plan.get("generation_completed") or plan.get("native_applied")
                    ),
                    "native_relation_found": relation_preexisting,
                    "selection_mode": selection_mode,
                    "candidate_source": row.get("candidate_source", "roster"),
                    "manual_deep_search": deep_search,
                    "repaired_fields": repaired_fields,
                    "repaired_count": 1 if repaired_fields else 0,
                }
                if movement_excluded_count:
                    stored["verification_result"]["movement_excluded_count"] = (
                        movement_excluded_count
                    )
                stored["last_result"] = {
                    "message": _youth_completion_message(
                        "academy_son", [{
                            "id": int(row["id"]),
                            "name": target_display_name,
                        }],
                    ),
                    "applied_count": 1,
                    "repaired_count": 1 if repaired_fields else 0,
                }
                _save(scope_id, latest)
        except Exception:
            try:
                for address, pointer in zip(name_fields, name_pointers, strict=True):
                    write_process_memory(process, address, struct.pack("<Q", pointer))
                if not preserve_ca:
                    write_process_memory(
                        process, player + layout.player_ca_offset,
                        struct.pack("<H", original_ca),
                    )
                write_process_memory(
                    process, player + layout.player_pa_offset,
                    struct.pack("<H", original_pa),
                )
                write_process_memory(process, positions_address, original_positions)
                write_process_memory(
                    process, nationality_address,
                    struct.pack("<Q", original_nationality),
                )
                _invalidate_reader_after_write(reader)
            except Exception:
                pass
            if allocated:
                _remote_free_block(process, free_address, allocated)
            for undo in reversed(relation_undo_stack):
                try:
                    _rollback_relation(process, undo)
                except Exception:
                    pass
            raise
    return {
        "finalized": True,
        "player": {
            "id": int(row["id"]), "name": target_display_name,
            "given_name": target_given_name,
            "surname": manager_last_name,
            "ca": original_ca if preserve_ca else target_ca, "pa": target_pa,
            "primary_position": primary_position or None,
            "secondary_position": secondary_position or None,
            "nationality_id": manager_nationality_id,
        },
        "selection_mode": selection_mode,
    }


def finalize_golden_generation_attributes(
    scope_id: str, team_id: int, team_address: Any, game_date: str | date,
    *, excluded_player_ids: set[int] | list[int] | tuple[int, ...] = (),
    consumed_slots: int = 0,
    require_stable_cohort: bool = False,
    allow_shortfall: bool = False,
) -> dict[str, Any]:
    """Apply a quality plan to verified players in the final youth roster."""
    # Kept in the callable contract for older integrations. Academy-son players
    # are excluded from selection, but never reduce the requested golden count.
    del consumed_slots
    with _LOCK:
        payload = _load(scope_id)
        plan = dict(
            payload.get("plans", {}).get(
                _plan_key(team_id, "golden_generation")
            ) or {}
        )
    if (
        not plan
        or plan.get("status") not in {"armed", "waiting"}
        or plan.get("game_key") not in SUPPORTED_GAMES
        or plan.get("execution_mode") not in {"hybrid", "roster_finalize"}
    ):
        return {"finalized": False, "reason": "当前没有待收尾的小妖青训计划"}
    config = normalize_youth_plan_config("golden_generation", plan.get("config"))
    processed = {
        int(value) for value in plan.get("processed_player_ids") or []
        if int(value) > 0
    }
    remaining = max(0, int(config["count"]) - len(processed))
    if remaining == 0:
        return {"finalized": True, "players": [], "remaining": 0}
    target_remaining = remaining
    current = _parse_game_date(game_date)
    pid, _path, layout = select_process_layout()
    if layout.key != plan.get("game_key") or layout.key not in SUPPORTED_GAMES:
        raise RuntimeError("小妖名单收尾所属游戏版本与当前连接不一致")
    excluded = {
        int(value) for value in excluded_player_ids if int(value) > 0
    }
    with open_process(pid, write_memory=True) as process:
        module = layout.module(process)
        if not module:
            raise RuntimeError(f"{layout.module_name} 尚未加载")
        reader = Reader(process, module.base_address, layout)
        try:
            reader.module = module
        except AttributeError:
            pass
        players = _youth_candidate_source_players(
            reader, plan, team_id, team_address, current,
        )
        roster_candidates = [
            row for row in _new_youth_candidates(
                plan, players, require_formed_at_club=False,
                exclude_movement=False,
            )
            if int(row.get("id") or 0) not in excluded
        ]
        observed_candidates = [
            row for row in roster_candidates
            if _movement_exclusion_reason(plan, row) is None
        ]
        movement_excluded_count = len(roster_candidates) - len(observed_candidates)
        candidates = observed_candidates
        if allow_shortfall and not candidates and not _youth_generation_started(
            plan, "golden_generation",
        ):
            return {
                "finalized": False,
                "reason": "尚未观察到本批青训生成，计划继续等待",
                "remaining": remaining,
            }
        if not candidates and not require_stable_cohort:
            if allow_shortfall:
                return {
                    "finalized": False,
                    "reason": "已按用户要求核验正式名单，但没有符合条件的小妖候选",
                    "eligible_count": 0,
                    "observed_count": len(observed_candidates),
                    "shortfall": remaining,
                    "remaining": remaining,
                }
            return {
                "finalized": False,
                "reason": "等待正式新增青训名单，或等待儿子计划先完成",
                "remaining": remaining,
            }
        armed_at = str(plan.get("armed_at") or "")
        locked_ids = {
            int(value) for value in plan.get("locked_candidate_ids") or []
            if int(value) > 0
        }
        if locked_ids:
            locked_rows = {
                int(row.get("id") or 0): row for row in candidates
                if int(row.get("id") or 0) in locked_ids
            }
            if locked_ids - processed - set(locked_rows):
                return {
                    "finalized": False,
                    "reason": "等待已锁定的小妖候选重新进入正式青训名单",
                    "remaining": remaining,
                }
            candidates = [locked_rows[player_id] for player_id in sorted(locked_ids)]
        shortage: dict[str, int] | None = None
        if locked_ids:
            shortage_count = max(0, target_remaining - len(candidates))
            if shortage_count and plan.get("cohort_status") == "insufficient":
                shortage = {
                    "eligible_count": len(candidates),
                    "observed_count": int(plan.get("cohort_observed_count") or 0),
                    "shortfall": shortage_count,
                }
        elif require_stable_cohort:
            stable_candidates, reason, shortage = _stable_youth_candidate_batch(
                scope_id, team_id, "golden_generation", plan,
                candidates, target_remaining,
                observation_candidates=observed_candidates,
            )
            if stable_candidates is None:
                return {
                    "finalized": False,
                    "reason": reason,
                    "remaining": remaining,
                }
            candidates = stable_candidates
        elif allow_shortfall and len(candidates) < target_remaining:
            shortage = {
                "eligible_count": len(candidates),
                "observed_count": len(observed_candidates),
                "shortfall": target_remaining - len(candidates),
            }
        prepared: dict[int, tuple[dict[str, Any], int]] = {}
        current_values: dict[int, int] = {}
        current_ca_values: dict[int, int] = {}
        for row in candidates:
            player_id = int(row.get("id") or 0)
            player = _address(row.get("address"))
            _validated_player_person(reader, player, player_id)
            prepared[player_id] = (row, player)
            current_values[player_id] = int(
                reader.u16(player + layout.player_pa_offset) or 0
            )
            current_ca_values[player_id] = int(
                reader.u16(player + layout.player_ca_offset) or 0
            )
        selected = _stable_random_candidates(
            candidates, armed_at, target_remaining,
        )
        preexisting_match_count = sum(
            int(config["min_pa"]) <= current_values[int(row["id"])] <= int(config["max_pa"])
            for row in selected
        )
        targets, planned_originals = _reserve_golden_ability_targets(
            scope_id, team_id, plan, selected, current_values,
            current_ca_values, config,
        )
        repaired_count = sum(
            current_values[int(row["id"])] != targets[int(row["id"])]
            for row in selected
        )
        changed_values: list[tuple[int, int]] = []
        applied: list[dict[str, Any]] = []
        try:
            for row in selected:
                player_id = int(row["id"])
                _prepared_row, player = prepared[player_id]
                current_pa = current_values[player_id]
                original_pa = planned_originals[player_id]
                target_pa = targets[player_id]
                if current_pa not in {original_pa, target_pa}:
                    raise RuntimeError(
                        f"球员 {player_id} 的 PA 已被其他操作修改，停止自动覆盖"
                    )
                if current_pa != target_pa:
                    changed_values.append((player, current_pa))
                    write_process_memory(
                        process, player + layout.player_pa_offset,
                        struct.pack("<H", target_pa),
                    )
                    _invalidate_reader_after_write(reader)
                if reader.u16(player + layout.player_pa_offset) != target_pa:
                    raise RuntimeError(f"{layout.key.upper()} 小妖 PA 写入回读失败")
                applied.append({
                    "id": int(row["id"]),
                    "name": row.get("name"),
                    "pa_before": original_pa,
                    "pa": target_pa,
                })
            with _LOCK:
                latest = _load(scope_id)
                stored = latest.get("plans", {}).get(
                    _plan_key(team_id, "golden_generation")
                )
                if (
                    not stored
                    or stored.get("armed_at") != plan.get("armed_at")
                    or stored.get("execution_mode") not in {"hybrid", "roster_finalize"}
                ):
                    raise RuntimeError("FM26 小妖青训计划已发生变化")
                stored_processed = {
                    int(value)
                    for value in stored.get("processed_player_ids") or []
                    if int(value) > 0
                }
                stored_processed.update(int(row["id"]) for row in applied)
                stored["processed_player_ids"] = sorted(stored_processed)
                stored["processed_count"] = len(stored_processed)
                stored["remaining_count"] = max(
                    0, int(config["count"]) - len(stored_processed)
                )
                results = list(stored.get("ability_results") or [])
                results.extend(applied)
                stored["ability_results"] = results
                hook_applied_count = max(
                    0,
                    int(config["count"])
                    - int(stored.get("hook_remaining_count", config["count"])),
                )
                verification_result = {
                    "expected_count": int(config["count"]),
                    "verified_count": len(stored_processed),
                    "candidate_count": len(candidates),
                    "hook_applied_count": hook_applied_count,
                    "preexisting_match_count": preexisting_match_count,
                    "repaired_count": repaired_count,
                    "hook_assessment": (
                        "verified_on_final_roster"
                        if preexisting_match_count >= target_remaining
                        else "partial_or_mismatched"
                        if hook_applied_count > 0
                        else "not_observed"
                    ),
                }
                if movement_excluded_count:
                    verification_result["movement_excluded_count"] = (
                        movement_excluded_count
                    )
                if shortage:
                    verification_result.update({
                        "eligible_count": max(
                            0, int(shortage.get("eligible_count") or 0),
                        ),
                        "observed_count": max(
                            0, int(shortage.get("observed_count") or 0),
                        ),
                        "shortfall": max(
                            0, int(shortage.get("shortfall") or 0),
                        ),
                        "shortfall_settlement_pending": True,
                    })
                stored["verification_result"] = verification_result
                if stored["remaining_count"] == 0:
                    stored["status"] = "completed"
                    stored["completed_at"] = datetime.now(timezone.utc).isoformat()
                    stored["last_result"] = {
                        "message": _youth_completion_message(
                            "golden_generation", results,
                        ),
                        "applied_count": len(stored_processed),
                        "repaired_count": repaired_count,
                    }
                else:
                    stored["status"] = "waiting"
                    stored["last_result"] = {
                        "message": (
                            f"已写入并回读 {len(stored_processed)} 人，"
                            f"仍等待 {stored['remaining_count']} 人进入正式名单"
                        ),
                        "applied_count": len(stored_processed),
                    }
                _save(scope_id, latest)
        except Exception:
            for player, previous_pa in reversed(changed_values):
                try:
                    write_process_memory(
                        process, player + layout.player_pa_offset,
                        struct.pack("<H", previous_pa),
                    )
                    _invalidate_reader_after_write(reader)
                except Exception:
                    pass
            raise
    return {
        "finalized": len(processed) + len(applied) >= int(config["count"]),
        "players": applied,
        "remaining": max(
            0,
            int(config["count"]) - len(processed) - len(applied),
        ),
        **(shortage or {}),
    }


def finalize_fm26_golden_generation_attributes(
    scope_id: str, team_id: int, team_address: Any, game_date: str | date,
    *, excluded_player_ids: set[int] | list[int] | tuple[int, ...] = (),
    consumed_slots: int = 0,
    require_stable_cohort: bool = False,
    allow_shortfall: bool = False,
) -> dict[str, Any]:
    """Backward-compatible entry point for older callers and tests."""
    del consumed_slots
    return finalize_golden_generation_attributes(
        scope_id, team_id, team_address, game_date,
        excluded_player_ids=excluded_player_ids,
        require_stable_cohort=require_stable_cohort,
        allow_shortfall=allow_shortfall,
    )


def apply_youth_plan(
    scope_id: str, team_id: int, team_address: Any, kind: str,
    game_date: str | date, *, manager_id: int = 0, manager_team_id: int = 0,
    manager_team_address: Any = 0, manager_address: Any = 0,
) -> dict[str, Any]:
    kind = str(kind or "").strip()
    with _LOCK:
        payload = _load(scope_id)
        plan = dict(payload.get("plans", {}).get(_plan_key(team_id, kind)) or {})
    if not plan:
        raise ValueError("请先启用该青训计划")
    if plan.get("status") in {"completed", "completed_with_refund"}:
        raise ValueError("该青训计划已经完成；下一赛季请重新启用")
    if kind == "academy_son":
        if manager_id <= 0:
            raise RuntimeError("尚未识别玩家经理，无法建立父子关系")
        result = finalize_academy_son_attributes(
            scope_id, team_id, team_address, game_date,
            manager_id=manager_id,
            manager_team_id=manager_team_id,
            manager_team_address=manager_team_address,
            manager_address=manager_address,
            allow_random_fallback=True,
            fallback_requires_formed_at_club=False,
            allow_shortfall=True,
            deep_search=True,
        )
        if not result.get("finalized"):
            return {
                "status": "insufficient" if result.get("shortfall") else "waiting",
                "applied": [],
                "message": str(result.get("reason") or "尚未识别到可处理的青训球员"),
                "plan": youth_plans_for_team(scope_id, team_id).get(kind),
                **{
                    key: result[key]
                    for key in ("shortfall", "eligible_count", "observed_count", "remaining")
                    if key in result
                },
            }
        player = dict(result.get("player") or {})
        return {
            "status": "completed",
            "applied": [player] if player else [],
            "message": _youth_completion_message(
                "academy_son", [player] if player else [],
            ),
            "plan": youth_plans_for_team(scope_id, team_id).get(kind),
        }
    if kind == "golden_generation":
        son_plan = dict(
            payload.get("plans", {}).get(_plan_key(team_id, "academy_son")) or {}
        )
        excluded = {
            int(value) for value in son_plan.get("processed_player_ids") or []
            if int(value) > 0
        }
        son_result = son_plan.get("ability_result") or {}
        if int(son_result.get("player_id") or 0) > 0:
            excluded.add(int(son_result["player_id"]))
        if son_plan.get("status") in {"armed", "waiting"}:
            return {
                "status": "waiting",
                "applied": [],
                "message": "等待同俱乐部儿子计划先完成",
                "plan": youth_plans_for_team(scope_id, team_id).get(kind),
            }
        result = finalize_golden_generation_attributes(
            scope_id, team_id, team_address, game_date,
            excluded_player_ids=excluded,
            allow_shortfall=True,
        )
        players = list(result.get("players") or [])
        if not result.get("finalized") and not players:
            return {
                "status": "insufficient" if result.get("shortfall") else "waiting",
                "applied": [],
                "message": str(result.get("reason") or "尚未识别到可处理的青训球员"),
                "plan": youth_plans_for_team(scope_id, team_id).get(kind),
                **{
                    key: result[key]
                    for key in ("shortfall", "eligible_count", "observed_count", "remaining")
                    if key in result
                },
            }
        return {
            "status": (
                "insufficient" if result.get("shortfall")
                else "completed" if result.get("finalized") else "waiting"
            ),
            "applied": players,
            "message": (
                _youth_completion_message("golden_generation", players)
                if result.get("finalized")
                else f"已写入并回读 {len(players)} 名小妖，等待剩余候选"
                if players else "青训计划已经完成"
            ),
            "plan": youth_plans_for_team(scope_id, team_id).get(kind),
            **{
                key: result[key]
                for key in ("shortfall", "eligible_count", "observed_count", "remaining")
                if key in result
            },
        }
    raise ValueError("未知的青训计划")


__all__ = [
    "academy_son_price", "active_golden_generation_plans", "active_youth_generation_plans",
    "apply_youth_plan", "arm_youth_plan", "complete_golden_son_overlap_refund",
    "complete_youth_plan_shortfall",
    "build_parent_child_relation_records", "disarm_youth_plan",
    "golden_generation_son_overlap_count", "golden_generation_year_status",
    "normalize_youth_plan_config", "read_youth_roster_snapshot",
    "reopen_misclassified_youth_shortfall",
    "finalize_fm26_golden_generation_attributes",
    "record_golden_generation_progress", "record_youth_generation_progress",
    "youth_plan_candidates", "youth_plans_for_team",
]
