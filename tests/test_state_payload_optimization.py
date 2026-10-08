from __future__ import annotations

from io import BytesIO
import gzip
import inspect
import json
from pathlib import Path
import threading
from unittest.mock import Mock, patch

from fm_odds_web import Handler, LocalOddsState
from tools.live_market import market_output


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_large_json_response_uses_gzip_when_client_accepts_it() -> None:
    handler = Handler.__new__(Handler)
    handler.headers = {"Accept-Encoding": "gzip, deflate"}
    handler.wfile = BytesIO()
    handler.send_response = Mock()
    headers: dict[str, str] = {}
    handler.send_header = lambda name, value: headers.__setitem__(name, value)
    handler.end_headers = Mock()

    payload = {"rows": [{"name": "repeated value", "value": 42}] * 500}
    handler._json(200, payload)

    body = handler.wfile.getvalue()
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Vary"] == "Accept-Encoding"
    assert int(headers["Content-Length"]) == len(body)
    assert json.loads(gzip.decompress(body)) == payload


def test_successful_post_json_attaches_available_mutation_patch() -> None:
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.state = Mock()
    handler._http_mutation_context = {"data_scope_id": "career-test", "data_version": 7}
    handler.state._mutation_patch_response.return_value = {
        "economy": {"inventory": []},
        "state_patch": {"economy": {"inventory": []}},
    }
    handler.headers = {}
    handler.wfile = BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()

    handler._json(200, {"economy": {"inventory": []}})

    handler.state._mutation_patch_response.assert_called_once_with(
        {"economy": {"inventory": []}},
        origin=handler._http_mutation_context,
    )
    assert json.loads(handler.wfile.getvalue()) == {
        "economy": {"inventory": []},
        "state_patch": {"economy": {"inventory": []}},
    }


def test_successful_post_keeps_success_when_optional_mutation_patch_fails() -> None:
    handler = Handler.__new__(Handler)
    handler.command = "POST"
    handler.state = Mock()
    handler._http_mutation_context = {
        "data_scope_id": "career-test", "data_version": 7,
    }
    handler.state._mutation_patch_response.side_effect = RuntimeError(
        "optional patch failed",
    )
    handler.headers = {}
    handler.wfile = BytesIO()
    handler.send_response = Mock()
    handler.send_header = Mock()
    handler.end_headers = Mock()
    response = {"records": 1, "balance": 90, "bets": [{"bet_id": "bet-1"}]}

    handler._json(201, response)

    handler.send_response.assert_called_once_with(201)
    assert json.loads(handler.wfile.getvalue()) == response


def test_frontend_loads_historical_results_on_demand() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'request("/api/results")' in script
    assert "request(`/api/team-results?team_id=${encodeURIComponent(teamId)}&limit=20`)" in script
    assert "app.teamResultsCache.has(cacheKey)" in script
    assert "app.teamResultsPromises.has(cacheKey)" in script
    assert "app.teamResultsCache.size >= 128" in script
    assert "app.teamResultsCache.clear()" in script
    assert "app.gameClock?.date || output().game_date" in script
    assert "await openTeamFormCard(Number(teamButton.dataset.teamCard)" in script
    assert "app.seasonResults || output().season_results || []" in script
    assert ".slice(0, 20);" in script
    assert "function teamLeagueStanding(teamId)" in script
    assert '"league.rank"' in script
    assert "leagueStanding || (rows.length ? `最近 ${rows.length} 场战绩`" in script
    assert '"team_form.subtitle"' in markup


def test_team_results_include_retained_history_outside_current_season() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.data_version = 7
    state.output = {
        "save_instance_id": "save-1",
        "season_results": [{
            "date": "2025-07-26",
            "competition_id": 10,
            "competition_name": "Friendly",
            "home": {"id": 1, "name": "Kawasaki"},
            "away": {"id": 2, "name": "Urawa"},
            "home_goals": 1,
            "away_goals": 0,
        }],
    }
    retained = [{
        "date": "2025-06-30",
        "competition": {"id": 20, "name": "J1 League"},
        "home_team": {"id": 3, "name": "Kobe"},
        "away_team": {"id": 1, "name": "Kawasaki"},
        "home_goals": 0,
        "away_goals": 2,
    }]

    with patch("fm_odds_web.read_result_history", return_value=retained):
        payload = state.public_team_results(1, 10)

    assert [row["date"] for row in payload["team_results"]] == ["2025-07-26", "2025-06-30"]
    assert payload["team_results"][1]["competition_name"] == "J1 League"


def test_team_results_reuse_bounded_versioned_cache_without_copying_unrelated_output() -> None:
    class UnrelatedPayload:
        def __deepcopy__(self, _memo):
            raise AssertionError("unrelated output should not be copied")

    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.data_version = 9
    state.last_connection_clock = {"date": "2025-08-02"}
    state.output = {
        "save_instance_id": "save-1",
        "game_date": "2025-08-02",
        "matches": UnrelatedPayload(),
        "season_results": [],
    }
    retained = [{
        "date": "2025-08-01",
        "competition": {"id": 20, "name": "J1 League"},
        "home_team": {"id": 1, "name": "Kawasaki"},
        "away_team": {"id": 2, "name": "Urawa"},
        "home_goals": 2,
        "away_goals": 0,
    }]

    with patch("fm_odds_web.read_result_history", return_value=retained) as read_history:
        first = state.public_team_results(1, 20)
        second = state.public_team_results(1, 10)

    assert first["team_results"] == second["team_results"]
    read_history.assert_called_once_with("save-1")

    state.data_version += 1
    with patch("fm_odds_web.read_result_history", return_value=retained) as read_history:
        state.public_team_results(1, 20)
    read_history.assert_called_once_with("save-1")


def test_team_results_exclude_matches_on_current_game_date() -> None:
    state = LocalOddsState.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.data_version = 8
    state.last_connection_clock = {"date": "2025-11-02", "time": "15:00"}
    state.output = {
        "save_instance_id": "save-1",
        "game_date": "2025-11-01",
        "season_results": [
            {
                "date": "2025-11-02",
                "competition_id": 10,
                "competition_name": "Development League",
                "home": {"id": 1, "name": "Kawasaki"},
                "away": {"id": 2, "name": "Crystal Sports"},
                "home_goals": 5,
                "away_goals": 1,
            },
            {
                "date": "2025-10-28",
                "competition_id": 10,
                "competition_name": "Development League",
                "home": {"id": 1, "name": "Kawasaki"},
                "away": {"id": 3, "name": "Brisbane"},
                "home_goals": 3,
                "away_goals": 2,
            },
        ],
    }

    with patch("fm_odds_web.read_result_history", return_value=[]):
        payload = state.public_team_results(1, 10)

    assert [row["date"] for row in payload["team_results"]] == ["2025-10-28"]


def test_frontend_coalesces_overlapping_club_poll_requests() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    load_club = script.split("async function loadClub()", 1)[1].split(
        "function clubProfileIsCurrent", 1,
    )[0]

    assert "if (app.clubLoadPromise) return app.clubLoadPromise;" in load_club
    assert "app.clubLoadPromise = loadPromise;" in load_club
    assert "app.clubLoadPromise = null;" in load_club


def test_public_matches_keep_only_the_profile_field_used_by_frontend() -> None:
    source = {
        "matches": [{
            "home": {
                "id": 1,
                "name": "Home",
                "profile": {"candidate_20_ca": 151.5, "players": [1, 2, 3]},
            },
            "away": {
                "id": 2,
                "name": "Away",
                "profile": {"candidate_20_ca": 148.0, "players": [4, 5, 6]},
            },
        }],
    }

    public = market_output(source, None)

    assert public["matches"][0]["home"]["profile"] == {"candidate_20_ca": 151.5}
    assert public["matches"][0]["away"]["profile"] == {"candidate_20_ca": 148.0}
    assert source["matches"][0]["home"]["profile"]["players"] == [1, 2, 3]


def test_verified_public_state_skips_the_redundant_initial_economy_snapshot() -> None:
    source = inspect.getsource(LocalOddsState._capture_public_state)

    assert "if connection_wallet_ready and not has_verified_save else None" in source
    assert source.count("public_economy()") == 1
    assert "read_memory=False" in source
    assert "_request_public_state_maintenance" not in source


def test_public_connection_uses_semantic_message_code_for_manager_context_errors() -> None:
    source = inspect.getsource(LocalOddsState._capture_public_state)

    assert '"message_code": connection_message_code' in source
    assert '"尚未定位当前经理的执教球队" in connection_error' in source
    assert 'connection_message_code = "managed_team_missing"' in source
    assert '"尚未定位当前人类经理对象" in connection_error' in source
    assert 'connection_message_code = "manager_missing"' in source
