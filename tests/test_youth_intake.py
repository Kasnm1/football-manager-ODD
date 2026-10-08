from __future__ import annotations

import json
import struct
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import youth_intake


def test_normalize_golden_generation_config() -> None:
    assert youth_intake.normalize_youth_plan_config("golden_generation", {
        "count": 4, "min_ca": 1, "max_ca": 200, "min_pa": 170, "max_pa": 180,
    }) == {"count": 4, "min_pa": 170, "max_pa": 180}
    with pytest.raises(ValueError, match="PA 必须选择预设档位"):
        youth_intake.normalize_youth_plan_config(
            "golden_generation", {"count": 1, "min_pa": 135, "max_pa": 145},
        )
    with pytest.raises(ValueError, match="人数必须在 1 至 10"):
        youth_intake.normalize_youth_plan_config(
            "golden_generation", {"count": 11, "min_pa": 190, "max_pa": 200},
        )
    with pytest.raises(ValueError, match="固定为 1 人"):
        youth_intake.normalize_youth_plan_config("academy_son", {"count": 6})
    assert youth_intake.normalize_youth_plan_config(
        "academy_son", {"count": 1},
    ) == {
        "count": 1, "name": "", "preserve_ca": True,
        "min_pa": 110, "max_pa": 140,
        "primary_position": "", "secondary_position": "",
    }
    normalized_son = youth_intake.normalize_youth_plan_config("academy_son", {
        "count": 1, "name": "Alexander", "min_ca": 120, "max_ca": 135,
        "min_pa": 150, "max_pa": 160,
        "primary_position": "st", "secondary_position": "amc",
    })
    assert normalized_son["name"] == "Alexander"
    assert normalized_son["primary_position"] == "ST"
    assert normalized_son["secondary_position"] == "AMC"
    with pytest.raises(ValueError, match="英文字母"):
        youth_intake.normalize_youth_plan_config("academy_son", {
            "count": 1, "name": "Alex 2", "min_ca": 85, "max_ca": 105,
            "min_pa": 130, "max_pa": 140,
        })
    random_config = youth_intake.normalize_youth_plan_config("academy_son", {
        "count": 1, "name": "Alexander", "min_ca": 50, "max_ca": 150,
        "min_pa": 80, "max_pa": 200,
    })
    assert random_config["min_ca"] == 50
    assert random_config["max_pa"] == 200
    with pytest.raises(ValueError, match="不能相同"):
        youth_intake.normalize_youth_plan_config("academy_son", {
            "count": 1, "name": "Alexander", "min_ca": 85, "max_ca": 105,
            "min_pa": 130, "max_pa": 140,
            "primary_position": "ST", "secondary_position": "ST",
        })


@pytest.mark.parametrize(("count", "min_pa", "max_pa", "price"), [
    (1, 110, 140, 5_000_000),
    (3, 140, 170, 450_000_000),
    (10, 170, 200, 50_000_000_000),
])
def test_golden_generation_price_uses_player_count_and_pa_band(
    count: int, min_pa: int, max_pa: int, price: int,
) -> None:
    assert youth_intake.golden_generation_price({
        "count": count, "min_pa": min_pa, "max_pa": max_pa,
    }) == price


@pytest.mark.parametrize(("config", "price"), [
    ({"name": "Alex", "primary_position": "ST", "secondary_position": "AMC",
      "min_ca": 85, "max_ca": 105, "min_pa": 110, "max_pa": 140}, 20_000_000),
    ({"name": "Alexander", "min_ca": 135, "max_ca": 150,
      "min_pa": 170, "max_pa": 200,
      "primary_position": "DC", "secondary_position": "DM"}, 101_000_000_000),
    ({"name": "Alex", "min_ca": 50, "max_ca": 150,
      "min_pa": 80, "max_pa": 200,
      "primary_position": "GK", "secondary_position": "SW"}, 510_000_000),
    ({"name": "", "min_ca": 85, "max_ca": 105,
      "min_pa": 110, "max_pa": 140,
      "primary_position": "ST", "secondary_position": ""}, 20_000_000),
])
def test_academy_son_price_combines_ca_and_pa_bands(
    config: dict[str, object], price: int,
) -> None:
    assert youth_intake.academy_son_price(config) == price


def test_academy_son_purchase_requires_primary_position() -> None:
    with pytest.raises(ValueError, match="主位置"):
        youth_intake.academy_son_price({
            "name": "", "min_ca": 85, "max_ca": 105,
            "min_pa": 110, "max_pa": 140,
        })


def test_new_academy_son_price_only_uses_pa_band() -> None:
    assert youth_intake.academy_son_price({
        "name": "Alex", "primary_position": "ST", "preserve_ca": True,
        "min_pa": 160, "max_pa": 190,
    }) == 3_000_000_000
    assert youth_intake.academy_son_price({
        "name": "Alex", "primary_position": "ST", "preserve_ca": True,
        "min_pa": 120, "max_pa": 200,
    }) == 500_000_000


def test_random_son_ability_bands_bias_low_and_keep_pa_above_ca(monkeypatch) -> None:
    random_values = iter((100, 0, 1, 0, 0))
    monkeypatch.setattr(
        youth_intake.secrets, "randbelow", lambda _limit: next(random_values),
    )

    ca, pa = youth_intake._roll_academy_son_abilities({
        "min_ca": 50, "max_ca": 150, "min_pa": 80, "max_pa": 200,
    })

    assert (ca, pa) == (50, 80)
    assert pa > ca


def test_random_son_pa_200_uses_one_percent_draw(monkeypatch) -> None:
    limits = []

    def jackpot(limit: int) -> int:
        limits.append(limit)
        return 0

    monkeypatch.setattr(youth_intake.secrets, "randbelow", jackpot)

    assert youth_intake._roll_low_biased_value(
        80, 200, one_percent_maximum=True,
    ) == 200
    assert limits == [100]


def test_overlapping_son_bands_still_generate_strictly_higher_pa(monkeypatch) -> None:
    random_values = iter((100, 100, 1, 0, 0))
    monkeypatch.setattr(
        youth_intake.secrets, "randbelow", lambda _limit: next(random_values),
    )

    ca, pa = youth_intake._roll_academy_son_abilities({
        "min_ca": 50, "max_ca": 150, "min_pa": 80, "max_pa": 200,
    })

    assert (ca, pa) == (150, 151)


def test_parent_child_records_use_permanent_reciprocal_reasons() -> None:
    child_to_manager, manager_to_child = youth_intake.build_parent_child_relation_records(
        0x1122334455667788, 0x8877665544332211,
    )
    assert len(child_to_manager) == len(manager_to_child) == 16
    assert int.from_bytes(child_to_manager[:8], "little") == 0x1122334455667788
    assert int.from_bytes(manager_to_child[:8], "little") == 0x8877665544332211
    assert child_to_manager[8:] == bytes([1, 0, 3, 1, 100, 79, 0, 0xFF])
    assert manager_to_child[8:] == bytes([3, 0, 3, 1, 100, 79, 0, 0xFF])
    assert youth_intake._relation_record(1, 6)[8:] == bytes([
        6, 0, 3, 1, 100, 79, 0, 0xFF,
    ])


def test_write_relation_initializes_and_rolls_back_empty_vector(
    monkeypatch,
) -> None:
    memory = {
        0x5000: b"\0" * 24,
        0x6000: b"\0" * 16,
    }
    freed = []

    class FakeReader:
        layout = SimpleNamespace(person_relationships_offset=0x20)

        @staticmethod
        def ptr(address):
            return 0x5000 if address == 0x3020 else 0

        @staticmethod
        def bytes(address, size):
            raw = memory.get(address)
            return raw[:size] if raw is not None else None

    monkeypatch.setattr(
        youth_intake, "_remote_malloc_block",
        lambda _process, size: (0x6000, 0x7000)
        if size == 16 else pytest.fail("unexpected allocation"),
    )
    monkeypatch.setattr(
        youth_intake, "_remote_free_block",
        lambda _process, free_address, allocated: freed.append(
            (free_address, allocated),
        ),
    )
    monkeypatch.setattr(
        youth_intake, "write_process_memory",
        lambda _process, address, data: memory.__setitem__(address, bytes(data)),
    )

    undo = youth_intake._write_relation(
        object(), FakeReader(), 0x3000, 0x4000,
        youth_intake.RELATION_REASON_CHILD,
    )

    assert undo["mode"] == "reallocated"
    assert memory[0x5000] == struct.pack("<QQQ", 0x6000, 0x6010, 0x6010)
    assert memory[0x6000] == youth_intake._relation_record(
        0x4000, youth_intake.RELATION_REASON_CHILD,
    )

    youth_intake._rollback_relation(object(), undo)

    assert memory[0x5000] == b"\0" * 24
    assert freed == [(0x7000, 0x6000)]


def test_new_youth_candidates_use_global_baseline_without_age_gate() -> None:
    plan = {
        "baseline_player_ids": [1],
        "processed_player_ids": [2],
        "global_player_baseline_ids": [6],
        "global_player_baseline_complete": True,
    }
    players = [
        {"id": 1, "age": 16, "formed_at_club": True},
        {"id": 2, "age": 16, "formed_at_club": True},
        {"id": 3, "age": 16, "formed_at_club": True},
        {"id": 4, "age": 15, "formed_at_club": True},
        {"id": 5, "age": None, "formed_at_club": True},
        {"id": 6, "age": 16, "formed_at_club": False},
        {"id": 7, "age": 17, "formed_at_club": True},
        {"id": 8, "age": 16, "formed_at_club": False},
    ]
    assert [
        row["id"] for row in youth_intake._new_youth_candidates(plan, players)
    ] == [3, 4, 5, 7, 8]
    assert [
        row["id"] for row in youth_intake._new_youth_candidates(
            plan, players, require_formed_at_club=True,
        )
    ] == [3, 4, 5, 7]
    # Historical plans without a complete global baseline still fall back to
    # the club roster baseline and already processed IDs.
    incomplete_plan = dict(plan, global_player_baseline_complete=False)
    assert [
        row["id"] for row in youth_intake._new_youth_candidates(
            incomplete_plan, players,
        )
    ] == [3, 4, 5, 6, 7, 8]


def test_manual_shortfall_requires_generation_progress() -> None:
    assert not youth_intake._youth_generation_started(
        {"kind": "academy_son", "native_applied": False}, "academy_son",
    )
    assert not youth_intake._youth_generation_started(
        {
            "kind": "golden_generation",
            "config": {"count": 3},
            "hook_remaining_count": 3,
        },
        "golden_generation",
    )
    assert youth_intake._youth_generation_started(
        {
            "kind": "golden_generation",
            "config": {"count": 3},
            "hook_remaining_count": 2,
        },
        "golden_generation",
    )


def test_youth_candidates_exclude_movement_arrivals_and_loan_returns() -> None:
    plan = {
        "baseline_player_ids": [],
        "baseline_loaned_out_ids": [4],
        "global_player_baseline_ids": [2],
        "global_player_baseline_complete": True,
        "processed_player_ids": [],
    }
    players = [
        {"id": 1, "movement": {"contract_type": 3}},
        {"id": 2, "movement": {"contract_type": 1}},
        {"id": 3, "movement": {
            "contract_type": 3,
            "loan": {"is_loaned_out": False},
        }},
        {"id": 4, "movement": {"contract_type": 3}},
        {"id": 5},
        {"id": 6, "movement": {"contract_type": 1}},
        {"id": 7, "movement": {"contract_type": 2}},
        {"id": 8, "movement": {"contract_type": 4}},
        {"id": 9, "movement": {"contract_type": 5}},
        {"id": 10, "movement": {"contract_type": 15}},
        {"id": 11, "movement": {"read_error": "合同指针为空"}},
    ]

    assert [
        row["id"] for row in youth_intake._new_youth_candidates(plan, players)
    ] == [1, 5, 6, 7, 8, 9, 10, 11]
    assert youth_intake._movement_exclusion_reason(plan, players[-1]) is None


@pytest.mark.parametrize(
    ("game_key", "contract_offset", "contract_size", "type_offset", "loan_offset"),
    [
        ("fm24", 0xC8, 0xC0, 0xB3, 0xD0),
        ("fm26", 0xA8, 0xC8, 0xC3, 0xB0),
    ],
)
def test_movement_snapshot_reads_versioned_contract_and_incoming_loan(
    game_key: str, contract_offset: int, contract_size: int,
    type_offset: int, loan_offset: int,
) -> None:
    person = 0x2000
    contract = 0x3000
    loan_container = 0x4000
    loan_contract = 0x5000
    target_team = 0x6000
    module_address = 0x100000
    loan_vtable_rva = 0x700
    raw_contract = bytearray(contract_size)
    struct.pack_into("<Q", raw_contract, 0x08, person)
    struct.pack_into("<Q", raw_contract, 0x10, target_team)
    raw_contract[type_offset] = youth_intake.YOUTH_CONTRACT_TYPE
    pointers = {
        person + contract_offset: contract,
        person + loan_offset: loan_container,
        loan_container: loan_contract,
        loan_contract: module_address + loan_vtable_rva,
        loan_contract + 0x08: person,
        loan_contract + 0x10: target_team,
    }

    class FakeReader:
        layout = SimpleNamespace(
            key=game_key, loan_contract_vtable_rva=loan_vtable_rva,
        )
        module_base = module_address

        @staticmethod
        def ptr(address):
            return pointers.get(address, 0)

        @staticmethod
        def bytes(address, size):
            if address == contract and size == contract_size:
                return bytes(raw_contract)
            return None

        @staticmethod
        def u32(_address):
            return 0

    snapshot = youth_intake._movement_snapshot(
        FakeReader(), person, target_team,
    )

    assert snapshot["contract_type"] == youth_intake.YOUTH_CONTRACT_TYPE
    assert snapshot["loan"] == {
        "team_address": hex(target_team),
        "is_loaned_out": False,
    }
    assert snapshot["is_loaned_out"] is False


def test_academy_son_prefers_candidate_with_hook_target_abilities() -> None:
    ordered = [
        {"id": 2002092233, "ca": 92, "pa": 158},
        {"id": 2002092234, "ca": 115, "pa": 165},
        {"id": 2002092235, "ca": 115, "pa": 170},
    ]

    assert youth_intake._hook_ability_candidate(ordered, 115, 165) == ordered[1]
    assert youth_intake._hook_ability_candidate(ordered, 120, 180) is None


def test_automatic_son_candidate_prefers_hook_then_primary_position() -> None:
    candidates = [
        {
            "id": 101, "ca": 80, "pa": 120,
            "primary_positions": ["GK"],
        },
        {
            "id": 102, "ca": 90, "pa": 130,
            "primary_positions": ["ST"],
        },
        {
            "id": 103, "ca": 147, "pa": 190,
            "primary_positions": ["MC"],
        },
    ]

    hook_player, hook_mode = youth_intake._automatic_son_candidate(
        candidates, armed_at="2028-12-01T00:00:00+00:00", target_slot=0,
        target_ca=147, target_pa=190, primary_position="ST",
        fallback_mode="automatic_cohort",
    )
    position_player, position_mode = youth_intake._automatic_son_candidate(
        candidates, armed_at="2028-12-01T00:00:00+00:00", target_slot=0,
        target_ca=150, target_pa=195, primary_position="ST",
        fallback_mode="automatic_cohort",
    )

    assert (hook_player["id"], hook_mode) == (103, "hook_ability_match")
    assert (position_player["id"], position_mode) == (
        102, "primary_position_match",
    )


def test_automatic_son_position_match_falls_back_to_highest_rating() -> None:
    candidate = {
        "id": 102, "ca": 90, "pa": 130,
        "position_ratings": {"AMR": 12, "ST": 18},
    }

    assert youth_intake._candidate_has_primary_position(candidate, "ST") is True
    assert youth_intake._candidate_has_primary_position(candidate, "AMR") is False


def test_golden_candidates_use_stable_random_order_not_existing_pa() -> None:
    candidates = [
        {"id": 101, "pa": 185},
        {"id": 102, "pa": 120},
        {"id": 103, "pa": 130},
    ]
    armed_at = "2028-12-01T00:00:00+00:00"
    expected = min(
        candidates,
        key=lambda row: youth_intake._candidate_order_key(armed_at, row["id"]),
    )

    first = youth_intake._stable_random_candidates(candidates, armed_at, 1)
    retried = youth_intake._stable_random_candidates(
        list(reversed(candidates)), armed_at, 1,
    )

    assert first == [expected]
    assert retried == [expected]


def test_schema_upgrade_clears_unsafe_active_candidate_locks(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": 9,
        "plans": {
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation",
                "status": "waiting", "game_key": "fm26",
                "execution_mode": "hybrid",
                "config": {"count": 1, "min_pa": 170, "max_pa": 180},
                "locked_candidate_ids": [101],
                "candidate_observation_ids": [101],
                "candidate_eligible_observation_ids": [101],
                "cohort_status": "locked",
            },
        },
    }), encoding="utf-8")

    plan = youth_intake._load("scope")["plans"]["42:golden_generation"]

    assert plan["cohort_status"] == "waiting"
    assert "locked_candidate_ids" not in plan
    assert "candidate_observation_ids" not in plan
    assert "candidate_eligible_observation_ids" not in plan


def test_load_keeps_a_hook_completed_golden_plan_waiting_for_roster(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": youth_intake.SCHEMA_VERSION,
        "plans": {
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation",
                "status": "waiting", "game_key": "fm26",
                "execution_mode": "hybrid", "hook_remaining_count": 0,
                "remaining_count": 3,
                "config": {"count": 3, "min_pa": 170, "max_pa": 180},
            },
        },
        "son_usage": {}, "golden_generation_usage": {},
    }), encoding="utf-8")

    plan = youth_intake.youth_plans_for_team(
        "scope", 42,
    )["golden_generation"]

    assert plan["status"] == "waiting"
    assert plan["generation_completed"] is True
    assert plan["remaining_count"] == 3
    assert youth_intake.active_golden_generation_plans("scope")[0]["count"] == 0


def test_load_reopens_a_legacy_hook_only_completion_for_roster_and_mail(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": 10,
        "plans": {
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation",
                "status": "completed", "game_key": "fm26",
                "execution_mode": "hybrid", "hook_remaining_count": 0,
                "remaining_count": 0,
                "completed_at": "2028-12-02T00:00:00+00:00",
                "config": {"count": 3, "min_pa": 170, "max_pa": 180},
                "verification_result": {
                    "expected_count": 3, "verified_count": 3,
                    "hook_applied_count": 3,
                    "hook_assessment": "generation_hook_completed",
                },
            },
        },
        "son_usage": {}, "golden_generation_usage": {},
    }), encoding="utf-8")

    plan = youth_intake.youth_plans_for_team(
        "scope", 42,
    )["golden_generation"]

    assert plan["status"] == "waiting"
    assert plan["generation_completed"] is True
    assert plan["remaining_count"] == 3
    assert "completed_at" not in plan
    assert youth_intake.active_golden_generation_plans("scope")[0]["count"] == 0


def test_load_reopens_an_active_plan_whose_son_consumed_a_golden_slot(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": 11,
        "plans": {
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation",
                "status": "waiting", "game_key": "fm26",
                "execution_mode": "hybrid", "hook_remaining_count": 0,
                "remaining_count": 1, "processed_player_ids": [102, 103],
                "locked_candidate_ids": [102, 103],
                "cohort_status": "locked",
                "config": {"count": 3, "min_pa": 170, "max_pa": 180},
                "verification_result": {
                    "expected_count": 3, "verified_count": 2,
                    "consumed_by_academy_son": 1, "shortfall": 1,
                },
            },
        },
        "son_usage": {}, "golden_generation_usage": {},
    }), encoding="utf-8")

    plan = youth_intake.youth_plans_for_team(
        "scope", 42,
    )["golden_generation"]

    assert plan["status"] == "waiting"
    assert plan["remaining_count"] == 1
    assert plan["cohort_status"] == "waiting"
    assert "locked_candidate_ids" not in plan
    assert "consumed_by_academy_son" not in plan["verification_result"]
    assert "shortfall" not in plan["verification_result"]


def test_youth_completion_messages_use_actual_player_names() -> None:
    assert youth_intake._youth_completion_message(
        "academy_son", [{"id": 101, "name": "Akira Morita"}],
    ) == "儿子 Akira Morita 已生成"
    assert youth_intake._youth_completion_message(
        "golden_generation", [
            {"id": 102, "name": "Kazu Oishi"},
            {"id": 103, "name": "Yuki Nishido"},
        ],
    ) == "小妖 Kazu Oishi、Yuki Nishido 已生成"


def test_automatic_son_selection_replaces_legacy_manual_reservation(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    payload = youth_intake._empty_payload()
    payload["plans"]["42:academy_son"] = {
        "team_id": 42, "kind": "academy_son", "status": "waiting",
        "armed_at": "plan-1", "selected_player_id": 99,
        "selected_player_mode": "manual_selection",
    }
    youth_intake._save("scope", payload)
    plan = youth_intake._load("scope")["plans"]["42:academy_son"]

    youth_intake._reserve_son_selection(
        "scope", 42, plan, 101, "primary_position_match",
    )

    stored = youth_intake._load("scope")["plans"]["42:academy_son"]
    assert stored["selected_player_id"] == 101
    assert stored["selected_player_mode"] == "primary_position_match"


def test_automatic_youth_cohort_locks_after_stable_observations(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "YOUTH_COHORT_STABLE_OBSERVATIONS", 2)
    monkeypatch.setattr(youth_intake, "YOUTH_COHORT_STABLE_SECONDS", 0.0)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 2, "min_pa": 170, "max_pa": 180}, "2028-12-01",
    )
    plan = youth_intake._load("scope")["plans"]["42:golden_generation"]
    candidates = [
        {"id": 102, "age": 16, "formed_at_club": True},
        {"id": 101, "age": 16, "formed_at_club": True},
    ]

    first, first_reason, first_shortfall = youth_intake._stable_youth_candidate_batch(
        "scope", 42, "golden_generation", plan, candidates, 2,
    )
    second, second_reason, second_shortfall = youth_intake._stable_youth_candidate_batch(
        "scope", 42, "golden_generation", plan, candidates, 2,
    )

    assert first is None
    assert first_shortfall is None
    assert "等待正式名单稳定" in str(first_reason)
    assert second_reason is None
    assert second_shortfall is None
    assert [row["id"] for row in second or []] == [101, 102]
    stored = youth_intake._load("scope")["plans"]["42:golden_generation"]
    assert stored["locked_candidate_ids"] == [101, 102]
    assert stored["cohort_status"] == "locked"
    public = youth_intake.youth_plans_for_team("scope", 42)["golden_generation"]
    assert "locked_candidate_ids" not in public
    assert public["candidate_count"] == 2


def test_stable_youth_cohort_reports_shortfall_only_after_a_real_batch(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "YOUTH_COHORT_STABLE_OBSERVATIONS", 2)
    monkeypatch.setattr(youth_intake, "YOUTH_COHORT_STABLE_SECONDS", 0.0)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 2, "min_pa": 170, "max_pa": 180}, "2028-12-01",
        payment={"bank": 60, "wallet": 40, "total": 100, "transaction_id": "tx-1"},
    )
    plan = youth_intake._load("scope")["plans"]["42:golden_generation"]
    eligible = [{"id": 101, "age": 16, "formed_at_club": True}]
    observed = eligible + [{"id": 102, "age": 16, "formed_at_club": False}]

    first, reason, shortfall = youth_intake._stable_youth_candidate_batch(
        "scope", 42, "golden_generation", plan, eligible, 2,
        observation_candidates=observed,
    )
    second, second_reason, second_shortfall = youth_intake._stable_youth_candidate_batch(
        "scope", 42, "golden_generation", plan, eligible, 2,
        observation_candidates=observed,
    )

    assert first is None
    assert "等待正式名单稳定" in str(reason)
    assert shortfall is None
    assert [row["id"] for row in second or []] == [101]
    assert second_reason is None
    assert second_shortfall == {
        "eligible_count": 1, "observed_count": 2, "shortfall": 1,
    }
    stored = youth_intake._load("scope")["plans"]["42:golden_generation"]
    assert stored["cohort_status"] == "observed_shortfall"
    assert "locked_candidate_ids" not in stored

    expanded = eligible + [{"id": 103, "age": 15, "formed_at_club": True}]
    expanded_observed = observed + [expanded[-1]]
    third, third_reason, third_shortfall = youth_intake._stable_youth_candidate_batch(
        "scope", 42, "golden_generation", plan, expanded, 2,
        observation_candidates=expanded_observed,
    )
    fourth, fourth_reason, fourth_shortfall = youth_intake._stable_youth_candidate_batch(
        "scope", 42, "golden_generation", plan, expanded, 2,
        observation_candidates=expanded_observed,
    )
    assert third is None
    assert "等待正式名单稳定" in str(third_reason)
    assert third_shortfall is None
    assert [row["id"] for row in fourth or []] == [101, 103]
    assert fourth_reason is None
    assert fourth_shortfall is None

    settled = youth_intake.complete_youth_plan_shortfall(
        "scope", 42, "golden_generation", shortfall=1,
        eligible_count=1, observed_count=2,
        refund={"bank": 30, "wallet": 20, "total": 50, "transaction_id": "refund-1"},
    )
    assert settled["status"] == "completed_with_refund"
    assert settled["remaining_count"] == 0
    assert settled["hook_remaining_count"] == 0
    assert settled["verification_result"]["refunded_count"] == 1
    assert settled["payment"]["transaction_id"] == "tx-1"


def test_empty_observation_never_becomes_automatic_shortfall(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "YOUTH_COHORT_STABLE_OBSERVATIONS", 2)
    monkeypatch.setattr(youth_intake, "YOUTH_COHORT_STABLE_SECONDS", 0.0)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 2, "min_pa": 170, "max_pa": 180}, "2028-12-01",
    )
    plan = youth_intake._load("scope")["plans"]["42:golden_generation"]

    for _ in range(3):
        rows, reason, shortfall = youth_intake._stable_youth_candidate_batch(
            "scope", 42, "golden_generation", plan, [], 2,
            observation_candidates=[],
        )
        assert rows is None
        assert "尚未观察到" in str(reason)
        assert shortfall is None
    stored = youth_intake._load("scope")["plans"]["42:golden_generation"]
    assert stored["cohort_status"] == "observing"
    assert int(stored.get("cohort_shortfall") or 0) == 0


def test_manual_son_candidates_do_not_filter_new_players_by_age(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake.secrets, "randbelow", lambda _limit: 0)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [{"id": 1}],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "academy_son", {
            "count": 1, "name": "Alexander", "min_ca": 85, "max_ca": 105,
            "min_pa": 130, "max_pa": 140,
            "primary_position": "ST", "secondary_position": "AMC",
        }, "2028-12-01",
    )
    layout = SimpleNamespace(
        key="fm26", module_name="game_plugin.dll",
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    monkeypatch.setattr(
        youth_intake, "select_process_layout", lambda: (1, "fm.exe", layout),
    )
    monkeypatch.setattr(
        youth_intake, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(pid=1)),
    )
    monkeypatch.setattr(youth_intake, "Reader", lambda *_args: object())
    monkeypatch.setattr(youth_intake, "_roster_snapshot", lambda *_args: [
        {"id": 1, "name": "Baseline", "age": 16, "formed_at_club": True},
        {"id": 2, "name": "Age 15", "age": 15, "formed_at_club": True,
         "ca": 72, "pa": 128},
        {"id": 3, "name": "No formed relation", "age": 16, "formed_at_club": False,
         "ca": 68, "pa": 122},
        {"id": 4, "name": "Adult", "age": 19, "formed_at_club": True,
         "movement": {"contract_type": 1}},
    ])

    assert youth_intake.youth_plan_candidates(
        "scope", 42, "0x1000", "academy_son", "2028-12-02",
    ) == [
        {"id": 2, "name": "Age 15", "age": 15, "ca": 72, "pa": 128},
        {"id": 3, "name": "No formed relation", "age": 16, "ca": 68, "pa": 122},
        {"id": 4, "name": "Adult", "age": 19, "ca": 0, "pa": 0},
    ]


def test_refunded_plan_with_rejected_observed_youth_can_reopen_for_roster_only(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [{"id": 1}],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 3, "min_pa": 170, "max_pa": 180}, "2028-12-01",
    )
    payload = youth_intake._load("scope")
    plan = payload["plans"]["42:golden_generation"]
    plan.update({
        "status": "completed_with_refund",
        "remaining_count": 0,
        "completed_at": "2028-12-02T00:00:00+00:00",
        "verification_result": {
            "expected_count": 3, "verified_count": 0,
            "observed_count": 3, "eligible_count": 0, "shortfall": 3,
        },
        "shortfall_result": {"missing_count": 3},
        "refund_result": {"total": 150},
        "candidate_observation_ids": [2002092233, 2002092234, 2002092235],
        "candidate_eligible_observation_ids": [],
        "locked_candidate_ids": [],
        "cohort_status": "insufficient",
        "cohort_shortfall": 3,
    })
    youth_intake._save("scope", payload)

    public_before = youth_intake.youth_plans_for_team("scope", 42)["golden_generation"]
    reopened = youth_intake.reopen_misclassified_youth_shortfall(
        "scope", 42, "golden_generation",
    )

    assert public_before["shortfall_recovery_available"] is True
    assert reopened["status"] == "waiting"
    assert reopened["remaining_count"] == 3
    assert reopened["roster_recovery_only"] is True
    assert reopened["prior_refund_result"]["total"] == 150
    raw = youth_intake._load("scope")["plans"]["42:golden_generation"]
    assert raw["baseline_player_ids"] == [1]
    assert "refund_result" not in raw
    assert youth_intake.active_youth_generation_plans("scope") == []


def test_refunded_real_shortfall_without_rejected_observed_youth_cannot_reopen(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 2, "min_pa": 170, "max_pa": 180}, "2028-12-01",
    )
    payload = youth_intake._load("scope")
    plan = payload["plans"]["42:golden_generation"]
    plan.update({
        "status": "completed_with_refund",
        "verification_result": {
            "observed_count": 1, "eligible_count": 1, "shortfall": 1,
        },
    })
    youth_intake._save("scope", payload)

    with pytest.raises(ValueError, match="没有旧候选门槛误判证据"):
        youth_intake.reopen_misclassified_youth_shortfall(
            "scope", 42, "golden_generation",
        )


def test_reopen_world_club_youth_plan_syncs_without_rearming_hook(
    monkeypatch,
) -> None:
    import fm_odds_web

    synced = []
    recovered = {
        "status": "waiting", "roster_recovery_only": True,
        "remaining_count": 3,
    }
    fake_state = SimpleNamespace(
        memory_lock=nullcontext(),
        _data_scope_id=lambda _output: "scope",
        _owned_world_club_target=lambda _team_id, **_kwargs: (
            "scope", {"save_instance_id": "save-1"},
            {"id": 42, "address": "0x1000"},
        ),
        _sync_youth_generation_plans=lambda scope, save_id: (
            synced.append((scope, save_id))
            or {"installed": False, "targets": [], "son_target": None}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "reopen_misclassified_youth_shortfall",
        lambda *_args: dict(recovered),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"golden_generation": dict(recovered)},
    )

    result = fm_odds_web.LocalOddsState.reopen_world_club_youth_plan(
        fake_state, {"team_id": 42, "kind": "golden_generation"},
    )

    assert result["reopened"] is True
    assert result["generation_hook"]["installed"] is False
    assert synced == [("scope", "save-1")]


def test_arm_plan_persists_version_and_baseline(monkeypatch, tmp_path) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm24",
        "players": [{"id": 30}, {"id": 10}, {"id": 30}],
        "global_player_ids": [99, 30, 10, 99],
        "global_player_baseline_complete": True,
    })

    plan = youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "academy_son", {"count": 1}, "2028-12-01",
    )

    assert plan["game_key"] == "fm24"
    assert plan["baseline_count"] == 2
    assert plan["status"] == "armed"
    saved = youth_intake._load("scope")
    raw = saved["plans"]["42:academy_son"]
    assert raw["baseline_player_ids"] == [10, 30]
    assert raw["global_player_baseline_ids"] == [10, 30, 99]
    assert raw["global_player_baseline_complete"] is True
    assert "global_player_baseline_ids" not in plan
    assert raw["config"] == {
        "count": 1, "name": "", "preserve_ca": True,
        "min_pa": 110, "max_pa": 140,
        "primary_position": "", "secondary_position": "",
    }
    assert "target_ca" not in raw
    assert 110 <= raw["target_pa"] <= 140
    assert 0 <= raw["target_slot"] <= 3


def test_roster_snapshot_attaches_module_before_building_database_index(
    monkeypatch,
) -> None:
    module = SimpleNamespace(base_address=0x100000, size=0x200000, path="fm.exe")
    layout = SimpleNamespace(
        key="fm26", module_name="game_plugin.dll",
        module=lambda _process: module,
    )
    process = SimpleNamespace()
    captured = {}

    class FakeReader:
        def __init__(self, opened_process, module_base, selected_layout):
            self.process = opened_process
            self.module_base = module_base
            self.layout = selected_layout

    def build_directory(reader):
        captured["module"] = reader.module
        return SimpleNamespace(person_uids_snapshot=lambda: (101, 102))

    monkeypatch.setattr(
        youth_intake, "select_process_layout",
        lambda: (26, "fm.exe", layout),
    )
    monkeypatch.setattr(
        youth_intake, "open_process", lambda *_args, **_kwargs: nullcontext(process),
    )
    monkeypatch.setattr(youth_intake, "Reader", FakeReader)
    monkeypatch.setattr(youth_intake, "database_index_for_reader", lambda _reader: None)
    monkeypatch.setattr(youth_intake, "DatabaseIndex", build_directory)
    monkeypatch.setattr(youth_intake, "_roster_snapshot", lambda *_args: [{"id": 7}])

    snapshot = youth_intake.read_youth_roster_snapshot(
        42, "0x1000", "2028-12-01",
    )

    assert captured["module"] is module
    assert snapshot == {
        "game_key": "fm26",
        "players": [{"id": 7}],
        "global_player_ids": [101, 102],
        "global_player_baseline_complete": True,
    }


def test_roster_snapshot_relocates_team_by_uid_before_reading_roster(monkeypatch) -> None:
    calls = []
    roster_addresses = []
    reader = SimpleNamespace(
        layout=SimpleNamespace(team_vtable_rva=0x200),
        module_base=0x100000,
        ptr=lambda _address: 0,
        bytes=lambda *_args: None,
        roster=lambda address: roster_addresses.append(address) or [],
    )

    def resolve(_reader, team_address, team_id):
        calls.append((team_address, team_id))
        return SimpleNamespace(team_address=0x5000, club_address=0x6000)

    monkeypatch.setattr(youth_intake, "resolve_team_club", resolve)

    assert youth_intake._roster_snapshot(
        reader, 42, hex(0x1000), youth_intake.date(2028, 12, 1),
    ) == []
    assert calls == [(0x1000, 42)]
    assert roster_addresses == [0x5000]


def test_primary_contract_fallback_finds_new_player_outside_roster(
    monkeypatch,
) -> None:
    team = 0x5000
    club = 0x6000
    person = 0x7000
    player = 0x6F00
    contract = 0x8000
    team_vtable = 0x100200
    person_raw = bytearray(0xB0)
    struct.pack_into("<Q", person_raw, 0xA8, contract)
    contract_raw = bytearray(0x18)
    struct.pack_into("<QQ", contract_raw, 0x08, person, team)
    club_by_team = {"value": club}

    class FakeDirectory:
        @staticmethod
        def player_targets():
            return {
                100: (0x7100, 0x7000, "actual_player"),
                101: (person, player, "actual_player"),
            }

    class FakeReader:
        module_base = 0x100000
        layout = SimpleNamespace(key="fm26", team_vtable_rva=0x200)

        @staticmethod
        def _fixed_size_snapshots(addresses, _size):
            return {
                address: (
                    bytes(person_raw) if address == person
                    else bytes(contract_raw) if address == contract
                    else b""
                )
                for address in addresses
            }

        @staticmethod
        def ptr(address):
            return {
                team: team_vtable,
                team + youth_intake.TEAM_CLUB: club_by_team["value"],
            }.get(address, 0)

        @staticmethod
        def u32(address):
            return 42 if address == team + 0x0C else 0

    monkeypatch.setattr(
        youth_intake, "resolve_team_club",
        lambda *_args, **_kwargs: SimpleNamespace(
            team_address=team, club_address=club,
        ),
    )
    monkeypatch.setattr(
        youth_intake, "_validated_player_person",
        lambda _reader, address, uid: person
        if (address, uid) == (player, 101) else pytest.fail("unexpected player"),
    )
    monkeypatch.setattr(
        youth_intake, "_player",
        lambda *_args, **_kwargs: {
            "id": 101, "name": "New Intake", "address": hex(player),
        },
    )
    monkeypatch.setattr(
        youth_intake, "_movement_snapshot",
        lambda *_args, **_kwargs: {"contract_type": 3, "loan": None},
    )

    result = youth_intake._primary_contract_youth_candidates(
        FakeReader(), FakeDirectory(), {
            "global_player_baseline_complete": True,
            "global_player_baseline_ids": [100],
            "processed_player_ids": [],
        }, 42, hex(team), youth_intake.date(2028, 12, 2),
    )

    assert result == [{
        "id": 101, "name": "New Intake", "address": hex(player),
        "candidate_source": "primary_contract",
        "formed_at_club": None,
        "movement": {"contract_type": 3, "loan": None},
    }]

    club_by_team["value"] = 0x6001
    assert youth_intake._primary_contract_youth_candidates(
        FakeReader(), FakeDirectory(), {
            "global_player_baseline_complete": True,
            "global_player_baseline_ids": [100],
            "processed_player_ids": [],
        }, 42, hex(team), youth_intake.date(2028, 12, 2),
    ) == []


def test_fm26_golden_plan_uses_verified_roster_finalization(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
        "global_player_ids": [103],
        "global_player_baseline_complete": True,
    })
    plan = youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 1, "min_pa": 190, "max_pa": 200}, "2028-12-01",
    )
    assert plan["execution_mode"] == "hybrid"
    values = {
        0x2010: 90, 0x2012: 140,
        0x3010: 95, 0x3012: 145,
        0x4010: 80, 0x4012: 145,
    }
    layout = SimpleNamespace(
        key="fm26", player_ca_offset=0x10, player_pa_offset=0x12,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )
    reader = SimpleNamespace(u16=lambda address: values.get(address, 0))
    monkeypatch.setattr(
        youth_intake, "select_process_layout",
        lambda: (1, "game_plugin.dll", layout),
    )
    monkeypatch.setattr(
        youth_intake, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace()),
    )
    monkeypatch.setattr(youth_intake, "Reader", lambda *_args: reader)
    monkeypatch.setattr(youth_intake, "_roster_snapshot", lambda *_args: [
        {
            "id": 101, "name": "Reserved Son", "age": 16,
            "formed_at_club": True, "address": "0x2000",
        },
        {
            "id": 102, "name": "Quality Player", "age": 16,
            "formed_at_club": False, "address": "0x3000",
        },
        {
            "id": 103, "name": "New Signing", "age": 16,
            "formed_at_club": False, "address": "0x4000",
        },
    ])
    monkeypatch.setattr(
        youth_intake, "_validated_player_person",
        lambda _reader, player, _uid: player + 0x100,
    )
    monkeypatch.setattr(youth_intake.secrets, "randbelow", lambda _limit: 5)

    def write(_process, address, data):
        values[address] = struct.unpack("<H", data)[0]

    monkeypatch.setattr(youth_intake, "write_process_memory", write)
    result = youth_intake.finalize_fm26_golden_generation_attributes(
        "scope", 42, "0x1000", "2028-12-02",
        excluded_player_ids={101},
    )

    assert result["finalized"] is True
    assert result["players"] == [{
        "id": 102, "name": "Quality Player", "pa_before": 145, "pa": 195,
    }]
    assert values[0x2012] == 140
    assert values[0x3012] == 195
    assert values[0x4012] == 145
    stored = youth_intake._load("scope")["plans"]["42:golden_generation"]
    assert stored["status"] == "completed"
    assert stored["processed_player_ids"] == [102]
    assert stored["planned_ability_targets"] == {"102": 195}
    assert stored["planned_ability_originals"] == {"102": 145}
    assert stored["verification_result"] == {
        "expected_count": 1,
        "verified_count": 1,
        "candidate_count": 1,
        "hook_applied_count": 0,
        "preexisting_match_count": 0,
        "repaired_count": 1,
        "hook_assessment": "not_observed",
    }


def test_golden_reconcile_preserves_a_matching_final_roster_pa(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 1, "min_pa": 170, "max_pa": 180}, "2028-12-01",
    )
    plan = youth_intake._load("scope")["plans"]["42:golden_generation"]

    targets, originals = youth_intake._reserve_golden_ability_targets(
        "scope", 42, plan,
        [{"id": 101, "name": "Hook Match"}],
        {101: 176},
        {101: 100},
        {"count": 1, "min_pa": 170, "max_pa": 180},
    )

    assert targets == {101: 176}
    assert originals == {101: 176}


def test_golden_target_is_always_strictly_above_current_ca(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    monkeypatch.setattr(youth_intake.secrets, "randbelow", lambda _limit: 0)
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 1, "min_pa": 110, "max_pa": 140}, "2028-12-01",
    )
    plan = youth_intake._load("scope")["plans"]["42:golden_generation"]

    targets, _originals = youth_intake._reserve_golden_ability_targets(
        "scope", 42, plan, [{"id": 101, "name": "High CA Youth"}],
        {101: 120}, {101: 130},
        {"count": 1, "min_pa": 110, "max_pa": 140},
    )

    assert targets == {101: 131}


def test_academy_son_rejects_a_second_club_target(monkeypatch, tmp_path) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "academy_son", {"count": 1}, "2028-12-01",
    )
    with pytest.raises(ValueError, match="唯一的儿子历练名额已经用完"):
        youth_intake.arm_youth_plan(
            "scope", 42, "0x2000", "academy_son", {"count": 1}, "2028-12-02",
        )

    assert "academy_son" in youth_intake.youth_plans_for_team("scope", 41)
    assert "academy_son" not in youth_intake.youth_plans_for_team("scope", 42)
    active = youth_intake.active_youth_generation_plans("scope")
    assert len(active) == 1
    assert active[0]["kind"] == "academy_son"
    assert active[0]["roster_finalize_only"] is True
    assert "ca" not in active[0]
    assert "pa" not in active[0]


def test_remove_youth_plans_for_teams_clears_plans_and_yearly_usage(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    payload = youth_intake._empty_payload()
    payload["plans"] = {
        "42:academy_son": {
            "team_id": 42, "kind": "academy_son", "armed_at": "son-42",
        },
        "42:golden_generation": {
            "team_id": 42, "kind": "golden_generation", "armed_at": "gold-42",
        },
        "84:golden_generation": {
            "team_id": 84, "kind": "golden_generation", "armed_at": "gold-84",
        },
    }
    payload["son_usage"] = {
        "2028": [{"team_id": 42, "armed_at": "son-42"}],
    }
    payload["golden_generation_usage"] = {
        "2028": [
            {"team_id": 42, "armed_at": "gold-42", "count": 1},
            {"team_id": 84, "armed_at": "gold-84", "count": 2},
        ],
    }
    youth_intake._save("rewind-scope", payload)

    removed = youth_intake.remove_youth_plans_for_teams(
        "rewind-scope", {42},
    )

    stored = youth_intake._load("rewind-scope")
    assert {row["kind"] for row in removed} == {
        "academy_son", "golden_generation",
    }
    assert set(stored["plans"]) == {"84:golden_generation"}
    assert stored["son_usage"] == {}
    assert stored["golden_generation_usage"] == {
        "2028": [{"team_id": 84, "armed_at": "gold-84", "count": 2}],
    }


def test_academy_son_progress_completes_after_first_native_hit(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm24", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "academy_son", {
            "count": 1, "min_ca": 85, "max_ca": 105,
            "min_pa": 130, "max_pa": 140,
        }, "2028-12-01",
    )
    active = youth_intake.active_youth_generation_plans("scope")[0]
    youth_intake.record_youth_generation_progress("scope", {
        "targets": [], "son_target": active, "son_applied_count": 1,
    })

    plan = youth_intake.youth_plans_for_team("scope", 42)["academy_son"]
    assert plan["status"] == "waiting"
    assert plan["native_applied"] is True
    assert plan["generation_completed"] is True
    waiting = youth_intake.active_youth_generation_plans("scope")[0]
    assert waiting["generation_completed"] is True
    assert waiting["applied"] == 1
    youth_intake.record_youth_generation_progress("scope", {
        "targets": [], "son_target": active, "son_applied_count": 1,
        "son_finalized": True,
        "son_player": {"id": 99, "name": "测试儿子", "ca": 95, "pa": 180},
    })

    plan = youth_intake.youth_plans_for_team("scope", 42)["academy_son"]
    assert plan["status"] == "completed"
    assert plan["remaining_count"] == 0


@pytest.mark.parametrize("terminal_status", ["completed", "completed_with_refund"])
def test_terminal_academy_son_plan_releases_slot_immediately(
    monkeypatch, tmp_path, terminal_status,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "academy_son", {"count": 1}, "2028-01-01",
    )

    payload = youth_intake._load("scope")
    payload["plans"]["41:academy_son"]["status"] = terminal_status
    youth_intake._save("scope", payload)

    status = youth_intake.academy_son_year_status("scope", "2028-12-31")
    replacement = youth_intake.arm_youth_plan(
        "scope", 42, "0x2000", "academy_son", {"count": 1}, "2028-12-31",
    )

    assert status == {
        "year": 2028, "limit": 1, "used": 0, "remaining": 1, "placements": [],
    }
    assert replacement["team_id"] == 42
    assert replacement["status"] == "armed"


def test_academy_son_year_status_repairs_orphaned_usage(monkeypatch, tmp_path) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    payload = youth_intake._empty_payload()
    payload["son_usage"] = {
        "2028": [{
            "team_id": 41, "armed_at": "cancelled-plan",
            "armed_game_date": "2028-03-01",
        }],
    }
    youth_intake._save("scope", payload)

    status = youth_intake.academy_son_year_status("scope", "2028-12-31")

    assert status == {
        "year": 2028, "limit": 1, "used": 0, "remaining": 1,
        "placements": [],
    }
    assert youth_intake._load("scope")["son_usage"] == {}


def test_academy_son_arm_ignores_orphaned_current_year_usage(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    payload = youth_intake._empty_payload()
    payload["son_usage"] = {
        "2028": [{
            "team_id": 41, "armed_at": "cancelled-plan",
            "armed_game_date": "2028-03-01",
        }],
    }
    youth_intake._save("scope", payload)

    plan = youth_intake.arm_youth_plan(
        "scope", 42, "0x2000", "academy_son",
        {"count": 1, "name": "Alex"}, "2028-12-31",
    )

    assert plan["team_id"] == 42
    status = youth_intake.academy_son_year_status("scope", "2028-12-31")
    assert status["used"] == 1
    assert [row["team_id"] for row in status["placements"]] == [42]


def test_cancel_academy_son_restores_yearly_slot(monkeypatch, tmp_path) -> None:
    import fm_odds_web

    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "academy_son", {"count": 1}, "2028-01-01",
    )
    state = SimpleNamespace(
        memory_lock=nullcontext(),
        youth_generation_hook=SimpleNamespace(status=lambda: {}),
        _owned_world_club_target=lambda *_args, **_kwargs: (
            "scope", {"save_instance_id": "scope"},
            {"id": 41, "name": "测试俱乐部", "address": "0x1000"},
        ),
        _sync_youth_generation_plans=lambda *_args: {
            "installed": False, "error": None,
        },
        _data_scope_id=lambda _output: "scope",
    )

    result = fm_odds_web.LocalOddsState.cancel_world_club_youth_plan(
        state, {"team_id": 41, "kind": "academy_son"},
    )

    assert result["cancelled"] is True
    assert youth_intake.youth_plans_for_team("scope", 41) == {}
    assert youth_intake.academy_son_year_status("scope", "2028-12-31") == {
        "year": 2028, "limit": 1, "used": 0, "remaining": 1,
        "placements": [],
    }
    replacement = youth_intake.arm_youth_plan(
        "scope", 42, "0x2000", "academy_son", {"count": 1}, "2028-12-31",
    )
    assert replacement["team_id"] == 42


def test_golden_generation_limit_is_per_team_and_resets_each_year(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    config = {"count": 10, "min_pa": 130, "max_pa": 140}
    youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "golden_generation", config, "2028-01-01",
    )
    youth_intake.arm_youth_plan(
        "scope", 42, "0x2000", "golden_generation", config, "2028-01-01",
    )
    assert youth_intake.golden_generation_year_status(
        "scope", 41, "2028-12-31",
    )["remaining"] == 0
    assert youth_intake.golden_generation_year_status(
        "scope", 42, "2028-12-31",
    )["used"] == 10

    payload = youth_intake._load("scope")
    payload["plans"]["41:golden_generation"]["status"] = "completed"
    youth_intake._save("scope", payload)
    next_plan = youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "golden_generation", config, "2029-01-01",
    )

    assert next_plan["armed_game_date"] == "2029-01-01"
    assert youth_intake.golden_generation_year_status(
        "scope", 41, "2029-01-01",
    )["used"] == 10


def test_completed_golden_plan_can_start_a_new_round_immediately(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    config = {"count": 10, "min_pa": 130, "max_pa": 140}
    youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "golden_generation", config, "2028-03-01",
    )
    payload = youth_intake._load("scope")
    payload["plans"]["41:golden_generation"]["status"] = "completed"
    youth_intake._save("scope", payload)

    quota = youth_intake.golden_generation_year_status(
        "scope", 41, "2028-03-01",
    )
    next_plan = youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "golden_generation", config, "2028-03-01",
    )

    assert quota["used"] == 0
    assert quota["remaining"] == 10
    assert next_plan["status"] == "armed"
    assert youth_intake.golden_generation_year_status(
        "scope", 41, "2028-03-01",
    )["used"] == 10


def test_golden_generation_status_repairs_mismatched_year_usage(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    payload = youth_intake._empty_payload()
    payload["plans"] = {
        "41:golden_generation": {
            "team_id": 41, "kind": "golden_generation", "status": "completed",
            "armed_at": "plan-2029", "armed_game_date": "2029-03-01",
            "config": {"count": 7, "min_pa": 190, "max_pa": 200},
        },
        "42:golden_generation": {
            "team_id": 42, "kind": "golden_generation", "status": "armed",
            "armed_at": "plan-2028", "armed_game_date": "2028-03-01",
            "config": {"count": 10, "min_pa": 130, "max_pa": 140},
        },
    }
    payload["golden_generation_usage"] = {
        "2028": [
            {
                "team_id": 41, "count": 10, "armed_at": "orphan-2028",
                "armed_game_date": "2028-02-01",
            },
            {
                "team_id": 42, "count": 10, "armed_at": "plan-2028",
                "armed_game_date": "2028-03-01",
            },
        ],
    }
    youth_intake._save("scope", payload)

    repaired = youth_intake.golden_generation_year_status(
        "scope", 41, "2028-12-31",
    )
    preserved = youth_intake.golden_generation_year_status(
        "scope", 42, "2028-12-31",
    )

    assert repaired["used"] == 0
    assert repaired["remaining"] == 10
    assert preserved["used"] == 10
    assert [
        row["team_id"] for row in youth_intake._load("scope")[
            "golden_generation_usage"
        ]["2028"]
    ] == [42]


def test_golden_generation_arm_ignores_orphaned_team_usage(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    payload = youth_intake._empty_payload()
    payload["golden_generation_usage"] = {
        "2028": [{
            "team_id": 41, "count": 10, "armed_at": "orphan-2028",
            "armed_game_date": "2028-02-01",
        }],
    }
    youth_intake._save("scope", payload)

    plan = youth_intake.arm_youth_plan(
        "scope", 41, "0x1000", "golden_generation",
        {"count": 3, "min_pa": 130, "max_pa": 140}, "2028-12-31",
    )

    assert plan["team_id"] == 41
    status = youth_intake.golden_generation_year_status(
        "scope", 41, "2028-12-31",
    )
    assert status["used"] == 3
    assert status["remaining"] == 7


@pytest.mark.parametrize(
    (
        "native_relation", "formed_at_club", "selection_mode",
        "secondary_position", "configured_name",
    ),
    [
        (True, True, "native_relation", "AMC", "Alexander"),
        (False, False, "random_fallback", "", ""),
    ],
)
def test_finalize_academy_son_preserves_ca_and_writes_pa_relation_fallback(
    monkeypatch, tmp_path, native_relation: bool, formed_at_club: bool,
    selection_mode: str, secondary_position: str, configured_name: str,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    random_values = iter((10, 0))
    monkeypatch.setattr(
        youth_intake.secrets, "randbelow", lambda _limit: next(random_values),
    )
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm26", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "academy_son", {
            "count": 1, "name": configured_name, "preserve_ca": True,
            "min_pa": 160, "max_pa": 170,
            "primary_position": "ST", "secondary_position": secondary_position,
        }, "2028-12-01",
    )

    values = {0x2010: 70, 0x2012: 120}
    pointers = {
        0x3020: 0x5000, 0x6000: 0x4000, 0x3050: 0xA100,
        0x3030: 0xC000, 0x3038: 0xC008,
        0x3040: 0xC010, 0x3048: 0xC018,
        0x4038: 0xD000,
    }
    strings = {
        0xC000: "OriginalGiven", 0xC008: "OriginalSurname",
        0xC010: "Original Common", 0xC018: "Original Full",
        0xD000: "ManagerSurname",
    }
    position_values = {0x2060: bytes([5] * 15)}
    layout = SimpleNamespace(
        key="fm26", player_ca_offset=0x10, player_pa_offset=0x12,
        player_positions_offset=0x60, person_nationality_offset=0x50,
        person_relationships_offset=0x20,
        person_first_name_offset=0x30, person_last_name_offset=0x38,
        person_common_name_offset=0x40, person_full_name_offset=0x48,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )

    class FakeReader:
        def __init__(self):
            self.layout = layout
            self.process = SimpleNamespace(pid=1)
            self.module_base = 0x100000
            self.string_cache = {}
            self.invalidations = 0

        def invalidate_prefetch(self):
            self.invalidations += 1

        def ptr(self, address):
            return pointers.get(address, 0)

        def u16(self, address):
            if native_relation and address == 0x6008:
                return youth_intake.RELATION_REASON_PARENT
            return values.get(address, 0)

        def bytes(self, address, size):
            if address == 0x5000 and size == 24:
                return struct.pack("<QQQ", 0x6000, 0x6010, 0x6010)
            if address == 0x2060 and size == 15:
                return position_values[address]
            return None

        def fm_nested_string_at(self, address):
            return strings.get(pointers.get(address, 0))

        def fm_string_at(self, address):
            return strings.get(pointers.get(address, 0))

    reader = FakeReader()
    monkeypatch.setattr(
        youth_intake, "select_process_layout", lambda: (1, "fm.exe", layout),
    )
    monkeypatch.setattr(
        youth_intake, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(pid=1)),
    )
    monkeypatch.setattr(youth_intake, "Reader", lambda *_args: reader)
    monkeypatch.setattr(youth_intake, "_roster_snapshot", lambda *_args: [{
        "id": 99, "name": "Original Common", "address": "0x2000",
        "age": 16, "formed_at_club": formed_at_club, "ca": 70, "pa": 120,
    }])
    monkeypatch.setattr(
        youth_intake, "_context_addresses", lambda *_args: (0x7000, 0x4000, 0x1000),
    )
    monkeypatch.setattr(
        youth_intake, "_validated_player_person", lambda *_args: 0x3000,
    )
    monkeypatch.setattr(
        youth_intake, "_manager_primary_nation",
        lambda *_args: (0xA000, 1651),
    )
    relation_writes = []
    monkeypatch.setattr(
        youth_intake, "_write_relation",
        lambda _process, _reader, source, target, reason: (
            relation_writes.append((source, target, reason))
            or {"mode": "updated", "address": 0, "original": b""}
        ),
    )
    def write_display_name(_process, fields, encoded_name):
        strings[0xE000] = encoded_name.decode("utf-8")
        pointers[fields[0]] = 0
        pointers[fields[1]] = 0
        pointers[fields[2]] = 0xE000
        pointers[fields[3]] = 0xE000
        return 0x8000, 0x9000

    monkeypatch.setattr(
        youth_intake, "_write_person_display_name", write_display_name,
    )

    def write_memory(_process, address, data):
        if len(data) == 2:
            values[address] = struct.unpack("<H", data)[0]
        elif len(data) == 8:
            pointers[address] = struct.unpack("<Q", data)[0]
        elif len(data) == 15:
            position_values[address] = bytes(data)
        else:
            raise AssertionError(f"unexpected write size: {len(data)}")

    monkeypatch.setattr(youth_intake, "write_process_memory", write_memory)

    result = youth_intake.finalize_academy_son_attributes(
        "scope", 42, "0x1000", "2028-12-02",
        manager_id=7, manager_team_id=8,
        manager_team_address="0x1000", manager_address="0x7000",
        allow_random_fallback=not native_relation,
        fallback_requires_formed_at_club=False,
    )

    assert result == {
        "finalized": True,
        "player": {
            "id": 99,
            "name": f"{configured_name or 'OriginalGiven'} ManagerSurname",
            "given_name": configured_name or "OriginalGiven",
            "surname": "ManagerSurname",
            "ca": 70, "pa": 170,
            "primary_position": "ST",
            "secondary_position": secondary_position or None,
            "nationality_id": 1651,
        },
        "selection_mode": selection_mode,
    }
    assert len(relation_writes) == (0 if native_relation else 2)
    assert reader.invalidations >= 1
    assert values[0x2010] == 70
    assert values[0x2012] == 170
    expected_positions = bytearray([1] * 15)
    expected_positions[12] = 20
    if secondary_position:
        expected_positions[10] = 12
    assert position_values[0x2060] == bytes(expected_positions)
    assert pointers[0x3050] == 0xA000
    assert strings[pointers[0x3030]] == (configured_name or "OriginalGiven")
    assert pointers[0x3038] == 0xD000
    assert pointers[0x3040] == 0
    stored = youth_intake._load("scope")["plans"]["42:academy_son"]
    assert stored["processed_player_ids"] == [99]
    assert stored["ability_result"]["player_name"] == (
        f"{configured_name or 'OriginalGiven'} ManagerSurname"
    )
    assert stored["ability_result"]["given_name"] == (
        configured_name or "OriginalGiven"
    )
    assert stored["ability_result"]["surname"] == "ManagerSurname"
    assert stored["ability_result"]["ca"] == 70
    assert stored["ability_result"]["pa"] == 170
    assert stored["ability_result"]["primary_position"] == "ST"
    assert stored["ability_result"]["primary_position_rating"] == 20
    assert stored["ability_result"]["secondary_position"] == (
        secondary_position or None
    )
    assert stored["ability_result"]["secondary_position_rating"] == (
        12 if secondary_position else None
    )
    assert stored["ability_result"]["nationality_id"] == 1651
    assert stored["ability_result"]["selection_mode"] == selection_mode


def test_finalize_academy_son_rolls_back_position_and_nationality(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm24", "players": [],
    })
    monkeypatch.setattr(youth_intake.secrets, "randbelow", lambda _limit: 0)
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "academy_son", {
            "count": 1, "name": "Alex", "min_ca": 85, "max_ca": 105,
            "min_pa": 130, "max_pa": 140,
            "primary_position": "DC", "secondary_position": "DM",
        }, "2028-12-01",
    )

    values = {0x2010: 70, 0x2012: 120}
    original_positions = bytes(range(1, 16))
    positions = {0x2060: original_positions}
    pointers = {
        0x3050: 0xA100,
        0x3030: 0xC000, 0x3038: 0xC008,
        0x3040: 0xC010, 0x3048: 0xC018,
        0x4038: 0xD000,
    }
    original_name_pointers = dict(pointers)
    strings = {
        0xC000: "Alex", 0xC008: "OldSurname",
        0xC010: "Alex", 0xC018: "Alex OldSurname",
        0xD000: "ManagerSurname",
    }
    layout = SimpleNamespace(
        key="fm24", player_ca_offset=0x10, player_pa_offset=0x12,
        player_positions_offset=0x60, person_nationality_offset=0x50,
        person_first_name_offset=0x30, person_last_name_offset=0x38,
        person_common_name_offset=0x40, person_full_name_offset=0x48,
        module=lambda _process: SimpleNamespace(base_address=0x100000),
    )

    class FakeReader:
        def __init__(self):
            self.layout = layout
            self.string_cache = {}

        def ptr(self, address):
            return pointers.get(address, 0)

        def u16(self, address):
            return values.get(address, 0)

        def bytes(self, address, size):
            if address == 0x2060 and size == 15:
                return positions[address]
            return None

        def fm_nested_string_at(self, address):
            return strings.get(pointers.get(address, 0))

        def fm_string_at(self, address):
            return strings.get(pointers.get(address, 0))

        def invalidate_prefetch(self):
            return None

    reader = FakeReader()
    monkeypatch.setattr(
        youth_intake, "select_process_layout", lambda: (1, "fm.exe", layout),
    )
    monkeypatch.setattr(
        youth_intake, "open_process",
        lambda *_args, **_kwargs: nullcontext(SimpleNamespace(pid=1)),
    )
    monkeypatch.setattr(youth_intake, "Reader", lambda *_args: reader)
    monkeypatch.setattr(youth_intake, "_roster_snapshot", lambda *_args: [{
        "id": 99, "name": "Alex", "address": "0x2000", "age": 16,
        "formed_at_club": True, "ca": 70, "pa": 120,
    }])
    monkeypatch.setattr(
        youth_intake, "_context_addresses", lambda *_args: (0x7000, 0x4000, 0x1000),
    )
    monkeypatch.setattr(
        youth_intake, "_validated_player_person", lambda *_args: 0x3000,
    )
    monkeypatch.setattr(youth_intake, "_has_parent_relation", lambda *_args: True)
    monkeypatch.setattr(
        youth_intake, "_manager_primary_nation", lambda *_args: (0xA000, 1651),
    )

    def write_display_name(_process, fields, encoded_name):
        strings[0xE000] = encoded_name.decode("utf-8")
        pointers[fields[0]] = 0
        pointers[fields[1]] = 0
        pointers[fields[2]] = 0xE000
        pointers[fields[3]] = 0xE000
        return 0x8000, 0x9000

    monkeypatch.setattr(
        youth_intake, "_write_person_display_name", write_display_name,
    )
    monkeypatch.setattr(youth_intake, "_remote_free_block", lambda *_args: None)

    def write_memory(_process, address, data):
        if len(data) == 2:
            values[address] = struct.unpack("<H", data)[0]
        elif len(data) == 8:
            pointers[address] = struct.unpack("<Q", data)[0]
        elif len(data) == 15:
            positions[address] = bytes(data)

    monkeypatch.setattr(youth_intake, "write_process_memory", write_memory)
    monkeypatch.setattr(
        youth_intake, "_save",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("persist failed")),
    )

    with pytest.raises(RuntimeError, match="persist failed"):
        youth_intake.finalize_academy_son_attributes(
            "scope", 42, "0x1000", "2028-12-02",
            manager_id=7, manager_team_id=8,
            manager_team_address="0x1000", manager_address="0x7000",
        )

    assert values == {0x2010: 70, 0x2012: 120}
    assert positions[0x2060] == original_positions
    assert pointers[0x3050] == 0xA100
    assert {
        address: pointers[address]
        for address in (0x3030, 0x3038, 0x3040, 0x3048)
    } == {
        address: original_name_pointers[address]
        for address in (0x3030, 0x3038, 0x3040, 0x3048)
    }


def test_golden_generation_progress_survives_restart_and_completes(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    monkeypatch.setattr(youth_intake, "read_youth_roster_snapshot", lambda *_args: {
        "game_key": "fm24", "players": [],
    })
    youth_intake.arm_youth_plan(
        "scope", 42, "0x1000", "golden_generation",
        {"count": 3, "min_pa": 170, "max_pa": 180}, "2028-12-01",
    )
    active = youth_intake.active_golden_generation_plans("scope")

    assert active[0]["count"] == 3
    youth_intake.record_golden_generation_progress("scope", [{
        **active[0], "remaining": 1,
    }])
    assert youth_intake.active_golden_generation_plans("scope")[0]["count"] == 1

    youth_intake.record_golden_generation_progress("scope", [{
        **active[0], "remaining": 0,
    }])
    active_after_hook = youth_intake.active_golden_generation_plans("scope")
    assert active_after_hook[0]["count"] == 0
    plan = youth_intake.youth_plans_for_team("scope", 42)["golden_generation"]
    assert plan["status"] == "waiting"
    assert plan["generation_completed"] is True
    assert plan["remaining_count"] == 3
    assert plan["hook_remaining_count"] == 0
    assert plan["verification_result"] == {
        "expected_count": 3,
        "verified_count": 0,
        "hook_applied_count": 3,
        "hook_assessment": "generation_hook_completed_pending_roster",
    }
    assert youth_intake.record_golden_generation_progress("scope", [{
        **active[0], "remaining": 0,
    }]) == 0


def test_golden_generation_arm_charges_selected_total(monkeypatch) -> None:
    import fm_odds_web

    charged = []
    synced = []
    fake_state = SimpleNamespace(
        memory_lock=nullcontext(),
        youth_generation_hook=SimpleNamespace(sync=lambda _plans: {
            "installed": True, "error": None,
        }),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda scope, save_id: (
            synced.append((scope, save_id))
            or {"installed": True, "error": None}
        ),
        _owned_world_club_target=lambda _team_id, **_kwargs: (
            "scope", {"game_date": "2029-12-01"},
            {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, kind, **details: charged.append((amount, kind, details)) or {
            "bank": amount, "wallet": 0, "total": amount,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "arm_youth_plan",
        lambda *_args, **kwargs: {"status": "waiting", "payment": kwargs["payment"]},
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )
    monkeypatch.setattr(fm_odds_web, "public_economy", lambda: {"casino_balance": 12})

    result = fm_odds_web.LocalOddsState.arm_world_club_youth_plan(
        fake_state, {
            "team_id": 42, "kind": "golden_generation",
            "config": {"count": 3, "min_pa": 180, "max_pa": 190},
        },
    )

    assert charged[0][0:2] == (15_000_000_000, "world_club_golden_generation")
    assert charged[0][2]["player_count"] == 3
    assert result["payment"]["total"] == 15_000_000_000
    assert synced == [("scope", "scope")]


def test_hybrid_plan_does_not_require_generation_hook(
    monkeypatch,
) -> None:
    import fm_odds_web

    refunded = []
    disarmed = []
    fake_state = SimpleNamespace(
        memory_lock=nullcontext(),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {
            "installed": False,
            "error": None,
        },
        _owned_world_club_target=lambda _team_id, **_kwargs: (
            "scope", {"game_date": "2029-12-01"},
            {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: {
            "bank": 5_000_000, "wallet": 0, "total": 5_000_000,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "arm_youth_plan",
        lambda *_args, **_kwargs: {
            "status": "armed", "execution_mode": "hybrid",
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "disarm_youth_plan",
        lambda *args, **kwargs: disarmed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda *args, **kwargs: refunded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )
    monkeypatch.setattr(fm_odds_web, "public_economy", lambda: {})

    result = fm_odds_web.LocalOddsState.arm_world_club_youth_plan(
        fake_state, {
            "team_id": 42, "kind": "golden_generation",
            "config": {"count": 1, "min_pa": 130, "max_pa": 140},
        },
    )

    assert result["plan"]["execution_mode"] == "hybrid"
    assert disarmed == []
    assert refunded == []


def test_youth_arm_hides_generation_internals_and_refunds(
    monkeypatch, capsys,
) -> None:
    import fm_odds_web

    refunded = []
    disarmed = []
    fake_state = SimpleNamespace(
        memory_lock=nullcontext(),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {
            "installed": False,
            "error": "FM26 青训 PA 特征码不是唯一命中",
        },
        _owned_world_club_target=lambda _team_id, **_kwargs: (
            "scope", {"game_date": "2029-12-01"},
            {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *_args, **_kwargs: {"bank": 5_000_000, "wallet": 0, "total": 5_000_000},
    )
    monkeypatch.setattr(
        fm_odds_web, "arm_youth_plan",
        lambda *_args, **_kwargs: {
            "status": "waiting", "execution_mode": "generation_hook",
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "disarm_youth_plan",
        lambda *args, **kwargs: disarmed.append((args, kwargs)),
    )
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda *args, **kwargs: refunded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )

    with pytest.raises(RuntimeError, match="青训功能暂时无法启用") as caught:
        fm_odds_web.LocalOddsState.arm_world_club_youth_plan(fake_state, {
            "team_id": 42, "kind": "golden_generation",
            "config": {"count": 1, "min_pa": 130, "max_pa": 140},
        })

    assert "特征码" not in str(caught.value)
    assert "特征码不是唯一命中" in capsys.readouterr().err
    assert len(disarmed) == 1
    assert len(refunded) == 1


def test_academy_son_arm_charges_selected_ca_and_pa_bands(monkeypatch) -> None:
    import fm_odds_web

    charged = []
    fake_state = SimpleNamespace(
        memory_lock=nullcontext(),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {
            "installed": True, "error": None,
        },
        _owned_world_club_target=lambda _team_id, **_kwargs: (
            "scope", {"game_date": "2029-12-01"},
            {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, kind, **details: charged.append((amount, kind, details)) or {
            "bank": amount, "wallet": 0, "total": amount,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "arm_youth_plan",
        lambda *_args, **kwargs: {"status": "armed", "payment": kwargs["payment"]},
    )
    monkeypatch.setattr(fm_odds_web, "youth_plans_for_team", lambda *_args: {})
    monkeypatch.setattr(fm_odds_web, "load_acquired_clubs", lambda *_args: {"clubs": []})
    monkeypatch.setattr(fm_odds_web, "active_academy_son_assignment", lambda *_args: None)
    monkeypatch.setattr(fm_odds_web, "public_economy", lambda: {"casino_balance": 12})

    result = fm_odds_web.LocalOddsState.arm_world_club_youth_plan(fake_state, {
        "team_id": 42, "kind": "academy_son", "config": {
            "count": 1, "name": "Alexander", "min_ca": 135, "max_ca": 150,
            "min_pa": 170, "max_pa": 180,
            "primary_position": "ST", "secondary_position": "AMC",
        },
    })

    assert charged[0][0:2] == (2_000_000_000, "world_club_academy_son")
    assert charged[0][2]["player_name"] == "Alexander"
    assert charged[0][2]["primary_position"] == "ST"
    assert charged[0][2]["secondary_position"] == "AMC"
    assert charged[0][2]["min_ca"] == 135
    assert result["payment"]["total"] == 2_000_000_000


def test_academy_son_assignment_resolves_active_club_name(monkeypatch) -> None:
    import fm_odds_web

    monkeypatch.setattr(fm_odds_web, "active_youth_generation_plans", lambda _scope: [
        {"kind": "academy_son", "team_id": 77},
    ])

    assert fm_odds_web.active_academy_son_assignment("scope", [
        {"id": 42, "name": "甲俱乐部"},
        {"id": 77, "name": "乙俱乐部"},
    ]) == {"team_id": 77, "team_name": "乙俱乐部"}


def test_academy_son_arm_rejects_another_club_before_charging(monkeypatch) -> None:
    import fm_odds_web

    state = SimpleNamespace(
        _owned_world_club_target=lambda *_args, **_kwargs: (
            "scope", {"game_date": "2029-12-01"},
            {"id": 42, "name": "甲俱乐部", "address": "0x1000"},
        ),
    )
    monkeypatch.setattr(fm_odds_web, "youth_plans_for_team", lambda *_args: {})
    monkeypatch.setattr(fm_odds_web, "load_acquired_clubs", lambda *_args: {
        "clubs": [{"id": 77, "name": "乙俱乐部"}],
    })
    monkeypatch.setattr(
        fm_odds_web, "active_academy_son_assignment",
        lambda *_args: {"team_id": 77, "team_name": "乙俱乐部"},
    )
    charged = []
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *args, **kwargs: charged.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="儿子已在（乙俱乐部）历练"):
        fm_odds_web.LocalOddsState.arm_world_club_youth_plan(state, {
            "team_id": 42, "kind": "academy_son", "config": {
                "count": 1, "name": "Alexander",
                "min_ca": 85, "max_ca": 105,
                "min_pa": 130, "max_pa": 140,
                "primary_position": "ST", "secondary_position": "AMC",
            },
        })
    assert charged == []


def test_golden_generation_cannot_be_purchased_twice_or_cancelled(monkeypatch) -> None:
    import fm_odds_web

    state = SimpleNamespace(
        _owned_world_club_target=lambda *_args, **_kwargs: (
            "scope", {"save_instance_id": "scope"},
            {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"golden_generation": {"status": "armed"}},
    )
    charged = []
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda *args, **kwargs: charged.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="已有进行中的青训质量投资"):
        fm_odds_web.LocalOddsState.arm_world_club_youth_plan(state, {
            "team_id": 42, "kind": "golden_generation",
            "config": {"count": 1, "min_pa": 170, "max_pa": 180},
        })
    with pytest.raises(ValueError, match="启用后不可取消"):
        fm_odds_web.LocalOddsState.cancel_world_club_youth_plan(state, {
            "team_id": 42, "kind": "golden_generation",
        })
    assert charged == []


def test_completed_golden_plan_service_allows_next_round(monkeypatch) -> None:
    import fm_odds_web

    charged = []
    armed = []
    state = SimpleNamespace(
        memory_lock=nullcontext(),
        _data_scope_id=lambda _output: "scope",
        _owned_world_club_target=lambda *_args, **_kwargs: (
            "scope", {"game_date": "2029-03-01", "save_instance_id": "scope"},
            {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
        ),
        _sync_youth_generation_plans=lambda *_args: {
            "installed": True, "error": None,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"golden_generation": {"status": "completed"}},
    )
    monkeypatch.setattr(
        fm_odds_web, "charge_combined_funds",
        lambda amount, *_args, **_kwargs: (
            charged.append(amount)
            or {"bank": amount, "wallet": 0, "total": amount}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "arm_youth_plan",
        lambda *_args, **kwargs: (
            armed.append(True)
            or {"status": "armed", "payment": kwargs["payment"]}
        ),
    )
    monkeypatch.setattr(fm_odds_web, "public_economy", lambda: {})

    result = fm_odds_web.LocalOddsState.arm_world_club_youth_plan(state, {
        "team_id": 42, "kind": "golden_generation",
        "config": {"count": 1, "min_pa": 170, "max_pa": 180},
    })

    assert charged
    assert armed == [True]
    assert result["plan"]["status"] == "armed"


def test_owned_youth_target_quickly_rebinds_a_stale_world_cache(monkeypatch) -> None:
    import fm_odds_web

    fake_state = SimpleNamespace(
        lock=nullcontext(),
        output={
            "save_instance_id": "save-1",
            "competition_formats": [{"stages": [{"teams": [{"address": "0x1000"}]}]}],
        },
        _bind_current_save=lambda: "scope",
        _data_scope_id=lambda _output: "scope",
        _world_club_native_cache=lambda _save_id: {
            "addresses_current": False,
            "clubs": [{"id": 42, "name": "缓存名称", "address": "0xOLD"}],
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42, "name": "已收购俱乐部"}]},
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda team_ids, hints, **_kwargs: {42: "0x2000"}
        if team_ids == {42} and hints == {0x1000} else {},
    )

    scope, _output, club = fm_odds_web.LocalOddsState._owned_world_club_target(
        fake_state, 42, quick_rebind=True,
    )

    assert scope == "scope"
    assert club["id"] == 42
    assert club["address"] == "0x2000"
    assert club["address_source"] == "quick_rebind"


def test_owned_youth_target_validates_cache_marked_current(monkeypatch) -> None:
    import fm_odds_web

    fake_state = SimpleNamespace(
        lock=nullcontext(),
        output={
            "save_instance_id": "save-1",
            "managed_teams": [{"address": "0x1000"}],
        },
        _bind_current_save=lambda: "scope",
        _data_scope_id=lambda _output: "scope",
        _world_club_native_cache=lambda _save_id: {
            "addresses_current": True,
            "clubs": [{"id": 42, "name": "缓存名称", "address": "0xOLD"}],
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42, "name": "已收购俱乐部"}]},
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda team_ids, hints, **_kwargs: {42: "0x2000"}
        if team_ids == {42} and hints == {0x1000} else {},
    )
    monkeypatch.setattr(
        fm_odds_web, "scan_native_world_clubs",
        lambda *_args, **_kwargs: pytest.fail("unexpected full scan"),
    )

    _scope, _output, club = fm_odds_web.LocalOddsState._owned_world_club_target(
        fake_state, 42, quick_rebind=True,
    )

    assert club["address"] == "0x2000"
    assert club["address_source"] == "quick_rebind"


def test_owned_world_club_target_uses_quick_rebind_by_default(monkeypatch) -> None:
    import fm_odds_web

    fake_state = SimpleNamespace(
        lock=nullcontext(),
        output={"save_instance_id": "save-1", "managed_teams": [{"address": "0x1000"}]},
        _bind_current_save=lambda: "scope",
        _data_scope_id=lambda _output: "scope",
        _world_club_native_cache=lambda _save_id: {
            "addresses_current": False,
            "clubs": [{"id": 42, "address": "0xOLD"}],
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "load_acquired_clubs",
        lambda _scope: {"clubs": [{"id": 42, "name": "已收购俱乐部"}]},
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda team_ids, hints, **_kwargs: {42: "0x2000"}
        if team_ids == {42} and hints == {0x1000} else {},
    )
    monkeypatch.setattr(
        fm_odds_web, "build_world_clubs",
        lambda *_args, **_kwargs: pytest.fail("default target lookup used full directory"),
    )

    _scope, _output, club = fm_odds_web.LocalOddsState._owned_world_club_target(
        fake_state, 42,
    )

    assert club["address"] == "0x2000"
    assert club["address_source"] == "quick_rebind"


def test_active_youth_plans_quickly_rebind_before_hook_sync(monkeypatch) -> None:
    import fm_odds_web

    synced = []
    fake_state = SimpleNamespace(
        youth_generation_scope_id=None,
        youth_generation_hook=SimpleNamespace(
            sync=lambda plans: synced.extend(plans) or {"installed": True},
            suspend=lambda error: pytest.fail(error),
        ),
        output={"managed_teams": [{"address": "0x1000"}]},
        _world_club_native_cache=lambda _save_id: {"addresses_current": False},
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{"team_id": 42, "kind": "academy_son", "count": 1}],
    )
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda _scope, _output: [{"id": 42}],
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda team_ids, hints: {42: "0x2000"}
        if team_ids == {42} and hints == {0x1000} else {},
    )

    status = fm_odds_web.LocalOddsState._sync_youth_generation_plans(
        fake_state, "scope", "save-1",
    )

    assert status["installed"] is True
    assert synced[0]["team_address"] == "0x2000"


def test_completed_son_generation_is_not_rearmed_after_restart_or_new_year(
    monkeypatch,
) -> None:
    import fm_odds_web

    synced = []
    fake_state = SimpleNamespace(
        youth_generation_scope_id=None,
        youth_generation_hook=SimpleNamespace(
            sync=lambda plans: synced.append([dict(plan) for plan in plans]) or {
                "installed": bool(plans), "error": None,
            },
        ),
        output={},
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{
            "team_id": 42, "kind": "academy_son", "count": 1,
            "generation_completed": True, "applied": 1,
        }],
    )
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda _scope, _output: [{"id": 42}],
    )
    monkeypatch.setattr(
        fm_odds_web, "world_club_team_address_hints",
        lambda _output: pytest.fail("completed generation resolved a team address"),
    )

    status = fm_odds_web.LocalOddsState._sync_youth_generation_plans(
        fake_state, "scope", "next-year-save",
    )

    assert status["installed"] is False
    assert synced == [[]]
    assert fake_state.youth_generation_scope_id == "scope"


def test_roster_finalize_only_son_does_not_arm_generation_hook(
    monkeypatch,
) -> None:
    import fm_odds_web

    synced = []
    fake_state = SimpleNamespace(
        youth_generation_scope_id=None,
        youth_generation_hook=SimpleNamespace(
            sync=lambda plans: synced.append([dict(plan) for plan in plans]) or {
                "installed": bool(plans), "error": None,
            },
        ),
        output={},
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{
            "team_id": 42, "kind": "academy_son", "count": 1,
            "roster_finalize_only": True,
        }],
    )
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda _scope, _output: [{"id": 42}],
    )
    monkeypatch.setattr(
        fm_odds_web, "world_club_team_address_hints",
        lambda _output: pytest.fail("roster-only plan resolved a Hook address"),
    )

    status = fm_odds_web.LocalOddsState._sync_youth_generation_plans(
        fake_state, "scope", "save-1",
    )

    assert status["installed"] is False
    assert synced == [[]]
    assert fake_state.youth_generation_scope_id == "scope"


def test_hook_controller_ignores_roster_finalize_only_plan(monkeypatch) -> None:
    from tools.youth_generation_hook import YouthGenerationHookController

    controller = YouthGenerationHookController()
    uninstalled = []
    monkeypatch.setattr(controller, "_uninstall", lambda: uninstalled.append(True))
    monkeypatch.setattr(
        controller, "status", lambda: {"installed": False, "son_target": None},
    )

    status = controller.sync([{
        "team_id": 42, "kind": "academy_son", "count": 1,
        "roster_finalize_only": True,
    }])

    assert status == {"installed": False, "son_target": None}
    assert uninstalled == [True]


def test_active_youth_plans_retry_stale_current_cache_with_targeted_scan(
    monkeypatch,
) -> None:
    import fm_odds_web

    synced = []

    def sync(plans):
        synced.append([dict(plan) for plan in plans])
        if len(synced) == 1:
            return {"installed": False, "error": "俱乐部 42 的球队地址已失效"}
        return {"installed": True, "error": None}

    fake_state = SimpleNamespace(
        lock=nullcontext(),
        youth_generation_scope_id=None,
        youth_generation_hook=SimpleNamespace(
            sync=sync, suspend=lambda error: pytest.fail(error),
        ),
        output={"managed_teams": [{"address": "0x1000"}]},
        _world_club_native_cache=lambda _save_id: {
            "addresses_current": True,
            "clubs": [{"id": 42, "address": "0xOLD"}],
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{"team_id": 42, "kind": "academy_son", "count": 1}],
    )
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda _scope, _output: [{"id": 42}],
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda team_ids, hints: {42: "0x2000"},
    )
    monkeypatch.setattr(
        fm_odds_web, "scan_native_world_clubs",
        lambda *_args, **_kwargs: pytest.fail("unexpected full scan"),
    )

    status = fm_odds_web.LocalOddsState._sync_youth_generation_plans(
        fake_state, "scope", "save-1",
    )

    assert status["installed"] is True
    assert synced[0][0]["team_address"] == "0xOLD"
    assert synced[1][0]["team_address"] == "0x2000"


def test_active_youth_plans_rebuild_world_cache_when_targeted_scan_misses(
    monkeypatch,
) -> None:
    import fm_odds_web

    saved = []
    synced = []
    scanned = {
        "schema_version": 4,
        "clubs": [{"id": 42, "address": "0x3000"}],
    }
    fake_state = SimpleNamespace(
        lock=nullcontext(),
        world_club_cache=None,
        world_club_cache_save_id="",
        youth_generation_scope_id=None,
        youth_generation_hook=SimpleNamespace(
            sync=lambda plans: synced.extend(plans) or {
                "installed": True, "error": None,
            },
            suspend=lambda error: pytest.fail(error),
        ),
        output={"managed_teams": [{"address": "0x1000"}]},
        _world_club_native_cache=lambda _save_id: {
            "addresses_current": False, "clubs": [],
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{"team_id": 42, "kind": "academy_son", "count": 1}],
    )
    monkeypatch.setattr(
        fm_odds_web, "account_acquired_clubs",
        lambda _scope, _output: [{"id": 42}],
    )
    monkeypatch.setattr(
        fm_odds_web, "resolve_native_team_addresses",
        lambda _team_ids, _hints: {},
    )
    monkeypatch.setattr(
        fm_odds_web, "scan_native_world_clubs",
        lambda save_id, team_hints: scanned
        if save_id == "save-1" and team_hints == [0x1000]
        else pytest.fail("unexpected scan arguments"),
    )
    monkeypatch.setattr(
        fm_odds_web, "save_native_world_clubs",
        lambda payload, save_id: saved.append((payload, save_id)),
    )

    status = fm_odds_web.LocalOddsState._sync_youth_generation_plans(
        fake_state, "scope", "save-1",
    )

    assert status["installed"] is True
    assert synced[0]["team_address"] == "0x3000"
    assert saved == [(scanned, "save-1")]
    assert fake_state.world_club_cache is scanned
    assert fake_state.world_club_cache_save_id == "save-1"


def test_persisted_son_hit_finalizes_random_ability_before_completion(
    monkeypatch,
) -> None:
    import fm_odds_web

    recorded = []
    finalized = []
    hook_status = {
        "targets": [],
        "son_target": {"team_id": 42, "token": 7},
        "son_applied_count": 1,
    }
    fake_state = SimpleNamespace(
        youth_generation_scope_id="scope",
        youth_generation_hook=SimpleNamespace(
            status=lambda: dict(hook_status),
            reconcile_son_quality=lambda: {
                "reconciled": True, "pending": False, "compensated": True,
            },
        ),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        _owned_world_club_target=lambda team_id, quick_rebind: (
            "scope",
            {
                "game_date": "2028-12-02", "selected_manager_id": 7,
                "manager": {"id": 7},
            },
            {"id": team_id, "address": "0x2000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "finalize_academy_son_attributes",
        lambda *args, **kwargs: (
            finalized.append((args, kwargs))
            or {
                "finalized": True,
                "player": {"id": 99, "name": "测试儿子", "ca": 95, "pa": 180},
            }
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "record_youth_generation_progress",
        lambda scope, status: recorded.append((scope, dict(status))) or 1,
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{"team_id": 42, "kind": "academy_son", "count": 1}],
    )

    status = fm_odds_web.LocalOddsState._persist_youth_generation_progress(
        fake_state,
    )

    assert status["son_finalized"] is True
    assert status["son_player"]["ca"] == 95
    assert recorded[0][0] == "scope"
    assert recorded[0][1]["son_applied_count"] == 1
    assert finalized[0][0][0:4] == ("scope", 42, "0x2000", "2028-12-02")
    assert finalized[0][1]["allow_random_fallback"] is True
    assert finalized[0][1]["require_stable_cohort"] is True


def test_roster_finalize_only_son_is_automatically_checked(
    monkeypatch,
) -> None:
    import fm_odds_web

    finalized = []
    fake_state = SimpleNamespace(
        youth_generation_scope_id="scope",
        youth_generation_hook=SimpleNamespace(status=lambda: {}),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        _owned_world_club_target=lambda team_id, quick_rebind: (
            "scope",
            {
                "game_date": "2028-12-02", "selected_manager_id": 7,
                "manager": {"id": 7},
            },
            {"id": team_id, "address": "0x2000"},
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{
            "team_id": 42, "kind": "academy_son", "count": 1,
            "roster_finalize_only": True,
        }],
    )
    monkeypatch.setattr(
        fm_odds_web, "finalize_academy_son_attributes",
        lambda *args, **kwargs: (
            finalized.append((args, kwargs))
            or {"finalized": False, "reason": "waiting for stable roster"}
        ),
    )

    status = fm_odds_web.LocalOddsState._persist_youth_generation_progress(
        fake_state,
    )

    assert status["son_finalized"] is False
    assert status["son_finalize_reason"] == "waiting for stable roster"
    assert finalized[0][0][0:4] == ("scope", 42, "0x2000", "2028-12-02")
    assert finalized[0][1]["require_stable_cohort"] is True


def test_background_son_shortfall_waits_for_manual_confirmation(
    monkeypatch,
) -> None:
    import fm_odds_web

    plan = {"team_id": 42, "kind": "academy_son", "count": 1}
    fake_state = SimpleNamespace(
        youth_generation_scope_id="scope",
        youth_generation_hook=SimpleNamespace(status=lambda: {}),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        _owned_world_club_target=lambda team_id, quick_rebind: (
            "scope",
            {
                "game_date": "2028-01-02", "selected_manager_id": 7,
                "manager": {"id": 7},
            },
            {"id": team_id, "name": "Youth FC", "address": "0x2000"},
        ),
        _settle_youth_plan_shortfall=lambda *_args, **_kwargs: pytest.fail(
            "background son shortfall entered refund settlement"
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans", lambda _scope: [dict(plan)],
    )
    monkeypatch.setattr(
        fm_odds_web, "finalize_academy_son_attributes",
        lambda *_args, **_kwargs: {
            "finalized": False, "shortfall": 1,
            "eligible_count": 0, "observed_count": 1,
        },
    )

    status = fm_odds_web.LocalOddsState._persist_youth_generation_progress(
        fake_state,
    )

    assert status["son_finalized"] is False
    assert status["son_shortfall_pending_manual_confirmation"] == {
        "eligible_count": 0, "observed_count": 1, "shortfall": 1,
    }
    assert "计划继续等待" in status["son_finalize_reason"]
    assert "son_refund" not in status


def test_background_golden_shortfall_repairs_refunds_and_completes(
    monkeypatch,
) -> None:
    import fm_odds_web

    plan = {"team_id": 42, "kind": "golden_generation", "count": 2}
    fake_state = SimpleNamespace(
        youth_generation_scope_id="scope",
        youth_generation_hook=SimpleNamespace(status=lambda: {}),
        club_context={},
        club_contexts={},
        _owned_world_club_target=lambda team_id, quick_rebind: (
            "scope", {"game_date": "2028-01-02"},
            {"id": team_id, "name": "Youth FC", "address": "0x2000"},
        ),
        _settle_youth_plan_shortfall=lambda *_args, **_kwargs: {
            "refund": {"total": 50},
            "mail": {"kind": "youth_shortfall_refund"},
            "plan": {
                "status": "completed_with_refund",
                "last_result": {"message": "已修正 1 人并退回 1 人费用"},
            },
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans", lambda _scope: [dict(plan)],
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team", lambda *_args: {},
    )
    monkeypatch.setattr(
        fm_odds_web, "finalize_golden_generation_attributes",
        lambda *_args, **_kwargs: {
            "finalized": False, "shortfall": 1,
            "eligible_count": 1, "observed_count": 1,
        },
    )

    status = fm_odds_web.LocalOddsState._persist_youth_generation_progress(
        fake_state,
    )

    result = status["roster_finalizations"][0]
    assert result["finalized"] is True
    assert result["remaining"] == 0
    assert result["status"] == "completed_with_refund"
    assert result["refund"] == {"total": 50}
    assert result["mail"] == {"kind": "youth_shortfall_refund"}
    assert result["reason"] == "已修正 1 人并退回 1 人费用"
    assert result["settlement"]["plan"]["status"] == "completed_with_refund"


def test_background_golden_retries_persisted_shortfall_settlement(
    monkeypatch,
) -> None:
    import fm_odds_web

    plan = {
        "team_id": 42,
        "kind": "golden_generation",
        "count": 10,
        "verification_result": {
            "eligible_count": 8,
            "observed_count": 8,
            "shortfall": 2,
            "shortfall_settlement_pending": True,
        },
    }
    settlements = []
    fake_state = SimpleNamespace(
        youth_generation_scope_id="scope",
        youth_generation_hook=SimpleNamespace(status=lambda: {}),
        club_context={},
        club_contexts={},
        _owned_world_club_target=lambda team_id, quick_rebind: (
            "scope", {"game_date": "2028-01-02"},
            {"id": team_id, "name": "Youth FC", "address": "0x2000"},
        ),
        _settle_youth_plan_shortfall=lambda *args, **kwargs: (
            settlements.append((args, kwargs))
            or {
                "refund": {"total": 20},
                "mail": {"kind": "youth_shortfall_refund"},
                "plan": {
                    "status": "completed_with_refund",
                    "last_result": {"message": "完成 8 人；已退款 2 人"},
                },
            }
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans", lambda _scope: [dict(plan)],
    )
    monkeypatch.setattr(
        fm_odds_web, "finalize_golden_generation_attributes",
        lambda *_args, **_kwargs: pytest.fail(
            "pending refund should settle before rescanning processed players"
        ),
    )

    status = fm_odds_web.LocalOddsState._persist_youth_generation_progress(
        fake_state,
    )

    result = status["roster_finalizations"][0]
    assert len(settlements) == 1
    assert settlements[0][0][-1]["shortfall"] == 2
    assert settlements[0][0][-1]["eligible_count"] == 8
    assert result["finalized"] is True
    assert result["status"] == "completed_with_refund"
    assert result["reason"] == "完成 8 人；已退款 2 人"


def test_background_youth_reconcile_runs_without_page_request(
    monkeypatch,
) -> None:
    import fm_odds_web

    calls = []
    state = SimpleNamespace(
        lock=threading.RLock(),
        memory_lock=threading.Lock(),
        refreshing=False,
        reconciling=False,
        club_refreshing=False,
        save_change_pending=False,
        youth_generation_scope_id="scope",
        youth_generation_reconcile_at=0.0,
        youth_generation_reconcile_error=None,
        _persist_youth_generation_progress=lambda: (
            calls.append("persist") or {"installed": True}
        ),
        _repair_youth_generation_hook_if_needed=lambda: (
            calls.append("repair") or {"installed": True}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{"team_id": 42, "kind": "golden_generation"}],
    )
    monkeypatch.setattr(
        fm_odds_web, "set_active_save_id",
        lambda scope: calls.append(("scope", scope)),
    )

    result = fm_odds_web.LocalOddsState._reconcile_youth_generation_if_due(
        state, 10.0,
    )
    throttled = fm_odds_web.LocalOddsState._reconcile_youth_generation_if_due(
        state, 11.0,
    )

    assert result["reconciled"] is True
    assert result["hook_repair"]["installed"] is True
    assert throttled is None
    assert calls == [("scope", "scope"), "persist", "repair"]


def test_first_son_hit_disarms_hook_even_while_roster_finalize_waits(
    monkeypatch,
) -> None:
    import fm_odds_web

    synced = []
    hook_status = {
        "targets": [],
        "son_target": {"team_id": 42, "token": 7},
        "son_applied_count": 1,
    }
    fake_state = SimpleNamespace(
        youth_generation_scope_id="scope",
        youth_generation_hook=SimpleNamespace(
            status=lambda: dict(hook_status),
            reconcile_son_quality=lambda: {
                "reconciled": False, "pending": False, "compensated": False,
            },
        ),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        output={"save_instance_id": "save-1"},
        _data_scope_id=lambda _output: "scope",
        _owned_world_club_target=lambda team_id, quick_rebind: (
            "scope",
            {
                "game_date": "2028-12-02", "selected_manager_id": 7,
                "manager": {"id": 7},
            },
            {"id": team_id, "address": "0x2000"},
        ),
        _sync_youth_generation_plans=lambda scope, save_id: (
            synced.append((scope, save_id))
            or {"installed": False, "targets": [], "son_target": None}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "record_youth_generation_progress", lambda *_args: 1,
    )
    monkeypatch.setattr(
        fm_odds_web, "active_youth_generation_plans",
        lambda _scope: [{
            "team_id": 42, "kind": "academy_son", "count": 1,
            "generation_completed": True, "applied": 1,
        }],
    )
    monkeypatch.setattr(
        fm_odds_web, "finalize_academy_son_attributes",
        lambda *_args, **_kwargs: {
            "finalized": False, "reason": "等待正式名单",
        },
    )

    status = fm_odds_web.LocalOddsState._persist_youth_generation_progress(
        fake_state,
    )

    assert status["son_finalized"] is False
    assert status["son_finalize_reason"] == "等待正式名单"
    assert synced == [("scope", "save-1")]
    assert status["post_finalize_hook_sync"]["installed"] is False


def test_manual_son_apply_uses_automatic_selection_and_rejects_player_override(
    monkeypatch,
) -> None:
    import fm_odds_web

    assert not hasattr(
        fm_odds_web.LocalOddsState, "world_club_youth_plan_candidates",
    )
    assert "/api/world-clubs/youth-plan/candidates" not in (
        Path(fm_odds_web.__file__).read_text(encoding="utf-8")
    )
    captured = {}
    state = SimpleNamespace(
        memory_lock=nullcontext(),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        _owned_world_club_target=lambda team_id, **_kwargs: (
            "scope",
            {
                "game_date": "2028-12-02", "save_instance_id": "save-1",
                "selected_manager_id": 7, "manager": {"id": 7},
            },
            {"id": team_id, "address": "0x2000"},
        ),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {"installed": False},
    )
    monkeypatch.setattr(
        fm_odds_web, "apply_youth_plan",
        lambda *args, **kwargs: (
            captured.update(apply=(args, kwargs))
            or {"status": "completed", "applied": [{"id": 99}]}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )
    result = fm_odds_web.LocalOddsState.apply_world_club_youth_plan(state, {
        "team_id": 42, "kind": "academy_son",
    })
    assert result["status"] == "completed"
    assert "selected_player_id" not in captured["apply"][1]
    with pytest.raises(ValueError, match="不能手动指定"):
        fm_odds_web.LocalOddsState.apply_world_club_youth_plan(state, {
            "team_id": 42, "kind": "academy_son", "player_id": 99,
        })


def test_manual_youth_apply_runs_both_club_plans_son_first(monkeypatch) -> None:
    import fm_odds_web

    calls = []
    state = SimpleNamespace(
        memory_lock=nullcontext(),
        club_context={}, club_contexts={},
        _owned_world_club_target=lambda team_id, **_kwargs: (
            "scope",
            {
                "game_date": "2028-12-02", "save_instance_id": "save-1",
                "selected_manager_id": 7, "manager": {"id": 7},
                "managed_teams": [],
            },
            {"id": team_id, "name": "Youth FC", "address": "0x2000"},
        ),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {"installed": False},
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {
            "academy_son": {"status": "armed"},
            "golden_generation": {"status": "waiting"},
        },
    )

    def apply(_scope, _team_id, _address, kind, _game_date, **_kwargs):
        calls.append(kind)
        if kind == "academy_son":
            return {
                "status": "completed", "message": "儿子 Son 已生成",
                "applied": [{"id": 101, "name": "Son"}],
            }
        assert calls == ["academy_son", "golden_generation"]
        return {
            "status": "completed", "message": "小妖 A、B 已生成",
            "applied": [{"id": 102, "name": "A"}, {"id": 103, "name": "B"}],
        }

    monkeypatch.setattr(fm_odds_web, "apply_youth_plan", apply)

    result = fm_odds_web.LocalOddsState.apply_world_club_youth_plan(state, {
        "team_id": 42, "kind": "golden_generation",
    })

    assert calls == ["academy_son", "golden_generation"]
    assert result["status"] == "completed"
    assert result["message"] == "儿子 Son 已生成；小妖 A、B 已生成"
    assert [row["id"] for row in result["applied"]] == [101, 102, 103]
    assert list(result["plan_results"]) == [
        "academy_son", "golden_generation",
    ]


def test_unemployed_manager_can_apply_academy_son_plan(monkeypatch) -> None:
    import fm_odds_web

    captured = {}
    state = SimpleNamespace(
        memory_lock=nullcontext(),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        _owned_world_club_target=lambda team_id, **_kwargs: (
            "scope",
            {
                "game_date": "2028-12-02", "save_instance_id": "save-1",
                "selected_manager_id": 7,
                "manager": {"id": 7, "manager_address": "0x7100"},
                "managed_team": None, "managed_teams": [],
                "unemployed_manager_detected": True,
                "unemployed_confirmed": True,
            },
            {"id": team_id, "address": "0x2000"},
        ),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {"installed": False},
    )
    monkeypatch.setattr(
        fm_odds_web, "apply_youth_plan",
        lambda *args, **kwargs: (
            captured.update(apply=(args, kwargs))
            or {"status": "completed", "applied": [{"id": 99}]}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )

    result = fm_odds_web.LocalOddsState.apply_world_club_youth_plan(state, {
        "team_id": 42, "kind": "academy_son",
    })

    assert result["status"] == "completed"
    assert captured["apply"][1]["manager_id"] == 7
    assert captured["apply"][1]["manager_team_id"] == 0
    assert captured["apply"][1]["manager_team_address"] is None
    assert captured["apply"][1]["manager_address"] == "0x7100"


def test_national_only_manager_uses_live_assignment_not_stale_club_context(
    monkeypatch,
) -> None:
    import fm_odds_web

    captured = {}
    national = {
        "id": 86, "team_type": "national", "address": "0x8600",
        "manager_address": "0x7100",
    }
    state = SimpleNamespace(
        memory_lock=nullcontext(),
        club_context={
            "team": {"id": 8, "address": "0x8000", "manager_address": "0x7000"},
        },
        club_contexts={},
        _owned_world_club_target=lambda team_id, **_kwargs: (
            "scope",
            {
                "game_date": "2028-12-02", "save_instance_id": "save-1",
                "selected_manager_id": 7, "manager": {"id": 7},
                "managed_team": national, "managed_teams": [national],
            },
            {"id": team_id, "address": "0x2000"},
        ),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {"installed": False},
    )
    monkeypatch.setattr(
        fm_odds_web, "apply_youth_plan",
        lambda *args, **kwargs: (
            captured.update(apply=(args, kwargs))
            or {"status": "completed", "applied": [{"id": 99}]}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )

    fm_odds_web.LocalOddsState.apply_world_club_youth_plan(state, {
        "team_id": 42, "kind": "academy_son",
    })

    assert captured["apply"][1]["manager_team_id"] == 86
    assert captured["apply"][1]["manager_team_address"] == "0x8600"
    assert captured["apply"][1]["manager_address"] == "0x7100"


def test_academy_son_core_accepts_manager_without_team(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(youth_intake, "_load", lambda _scope: {
        "plans": {
            "42:academy_son": {"status": "armed"},
        },
    })
    monkeypatch.setattr(
        youth_intake, "finalize_academy_son_attributes",
        lambda *args, **kwargs: (
            captured.update(call=(args, kwargs))
            or {"finalized": True, "player": {"id": 99}}
        ),
    )
    monkeypatch.setattr(
        youth_intake, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "completed"}},
    )

    result = youth_intake.apply_youth_plan(
        "scope", 42, "0x2000", "academy_son", "2028-12-02",
        manager_id=7, manager_team_id=0, manager_address="0x7100",
    )

    assert result["status"] == "completed"
    assert captured["call"][1]["manager_team_id"] == 0


def test_manager_context_resolves_unemployed_manager_without_contract(
    monkeypatch,
) -> None:
    from tools import club_reader

    reader = SimpleNamespace(
        process=SimpleNamespace(pid=1234),
        module_base=0x100000,
        layout=SimpleNamespace(manager_person_offset=0x450),
    )
    monkeypatch.setattr(
        club_reader, "_human_manager_matches",
        lambda _reader, address, manager_id: address == 0x7100 and manager_id == 7,
    )

    assert club_reader._context_addresses(
        reader, 7, 0, manager_address="0x7100",
    ) == (0x7100, 0x7550, 0)


def test_youth_shortfall_refund_is_proportional_and_uses_source_transaction(
    monkeypatch,
) -> None:
    import fm_odds_web

    captured = {}
    plan = {
        "status": "armed",
        "armed_at": "2028-01-02T03:04:05+00:00",
        "config": {"count": 4, "min_pa": 170, "max_pa": 180},
        "payment": {
            "bank": 60, "wallet": 40, "total": 100,
            "transaction_id": "purchase-1",
        },
    }
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"golden_generation": plan},
    )
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda payment, kind, **details: (
            captured.update(payment=payment, kind=kind, details=details)
            or {**payment, "transaction_id": "refund-1", "already_refunded": False}
        ),
    )
    monkeypatch.setattr(
        fm_odds_web, "archive_youth_shortfall_refund_mail",
        lambda **kwargs: captured.setdefault("mail", kwargs) or {"id": "mail-1"},
    )
    monkeypatch.setattr(
        fm_odds_web, "complete_youth_plan_shortfall",
        lambda *args, **kwargs: (
            captured.update(completed=(args, kwargs))
            or {"status": "completed_with_refund", "last_result": {"message": "已退款"}}
        ),
    )

    settled = fm_odds_web.LocalOddsState._settle_youth_plan_shortfall(
        SimpleNamespace(), "scope", {"game_date": "2028-12-02"},
        {"id": 42, "name": "Youth FC"}, "golden_generation",
        {"shortfall": 2, "eligible_count": 2, "observed_count": 5},
    )

    assert captured["payment"] == {
        "bank": 30.0, "wallet": 20.0, "total": 50.0,
        "transaction_id": "purchase-1",
    }
    assert captured["details"]["missing_count"] == 2
    assert captured["mail"]["fulfilled_count"] == 2
    assert captured["completed"][1]["shortfall"] == 2
    assert settled["plan"]["status"] == "completed_with_refund"


def test_recovered_youth_plan_cannot_enter_refund_settlement(monkeypatch) -> None:
    import fm_odds_web

    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {
            "golden_generation": {
                "status": "waiting", "roster_recovery_only": True,
                "config": {"count": 3, "min_pa": 170, "max_pa": 180},
                "prior_refund_result": {"total": 150},
            },
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "refund_combined_funds",
        lambda *_args, **_kwargs: pytest.fail("recovery issued a second refund"),
    )

    with pytest.raises(RuntimeError, match="不能重复退款"):
        fm_odds_web.LocalOddsState._settle_youth_plan_shortfall(
            SimpleNamespace(), "scope", {"game_date": "2028-12-02"},
            {"id": 42, "name": "Youth FC"}, "golden_generation",
            {"shortfall": 1, "eligible_count": 2, "observed_count": 2},
        )


def test_manual_recovery_shortfall_stays_open_without_second_refund(
    monkeypatch,
) -> None:
    import fm_odds_web

    recovered = {
        "status": "waiting", "roster_recovery_only": True,
        "remaining_count": 1,
    }
    fake_state = SimpleNamespace(
        memory_lock=nullcontext(),
        club_contexts={},
        club_context={},
        _data_scope_id=lambda _output: "scope",
        _owned_world_club_target=lambda _team_id: (
            "scope",
            {
                "save_instance_id": "save-1", "game_date": "2028-12-02",
                "manager": {}, "managed_teams": [],
            },
            {"id": 42, "name": "Youth FC", "address": "0x1000"},
        ),
        _sync_youth_generation_plans=lambda *_args: {
            "installed": False, "targets": [], "son_target": None,
        },
        _settle_youth_plan_shortfall=lambda *_args, **_kwargs: pytest.fail(
            "recovery entered refund settlement"
        ),
    )
    monkeypatch.setattr(fm_odds_web, "managed_team_rows", lambda _output: [])
    monkeypatch.setattr(
        fm_odds_web, "apply_youth_plan",
        lambda *_args, **_kwargs: {
            "status": "insufficient", "shortfall": 1,
            "eligible_count": 2, "observed_count": 2,
            "plan": dict(recovered), "applied": [],
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"golden_generation": dict(recovered)},
    )

    result = fm_odds_web.LocalOddsState.apply_world_club_youth_plan(
        fake_state, {"team_id": 42, "kind": "golden_generation"},
    )

    assert result["status"] == "insufficient"
    assert result["plan"]["status"] == "waiting"
    assert "不会重复退款" in result["message"]
    assert "refund" not in result


def test_frontend_exposes_both_youth_plan_actions() -> None:
    root = (Path(__file__).parents[1] / "src")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    markup = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert '"academy_son"' in script
    assert '"golden_generation"' in script
    assert "/api/world-clubs/youth-plan/arm" in script
    assert "/api/world-clubs/youth-plan/cancel" in script
    assert "/api/world-clubs/youth-plan/apply" in script
    assert "/api/world-clubs/youth-plan/reopen" in script
    assert "/api/world-clubs/youth-plan/candidates" not in script
    assert "恢复误判计划" in script
    assert "不会再次扣款" in script
    assert "尝试手动生成" not in script
    assert "手动启动核验" in script
    assert "请确认游戏新的一年青训已生成，本轮青训名单中是否没有生成出计划内的人数和属性？如果确认将启动手动修正。" in script
    assert "是否没有生成出计划内的人数和属性？将启动手动修正。" not in script
    assert 'id="portfolio-youth-manual-unlock"' in script
    assert 'id="portfolio-youth-manual-candidate"' not in script
    assert "立即核验并补写本队全部计划" in script
    assert "自动选择儿子候选" not in script
    assert "生成期 Hook 已记录" in script
    assert "最终核验" in script
    assert "请选择要写入为儿子的青训球员" not in script
    assert "player_id:selectedPlayerId" not in script
    assert "当前轮已安排小妖青训" in script
    assert "把儿子送到此处历练" in script
    assert "sonCaPrices" not in script
    assert 'id="owned-youth-son-ca-band"' not in markup
    assert 'id="portfolio-son-ca"' not in script
    assert "preserve_ca:true" in script
    assert "随机 120～200" in markup
    assert '-7（PA 110～140）' in markup
    assert '-10（PA 170～200）' in markup
    assert '"120-150":15000000' in script
    assert '"170-200":10000000000' in script
    assert 'portfolioPaBandLabels' in script
    assert "名字留空时沿用候选原名" in script
    assert "姓氏及第一国籍继承玩家" in script
    assert "儿子已在（" in script
    assert "sonPlanElsewhere" in script
    assert "owned-youth-son-count" not in script
    assert "计划人数" in markup
    assert "请在青训预览日之前使用。" in markup
    assert 'id="owned-youth-plan-status"' in markup
    assert "正在校验俱乐部、扣款并启用青训功能" in script
    assert 'data-lucide="loader-circle"' in script
    assert "owned-youth-plan-check" not in markup
    assert 'id="owned-youth-son-name"' in markup
    assert "儿子名字（姓氏继承玩家）" in markup
    assert 'id="owned-youth-son-primary-position"' in markup
    assert 'id="owned-youth-son-secondary-position"' in markup
    assert "primary_position:sonPrimaryPosition.value" in script
    assert "secondary_position:sonSecondaryPosition.value" in script
    assert "primary_position:$(\"#portfolio-son-primary-position\").value" in script
    assert "secondary_position:$(\"#portfolio-son-secondary-position\").value" in script
    assert "主位置固定 20，副位置可不选；选择时固定 12，其余位置为 1，姓氏及第一国籍继承玩家" in script
    assert 'sonName.required = false' in script
    assert 'new Option("无副位置", "")' in script
    assert 'sonPositionOptions("", true)' in script
    assert 'pattern="[A-Za-z]+"' in markup
    assert 'id="owned-youth-son-pa-band"' in markup
    assert 'value="120-200">随机 120～200 · £500M' in markup
    assert 'value="150-165"' not in markup
    assert "portfolioGoldenPaPrices" in script
    assert "portfolioSonPaPrices" in script
    assert "const form = event.currentTarget;" in script
    assert "const submit = form.querySelector('button[type=\"submit\"]');" in script
    assert "const submit = event.currentTarget.querySelector" not in script
    assert "const cancelButton = event.currentTarget;" in script
    assert "已支付费用不会退还，本年度名额将恢复" in script
    assert "本年度名额不会退还" not in script
    assert "正式青训名单确认后写入 PA，保留游戏原生 CA" not in script
    assert 'const terminalPlan = ["completed", "completed_with_refund"].includes(rawPlan?.status);' in script
    assert "const recoverableTerminalPlan = terminalPlan" in script
    assert "const showTerminalPreview = terminalPlan" not in script
    assert "portfolioYouthTerminalPreview" not in script
    assert "本年名额已用完" in script
    assert "与儿子历练互不冲突" in script
    assert "可与儿子历练同时安排" not in script
    assert "此操作不可撤销，请确认" in script
    assert 'fmoddConfirm(confirmation, "确认", "确认投资")' in script


def test_existing_youth_plans_lock_their_saved_form_values() -> None:
    script = ((Path(__file__).parents[1] / "src") / "web" / "app.js").read_text(
        encoding="utf-8",
    )
    renderer = script.split("function renderPortfolioYouth", 1)[1].split(
        "async function loadPortfolioYouth", 1,
    )[0]

    assert 'const savedPlanConfig = plan ? (plan.config || {}) : null;' in renderer
    assert '"人数不足 · 已退款"' in renderer
    assert '$("#portfolio-youth-count").value = String(Number(savedPlanConfig.count || 1))' in renderer
    assert 'setSavedBand($("#portfolio-youth-pa"), savedPlanConfig.min_pa, savedPlanConfig.max_pa)' in renderer
    assert 'setSavedBand($("#portfolio-son-pa"), savedPlanConfig.min_pa, savedPlanConfig.max_pa)' in renderer
    assert 'Number(savedPlanConfig?.count || 0)' in renderer
    assert 'lockedFields.filter(Boolean).forEach((field) => { field.disabled = true; })' in renderer
    assert 'const activePlan = Boolean(plan && ["armed", "waiting"].includes(plan.status));' in renderer
    assert ': activePlan ? "计划已建立"' in renderer
    assert ': plan ? "已完成" : quotaExhausted ? "本年名额已用完" : "确认投资";' in renderer
    assert 'const terminalPlan = ["completed", "completed_with_refund"].includes(rawPlan?.status);' in renderer
    assert "const plan = terminalPlan && !recoverableTerminalPlan ? null : rawPlan;" in renderer
    assert "showTerminalPreview" not in renderer
    assert "const currentPlanYear" not in renderer
    assert "本轮最多可安排 ${goldenQuota.limit} 人" in renderer


def test_golden_generation_apply_uses_manual_roster_fallback(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": 1,
        "plans": {
            "42:golden_generation": {
                "team_id": 42,
                "kind": "golden_generation",
                "status": "armed",
                "game_key": "fm24",
                "config": {"count": 2, "min_pa": 170, "max_pa": 180},
            },
        },
    }), encoding="utf-8")

    monkeypatch.setattr(
        youth_intake, "finalize_golden_generation_attributes",
        lambda *_args, **_kwargs: {
            "finalized": True,
            "players": [{"id": 99, "name": "Manual", "pa": 175}],
            "remaining": 0,
        },
    )

    result = youth_intake.apply_youth_plan(
        "scope", 42, "0x1000", "golden_generation", "2028-12-01",
    )

    assert result["status"] == "completed"
    assert result["applied"][0]["pa"] == 175


def test_manual_golden_plan_waits_for_son_before_selecting_candidates(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": 1,
        "plans": {
            "42:academy_son": {
                "team_id": 42, "kind": "academy_son", "status": "waiting",
            },
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation", "status": "armed",
                "game_key": "fm26",
                "config": {"count": 2, "min_pa": 170, "max_pa": 180},
            },
        },
    }), encoding="utf-8")
    monkeypatch.setattr(
        youth_intake, "finalize_golden_generation_attributes",
        lambda *_args, **_kwargs: pytest.fail("小妖不应先于儿子计划执行"),
    )

    result = youth_intake.apply_youth_plan(
        "scope", 42, "0x1000", "golden_generation", "2028-12-01",
    )

    assert result["status"] == "waiting"
    assert result["message"] == "等待同俱乐部儿子计划先完成"


def test_manual_golden_plan_excludes_completed_son_and_reports_names(
    monkeypatch,
) -> None:
    captured = {}
    monkeypatch.setattr(youth_intake, "_load", lambda _scope: {
        "plans": {
            "42:academy_son": {
                "team_id": 42, "kind": "academy_son", "status": "completed",
                "processed_player_ids": [101],
                "ability_result": {
                    "player_id": 101, "player_name": "Son", "pa_before": 175,
                },
            },
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation", "status": "armed",
                "config": {"count": 2, "min_pa": 170, "max_pa": 180},
                "hook_remaining_count": 0,
            },
        },
    })
    monkeypatch.setattr(
        youth_intake, "finalize_golden_generation_attributes",
        lambda *args, **kwargs: (
            captured.update(args=args, kwargs=kwargs)
            or {
                "finalized": True,
                "players": [
                    {"id": 102, "name": "A"},
                    {"id": 103, "name": "B"},
                ],
                "remaining": 0,
            }
        ),
    )
    monkeypatch.setattr(
        youth_intake, "youth_plans_for_team",
        lambda *_args: {"golden_generation": {"status": "completed"}},
    )

    result = youth_intake.apply_youth_plan(
        "scope", 42, "0x1000", "golden_generation", "2028-12-01",
    )

    assert captured["kwargs"]["excluded_player_ids"] == {101}
    assert "consumed_slots" not in captured["kwargs"]
    assert result["message"] == "小妖 A、B 已生成"


def test_son_never_consumes_a_golden_slot() -> None:
    golden = {
        "config": {"count": 10, "min_pa": 190, "max_pa": 200},
        "hook_remaining_count": 0,
    }
    son = {
        "status": "completed",
        "ability_result": {"player_id": 101, "pa_before": 196},
    }

    assert youth_intake.golden_generation_son_overlap_count(golden, son) == 0
    son["ability_result"]["pa_before"] = 189
    assert youth_intake.golden_generation_son_overlap_count(golden, son) == 0
    son["ability_result"]["pa_before"] = 196
    son["status"] = "waiting"
    assert youth_intake.golden_generation_son_overlap_count(golden, son) == 0


def test_legacy_son_overlap_refund_cannot_reclassify_a_golden_plan(
    monkeypatch, tmp_path,
) -> None:
    path = tmp_path / "youth_intake_plans.json"
    monkeypatch.setattr(youth_intake, "_plans_path", lambda _scope: path)
    path.write_text(json.dumps({
        "schema_version": youth_intake.SCHEMA_VERSION,
        "plans": {
            "42:academy_son": {
                "team_id": 42, "kind": "academy_son", "status": "completed",
                "ability_result": {"player_id": 101, "pa_before": 196},
            },
            "42:golden_generation": {
                "team_id": 42, "kind": "golden_generation",
                "status": "completed", "hook_remaining_count": 0,
                "config": {"count": 3, "min_pa": 190, "max_pa": 200},
                "processed_player_ids": [102, 103, 104],
                "ability_results": [
                    {"id": 102, "pa_before": 150, "pa": 195},
                    {"id": 103, "pa_before": 194, "pa": 194},
                    {"id": 104, "pa_before": 197, "pa": 197},
                ],
                "planned_ability_targets": {
                    "102": 195, "103": 194, "104": 197,
                },
                "planned_ability_originals": {
                    "102": 150, "103": 194, "104": 197,
                },
                "verification_result": {"verified_count": 3},
            },
        },
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="未确认儿子占用了同队小妖 Hook 名额"):
        youth_intake.complete_golden_son_overlap_refund(
            "scope", 42,
            refund={"bank": 50, "wallet": 0, "total": 50},
        )

    raw = youth_intake._load("scope")["plans"]["42:golden_generation"]
    assert raw["status"] == "completed"
    assert raw["processed_player_ids"] == [102, 103, 104]
    assert [row["id"] for row in raw["ability_results"]] == [102, 103, 104]


def test_youth_completion_mail_is_named_and_idempotent(monkeypatch) -> None:
    import fm_odds_web

    records = []
    monkeypatch.setattr(fm_odds_web, "load_mail", lambda: list(records))

    def save(saved):
        records[:] = list(saved)

    monkeypatch.setattr(fm_odds_web, "save_mail", save)
    payload = {
        "armed_at": "plan-1", "kind": "golden_generation", "team_id": 42,
        "team_name": "柏太阳神", "game_date": "2028-12-02",
        "player_names": ["A", "B"],
    }

    first = fm_odds_web.archive_youth_completion_mail(**payload)
    second = fm_odds_web.archive_youth_completion_mail(**payload)

    assert first["title"] == "小妖 A、B 已生成"
    assert first["message"] == "小妖 A、B 已生成"
    assert second is None
    assert len(records) == 1


def test_completion_mail_failure_does_not_turn_successful_write_into_failure(
    monkeypatch,
) -> None:
    import fm_odds_web

    state = SimpleNamespace(
        memory_lock=nullcontext(),
        club_context={}, club_contexts={},
        _owned_world_club_target=lambda team_id, **_kwargs: (
            "scope",
            {
                "game_date": "2028-12-02", "save_instance_id": "save-1",
                "selected_manager_id": 7, "manager": {"id": 7},
            },
            {"id": team_id, "name": "柏太阳神", "address": "0x2000"},
        ),
        _data_scope_id=lambda _output: "scope",
        _sync_youth_generation_plans=lambda *_args: {"installed": False},
    )
    completed_plan = {
        "armed_at": "plan-1", "status": "completed",
        "ability_result": {"player_id": 101, "player_name": "Son"},
    }
    monkeypatch.setattr(
        fm_odds_web, "apply_youth_plan",
        lambda *_args, **_kwargs: {
            "status": "completed", "applied": [{"id": 101}],
            "message": "儿子 Son 已生成", "plan": completed_plan,
        },
    )
    monkeypatch.setattr(
        fm_odds_web, "archive_youth_completion_mail",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("mail unavailable")),
    )
    monkeypatch.setattr(
        fm_odds_web, "youth_plans_for_team",
        lambda *_args: {"academy_son": {"status": "armed"}},
    )

    result = fm_odds_web.LocalOddsState.apply_world_club_youth_plan(state, {
        "team_id": 42, "kind": "academy_son",
    })

    assert result["status"] == "completed"
    assert result["message"] == "儿子 Son 已生成"
    assert result["mail_error"] == "mail unavailable"


def test_youth_baseline_failure_does_not_publish_partial_snapshot(monkeypatch):
    module = SimpleNamespace(base_address=0x100000)
    layout = SimpleNamespace(key="fm26", module_name="game_plugin.dll",
                             module=lambda _process: module)
    def incomplete():
        raise RuntimeError("native person UID baseline contains an unreadable object")
    monkeypatch.setattr(youth_intake, "select_process_layout", lambda: (26, "fm.exe", layout))
    monkeypatch.setattr(youth_intake, "open_process", lambda *_a: nullcontext(object()))
    monkeypatch.setattr(youth_intake, "Reader", lambda *_a: SimpleNamespace())
    monkeypatch.setattr(youth_intake, "_attach_database_index", lambda _r: SimpleNamespace(
        person_uids_snapshot=incomplete))
    monkeypatch.setattr(youth_intake, "_roster_snapshot", lambda *_a: pytest.fail(
        "partial baseline must not proceed to plan creation"))
    with pytest.raises(RuntimeError, match="全球球员 UID 基线") as failure:
        youth_intake.read_youth_roster_snapshot(42, "0x1000", "2030-12-01")
    assert "unreadable" in str(failure.value.__cause__)
