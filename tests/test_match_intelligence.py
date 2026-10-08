from contextlib import nullcontext
from unittest.mock import patch

import pytest

import fm_odds_web
from tools import club_economy, preview_cup_odds


@pytest.fixture(autouse=True)
def default_betting_limit_settings():
    with patch.object(
        club_economy, "load_settings",
        return_value={"betting_limits_enabled": True, "unlimited_betting": True},
    ):
        yield


def candidate(*, tiers=None):
    return {
        "fixture_id": "2028-06-10|7|10|20",
        "fixture_date": "2028-06-10",
        "kickoff_minutes": 900,
        "kickoff_time": "15:00",
        "competition_id": 7,
        "competition_name": "测试联赛",
        "home": {"id": 10, "name": "主队", "address": "0x10"},
        "away": {"id": 20, "name": "客队", "address": "0x20"},
        "home_goals": 2,
        "away_goals": 1,
        "available_tiers": tiers or ["outcome", "total_goals", "score"],
    }


def economy_state(balance=1_000_000.0):
    return {
        "general_balance": balance,
        "general_balance_minor": int(balance * 100),
        "transactions": [],
        "owned_activity_centres": ["intelligence"],
        "owned_activity_facilities": [],
        "match_intelligence_purchases": {},
        "free_services": False,
    }


def test_purchase_reprices_up_when_funds_increase_but_never_down():
    state = economy_state(100_000_000.0)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "save_economy"),
    ):
        first = club_economy.open_match_intelligence_status([candidate()], "2028-06-10")
        state["general_balance"] = 200_000_000.0
        state["general_balance_minor"] = 20_000_000_000
        result = club_economy.purchase_match_intelligence(
            candidate(), "outcome", "repriced-request",
        )

    assert first["candidates"][0]["tiers"][0]["price"] == 40_000_000
    assert result["payment"]["total"] == 80_000_000
    assert result["purchase"]["fixture_id"] == candidate()["fixture_id"]
    assert state["match_intelligence_price_locks"]["2028-06-10"]["pricing_base"] == 200_000_000


def test_mismatched_intelligence_payment_is_refunded_once_and_mailed():
    fixture_id = candidate()["fixture_id"]
    state = economy_state(0.0)
    state["match_intelligence_purchases"] = {
        fixture_id: {
            "fixture_id": fixture_id,
            "home": {"name": "主队"}, "away": {"name": "客队"},
            "predictions": {
                "outcome": {"reveal": {"outcome": "home"}},
            },
            "payments": [{
                "tier": "outcome", "client_submission_id": "intel-1",
                "payment": {"bank": 40.0, "wallet": 60.0,
                             "total": 100.0, "transaction_id": "charge-1"},
            }],
        },
    }
    settled = [{
        "bet_id": "bet-1", "leg_records": [{
            "fixture_date": "2028-06-10", "competition_id": 7,
            "home_id": 10, "away_id": 20,
        }],
        "settlement": [{"score": "0-1"}],
    }]
    mail_records = []
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_document", return_value=mail_records),
        patch.object(club_economy, "commit_documents_with_wallet_adjustments") as commit,
    ):
        first = club_economy.reconcile_match_intelligence_refunds(
            settled, "2028-06-11",
        )
        second = club_economy.reconcile_match_intelligence_refunds(
            settled, "2028-06-11",
        )

    assert len(first) == 1
    assert first[0]["total"] == 100.0
    assert first[0]["already_refunded"] is False
    assert second == []
    assert state["match_intelligence_purchases"][fixture_id]["payments"][0]["refunded"]
    commit.assert_called_once()
    adjustments = commit.call_args.args[1]
    assert adjustments[0]["amount"] == 60.0


@pytest.mark.parametrize(
    ("row", "expected_count"),
    [
        (
            {
                "available": True,
                "tiers": [
                    {"key": "outcome", "purchased": False},
                    {"key": "total_goals", "purchased": False},
                    {"key": "score", "purchased": False},
                ],
            },
            1,
        ),
        (
            {
                "available": True,
                "tiers": [
                    {"key": "outcome", "purchased": True},
                    {"key": "total_goals", "purchased": True},
                    {"key": "score", "purchased": True},
                ],
            },
            0,
        ),
        (
            {
                "available": True,
                "tiers": [
                    {"key": "outcome", "purchased": True},
                    {"key": "total_goals", "purchased": False},
                    {"key": "score", "purchased": True},
                ],
            },
            1,
        ),
        (
            {
                "available": False,
                "tiers": [{"key": "outcome", "purchased": False}],
            },
            0,
        ),
        ({"available": True}, 0),
    ],
)
def test_intelligence_notice_only_counts_current_fixtures_with_unpaid_tiers(
    row, expected_count,
):
    candidates = fm_odds_web.purchasable_match_intelligence_candidates(
        {"candidates": [row]}
    )

    assert len(candidates) == expected_count


def test_hidden_history_result_becomes_7f_candidate_only_when_fixture_is_still_open():
    matches = [{
        "fixture_date": "2028-06-10", "kickoff_minutes": 900,
        "kickoff_time": "15:00", "competition_id": 7,
        "competition_name": "测试联赛",
        "home": {"id": 10, "name": "主队"},
        "away": {"id": 20, "name": "客队"},
    }]
    future_result = {
        "date": "2028-06-10", "competition": {"id": 7, "name": "测试联赛"},
        "home_team": {"id": 10, "name": "主队"},
        "away_team": {"id": 20, "name": "客队"},
        "home_goals": 2, "away_goals": 1,
        "result_source": "basic_scoreline",
    }
    unrelated = {
        **future_result,
        "date": "2028-06-09",
    }

    rows = preview_cup_odds.hidden_future_result_candidates(
        object(), [future_result, unrelated], matches, "2028-06-10",
    )

    assert len(rows) == 1
    assert rows[0]["fixture_id"] == "2028-06-10|7|10|20"
    assert rows[0]["available_tiers"] == ["outcome", "total_goals", "score"]
    assert rows[0]["home_goals"] == 2


def test_intelligence_targets_keep_exact_kickoff_and_close_one_minute_later():
    matches = [{
        "fixture_date": "2028-06-10", "kickoff_minutes": 900,
        "kickoff_time": "15:00", "competition_id": 7,
        "fixture_address": "0x1234",
        "home": {"id": 10, "name": "Home"},
        "away": {"id": 20, "name": "Away"},
    }]

    eligible, keys, hints = preview_cup_odds.open_match_intelligence_targets(
        matches, "2028-06-10", 900,
    )
    closed, closed_keys, _closed_hints = (
        preview_cup_odds.open_match_intelligence_targets(
            matches, "2028-06-10", 901,
        )
    )

    key = ("2028-06-10", 7, 10, 20)
    assert eligible == matches
    assert keys == {key}
    assert hints == {key: 0x1234}
    assert closed == []
    assert closed_keys == set()


def test_intelligence_withholds_fixture_when_cached_evidence_has_conflicting_scores():
    matches = [{
        "fixture_date": "2028-06-10", "kickoff_minutes": 900,
        "kickoff_time": "15:00", "competition_id": 7,
        "home": {"id": 10, "name": "Home"},
        "away": {"id": 20, "name": "Away"},
    }]
    base = {
        "date": "2028-06-10",
        "competition": {"id": 7, "name": "Test League"},
        "home_team": {"id": 10, "name": "Home"},
        "away_team": {"id": 20, "name": "Away"},
        "result_source": "basic_scoreline",
    }

    rows = preview_cup_odds.hidden_future_result_candidates(
        object(),
        [
            {**base, "home_goals": 1, "away_goals": 0},
            {**base, "home_goals": 2, "away_goals": 1},
        ],
        matches,
        "2028-06-10",
    )

    assert rows == []


def test_intelligence_withholds_fixture_when_result_conflict_marker_has_no_score():
    matches = [{
        "fixture_date": "2028-06-10", "kickoff_minutes": 900,
        "competition_id": 7,
        "home": {"id": 10, "name": "Home"},
        "away": {"id": 20, "name": "Away"},
    }]
    identity = {
        "date": "2028-06-10",
        "competition": {"id": 7, "name": "Test League"},
        "home_team": {"id": 10, "name": "Home"},
        "away_team": {"id": 20, "name": "Away"},
    }

    rows = preview_cup_odds.hidden_future_result_candidates(
        object(),
        [
            {**identity, "home_goals": 1, "away_goals": 0,
             "result_source": "basic_scoreline"},
            {**identity, "result_conflict": True,
             "result_conflict_candidates": [{"home_goals": 1, "away_goals": 0}]},
        ],
        matches,
        "2028-06-10",
    )

    assert rows == []


def test_current_intelligence_candidates_exclude_every_fixture_before_now():
    previous_day = {**candidate(), "fixture_date": "2028-06-09"}
    earlier_today = {**candidate(), "kickoff_minutes": 899, "kickoff_time": "14:59"}
    exact_now = candidate()
    later_today = {
        **candidate(),
        "fixture_id": "2028-06-10|7|30|40",
        "kickoff_minutes": 901,
        "kickoff_time": "15:01",
        "home": {"id": 30, "name": "稍后主队"},
        "away": {"id": 40, "name": "稍后客队"},
    }
    next_day = {
        **candidate(),
        "fixture_id": "2028-06-11|7|10|20",
        "fixture_date": "2028-06-11",
    }
    absent_from_market = {
        **candidate(),
        "fixture_id": "2028-06-10|7|50|60",
        "home": {"id": 50, "name": "未开盘主队"},
        "away": {"id": 60, "name": "未开盘客队"},
    }
    stale_names = {
        **exact_now,
        "home": {"id": 10, "name": "旧主队名"},
        "away": {"id": 20, "name": "旧客队名"},
    }

    rows = fm_odds_web.current_match_intelligence_candidates(
        [previous_day, earlier_today, stale_names, later_today, next_day,
         absent_from_market],
        [exact_now, later_today, next_day],
        "2028-06-10",
        900,
    )

    assert rows == [exact_now, later_today, next_day]


def test_public_intelligence_projection_excludes_fixture_before_current_time():
    state = object.__new__(fm_odds_web.LocalOddsState)
    state.output = {
        "save_instance_id": "save-1",
        "game_date": "2028-06-10",
        "matches": [candidate()],
    }
    state.save_change_pending = False
    state.last_connection_clock = {
        "date": "2028-06-10",
        "minutes": 901,
    }
    state.match_intelligence_results = [candidate()]
    state.match_intelligence_public_cache_scope_id = "scope-1"
    state.match_intelligence_public_cache = {"unlocked": True}
    state._data_scope_id = lambda _output: "scope-1"

    with (
        patch.object(fm_odds_web, "set_active_save_id"),
        patch.object(
            fm_odds_web, "open_match_intelligence_status",
            side_effect=lambda candidates, _game_date: {"candidates": candidates},
        ),
    ):
        result = state.public_match_intelligence()

    assert result["candidates"] == []


def test_intelligence_purchase_fails_closed_when_live_clock_is_unavailable():
    state = object.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = nullcontext()
    state.lock = nullcontext()
    state.output = {"game_date": "2028-06-10"}
    state.match_intelligence_results = [candidate()]
    state._bind_current_save = lambda: None

    with patch.object(fm_odds_web, "read_game_clock", return_value=None):
        with pytest.raises(ValueError, match="无法确认当前游戏时间"):
            state.buy_match_intelligence({
                "fixture_id": candidate()["fixture_id"],
                "tier": "outcome",
                "client_submission_id": "request-clock-missing",
            })


def test_intelligence_purchase_rejects_fixture_before_live_clock():
    state = object.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = nullcontext()
    state.lock = nullcontext()
    state.output = {"game_date": "2028-06-10", "matches": [candidate()]}
    state.match_intelligence_results = [candidate()]
    state._bind_current_save = lambda: None

    with patch.object(
        fm_odds_web, "read_game_clock",
        return_value={"date": "2028-06-10", "minutes": 901},
    ):
        with pytest.raises(ValueError, match="当前没有可获取"):
            state.buy_match_intelligence({
                "fixture_id": candidate()["fixture_id"],
                "tier": "outcome",
                "client_submission_id": "request-after-kickoff",
            })


def test_intelligence_purchase_rejects_fixture_absent_from_current_market():
    state = object.__new__(fm_odds_web.LocalOddsState)
    state.memory_lock = nullcontext()
    state.lock = nullcontext()
    other_market = {
        **candidate(),
        "fixture_id": "2028-06-10|7|30|40",
        "home": {"id": 30, "name": "其他主队"},
        "away": {"id": 40, "name": "其他客队"},
    }
    state.output = {"game_date": "2028-06-10", "matches": [other_market]}
    state.match_intelligence_results = [candidate()]
    state._bind_current_save = lambda: None

    with patch.object(
        fm_odds_web, "read_game_clock",
        return_value={"date": "2028-06-10", "minutes": 900},
    ):
        with pytest.raises(ValueError, match="当前没有可获取"):
            state.buy_match_intelligence({
                "fixture_id": candidate()["fixture_id"],
                "tier": "outcome",
                "client_submission_id": "request-market-missing",
            })


def test_live_intelligence_probe_reuses_bounded_result_pool_reader():
    matches = [{
        "fixture_date": "2028-06-10", "kickoff_minutes": 900,
        "kickoff_time": "15:00", "competition_id": 7,
        "competition_name": "Test League", "fixture_address": "0x1234",
        "home": {"id": 10, "name": "Home"},
        "away": {"id": 20, "name": "Away"},
    }]
    result = {
        "date": "2028-06-10",
        "competition": {"id": 7, "name": "Test League"},
        "home_team": {"id": 10, "name": "Home"},
        "away_team": {"id": 20, "name": "Away"},
        "home_goals": 1, "away_goals": 2,
        "result_source": "basic_scoreline",
    }
    reader = object()
    with (
        patch.object(
            preview_cup_odds, "open_supported_reader",
            return_value=nullcontext(("fm.exe", object(), object(), reader)),
        ),
        patch.object(
            preview_cup_odds, "_runtime_state",
            return_value={"result_region_spans": [(0x1000, 0x1000)]},
        ),
        patch.object(
            preview_cup_odds, "probe_live_completed_results_with_reader",
            return_value={
                "results": [result], "changed_objects": 1,
                "bytes_scanned": 128, "region_count": 1,
            },
        ) as probe,
    ):
        response = preview_cup_odds.probe_match_intelligence_candidates(
            matches, "2028-06-10", 900,
        )

    probe.assert_called_once_with(
        reader,
        {("2028-06-10", 7, 10, 20)},
        allow_expensive_rescan=True,
        fixture_hints={("2028-06-10", 7, 10, 20): 0x1234},
    )
    assert response["candidates"][0]["fixture_id"] == "2028-06-10|7|10|20"
    assert response["candidates"][0]["away_goals"] == 2


def test_public_status_withholds_every_unpaid_result_field():
    state = economy_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.match_intelligence_status([candidate()])

    row = result["candidates"][0]
    assert row["reveal"] == {}
    assert "home_goals" not in row
    assert "away_goals" not in row
    assert [tier["key"] for tier in row["tiers"]] == [
        "outcome", "total_goals", "score",
    ]
    assert [tier["price"] for tier in row["tiers"]] == [
        400_000, 400_000, 880_000,
    ]
    assert [tier["random_price"] for tier in row["tiers"]] == [
        350_000, 350_000, 850_000,
    ]
    assert [tier["specific_percentage"] for tier in row["tiers"]] == [
        40.0, 40.0, 88.0,
    ]
    assert [tier["random_percentage"] for tier in row["tiers"]] == [
        35.0, 35.0, 85.0,
    ]


def test_random_intelligence_excludes_fixtures_with_that_tier_purchased():
    first = candidate()
    second = {**candidate(), "fixture_id": "2028-06-10|7|30|40"}
    status = {
        "candidates": [
            {"fixture_id": first["fixture_id"], "tiers": [
                {"key": "outcome", "purchased": True},
            ]},
            {"fixture_id": second["fixture_id"], "tiers": [
                {"key": "outcome", "purchased": False},
            ]},
        ],
    }

    selected = fm_odds_web.random_match_intelligence_candidate(
        [first, second], status, "outcome",
    )

    assert selected["fixture_id"] == second["fixture_id"]


@pytest.mark.parametrize("settings", [
    {
        "betting_limits_enabled": False,
        "unlimited_betting": False,
        "default_betting_limit_single": 20_000_000.0,
    },
    {
        "betting_limits_enabled": True,
        "unlimited_betting": False,
        "betting_limit_single": 20_000_000.0,
    },
])
def test_effective_single_betting_limit_sets_price_without_minimum(settings):
    state = economy_state(1_000_000_000.0)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_settings", return_value=settings),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy"),
    ):
        result = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )

    row = result["candidates"][0]
    assert result["pricing_basis"] == "daily_entry_single_betting_limit_percentage"
    assert result["pricing"]["pricing_source"] == "single_betting_limit"
    assert result["pricing"]["combined_funds"] == 1_000_000_000
    assert result["pricing"]["pricing_base"] == 20_000_000
    assert [tier["specific_price"] for tier in row["tiers"]] == [
        8_000_000, 8_000_000, 17_600_000,
    ]
    assert [tier["random_price"] for tier in row["tiers"]] == [
        7_000_000, 7_000_000, 17_000_000,
    ]


def test_single_betting_limit_above_funds_uses_current_combined_funds():
    state = economy_state(20_000_000.0)
    settings = {
        "betting_limits_enabled": True,
        "unlimited_betting": False,
        "betting_limit_single": 100_000_000.0,
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_settings", return_value=settings),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy"),
    ):
        result = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )

    row = result["candidates"][0]
    assert result["pricing"]["combined_funds"] == 20_000_000
    assert result["pricing"]["pricing_base"] == 20_000_000
    assert row["tiers"][0]["specific_description"] == (
        "单关投注上限与合计资金较低值40%"
    )
    assert [tier["specific_price"] for tier in row["tiers"]] == [
        8_000_000, 8_000_000, 17_600_000,
    ]
    assert [tier["random_price"] for tier in row["tiers"]] == [
        7_000_000, 7_000_000, 17_000_000,
    ]


def test_capped_price_lock_only_rises_with_funds_and_stops_at_limit():
    state = economy_state(20_000_000.0)
    settings = {
        "betting_limits_enabled": True,
        "unlimited_betting": False,
        "betting_limit_single": 100_000_000.0,
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_settings", return_value=settings),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy") as save,
    ):
        first = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )
        state["general_balance_minor"] = 1_000_000_000
        lower = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )
        state["general_balance_minor"] = 5_000_000_000
        higher = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )
        state["general_balance_minor"] = 15_000_000_000
        capped = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )

    assert first["pricing"]["pricing_base"] == 20_000_000
    assert lower["pricing"]["pricing_base"] == 20_000_000
    assert higher["pricing"]["pricing_base"] == 50_000_000
    assert capped["pricing"]["pricing_base"] == 100_000_000
    assert save.call_count == 3


def test_capped_price_lock_repairs_legacy_base_above_observed_funds():
    state = economy_state(20_000_000.0)
    state["match_intelligence_price_locks"] = {
        "2028-06-10": {
            "game_date": "2028-06-10",
            "combined_funds": 20_000_000.0,
            "combined_funds_minor": 2_000_000_000,
            "pricing_base": 100_000_000.0,
            "pricing_base_minor": 10_000_000_000,
            "pricing_source": "single_betting_limit",
            "locked_at": "2028-06-10T00:00:00+00:00",
        },
    }
    settings = {
        "betting_limits_enabled": True,
        "unlimited_betting": False,
        "betting_limit_single": 100_000_000.0,
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_settings", return_value=settings),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy") as save,
    ):
        result = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )

    assert result["pricing"]["pricing_base"] == 20_000_000
    assert result["candidates"][0]["tiers"][0]["price"] == 8_000_000
    assert state["match_intelligence_price_locks"]["2028-06-10"][
        "pricing_base"
    ] == 20_000_000
    save.assert_called_once_with(state)


def test_enabling_unlimited_betting_restores_original_intelligence_pricing():
    state = economy_state(100_000_000.0)
    capped = {
        "betting_limits_enabled": True,
        "unlimited_betting": False,
        "betting_limit_single": 20_000_000.0,
    }
    unlimited = {
        "betting_limits_enabled": True,
        "unlimited_betting": True,
        "betting_limit_single": 20_000_000.0,
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(
            club_economy, "load_settings", side_effect=[capped, unlimited],
        ),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy") as save,
    ):
        capped_status = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )
        unlimited_status = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )

    assert capped_status["pricing_basis"] == (
        "daily_entry_single_betting_limit_percentage"
    )
    assert [tier["price"] for tier in capped_status["candidates"][0]["tiers"]] == [
        8_000_000, 8_000_000, 17_600_000,
    ]
    assert unlimited_status["pricing_basis"] == (
        "daily_entry_combined_funds_percentage"
    )
    assert unlimited_status["pricing"] == {
        "locked": True,
        "game_date": "2028-06-10",
        "combined_funds": 100_000_000,
        "locked_at": state["match_intelligence_price_locks"]["2028-06-10"][
            "locked_at"
        ],
    }
    assert all(
        "minimum_price" not in tier
        for tier in unlimited_status["candidates"][0]["tiers"]
    )
    assert [tier["price"] for tier in unlimited_status["candidates"][0]["tiers"]] == [
        40_000_000, 40_000_000, 88_000_000,
    ]
    assert save.call_count == 2


def test_purchased_intelligence_unlocks_fixture_for_betting_engine_guards():
    state = economy_state()
    fixture = candidate()
    state["match_intelligence_purchases"] = {
        fixture["fixture_id"]: {
            **fixture,
            "predictions": {
                "outcome": {
                    "reveal": {"outcome": "home", "outcome_label": "主队获胜"},
                },
            },
        },
        "malformed": {"predictions": {"outcome": {"reveal": {}}}},
        "2028-06-11|7|30|40": {},
    }

    assert club_economy.match_intelligence_betting_unlocked_fixture_keys(
        payload=state,
    ) == {("2028-06-10", 7, 10, 20)}


def test_explicit_daily_entry_locks_prices_until_the_game_date_changes():
    state = economy_state(1_000_000_000.0)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy") as save,
    ):
        first = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )
        state["general_balance"] = 700_000_000.0
        state["general_balance_minor"] = 70_000_000_000
        same_day = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-10",
        )
        state["general_balance"] = 400_000_000.0
        state["general_balance_minor"] = 40_000_000_000
        next_day = club_economy.open_match_intelligence_status(
            [candidate()], "2028-06-11",
        )

    assert first["pricing"] == {
        "locked": True,
        "game_date": "2028-06-10",
        "combined_funds": 1_000_000_000,
        "locked_at": state["match_intelligence_price_locks"]["2028-06-10"]["locked_at"],
    }
    assert [tier["price"] for tier in first["candidates"][0]["tiers"]] == [
        400_000_000, 400_000_000, 880_000_000,
    ]
    assert [tier["price"] for tier in same_day["candidates"][0]["tiers"]] == [
        400_000_000, 400_000_000, 880_000_000,
    ]
    assert [tier["price"] for tier in next_day["candidates"][0]["tiers"]] == [
        160_000_000, 160_000_000, 352_000_000,
    ]
    assert save.call_count == 2


def test_welfare_status_reuses_loaded_economy_for_intelligence_snapshot():
    state = economy_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state) as load,
        patch.object(club_economy, "load_settings", return_value={}),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.welfare_status([candidate()])

    load.assert_called_once()
    assert len(result["_match_intelligence"]["candidates"]) == 1


def test_welfare_pricing_cache_is_bounded_keyed_and_returns_isolated_maps():
    club_economy._welfare_pricing_cached.cache_clear()
    state = economy_state()
    settings = {
        "money_currency": "GBP", "money_rate": 1.0,
        "purchase_money_scale": 1.0,
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "load_settings", return_value=settings),
        patch.object(
            club_economy, "local_purchase_price",
            wraps=club_economy.local_purchase_price,
        ) as convert,
    ):
        first = club_economy.welfare_status()
        first_conversion_count = convert.call_count
        second = club_economy.welfare_status()

        assert first_conversion_count > 0
        assert convert.call_count == first_conversion_count
        first["language_learning"]["instant_prices"]["0"] = -1
        assert second["language_learning"]["instant_prices"]["0"] >= 0

        settings["money_rate"] = 2.0
        club_economy.welfare_status()
        assert convert.call_count > first_conversion_count

    assert club_economy._welfare_pricing_cached.cache_info().maxsize == 16


def test_match_intelligence_prices_each_tier_once_per_status_projection():
    state = economy_state()
    candidates = []
    for index in range(50):
        row = candidate()
        row.update({
            "fixture_id": f"2028-06-10|7|{100 + index}|{200 + index}",
            "home": {"id": 100 + index, "name": f"主队{index}"},
            "away": {"id": 200 + index, "name": f"客队{index}"},
        })
        candidates.append(row)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(
            club_economy, "_match_intelligence_quote_minor",
            wraps=club_economy._match_intelligence_quote_minor,
        ) as quote,
    ):
        result = club_economy.match_intelligence_status(candidates)

    assert len(result["candidates"]) == 50
    assert quote.call_count == len(club_economy.MATCH_INTELLIGENCE_TIER_ORDER) * 2
    assert result["candidates"][0]["tiers"] is not result["candidates"][1]["tiers"]
    assert result["candidates"][0]["tiers"][0] is not result["candidates"][1]["tiers"][0]


def test_purchased_intelligence_remains_as_previous_history_after_window_closes():
    state = economy_state()
    state["match_intelligence_purchases"] = {
        "2028-06-10|7|10|20": {
            "fixture_id": "2028-06-10|7|10|20",
            "fixture_date": "2028-06-10",
            "kickoff_time": "15:00",
            "competition_id": 7,
            "competition_name": "测试联赛",
            "home": {"id": 10, "name": "主队"},
            "away": {"id": 20, "name": "客队"},
            "tier": "score",
            "reveal": {
                "outcome_label": "主队获胜",
                "home_goals": 2,
                "away_goals": 1,
            },
        },
    }
    with patch.object(club_economy, "load_economy", return_value=state):
        result = club_economy.match_intelligence_status([])

    assert len(result["candidates"]) == 1
    row = result["candidates"][0]
    assert row["available"] is False
    assert row["purchased_today"] is False
    assert row["purchased_tiers"] == ["outcome", "total_goals", "score"]


def test_legacy_score_purchase_maps_to_three_result_columns_without_events():
    state = economy_state()
    state["match_intelligence_purchases"] = {
        "2028-06-10|7|10|20": {
            "tier": "full_report",
            "reveal": {
                "outcome": "home", "outcome_label": "主队获胜",
                "home_goals": 2, "away_goals": 1,
                "goal_events": [{"scorer": {"name": "不应公开"}}],
            },
        },
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        row = club_economy.match_intelligence_status([candidate()])["candidates"][0]

    assert row["purchased_tiers"] == ["outcome", "total_goals", "score"]
    assert row["reveal"] == {
        "outcome": "home", "outcome_label": "主队获胜",
        "total_goals": 3, "home_goals": 2, "away_goals": 1,
    }


def test_purchasing_one_candidate_keeps_other_candidates_available():
    state = economy_state()
    state["match_intelligence_purchases"] = {
        "2028-06-10|7|10|20": {
            "tier": "outcome",
            "reveal": {"outcome": "home", "outcome_label": "主队获胜"},
        },
    }
    other = {
        **candidate(),
        "fixture_id": "2028-06-10|7|30|40",
        "home": {"id": 30, "name": "另一主队"},
        "away": {"id": 40, "name": "另一客队"},
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        result = club_economy.match_intelligence_status([candidate(), other])

    rows = {row["fixture_id"]: row for row in result["candidates"]}
    assert len(rows) == 2
    assert rows["2028-06-10|7|10|20"]["purchased_tiers"] == ["outcome"]
    assert rows["2028-06-10|7|30|40"]["purchased_tiers"] == []
    assert any(not tier["purchased"] for tier in rows["2028-06-10|7|30|40"]["tiers"])


def test_specific_score_prediction_charges_88_percent_and_unlocks_all_columns():
    state = economy_state(1_000_000_000.0)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "save_economy") as save,
    ):
        result = club_economy.purchase_match_intelligence(
            candidate(), "score", "request-1",
        )
        included = club_economy.purchase_match_intelligence(
            candidate(), "outcome", "request-2",
        )

    assert result["payment"]["total"] == 880_000_000
    assert state["general_balance"] == 120_000_000
    assert list(result["purchase"]["predictions"]) == [
        "outcome", "total_goals", "score",
    ]
    assert result["purchase"]["predictions"]["outcome"]["reveal"] == {
        "outcome": "home", "outcome_label": "主队获胜",
    }
    assert result["purchase"]["predictions"]["total_goals"]["reveal"] == {
        "total_goals": 3,
    }
    assert result["purchase"]["predictions"]["score"]["reveal"] == {
        "home_goals": 2, "away_goals": 1,
    }
    assert result["purchase"]["purchased_game_date"] == "2028-06-10"
    assert {
        record["purchased_game_date"]
        for record in result["purchase"]["predictions"].values()
    } == {"2028-06-10"}
    assert included["already_purchased"] is True
    assert included["payment"]["total"] == 0.0
    save.assert_called_once_with(state)


def test_random_prediction_charges_random_mode_percentage():
    state = economy_state(1_000_000_000.0)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "save_economy"),
    ):
        result = club_economy.purchase_match_intelligence(
            candidate(), "outcome", "random-request-1",
            purchase_mode="random",
        )

    assert result["payment"]["total"] == 350_000_000
    assert state["general_balance"] == 650_000_000


def test_prediction_tier_descriptions_use_current_amount_wording():
    assert club_economy.MATCH_INTELLIGENCE_TIERS["outcome"]["description"] == (
        "今日金额40%"
    )
    assert club_economy.MATCH_INTELLIGENCE_TIERS["total_goals"]["description"] == (
        "今日金额40%"
    )
    assert club_economy.MATCH_INTELLIGENCE_TIERS["outcome"][
        "random_description"
    ] == "今日金额35%"
    assert club_economy.MATCH_INTELLIGENCE_TIERS["score"]["description"].endswith(
        "· 并包含前两项"
    )
    assert club_economy.MATCH_INTELLIGENCE_TIERS["score"][
        "random_description"
    ].startswith("今日锁价基准85%")


def test_existing_score_only_purchase_is_read_as_all_columns_unlocked():
    state = economy_state()
    state["match_intelligence_purchases"] = {
        "2028-06-10|7|10|20": {
            "fixture_id": "2028-06-10|7|10|20",
            "home": {"id": 10, "name": "主队"},
            "away": {"id": 20, "name": "客队"},
            "predictions": {
                "score": {
                    "tier": "score",
                    "reveal": {"home_goals": 2, "away_goals": 1},
                    "purchased_at": "2028-06-10T15:00:00",
                    "purchased_game_date": "2028-06-10",
                },
            },
        },
    }
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
    ):
        row = club_economy.match_intelligence_status([candidate()])["candidates"][0]

    assert row["purchased_tiers"] == ["outcome", "total_goals", "score"]
    assert row["purchased_today"] is True
    assert row["reveal"] == {
        "outcome": "home", "outcome_label": "主队获胜",
        "total_goals": 3, "home_goals": 2, "away_goals": 1,
    }


def test_prediction_columns_are_independent_and_submission_is_idempotent():
    state = economy_state(1_000_000_000.0)
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "load_wallet", return_value={}),
        patch.object(club_economy, "save_economy") as save,
    ):
        first = club_economy.purchase_match_intelligence(
            candidate(), "outcome", "request-1",
        )
        repeated = club_economy.purchase_match_intelligence(
            candidate(), "outcome", "request-1",
        )
        total_goals = club_economy.purchase_match_intelligence(
            candidate(), "total_goals", "request-2",
        )

    assert first["payment"]["total"] == 400_000_000
    assert repeated["already_purchased"] is True
    assert repeated["payment"]["total"] == 400_000_000
    assert total_goals["payment"]["total"] == 400_000_000
    assert total_goals["purchase"]["predictions"]["outcome"]["reveal"] == {
        "outcome": "home", "outcome_label": "主队获胜",
    }
    assert total_goals["purchase"]["predictions"]["total_goals"]["reveal"] == {
        "total_goals": 3,
    }
    assert state["general_balance"] == 200_000_000
    assert save.call_count == 2


def test_unavailable_detail_tier_never_charges():
    state = economy_state()
    with (
        patch.object(club_economy, "load_economy", return_value=state),
        patch.object(club_economy, "available_balance", return_value=0.0),
        patch.object(club_economy, "save_economy") as save,
    ):
        with pytest.raises(ValueError, match="情报项目无效"):
            club_economy.purchase_match_intelligence(
                candidate(), "full_report", "request-1",
            )

    assert state["general_balance"] == 1_000_000
    save.assert_not_called()
