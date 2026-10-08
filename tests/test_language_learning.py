import threading
from unittest.mock import MagicMock, call, patch

import pytest

from tools import club_economy
from fm_odds_web import LocalOddsState


def language_state(*, auto=None, plans=None, balance=0.0):
    return {
        "general_balance": float(balance),
        "general_balance_minor": int(round(float(balance) * 100)),
        "transactions": [],
        "owned_activity_centres": ["talk_room"],
        "owned_activity_facilities": ["language_classroom"],
        "activity_floor_cooldowns": {},
        "activity_centre_rules_version": club_economy.ACTIVITY_CENTRE_RULES_VERSION,
        "language_learning": {
            "auto": dict(auto or {"enabled": False}),
            "plans": dict(plans or {}),
            "history": [],
        },
    }


def player(player_id, level=0, *, language_id=77, name="Player"):
    languages = [] if level <= 0 else [{
        "id": language_id, "name": "英语", "proficiency": level, "maximum": 10,
    }]
    return {
        "id": player_id, "name": name, "address": hex(0x1000 + player_id),
        "team_id": 9, "team_name": "Club", "team_type": "club",
        "languages": languages,
    }


def staff(staff_id, level=0, *, language_id=77, name="Staff"):
    row = player(staff_id, level, language_id=language_id, name=name)
    row.update({"role": "教练", "team_type": "club"})
    return row


def test_language_level_schedule_totals_exactly_300_days():
    assert club_economy.LANGUAGE_LEVEL_DAYS == (
        7, 10, 15, 20, 25, 30, 35, 43, 52, 63,
    )
    assert sum(club_economy.LANGUAGE_LEVEL_DAYS) == 300
    plan = {"started_on": "2026-01-01", "start_level": 0}
    assert club_economy._planned_language_level(plan, "2026-01-07")[0] == 0
    assert club_economy._planned_language_level(plan, "2026-01-08")[0] == 1
    assert club_economy._planned_language_level(plan, "2026-01-18")[0] == 2
    assert club_economy._planned_language_level(plan, "2026-10-28")[0] == 10


def test_instant_language_prices_are_proportional_thousand_multiples():
    assert tuple(
        int(club_economy.language_instant_level_price(level)) for level in range(10)
    ) == (22_000, 32_000, 48_000, 63_000, 79_000, 95_000, 111_000, 137_000, 165_000, 200_000)
    assert all(
        club_economy.language_instant_level_price(level) % 1_000 == 0
        for level in range(10)
    )
    assert club_economy.language_instant_max_price(9) == 200_000
    assert club_economy.language_instant_max_price(8) == 365_000
    assert club_economy.language_instant_max_price(0) == 952_000


def test_welfare_status_keeps_paid_language_quotes_when_free_services_is_stale():
    state = language_state()
    state["free_services"] = True
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_settings", return_value={}),
        patch.object(
            club_economy, "local_purchase_price",
            side_effect=lambda value, *_: float(value),
        ),
    ):
        status = club_economy.welfare_status()

    assert status["free_services"] is True
    assert status["language_learning"]["instant_prices"]["0"] == 22_000
    assert status["language_learning"]["instant_prices"]["9"] == 200_000
    assert status["language_learning"]["instant_max_prices"]["0"] == 952_000


def test_manual_language_learning_enrolls_once_without_floor_cooldown():
    state = language_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
    ):
        first = club_economy.enroll_language_learning(
            77, "英语", "2026-01-01", [player(1)], source="manual",
        )
        second = club_economy.enroll_language_learning(
            77, "英语", "2026-01-01", [player(1)], source="manual",
        )

    assert first["enrolled"] == [1]
    assert second["enrolled"] == []
    assert state["activity_floor_cooldowns"] == {}
    save.assert_called_once_with(state)


def test_automatic_language_learning_keeps_monitoring_new_players():
    state = language_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        started = club_economy.configure_automatic_language_learning(
            77, "英语", True, "2026-01-01", [player(1, 10), player(2, 4)],
        )
        refreshed = club_economy.process_language_learning(
            "2026-01-02", [player(1, 10), player(2, 4), player(3, 0)],
            MagicMock(), MagicMock(),
        )

    assert started["enrolled"] == [2]
    assert refreshed["enrolled"] == [3]
    assert state["language_learning"]["auto"]["enabled"] is True
    assert "player:3:77" in state["language_learning"]["plans"]
    assert state["activity_floor_cooldowns"] == {}


def test_player_and_staff_language_plans_with_same_uid_do_not_collide():
    state = language_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        club_economy.enroll_language_learning(
            77, "英语", "2026-01-01", [player(1)], source="manual",
        )
        club_economy.enroll_staff_language_learning(
            77, "英语", "2026-01-01", [staff(1)], source="manual",
        )

    plans = state["language_learning"]["plans"]
    assert plans["player:1:77"]["person_kind"] == "player"
    assert plans["staff:1:77"]["person_kind"] == "staff"
    assert plans["player:1:77"]["player_name"] == "Player"
    assert plans["staff:1:77"]["staff_name"] == "Staff"


def test_player_and_staff_automatic_language_configs_are_independent():
    state = language_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        club_economy.configure_automatic_language_learning(
            77, "英语", True, "2026-01-01", [player(1, 10)],
        )
        club_economy.configure_automatic_staff_language_learning(
            88, "西班牙语", True, "2026-01-01", [staff(2, 10, language_id=88)],
        )
        club_economy.configure_automatic_staff_language_learning(
            88, "西班牙语", False, "2026-01-02", [],
        )

    automatic = state["language_learning"]["auto"]
    assert automatic["enabled"] is True
    assert automatic["player_enabled"] is True
    assert automatic["player_language_id"] == 77
    assert automatic["staff_enabled"] is False
    assert automatic["staff_language_id"] == 88


def test_staff_course_becomes_unavailable_without_writing_after_departure():
    state = language_state(plans={
        "staff:1:77": {
            "person_kind": "staff", "person_id": 1, "staff_id": 1,
            "person_name": "Staff", "language_id": 77,
            "language_name": "英语", "source": "manual", "status": "active",
            "started_on": "2026-01-01", "start_level": 0, "current_level": 0,
        },
    })
    apply_staff = MagicMock()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        club_economy.process_language_learning(
            "2026-01-08", [], MagicMock(), MagicMock(),
            staff=[], apply_staff_level=apply_staff,
            rollback_staff_level=MagicMock(),
        )

    plan = state["language_learning"]["plans"]["staff:1:77"]
    assert plan["status"] == "target_unavailable"
    assert "重新确认" in plan["last_error"]
    apply_staff.assert_not_called()


def test_staff_course_uses_shared_scheduler_when_target_is_current():
    state = language_state(plans={
        "staff:1:77": {
            "person_kind": "staff", "person_id": 1, "staff_id": 1,
            "person_name": "Staff", "language_id": 77,
            "language_name": "英语", "source": "manual", "status": "active",
            "started_on": "2026-01-01", "start_level": 0, "current_level": 0,
        },
    })
    target = staff(1)
    apply_staff = MagicMock(return_value={
        "before": 0, "after": 1, "modified": True, "_rollback": {},
    })
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        result = club_economy.process_language_learning(
            "2026-01-08", [], MagicMock(), MagicMock(),
            staff=[target], apply_staff_level=apply_staff,
            rollback_staff_level=MagicMock(),
        )

    apply_staff.assert_called_once_with(target, 77, 1)
    assert result["advanced"][0]["person_kind"] == "staff"
    assert state["language_learning"]["plans"]["staff:1:77"]["current_level"] == 1


def test_cancelled_automatic_course_is_not_reenrolled_on_refresh():
    state = language_state(
        auto={
            "enabled": True, "language_id": 77, "language_name": "英语",
            "excluded_player_ids": [],
        },
        plans={
            "1:77": {
                "player_id": 1, "player_name": "Player", "language_id": 77,
                "language_name": "英语", "source": "auto", "status": "active",
                "started_on": "2026-01-01", "start_level": 2, "current_level": 2,
            },
        },
    )
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        cancelled = club_economy.cancel_language_learning(77, "2026-01-02", [1])
        refreshed = club_economy.process_language_learning(
            "2026-01-03", [player(1, 2)], MagicMock(), MagicMock(),
        )

    assert cancelled["cancelled"] == [1]
    assert state["language_learning"]["plans"]["1:77"]["status"] == "cancelled"
    assert state["language_learning"]["auto"]["excluded_player_ids"] == [1]
    assert refreshed["enrolled"] == []


def test_instant_language_upgrade_charges_level_price_and_completes_active_plan():
    state = language_state(balance=500_000, plans={
        "1:77": {
            "player_id": 1, "player_name": "Player", "language_id": 77,
            "language_name": "英语", "source": "manual", "status": "active",
            "started_on": "2026-01-01", "start_level": 9, "current_level": 9,
        },
    })
    target = player(1, 9)
    memory_result = {
        "before": 9, "after": 10, "modified": True,
        "_rollback": {"kind": "value"},
    }
    apply_level = MagicMock(return_value=memory_result)
    rollback = MagicMock()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
        patch.object(club_economy, "load_wallet", return_value={"balance": 0.0}),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "local_purchase_price", side_effect=lambda value, *_: float(value)),
    ):
        result = club_economy.advance_language_learning_now(
            77, "英语", "2026-01-02", [target], apply_level, rollback,
        )

    apply_level.assert_called_once_with(target, 77, 10)
    rollback.assert_not_called()
    save.assert_called_once_with(state)
    assert result["payment"]["total"] == 200_000
    assert state["general_balance_minor"] == 300_000 * 100
    assert state["language_learning"]["plans"]["1:77"]["status"] == "completed"
    assert target["languages"][0]["proficiency"] == 10


def test_instant_language_max_charges_all_remaining_levels_and_writes_ten_once():
    state = language_state(balance=2_000_000, plans={
        "1:77": {
            "player_id": 1, "player_name": "First", "language_id": 77,
            "language_name": "英语", "source": "manual", "status": "active",
            "started_on": "2026-01-01", "start_level": 2, "current_level": 2,
        },
    })
    first = player(1, 2, name="First")
    second = player(2, 8, name="Second")
    apply_level = MagicMock(side_effect=[
        {"before": 2, "after": 10, "modified": True, "_rollback": {"kind": "value"}},
        {"before": 8, "after": 10, "modified": True, "_rollback": {"kind": "value"}},
    ])
    rollback = MagicMock()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
        patch.object(club_economy, "load_wallet", return_value={"balance": 0.0}),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "local_purchase_price", side_effect=lambda value, *_: float(value)),
    ):
        result = club_economy.advance_language_learning_now(
            77, "英语", "2026-01-02", [first, second], apply_level, rollback,
            to_maximum=True,
        )

    assert apply_level.call_args_list == [
        call(first, 77, 10),
        call(second, 77, 10),
    ]
    rollback.assert_not_called()
    save.assert_called_once_with(state)
    assert result["payment"]["total"] == 1_263_000
    assert [row["price"] for row in result["upgraded"]] == [898_000, 365_000]
    assert state["general_balance_minor"] == 737_000 * 100
    assert state["language_learning"]["plans"]["1:77"]["status"] == "completed"
    assert first["languages"][0]["proficiency"] == 10
    assert second["languages"][0]["proficiency"] == 10


def test_instant_language_upgrade_rolls_back_memory_when_account_save_fails():
    state = language_state(balance=100_000)
    memory_result = {
        "before": 0, "after": 1, "modified": True,
        "_rollback": {"kind": "insert"},
    }
    rollback = MagicMock()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy", side_effect=OSError("disk full")),
        patch.object(club_economy, "load_wallet", return_value={"balance": 0.0}),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "local_purchase_price", side_effect=lambda value, *_: float(value)),
    ):
        with pytest.raises(OSError, match="disk full"):
            club_economy.advance_language_learning_now(
                77, "英语", "2026-01-02", [player(1, 0)],
                MagicMock(return_value=memory_result), rollback,
            )

    rollback.assert_called_once_with(memory_result)


def test_language_learning_crosses_all_due_levels_in_one_refresh():
    state = language_state(plans={
        "1:77": {
            "player_id": 1, "player_name": "Player", "language_id": 77,
            "language_name": "英语", "source": "manual", "status": "active",
            "started_on": "2026-01-01", "start_level": 0, "current_level": 0,
        },
    })
    apply_level = MagicMock(return_value={"before": 0, "after": 10, "_rollback": {}})
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
    ):
        result = club_economy.process_language_learning(
            "2026-10-28", [player(1, 0)], apply_level, MagicMock(),
        )

    apply_level.assert_called_once()
    assert apply_level.call_args.args[1:] == (77, 10)
    assert result["completed"] == [1]
    assert state["language_learning"]["plans"]["1:77"]["status"] == "completed"


def test_language_memory_changes_roll_back_when_progress_cannot_be_saved():
    state = language_state(plans={
        "1:77": {
            "player_id": 1, "player_name": "Player", "language_id": 77,
            "language_name": "英语", "source": "manual", "status": "active",
            "started_on": "2026-01-01", "start_level": 0, "current_level": 0,
        },
    })
    memory_result = {"before": 0, "after": 1, "_rollback": {"kind": "insert"}}
    rollback = MagicMock()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy", side_effect=OSError("disk full")),
    ):
        with pytest.raises(OSError, match="disk full"):
            club_economy.process_language_learning(
                "2026-01-08", [player(1, 0)],
                MagicMock(return_value=memory_result), rollback,
            )

    rollback.assert_called_once_with(memory_result)


def test_language_api_auto_mode_deduplicates_dual_coaching_rosters():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = MagicMock()
    state.output = {"game_date": "2026-01-01"}
    club_player = player(1, 2)
    national_duplicate = player(1, 2)
    national_only = player(2, 0)
    state.club_profile = None
    state.club_profiles = {
        9: {"team": {"id": 9, "name": "Club", "team_type": "club"}, "players": [club_player]},
        10: {"team": {"id": 10, "name": "Nation", "team_type": "national"}, "players": [national_duplicate, national_only]},
    }
    configured = MagicMock(return_value={"enrolled": [1, 2], "skipped": []})
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-01-01"}),
        patch("fm_odds_web.assert_activity_facility_unlocked"),
        patch("fm_odds_web.native_language_catalog", return_value=[{"id": 77, "name": "英语"}]),
        patch("fm_odds_web.configure_automatic_language_learning", configured),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        result = state.update_language_learning({
            "mode": "auto", "language_id": 77, "enabled": True,
        })

    candidates = configured.call_args.args[4]
    assert [row["id"] for row in candidates] == [1, 2]
    assert candidates[0]["team_id"] == 9
    assert result["enrolled"] == [1, 2]


def test_language_api_instant_mode_uses_selected_managed_player_and_native_writer():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = MagicMock()
    state.output = {"game_date": "2026-01-01"}
    target = player(1, 4)
    state.club_profile = None
    state.club_profiles = {
        9: {"team": {"id": 9, "name": "Club", "team_type": "club"}, "players": [target]},
    }
    advanced = MagicMock(return_value={
        "upgraded": [{"player_id": 1, "before": 4, "after": 5, "price": 79_000}],
        "payment": {"total": 79_000},
    })
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-01-01"}),
        patch("fm_odds_web.assert_activity_facility_unlocked"),
        patch("fm_odds_web.native_language_catalog", return_value=[{"id": 77, "name": "English"}]),
        patch("fm_odds_web.advance_language_learning_now", advanced),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        result = state.update_language_learning({
            "mode": "instant", "language_id": 77,
            "players": [{"player_id": 1, "team_id": 9}],
        })

    assert advanced.call_args.args[:4] == (77, "English", "2026-01-01", [target])
    assert callable(advanced.call_args.args[4])
    assert result["payment"]["total"] == 79_000


def language_api_state_with_staff():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = MagicMock()
    state.output = {"game_date": "2026-01-01"}
    target = staff(1, 3)
    state.club_profile = None
    state.club_profiles = {
        9: {
            "team": {"id": 9, "name": "Club", "team_type": "club"},
            "staff": [target],
        },
    }
    return state, target


def test_staff_language_api_blocks_unverified_writes_before_hydration():
    state, _target = language_api_state_with_staff()
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-01-01"}),
        patch("fm_odds_web.assert_activity_facility_unlocked"),
        patch("fm_odds_web.native_language_catalog", return_value=[{"id": 77, "name": "English"}]),
        patch("fm_odds_web.staff_language_learning_capability", return_value={
            "implemented": True, "write_enabled": False, "reason": "待实机验证",
        }),
        patch.object(state, "_hydrate_staff_languages") as hydrate,
    ):
        with pytest.raises(RuntimeError, match="待实机验证"):
            state.update_language_learning({
                "mode": "manual", "person_kind": "staff", "language_id": 77,
                "targets": [{"person_kind": "staff", "person_id": 1, "team_id": 9}],
            })

    hydrate.assert_not_called()


def test_staff_language_api_routes_verified_current_team_target():
    state, target = language_api_state_with_staff()
    enrolled = MagicMock(return_value={"enrolled": [1], "skipped": []})

    def hydrate(rows):
        return [{**row, "languages": list(target["languages"])} for row in rows]

    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-01-01"}),
        patch("fm_odds_web.assert_activity_facility_unlocked"),
        patch("fm_odds_web.native_language_catalog", return_value=[{"id": 77, "name": "English"}]),
        patch("fm_odds_web.staff_language_learning_capability", return_value={
            "implemented": True, "write_enabled": True, "reason": "",
        }),
        patch.object(state, "_hydrate_staff_languages", side_effect=hydrate),
        patch("fm_odds_web.enroll_staff_language_learning", enrolled),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        result = state.update_language_learning({
            "mode": "manual", "person_kind": "staff", "language_id": 77,
            "targets": [{"person_kind": "staff", "person_id": 1, "team_id": 9}],
        })

    candidates = enrolled.call_args.args[3]
    assert [(row["id"], row["team_id"]) for row in candidates] == [(1, 9)]
    assert candidates[0]["languages"] == target["languages"]
    assert result["enrolled"] == [1]


def test_staff_language_api_rejects_stale_team_identity():
    state, _target = language_api_state_with_staff()
    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-01-01"}),
        patch("fm_odds_web.assert_activity_facility_unlocked"),
        patch("fm_odds_web.native_language_catalog", return_value=[{"id": 77, "name": "English"}]),
        patch("fm_odds_web.staff_language_learning_capability", return_value={
            "implemented": True, "write_enabled": True, "reason": "",
        }),
    ):
        with pytest.raises(ValueError, match="已经不属于当前执教队伍"):
            state.update_language_learning({
                "mode": "manual", "person_kind": "staff", "language_id": 77,
                "targets": [{"person_kind": "staff", "person_id": 1, "team_id": 10}],
            })
