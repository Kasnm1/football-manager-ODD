from contextlib import nullcontext
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools import club_economy, club_reader
from fm_odds_web import LocalOddsState


def economy_state(*, owned=None, facilities=None, balance=0.0):
    return {
        "general_balance": float(balance),
        "transactions": [],
        "owned_activity_centres": list(owned or ()),
        "owned_activity_facilities": list(facilities or ()),
        "activity_floor_cooldowns": {},
        "activity_centre_rules_version": club_economy.ACTIVITY_CENTRE_RULES_VERSION,
        "media_activity_daily_usage": {},
        "activity_progress": {},
        "activity_daily_usage": {},
        "activity_history": [],
        "psychological_counseling": {},
        "free_services": False,
        "medical_treatments": {},
    }


def test_development_runtime_unlocks_all_activity_content_without_changing_save():
    state = economy_state()
    club_economy.configure_activity_centre_development_unlocks(True)
    try:
        with (
            patch.object(club_economy, "load_economy", return_value=state),
            patch.object(club_economy, "load_settings", return_value={}),
        ):
            status = club_economy.welfare_status()
            media = club_economy.assert_activity_centre_unlocked("media")
            language_room = club_economy.assert_activity_facility_unlocked(
                "language_classroom"
            )
            intelligence = club_economy.match_intelligence_status([])
    finally:
        club_economy.configure_activity_centre_development_unlocks(False)

    assert all(row["available"] for row in status["activity_centres"].values())
    assert all(row["owned"] for row in status["activity_centres"].values())
    assert all(row["owned"] for row in status["activity_facilities"].values())
    assert media["name"] == "媒体中心"
    assert language_room["name"] == "语言教室"
    assert intelligence["unlocked"] is True
    assert state["owned_activity_centres"] == []
    assert state["owned_activity_facilities"] == []


def test_first_and_second_floor_purchase_prices_are_persisted():
    first_floor = economy_state(balance=2_000_000)
    with (
        patch.object(club_economy, "load_economy", return_value=first_floor),
        patch.object(club_economy, "save_economy") as save_first,
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.purchase_activity_centre("entertainment")

    assert result["payment"]["total"] == 2_000_000
    assert first_floor["general_balance"] == 0
    assert first_floor["owned_activity_centres"] == ["entertainment"]
    save_first.assert_called_once_with(first_floor)

    second_floor = economy_state(balance=2_000_000)
    with (
        patch.object(club_economy, "load_economy", return_value=second_floor),
        patch.object(club_economy, "save_economy") as save_second,
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.purchase_activity_centre("talk_room")

    assert result["payment"]["total"] == 2_000_000
    assert second_floor["general_balance"] == 0
    assert second_floor["owned_activity_centres"] == ["talk_room"]
    save_second.assert_called_once_with(second_floor)


def test_fifth_floor_is_intelligence_centre_and_costs_50m():
    option = club_economy.ACTIVITY_CENTRES["intelligence"]
    state = economy_state(balance=50_000_000)

    assert option["name"] == "情报中心"
    assert option["floor"] == "5F"
    assert option["price"] == 50_000_000.0

    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.purchase_activity_centre("intelligence")

    assert result["payment"]["total"] == 50_000_000
    assert state["general_balance"] == 0
    assert state["owned_activity_centres"] == ["intelligence"]
    save.assert_called_once_with(state)


@pytest.mark.parametrize("centre", ["media"])
def test_third_floor_is_unavailable_and_never_charge(centre):
    state = economy_state(balance=100_000_000)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        with pytest.raises(ValueError, match="暂未开启"):
            club_economy.purchase_activity_centre(centre)

    assert state["general_balance"] == 100_000_000
    save.assert_not_called()


def test_fourth_floor_costs_10m_and_includes_nationality_processing():
    option = club_economy.ACTIVITY_CENTRES["international"]
    state = economy_state(balance=10_000_000)

    assert option["available"] is True
    assert option["price"] == 10_000_000.0
    assert club_economy.ACTIVITY_FACILITIES["naturalization"]["included_with_centre"] is True

    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.purchase_activity_centre("international")
        facility = club_economy.assert_activity_facility_unlocked("naturalization")

    assert result["payment"]["total"] == 10_000_000
    assert state["general_balance"] == 0
    assert state["owned_activity_centres"] == ["international"]
    assert facility["name"] == "国籍处理中心"
    save.assert_called_once_with(state)


def test_international_nations_uses_verified_save_identity_for_world_scan_cache():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.output = {
        "save_instance_id": "save-verified",
        "account_scope_id": "account-wallet",
    }
    state._bind_current_save = MagicMock()
    state._data_scope_id = MagicMock(return_value="account-wallet")
    state._world_club_native_cache = MagicMock(return_value={
        "addresses_current": True,
        "nations": [
            {"id": 110, "name": "中国", "continent": "亚洲", "address": "0x110"},
            {"id": 392, "name": "日本", "continent": "亚洲", "address": "0x392"},
        ],
    })

    with patch("fm_odds_web.assert_activity_centre_unlocked"):
        response = state.international_nations()

    state._world_club_native_cache.assert_called_once_with("save-verified")
    assert {row["name"] for row in response["nations"]} == {"中国", "日本"}


def test_international_activity_uses_verified_save_identity_for_world_scan_cache():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "save_instance_id": "save-verified",
        "account_scope_id": "account-wallet",
        "game_date": "2028-01-01",
    }
    state._bind_current_save = MagicMock()
    state._data_scope_id = MagicMock(return_value="account-wallet")
    state._world_club_native_cache = MagicMock(return_value={
        "addresses_current": True,
        "nations": [{"id": 9, "name": "Nation", "address": "0x9000"}],
    })
    player = {
        "id": 42, "name": "Player", "address": "0x4200", "nationality_id": 7,
    }
    state._live_player_profile = MagicMock(return_value=(
        {"team": {"id": 3, "team_type": "club"}}, player,
    ))

    with (
        patch("fm_odds_web.assert_activity_centre_unlocked"),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2028-01-01"}),
        patch("fm_odds_web.free_services_enabled", return_value=True),
        patch("fm_odds_web.update_world_player_primary_nationality", return_value={
            "player_id": 42, "before_nation_id": 7, "nation_id": 9, "changed": True,
        }),
        patch("fm_odds_web.record_international_activity", return_value={"id": "history-1"}),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        state.international_activity({
            "player_id": 42, "team_id": 3, "nation_id": 9,
            "scope": "club", "slot": "primary", "expected_nation_id": 7,
        })

    state._world_club_native_cache.assert_called_once_with("save-verified")


def test_international_activity_can_remove_secondary_nationality_without_charge():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-verified", "game_date": "2028-01-01"}
    state._bind_current_save = MagicMock()
    state._data_scope_id = MagicMock(return_value="save-verified")
    state._world_club_native_cache = MagicMock(return_value={
        "addresses_current": True,
        "nations": [{"id": 9, "name": "Nation", "address": "0x9000"}],
    })
    player = {
        "id": 42, "name": "Player", "address": "0x4200",
        "other_nationalities": [{"id": 9, "name": "Nation"}],
    }
    state._live_player_profile = MagicMock(return_value=(
        {"team": {"id": 3, "team_type": "club"}}, player,
    ))

    with (
        patch("fm_odds_web.assert_activity_centre_unlocked"),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2028-01-01"}),
        patch("fm_odds_web.remove_player_second_nationality", return_value={
            "player_id": 42, "nation_id": 9, "removed": True,
        }) as remove,
        patch("fm_odds_web.record_international_activity", return_value={"id": "history-1"}),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
        patch("fm_odds_web.charge_combined_funds") as charge,
    ):
        response = state.international_activity({
            "action": "remove_secondary", "player_id": 42, "team_id": 3,
            "nation_id": 9, "scope": "club",
        })

    remove.assert_called_once_with(42, "0x4200", 9, "0x9000")
    charge.assert_not_called()
    assert response["price_gbp"] == 0
    assert player["other_nationalities"] == []
    assert player["second_nationality"] is None


@pytest.mark.parametrize(
    ("scope", "expected_price"),
    [("club", 1_000_000.0), ("world", 100_000_000.0)],
)
def test_international_activity_charges_server_price_per_player(scope, expected_price):
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"game_date": "2028-01-01"}
    state._bind_current_save = MagicMock()
    state._data_scope_id = MagicMock(return_value="save-1")
    state._world_club_native_cache = MagicMock(return_value={
        "addresses_current": True,
        "nations": [{"id": 9, "name": "Nation", "address": "0x9000"}],
    })
    player = {
        "id": 42, "name": "Player", "address": "0x4200", "nationality_id": 7,
    }
    profile = {"team": {"id": 3, "team_type": "club"}}
    state._live_player_profile = MagicMock(return_value=(profile, player))
    payment = {
        "bank": expected_price, "wallet": 0.0, "total": expected_price,
        "transaction_id": "payment-1",
    }

    with (
        patch("fm_odds_web.assert_activity_centre_unlocked"),
        patch("fm_odds_web.read_world_player_profile", return_value=player),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2028-01-01"}),
        patch("fm_odds_web.free_services_enabled", return_value=False),
        patch("fm_odds_web.local_purchase_price", side_effect=float),
        patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
        patch("fm_odds_web.update_world_player_primary_nationality", return_value={
            "player_id": 42, "before_nation_id": 7, "nation_id": 9, "changed": True,
        }),
        patch("fm_odds_web.record_international_activity", return_value={"id": "history-1"}),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        response = state.international_activity({
            "player_id": 42, "team_id": 3, "nation_id": 9,
            "scope": scope, "slot": "primary", "expected_nation_id": 7,
        })

    assert charge.call_args.args[:2] == (expected_price, "nationality_processing")
    assert charge.call_args.kwargs["scope"] == scope
    assert response["price_gbp"] == expected_price
    assert response["payment"] == payment


def test_international_activity_refunds_when_native_write_fails():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"game_date": "2028-01-01"}
    state._bind_current_save = MagicMock()
    state._data_scope_id = MagicMock(return_value="save-1")
    state._world_club_native_cache = MagicMock(return_value={
        "addresses_current": True,
        "nations": [{"id": 9, "name": "Nation", "address": "0x9000"}],
    })
    player = {
        "id": 42, "name": "Player", "address": "0x4200", "nationality_id": 7,
    }
    state._live_player_profile = MagicMock(return_value=(
        {"team": {"id": 3, "team_type": "club"}}, player,
    ))
    payment = {
        "bank": 1_000_000.0, "wallet": 0.0, "total": 1_000_000.0,
        "transaction_id": "payment-1",
    }

    with (
        patch("fm_odds_web.assert_activity_centre_unlocked"),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2028-01-01"}),
        patch("fm_odds_web.free_services_enabled", return_value=False),
        patch("fm_odds_web.local_purchase_price", side_effect=float),
        patch("fm_odds_web.charge_combined_funds", return_value=payment),
        patch("fm_odds_web.update_world_player_primary_nationality", side_effect=RuntimeError("write failed")),
        patch("fm_odds_web.refund_combined_funds") as refund,
    ):
        with pytest.raises(RuntimeError, match="write failed"):
            state.international_activity({
                "player_id": 42, "team_id": 3, "nation_id": 9,
                "scope": "club", "slot": "primary", "expected_nation_id": 7,
            })

    assert refund.call_args.args[:2] == (payment, "nationality_processing_rollback")
    assert refund.call_args.kwargs["scope"] == "club"


def test_international_activity_refunds_after_history_rollback():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"game_date": "2028-01-01"}
    state._bind_current_save = MagicMock()
    state._data_scope_id = MagicMock(return_value="save-1")
    state._world_club_native_cache = MagicMock(return_value={
        "addresses_current": True,
        "nations": [{"id": 9, "name": "Nation", "address": "0x9000"}],
    })
    player = {"id": 42, "name": "Player", "address": "0x4200"}
    state._live_player_profile = MagicMock(return_value=(
        {"team": {"id": 3, "team_type": "club"}}, player,
    ))
    payment = {
        "bank": 1_000_000.0, "wallet": 0.0, "total": 1_000_000.0,
        "transaction_id": "payment-1",
    }
    native_result = {"player_id": 42, "nation_id": 9, "_rollback": {"token": True}}

    with (
        patch("fm_odds_web.assert_activity_centre_unlocked"),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2028-01-01"}),
        patch("fm_odds_web.free_services_enabled", return_value=False),
        patch("fm_odds_web.local_purchase_price", side_effect=float),
        patch("fm_odds_web.charge_combined_funds", return_value=payment),
        patch("fm_odds_web.apply_player_second_nationality", return_value=native_result),
        patch("fm_odds_web.record_international_activity", side_effect=RuntimeError("history failed")),
        patch("fm_odds_web.restore_player_second_nationality") as restore,
        patch("fm_odds_web.refund_combined_funds") as refund,
    ):
        with pytest.raises(RuntimeError, match="history failed"):
            state.international_activity({
                "player_id": 42, "team_id": 3, "nation_id": 9,
                "scope": "club", "slot": "secondary",
            })

    restore.assert_called_once_with(native_result)
    assert refund.call_args.args[:2] == (payment, "nationality_processing_rollback")


@pytest.mark.parametrize(
    ("facility", "price", "centre"),
    [
        ("hot_spring", 200_000, "entertainment"),
        ("football_game", 200_000, "entertainment"),
        ("massage", 500_000, "entertainment"),
        ("retirement", 200_000, "talk_room"),
        ("black_room", 1_000_000, "talk_room"),
        ("language_classroom", 1_000_000, "talk_room"),
    ],
)
def test_activity_rooms_require_separate_purchase(facility, price, centre):
    state = economy_state(owned=(centre,), balance=price)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.purchase_activity_facility(facility)

    assert result["payment"]["total"] == price
    assert facility in state["owned_activity_facilities"]
    save.assert_called_once_with(state)


def test_floor_purchase_includes_bar_and_counseling_but_not_retirement_room():
    entertainment = economy_state(owned=("entertainment",))
    talk_room = economy_state(owned=("talk_room",))
    with patch.object(club_economy, "load_economy", return_value=entertainment):
        assert club_economy.assert_activity_facility_unlocked("drinks")["name"] == "社交吧台"
        with pytest.raises(ValueError, match="请先解锁恢复温泉"):
            club_economy.assert_activity_facility_unlocked("hot_spring")
    with patch.object(club_economy, "load_economy", return_value=talk_room):
        assert club_economy.assert_activity_facility_unlocked("psychological_counseling")["name"] == "心理辅导室"
        with pytest.raises(ValueError, match="请先解锁退役计划交流"):
            club_economy.assert_activity_facility_unlocked("retirement")
        with pytest.raises(ValueError, match="请先解锁小黑屋"):
            club_economy.assert_activity_facility_unlocked("black_room")
        with pytest.raises(ValueError, match="请先解锁语言教室"):
            club_economy.assert_activity_facility_unlocked("language_classroom")


def test_entertainment_activities_are_free_and_share_two_day_floor_cooldown():
    state = economy_state(
        owned=("entertainment",), facilities=("hot_spring", "massage"), balance=0,
    )
    apply_effect = lambda points: {"applied": points, "after": points, "effect": {"field": "morale", "after": 12}}
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy"),
        patch.object(club_economy, "load_wallet", return_value={}),
    ):
        result = club_economy.perform_player_activity(
            1, 2, "球员", "hot_spring", "2026-07-28", 0, apply_effect,
        )
        with pytest.raises(ValueError, match="1F活动冷却至 2026-07-30"):
            club_economy.perform_player_activity(
                1, 2, "球员", "massage", "2026-07-29", 0, apply_effect,
            )
        next_result = club_economy.perform_player_activity(
            1, 2, "球员", "massage", "2026-07-30", 0, apply_effect,
        )

    assert result["effect"]["field"] == "morale"
    assert result["payment"]["total"] == 0
    assert result["cooldown_until"] == "2026-07-30"
    assert next_result["cooldown_until"] == "2026-08-01"


def test_talk_room_activities_share_the_same_floor_cooldown():
    state = economy_state(owned=("talk_room",), facilities=("retirement",))
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
    ):
        cooldown_until = club_economy.record_activity_floor_cooldown(
            1, 2, "talk_room", "2026-07-28",
        )
        with pytest.raises(ValueError, match="2F活动冷却至 2026-07-30"):
            club_economy.assert_activity_floor_available(
                1, 2, "talk_room", "2026-07-29",
            )

    assert cooldown_until == "2026-07-30"
    save.assert_called_once_with(state)


def test_psychological_counseling_paid_unlock_charges_100k_and_forces_clear():
    economy = economy_state(owned=("talk_room",), balance=100_000)
    state = object.__new__(LocalOddsState)
    state.memory_lock = threading.RLock()
    state.lock = threading.RLock()
    state.output = {
        "selected_manager_id": 1,
        "manager": {"id": 1, "manager_address": "0x1000"},
        "game_date": "2028-01-01",
    }
    player = {
        "id": 2, "name": "球员", "address": "0x2000",
        "manager_intimacy": 40, "has_unhappiness": True,
    }
    profile = {"team": {"id": 3, "address": "0x3000"}}
    state._bind_current_save = MagicMock()
    state._live_player_profile = MagicMock(return_value=(profile, player))
    payment = {"bank": 100_000.0, "wallet": 0.0, "total": 100_000.0}
    native_effect = {
        "field": "psychological_counseling",
        "effects": [{"field": "unhappiness", "before": True, "after": False}],
        "_restores": [(0x4000, b"original")],
    }

    with (
        patch.object(club_economy, "load_economy", return_value=economy),
        patch.object(club_economy, "save_economy"),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2028-01-01"}),
        patch("fm_odds_web.player_has_unhappiness", return_value=True),
        patch("fm_odds_web.free_services_enabled", return_value=False),
        patch("fm_odds_web.local_purchase_price", side_effect=float),
        patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
        patch("fm_odds_web.apply_psychological_counseling_effect", return_value=native_effect),
        patch("fm_odds_web.public_economy", return_value={"bank_balance": 0}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        response = state.psychological_counseling({
            "player_id": 2, "team_id": 3, "guaranteed": True,
        })

    charge.assert_called_once()
    assert charge.call_args.args[:2] == (100_000.0, "psychological_counseling_unlock")
    assert response["counseling"]["outcome"] == "unlocked"
    assert response["counseling"]["guaranteed"] is True
    assert response["counseling"]["payment"] == payment
    assert player["has_unhappiness"] is False


def test_failed_native_activity_can_restore_reserved_floor_cooldown():
    state = economy_state(owned=("talk_room",), facilities=("black_room",))
    key = "1:2:talk_room"
    state["activity_floor_cooldowns"][key] = {
        "last_game_date": "2026-07-01", "cooldown_until": "2026-07-03",
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "save_economy") as save,
    ):
        reservation = club_economy.reserve_activity_floor_cooldown(
            1, 2, "talk_room", "2026-07-28",
        )
        restored = club_economy.rollback_activity_floor_cooldown(reservation)

    assert restored is True
    assert state["activity_floor_cooldowns"][key] == {
        "last_game_date": "2026-07-01", "cooldown_until": "2026-07-03",
    }
    assert save.call_count == 2


def test_player_activity_reloads_selected_team_before_memory_write():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = MagicMock()
    state.output = {
        "selected_manager_id": 7,
        "manager": {"id": 7, "manager_address": "0x7000"},
        "game_date": "2026-07-28",
    }
    profile = {
        "team": {"id": 9, "address": "0x9000", "team_type": "club"},
    }
    player = {
        "id": 42, "name": "Player", "address": "0xFRESH",
        "manager_intimacy": 0,
    }
    state._live_player_profile = MagicMock(return_value=(profile, player))

    def perform_activity(
        _manager_id, _player_id, _player_name, _activity,
        _game_date, _current_intimacy, apply_effects,
    ):
        memory_result = apply_effects(0)
        return {
            "integer_applied": 0,
            "effect": memory_result["effect"],
            "intimacy": memory_result["after"],
        }

    with (
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-07-28"}),
        patch("fm_odds_web.perform_player_activity", side_effect=perform_activity),
        patch("fm_odds_web.apply_player_activity_effect", return_value={
            "field": "fitness", "before": 95.0, "after": 100.0,
        }) as apply_effect,
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        state.player_activity({
            "player_id": 42, "team_id": 9, "activity": "hot_spring",
        })

    state._live_player_profile.assert_called_once_with(42, 9)
    apply_effect.assert_called_once_with(42, "0xFRESH", "hot_spring")


def test_player_activity_batch_reuses_memory_session_and_continues_after_failure():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "selected_manager_id": 7,
        "manager": {"id": 7, "manager_address": "0x7000"},
        "game_date": "2026-07-28",
    }
    state._canteen_sea_cucumber_context = MagicMock(
        return_value=(state.output["manager"], 7, "2026-07-28"),
    )
    state._archive_activity_relationship = MagicMock(return_value={"created": True})
    profiles = {
        42: ({"team": {"id": 9, "address": "0x9000", "team_type": "club"}},
             {"id": 42, "name": "Player A", "address": "0xA", "manager_intimacy": 0}),
        43: ({"team": {"id": 9, "address": "0x9000", "team_type": "club"}},
             {"id": 43, "name": "Player B", "address": "0xB", "manager_intimacy": 0}),
    }
    profiles[44] = (
        {"team": {"id": 9, "address": "0x9000", "team_type": "club"}},
        {"id": 44, "name": "Player C", "address": "0xC", "manager_intimacy": 0},
    )
    state._live_player_profile = MagicMock(side_effect=profiles.get)

    class Session:
        def __init__(self):
            self.effects = []
            self.intimacies = []

        def apply_effect(self, player_id, address, activity):
            self.effects.append((player_id, address, activity))
            return {"field": "fitness", "before": 95.0, "after": 100.0}

        def apply_intimacy(self, *args, **kwargs):
            self.intimacies.append((args, kwargs))
            return {"before": 0, "after": 1, "applied": 1}

    memory_session = Session()

    def perform_activity(
        _manager_id, player_id, _player_name, _activity,
        _game_date, _current_intimacy, apply_effects,
    ):
        if player_id == 43:
            raise ValueError("活动冷却中")
        memory_result = apply_effects(1)
        return {
            "integer_applied": 1,
            "effect": memory_result["effect"],
            "intimacy": memory_result["after"],
        }

    with (
        patch("fm_odds_web.player_activity_memory_session", return_value=nullcontext(memory_session)) as open_session,
        patch("fm_odds_web.perform_player_activity", side_effect=perform_activity),
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        response = state.player_activity_batch({
            "items": [
                {"player_id": 42, "team_id": 9, "activity": "hot_spring"},
                {"player_id": 43, "team_id": 9, "activity": "hot_spring"},
                {"player_id": "invalid", "team_id": 9, "activity": "hot_spring"},
                {"player_id": 44, "team_id": 9, "activity": "hot_spring"},
            ],
        })

    open_session.assert_called_once_with()
    assert response["requested"] == 4
    assert response["completed"] == 2
    assert response["results"][0]["player_id"] == 42
    assert [row["player_id"] for row in response["results"]] == [42, 44]
    assert response["failures"][0] == {"player_id": 43, "error": "活动冷却中"}
    assert response["failures"][1]["player_id"] == 0
    assert memory_session.effects == [(42, "0xA", "hot_spring"), (44, "0xC", "hot_spring")]
    assert len(memory_session.intimacies) == 2



def test_language_learning_instant_max_routes_to_real_maximum_transaction():
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state._bind_current_save = MagicMock()
    state.output = {"game_date": "2026-08-11"}
    state.club_profile = None
    state.club_profiles = {
        "club": {
            "team": {"id": 9, "name": "Club", "team_type": "club"},
            "players": [{"id": 42, "name": "Player", "address": "0x1234", "languages": []}],
        },
    }
    expected = {"upgraded": [{"player_id": 42, "before": 0, "after": 10}]}

    with (
        patch("fm_odds_web.assert_activity_facility_unlocked"),
        patch("fm_odds_web.read_game_clock", return_value={"date": "2026-08-11"}),
        patch("fm_odds_web.native_language_catalog", return_value=[{"id": 77, "name": "英语"}]),
        patch("fm_odds_web.advance_language_learning_now", return_value=expected) as advance,
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.welfare_status", return_value={}),
    ):
        result = state.update_language_learning({
            "mode": "instant_max", "language_id": 77,
            "players": [{"player_id": 42, "team_id": 9}],
        })

    assert result["upgraded"][0]["after"] == 10
    assert advance.call_args.kwargs == {"to_maximum": True}
    assert advance.call_args.args[:4] == (
        77, "英语", "2026-08-11", state.club_profiles["club"]["players"],
    )

def test_media_interview_increases_morale_by_two_and_reads_back():
    layout = SimpleNamespace(
        module_name="game_plugin.dll",
        module=lambda _process: SimpleNamespace(base_address=0x50000000),
        player_morale_offset=0x26C,
    )
    values = {0x1000 + 0x26C: 12}

    class Reader:
        def __init__(self, *_args):
            pass

        def u8(self, address):
            return values.get(address)

        def bytes(self, address, size):
            value = values.get(address)
            return bytes([value]) if size == 1 and value is not None else None

    def write(_process, address, data):
        values[address] = data[0]

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", Reader),
        patch.object(club_reader, "_validated_player_person", return_value=0x2000),
        patch.object(club_reader, "write_process_memory", side_effect=write),
    ):
        result = club_reader.apply_player_activity_effect(42, "0x1000", "media_interview")

    assert result["field"] == "morale"
    assert result["before"] == 12
    assert result["after"] == 14
    assert values[0x126C] == 14


def test_press_publicity_updates_and_restores_verified_supporter_fields():
    module_base = 0x50000000
    team, club, detail = 0x1000, 0x2000, 0x3000
    layout = SimpleNamespace(
        module_name="game_plugin.dll",
        module=lambda _process: SimpleNamespace(base_address=module_base),
        club_vtable_rva=0x1234,
        club_detail2_offset=0x100,
        club_social_media_followers_offset=0xF0,
        club_supporters_profile_offset=0x10C,
    )
    pointers = {
        team + club_reader.TEAM_CLUB: club,
        club: module_base + layout.club_vtable_rva,
        club + layout.club_detail2_offset: detail,
    }
    u32_values = {team + club_reader.ENTITY_UID: 42, club + club_reader.ENTITY_UID: 42, detail + 0xF0: 100_000}
    byte_values = {detail + 0x10C: bytes([12, 13, 10, 8, 11, 14])}

    class Reader:
        def __init__(self, *_args):
            self.module_base = module_base
            self.layout = layout

        def team(self, address):
            return {"id": 42} if address == team else None

        def ptr(self, address):
            return pointers.get(address)

        def u32(self, address):
            return u32_values.get(address)

        def bytes(self, address, size):
            raw = byte_values.get(address)
            if raw is not None:
                return raw[:size]
            value = u32_values.get(address)
            return value.to_bytes(4, "little")[:size] if value is not None else None

    def write(_process, address, data):
        if len(data) == 4:
            u32_values[address] = int.from_bytes(data, "little")
        else:
            byte_values[address] = bytes(data)

    with (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(object())),
        patch.object(club_reader, "Reader", Reader),
        patch.object(club_reader, "write_process_memory", side_effect=write),
    ):
        result = club_reader.apply_club_publicity_effect(42, hex(team))
        assert result["social_media_followers"]["after"] == 110_000
        assert result["supporters"]["loyalty_after"] == 13
        assert result["supporters"]["passion_after"] == 14
        club_reader.restore_club_publicity_effect(result)

    assert u32_values[detail + 0xF0] == 100_000
    assert byte_values[detail + 0x10C] == bytes([12, 13, 10, 8, 11, 14])


class RelationMemory:
    def __init__(self):
        self.values = {}

    def put(self, address, raw):
        for index, value in enumerate(bytes(raw)):
            self.values[int(address) + index] = value

    def get(self, address, size):
        try:
            return bytes(self.values[int(address) + index] for index in range(int(size)))
        except KeyError:
            return None


def nationality_harness(*, capacity_entries=1, existing=b""):
    memory = RelationMemory()
    module_base = 0x50000000
    player, person = 0x1000, 0x2000
    container, begin = 0x3000, 0x6000
    nation, men_container, primary = 0x4000, 0x5000, 0x4500
    end = begin + len(existing)
    capacity = begin + capacity_entries * 16
    layout = SimpleNamespace(
        key="fm26", distribution="steam", module_name="game_plugin.dll",
        person_nationality_offset=0x68, person_relationships_offset=0x78,
        nation_men_container_offset=0x108, nation_vtable_rva=0x1234,
        module=lambda _process: SimpleNamespace(base_address=module_base),
    )
    memory.put(person + 0x68, primary.to_bytes(8, "little"))
    memory.put(person + 0x78, container.to_bytes(8, "little"))
    memory.put(container, __import__("struct").pack("<QQQ", begin, end, capacity))
    memory.put(begin, existing + bytes(max(0, capacity - begin - len(existing))))
    memory.put(nation + club_reader.ENTITY_UID, (77).to_bytes(4, "little"))
    memory.put(nation + 0x108, men_container.to_bytes(8, "little"))
    memory.put(men_container, (module_base + 0x1234).to_bytes(8, "little"))

    class Reader:
        def __init__(self, *_args):
            self.layout = layout

        def bytes(self, address, size):
            return memory.get(address, size)

        def ptr(self, address):
            raw = memory.get(address, 8)
            return int.from_bytes(raw, "little") if raw else None

        def u32(self, address):
            raw = memory.get(address, 4)
            return int.from_bytes(raw, "little") if raw else None

        def u8(self, address):
            raw = memory.get(address, 1)
            return raw[0] if raw else None

    patches = (
        patch.object(club_reader, "select_process_layout", return_value=(1, "fm.exe", layout)),
        patch.object(club_reader, "open_process", return_value=nullcontext(SimpleNamespace())),
        patch.object(club_reader, "Reader", Reader),
        patch.object(club_reader, "_validated_player_person", return_value=person),
        patch.object(club_reader, "write_process_memory", side_effect=lambda _p, a, d: memory.put(a, d)),
    )
    return memory, layout, patches, {"player": player, "container": container, "begin": begin, "nation": nation}


def test_second_nationality_appends_in_place_with_eligible_record():
    memory, _layout, patches, addresses = nationality_harness(capacity_entries=1)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = club_reader.apply_player_second_nationality(
            42, hex(addresses["player"]), 77, hex(addresses["nation"]),
        )

    record = memory.get(addresses["begin"], 16)
    assert record == addresses["nation"].to_bytes(8, "little") + b"\x00\x00\x08\x09\x64\x50\x00\xff"
    assert result["eligible_for_nation"] is True
    assert int.from_bytes(memory.get(addresses["container"] + 8, 8), "little") == addresses["begin"] + 16


def test_second_nationality_replaces_existing_slot_and_rolls_back():
    existing = (0x7100).to_bytes(8, "little") + b"\x00\x00\x08\x09\x64\x50\x00\xff"
    memory, _layout, patches, addresses = nationality_harness(
        capacity_entries=1, existing=existing,
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = club_reader.apply_player_second_nationality(
            42, hex(addresses["player"]), 77, hex(addresses["nation"]),
            replace_existing=True,
        )

        assert memory.get(addresses["begin"], 16) == (
            addresses["nation"].to_bytes(8, "little") + existing[8:]
        )
        assert int.from_bytes(memory.get(addresses["container"] + 8, 8), "little") == addresses["begin"] + 16
        club_reader.restore_player_second_nationality(result)

    assert memory.get(addresses["begin"], 16) == existing


def test_second_nationality_reallocates_and_rollback_restores_header():
    existing = (0x7100).to_bytes(8, "little") + b"\x00\x00\x03\x01\x32\x00\x00\xff"
    memory, _layout, patches, addresses = nationality_harness(
        capacity_entries=1, existing=existing,
    )
    freed = []
    with (
        patches[0], patches[1], patches[2], patches[3], patches[4],
        patch.object(club_reader, "_remote_malloc_block", return_value=(0x8000, 0x9000)),
        patch.object(club_reader, "_remote_free_block", side_effect=lambda _p, f, a: freed.append((f, a))),
    ):
        result = club_reader.apply_player_second_nationality(
            42, hex(addresses["player"]), 77, hex(addresses["nation"]),
        )
        assert memory.get(0x8000, 16) == existing
        club_reader.restore_player_second_nationality(result)

    assert memory.get(addresses["container"], 24) == __import__("struct").pack(
        "<QQQ", addresses["begin"], addresses["begin"] + 16, addresses["begin"] + 16,
    )
    assert freed == [(0x9000, 0x8000)]


def test_second_nationality_rejects_primary_nation_without_write():
    memory, _layout, patches, addresses = nationality_harness(capacity_entries=1)
    memory.put(0x2000 + 0x68, addresses["nation"].to_bytes(8, "little"))
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        with pytest.raises(ValueError, match="主要国籍"):
            club_reader.apply_player_second_nationality(
                42, hex(addresses["player"]), 77, hex(addresses["nation"]),
            )


def test_second_nationality_remove_compacts_vector_and_clears_tail():
    first = (0x7100).to_bytes(8, "little") + b"\x00\x00\x03\x01\x32\x00\x00\xff"
    target = (0x4000).to_bytes(8, "little") + b"\x00\x00\x08\x09\x64\x50\x00\xff"
    last = (0x7200).to_bytes(8, "little") + b"\x00\x00\x04\x02\x28\x00\x00\xff"
    memory, _layout, patches, addresses = nationality_harness(
        capacity_entries=3, existing=first + target + last,
    )

    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        result = club_reader.remove_player_second_nationality(
            42, hex(addresses["player"]), 77, hex(addresses["nation"]),
        )

    assert result == {"player_id": 42, "nation_id": 77, "removed": True}
    assert memory.get(addresses["begin"], 48) == first + last + bytes(16)
    assert int.from_bytes(
        memory.get(addresses["container"] + 8, 8), "little",
    ) == addresses["begin"] + 32


def test_second_nationality_remove_rolls_back_after_header_write_failure():
    target = (0x4000).to_bytes(8, "little") + b"\x00\x00\x08\x09\x64\x50\x00\xff"
    last = (0x7200).to_bytes(8, "little") + b"\x00\x00\x04\x02\x28\x00\x00\xff"
    memory, _layout, patches, addresses = nationality_harness(
        capacity_entries=2, existing=target + last,
    )
    original_header = memory.get(addresses["container"], 24)
    failed = False

    def write_with_header_failure(_process, address, raw):
        nonlocal failed
        if int(address) == addresses["container"] and bytes(raw) != original_header and not failed:
            failed = True
            raise RuntimeError("synthetic header failure")
        memory.put(address, raw)

    with (
        patches[0], patches[1], patches[2], patches[3],
        patch.object(club_reader, "write_process_memory", side_effect=write_with_header_failure),
    ):
        with pytest.raises(RuntimeError, match="synthetic header failure"):
            club_reader.remove_player_second_nationality(
                42, hex(addresses["player"]), 77, hex(addresses["nation"]),
            )

    assert memory.get(addresses["container"], 24) == original_header
    assert memory.get(addresses["begin"], 32) == target + last
