from __future__ import annotations

import threading
from unittest.mock import patch

from fm_odds_web import API_ROUTES, LocalOddsState
from tools.transfer_history import public_transfer_history


def _club_payload(team_id: int, name: str, player_id: int, *, active: bool) -> dict:
    return {
        "club": {"id": team_id, "name": name},
        "coverage": {
            "tracking_started": "2027-07-01",
            "last_synced_game_date": "2030-07-10",
            "native_history_complete": False,
        },
        "players": [{
            "id": player_id,
            "status": "current" if active else "history",
            "status_label": "在队" if active else "历史成员",
            "first_seen_game_date": "2027-07-02",
            "last_seen_game_date": "2030-07-10",
            "left_current_roster_date": None if active else "2030-07-10",
            "snapshot": {
                "name": f"球员{player_id}",
                "player_contract": {"joined_club_date": "2027-07-01"},
            },
            "events": ([] if active else [{
                "type": "left_current_roster", "game_date": "2030-07-10",
            }]),
            "career_record": {
                "transfer_in_fee": 20_000_000,
                "transfer_out_fee": None if active else 45_000_000,
            },
        }],
    }


def test_transfer_history_keeps_all_clubs_for_the_same_player_manager() -> None:
    manager_records = {
        "schema_version": 1,
        "records": {
            "77:10": {"manager_id": 77, "team_id": 10},
            "77:20": {"manager_id": 77, "team_id": 20},
            "88:30": {"manager_id": 88, "team_id": 30},
        },
    }
    index = {"clubs": [
        {"id": 10, "name": "旧俱乐部"},
        {"id": 20, "name": "当前俱乐部"},
        {"id": 30, "name": "其他经理俱乐部"},
    ]}
    payloads = {
        10: _club_payload(10, "旧俱乐部", 101, active=False),
        20: _club_payload(20, "当前俱乐部", 202, active=True),
        30: _club_payload(30, "其他经理俱乐部", 303, active=True),
    }
    with patch("tools.transfer_history.load_document", return_value=manager_records), patch(
        "tools.transfer_history.public_club_legacy_index", return_value=index,
    ), patch(
        "tools.transfer_history.public_club_legacy",
        side_effect=lambda _career, _scope, team_id: payloads[team_id],
    ):
        result = public_transfer_history(
            "career-a", "account-a", manager_id=77, current_team_id=20,
        )

    assert {row["id"] for row in result["clubs"]} == {10, 20}
    assert {row["player_id"] for row in result["players"]} == {101, 202}
    old_player = next(row for row in result["players"] if row["player_id"] == 101)
    assert old_player["joined_on"] == "2027-07-01"
    assert old_player["left_on"] == "2030-07-10"
    assert old_player["purchase_fee"] == 20_000_000
    assert old_player["sale_fee"] == 45_000_000
    assert old_player["known_profit"] == 25_000_000
    incoming = [row for row in result["transfers"] if row["direction"] == "in"]
    outgoing = [row for row in result["transfers"] if row["direction"] == "out"]
    assert {row["player_id"] for row in incoming} == {101, 202}
    assert {row["player_id"] for row in outgoing} == {101}
    assert incoming[0]["year"] == "2027"
    assert outgoing[0]["date"] == "2030-07-10"
    assert outgoing[0]["year"] == "2030"
    assert outgoing[0]["fee"] == 45_000_000
    assert result["years"] == ["2030", "2027"]
    assert result["summary"]["incoming"] == 2
    assert result["summary"]["outgoing"] == 1
    assert result["summary"]["known_purchase_total"] == 40_000_000
    assert result["summary"]["known_sale_total"] == 45_000_000
    assert result["coverage"]["native_history_complete"] is False


def test_transfer_history_api_binds_career_account_and_current_club() -> None:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.output = {
        "save_instance_id": "career-a",
        "selected_manager_id": 77,
        "manager": {"id": 77},
        "managed_teams": [
            {"id": 20, "name": "当前俱乐部", "team_type": "club"},
            {"id": 90, "name": "国家队", "team_type": "national"},
        ],
    }
    state._data_scope_id = lambda _output: "account-a"
    expected = {"schema_version": 1, "clubs": [], "players": [], "transfers": []}
    with patch("fm_odds_web.load_public_transfer_history", return_value=expected) as load:
        result = API_ROUTES.dispatch(
            "GET", "/api/club/transfer-history",
            type("Handler", (), {"state": state})(), query={},
        )

    assert result.payload["data_scope_id"] == "account-a"
    load.assert_called_once_with(
        "career-a", "account-a", manager_id=77, current_team_id=20,
    )
