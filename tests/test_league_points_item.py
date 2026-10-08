from __future__ import annotations

import threading
from unittest.mock import patch

from fm_odds_web import LocalOddsState


def _state() -> LocalOddsState:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "save_instance_id": "save-1",
        "managed_teams": [
            {"id": 10, "name": "玩家俱乐部", "team_type": "club"},
            {"id": 11, "name": "玩家国家队", "team_type": "national"},
        ],
    }
    return state


def test_league_points_item_defaults_to_managed_club_and_supports_batch_use() -> None:
    state = _state()
    inventory = [
        {"id": "point-1", "sku": "league_point_plus_one", "status": "available"},
        {"id": "point-2", "sku": "league_point_plus_one", "status": "available"},
    ]
    standings = {
        "competitions": [{
            "competition_id": 20, "competition_name": "测试联赛",
            "teams": [{
                "team_id": 10, "team_name": "玩家俱乐部",
                "native_standing_address": "0x3000", "points": 16,
            }],
        }],
    }
    with (
        patch.object(state, "_bind_current_save"),
        patch("fm_odds_web.public_economy", return_value={"inventory": inventory}),
        patch("fm_odds_web.public_league_standings", return_value=standings),
        patch("fm_odds_web.add_points_adjustment", return_value=standings) as add_points,
        patch(
            "fm_odds_web.consume_instant_items", return_value={"inventory": []},
        ) as consume,
    ):
        response = state.inventory_use_league_points({
            "item_ids": ["point-1", "point-2"],
        })

    adjustment = add_points.call_args.args[1]
    assert adjustment["competition_id"] == 20
    assert adjustment["team_id"] == 10
    assert adjustment["delta"] == 2
    assert adjustment["reason"] == "联赛积分道具"
    assert adjustment["source_id"].startswith("league_points_item:")
    consume.assert_called_once()
    assert consume.call_args.args == (
        ["point-1", "point-2"], "league_point_plus_one",
    )
    assert response["league_points"] == {
        "team_id": 10, "team_name": "玩家俱乐部",
        "competition_id": 20, "competition_name": "测试联赛", "added": 2,
    }


def test_league_points_item_rejects_non_managed_target() -> None:
    state = _state()
    inventory = [{
        "id": "point-1", "sku": "league_point_plus_one", "status": "available",
    }]
    with (
        patch.object(state, "_bind_current_save"),
        patch("fm_odds_web.public_economy", return_value={"inventory": inventory}),
        patch("fm_odds_web.add_points_adjustment") as add_points,
    ):
        try:
            state.inventory_use_league_points({
                "item_ids": ["point-1"], "team_id": 999,
            })
        except ValueError as error:
            assert "执教俱乐部" in str(error)
        else:
            raise AssertionError("non-managed target was accepted")
    add_points.assert_not_called()


def test_league_points_item_ignores_non_writable_secondary_table() -> None:
    state = _state()
    standings = {
        "competitions": [
            {
                "competition_id": 20,
                "competition_name": "测试联赛",
                "teams": [{
                    "team_id": 10,
                    "team_name": "玩家俱乐部",
                    "native_standing_address": "0x3000",
                }],
            },
            {
                "competition_id": 30,
                "competition_name": "联赛阶段杯赛",
                "teams": [{
                    "team_id": 10,
                    "team_name": "玩家俱乐部",
                    "native_standing_address": None,
                }],
            },
        ],
    }

    with patch("fm_odds_web.public_league_standings", return_value=standings):
        competition, team = state._managed_league_points_target()

    assert competition["competition_id"] == 20
    assert team["native_standing_address"] == "0x3000"


def test_league_points_item_frontend_exposes_batch_dialog_and_api() -> None:
    app = open("web/app.js", encoding="utf-8").read()
    page = open("web/index.html", encoding="utf-8").read()

    assert 'const LEAGUE_POINTS_SKU = "league_point_plus_one"' in app
    assert 'data-use-league-points=' in app
    assert "managedClubIds.has(Number(team.team_id)) && team.native_standing_address" in app
    assert '"/api/inventory/use-league-points"' in app
    assert 'id="league-points-dialog"' in page
    assert 'id="league-points-quantity"' in page
