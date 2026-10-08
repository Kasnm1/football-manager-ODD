from copy import deepcopy
import threading
from unittest.mock import MagicMock, patch

from fm_odds_web import API_ROUTES, LocalOddsState
from tools.app_paths import active_save_id, set_active_save_id
from tools.manager_record import sync_player_manager_record


def test_player_manager_record_filters_tenure_team_and_deduplicates() -> None:
    saved = {}
    results = [
        {"date": "2026-06-30", "competition_id": 1, "home": {"id": 42}, "away": {"id": 7}, "home_goals": 3, "away_goals": 0},
        {"date": "2026-07-02", "competition_id": 1, "home": {"id": 42}, "away": {"id": 7}, "home_goals": 2, "away_goals": 0},
        {"date": "2026-07-05", "competition": {"id": 2}, "home_team": {"id": 8}, "away_team": {"id": 42}, "home_goals": 1, "away_goals": 1},
        {"date": "2026-07-08", "competition_id": 2, "home": {"id": 9}, "away": {"id": 42}, "home_goals": 2, "away_goals": 1},
        {"date": "2026-07-08", "competition_id": 2, "home": {"id": 9}, "away": {"id": 42}, "home_goals": 2, "away_goals": 1},
        {"date": "2026-07-09", "competition_id": 2, "home": {"id": 10}, "away": {"id": 11}, "home_goals": 2, "away_goals": 1},
    ]

    with patch("tools.manager_record.load_document", return_value=saved), patch(
        "tools.manager_record.save_document"
    ) as save:
        record = sync_player_manager_record(77, 42, "2026-07-01", results, "account-a")

    assert record["total"] == {
        "played": 3, "wins": 1, "draws": 1, "losses": 1, "win_rate": 33.3,
    }
    save.assert_called_once()


def test_player_manager_record_keeps_prior_results_after_contract_renewal() -> None:
    stored = {"schema_version": 1, "records": {}}

    def save(_key, payload, _scope):
        stored.clear()
        stored.update(deepcopy(payload))

    first = [{"date": "2026-07-02", "competition_id": 1, "home": {"id": 42}, "away": {"id": 7}, "home_goals": 2, "away_goals": 0}]
    second = [{"date": "2027-07-02", "competition_id": 1, "home": {"id": 8}, "away": {"id": 42}, "home_goals": 0, "away_goals": 1}]
    with patch("tools.manager_record.load_document", side_effect=lambda *_args, **_kwargs: deepcopy(stored)), patch(
        "tools.manager_record.save_document", side_effect=save,
    ):
        sync_player_manager_record(77, 42, "2026-07-01", first, "account-a")
        record = sync_player_manager_record(77, 42, "2027-07-01", second, "account-a")

    assert record["tenure_start"] == "2026-07-01"
    assert record["total"]["wins"] == 2
    assert record["total"]["played"] == 2


def test_public_club_attaches_record_to_managed_club_profile() -> None:
    profile = {
        "team": {"id": 42, "team_type": "club"},
        "manager": {"id": 77},
        "contract": {"start_date": "2026-07-01"},
    }
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.club_profiles = {42: profile}
    state.club_profile = profile
    state.club_refreshing = False
    state.club_last_updated = None
    state.club_error = None
    state.club_progress = 100
    state.club_progress_text = "读取完成"
    state.club_medical_auto_result = None
    state.output = {
        "save_instance_id": "career-a",
        "season_results": [],
    }
    state._data_scope_id = lambda _output: "account-a"
    expected = {"total": {"played": 3, "wins": 2, "draws": 1, "losses": 0, "win_rate": 66.7}}

    with patch("fm_odds_web.read_result_history", return_value=[]) as history, patch(
        "fm_odds_web.sync_player_manager_record", return_value=expected,
    ) as sync, patch("fm_odds_web.public_economy", return_value={}), patch(
        "fm_odds_web.welfare_status", return_value={},
    ), patch("fm_odds_web.set_active_save_id") as bind_scope:
        payload = state.public_club()
        cached_payload = state.public_club()

    assert payload["data_scope_id"] == "account-a"
    assert payload["profiles"][0]["manager_record"] == expected
    assert payload["profile"]["manager_record"] == expected
    assert cached_payload["profile"]["manager_record"] == expected
    assert bind_scope.call_count == 2
    bind_scope.assert_called_with("account-a")
    sync.assert_called_once_with(77, 42, "2026-07-01", [], "account-a")
    history.assert_called_once_with("career-a")


def test_public_club_compact_payload_skips_history_and_training_projection() -> None:
    player = {
        "id": 9, "name": "Player", "ca": 120, "pa": 150,
        "training_ca": {"costs": {"large": [1] * 100}},
    }
    profile = {
        "team": {"id": 42, "team_type": "club"},
        "manager": {"id": 77},
        "contract": {"start_date": "2026-07-01"},
        "players": [player],
        "squads": [{"team_id": 42, "players": [player]}],
    }
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.club_profiles = {42: profile}
    state.club_profile = profile
    state.club_refreshing = False
    state.club_last_updated = None
    state.club_error = None
    state.club_progress = 100
    state.club_progress_text = "读取完成"
    state.club_medical_auto_result = None
    season_result = {"date": "2026-08-01"}
    state.output = {
        "save_instance_id": "career-a", "season_results": [season_result],
    }
    state._data_scope_id = lambda _output: "account-a"

    with patch("fm_odds_web.read_result_history") as history, patch(
        "fm_odds_web.sync_player_manager_record", return_value=None,
    ) as sync, patch("fm_odds_web.public_economy", return_value={}), patch(
        "fm_odds_web.welfare_status", return_value={},
    ), patch("fm_odds_web.set_active_save_id"):
        payload = state.public_club(compact=True)

    history.assert_not_called()
    assert sync.call_args.args[3] == [season_result]
    assert payload["profile"] is None
    assert "training_ca" not in payload["profiles"][0]["players"][0]
    assert "training_ca" not in payload["profiles"][0]["squads"][0]["players"][0]
    assert "training_ca" in player


def test_club_route_forwards_compact_query_to_public_payload() -> None:
    state = MagicMock()
    state.public_club.return_value = {"ready": True}

    result = API_ROUTES.dispatch(
        "GET", "/api/club", type("Handler", (), {"state": state})(),
        query={"compact": ["1"]},
    )

    assert result.payload == {"ready": True}
    state.public_club.assert_called_once_with(compact=True)


def test_public_club_skips_manager_history_while_refreshing() -> None:
    player = {"id": 9, "training_ca": {"costs": {"large": [1] * 100}}}
    profile = {
        "team": {"id": 42, "team_type": "club"},
        "manager": {"id": 77},
        "contract": {"start_date": "2026-07-01"},
        "players": [player],
        "squads": [{"team_id": 42, "players": [player]}],
    }
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.club_profiles = {}
    state.club_loading_profiles = {42: profile}
    state.club_profile = None
    state.club_refreshing = True
    state.club_last_updated = None
    state.club_error = None
    state.club_progress = 50
    state.club_progress_text = "读取中"
    state.club_medical_auto_result = None
    state.output = {"save_instance_id": "career-a", "season_results": []}
    state._data_scope_id = lambda _output: "account-a"

    with patch("fm_odds_web.read_result_history") as history, patch(
        "fm_odds_web.sync_player_manager_record",
    ) as sync, patch("fm_odds_web.public_economy", return_value={}), patch(
        "fm_odds_web.welfare_status", return_value={},
    ) as welfare, patch("fm_odds_web.set_active_save_id"):
        payload = state.public_club(compact=True)

    assert payload["refreshing"] is True
    assert payload["profile"] is None
    assert "training_ca" not in payload["profiles"][0]["players"][0]
    assert "training_ca" not in payload["profiles"][0]["squads"][0]["players"][0]
    history.assert_not_called()
    sync.assert_not_called()
    welfare.assert_not_called()
    assert payload["economy"] is None
    assert payload["welfare"] is None


def test_public_club_treats_queued_startup_read_as_nonblocking_progress() -> None:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.club_profiles = {}
    state.club_loading_profiles = {}
    state.club_profile = None
    state.club_refreshing = False
    state.club_refresh_pending = True
    state.club_last_updated = None
    state.club_error = None
    state.club_progress = 0
    state.club_progress_text = "等待当前经理和执教队伍确认"
    state.club_medical_auto_result = None
    state.output = {"save_instance_id": "career-a", "season_results": []}
    state._data_scope_id = lambda _output: "account-a"

    with patch("fm_odds_web.public_economy") as economy, patch(
        "fm_odds_web.welfare_status",
    ) as welfare, patch("fm_odds_web.set_active_save_id"):
        payload = state.public_club(compact=True)

    assert payload["refreshing"] is True
    assert payload["progress"] == 0
    assert payload["progress_text"] == "等待当前经理和执教队伍确认"
    economy.assert_not_called()
    welfare.assert_not_called()


def test_public_club_compact_cache_does_not_hide_full_history() -> None:
    profile = {
        "team": {"id": 42, "team_type": "club"},
        "manager": {"id": 77},
        "contract": {"start_date": "2026-07-01"},
    }
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.club_profiles = {42: profile}
    state.club_profile = profile
    state.club_refreshing = False
    state.club_last_updated = None
    state.club_error = None
    state.club_progress = 100
    state.club_progress_text = "读取完成"
    state.club_medical_auto_result = None
    state.output = {"save_instance_id": "career-a", "season_results": []}
    state._data_scope_id = lambda _output: "account-a"

    with patch(
        "fm_odds_web.read_result_history", return_value=[{"date": "2026-07-01"}],
    ) as history, patch(
        "fm_odds_web.sync_player_manager_record", return_value=None,
    ) as sync, patch("fm_odds_web.public_economy", return_value={}), patch(
        "fm_odds_web.welfare_status", return_value={},
    ), patch("fm_odds_web.set_active_save_id"):
        state.public_club(compact=True)
        state.public_club()

    history.assert_called_once_with("career-a")
    assert sync.call_count == 2
    assert sync.call_args.args[3] == [{"date": "2026-07-01"}]


def test_manager_record_cache_can_be_invalidated_after_result_history_changes() -> None:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.club_manager_record_cache_key = ("account-a", 1)
    state.club_manager_record_cache = {42: {"total": {"played": 3}}}

    state._invalidate_club_manager_record_cache()

    assert state.club_manager_record_cache_key is None
    assert state.club_manager_record_cache == {}


def test_club_refresh_worker_binds_account_before_welfare_processing() -> None:
    profile = {
        "team": {"id": 42, "name": "Test Club"},
        "manager": {"id": 77},
        "players": [],
        "staff": [],
    }
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {
        "save_instance_id": "career-a",
        "game_date": "2026-07-29",
        "manager": {"id": 77},
        "managed_teams": [{"id": 42, "name": "Test Club", "team_type": "club"}],
    }
    state._data_scope_id = lambda _output: "account-a"
    state._apply_default_medical_treatments = MagicMock(return_value={})
    state.club_profiles = {}
    state.club_contexts = {}
    state.club_profile = None
    state.club_context = None
    state.club_loading_profiles = {}
    state.club_progress = 0
    state.club_progress_text = None
    state.club_refreshing = True
    state.club_error = None

    def scoped_welfare() -> dict:
        assert active_save_id() == "account-a"
        return {"treatments": {}}

    set_active_save_id(None)
    try:
        with patch(
            "fm_odds_web.read_club_profile", return_value=deepcopy(profile),
        ), patch(
            "fm_odds_web.read_game_clock",
            return_value={"date": "2026-07-29", "minutes": 720},
        ), patch(
            "fm_odds_web.welfare_status", side_effect=scoped_welfare,
        ) as welfare, patch("fm_odds_web.apply_player_aliases") as aliases, patch.object(
            state, "_process_language_learning", return_value={},
        ) as language_learning:
            state._club_refresh_worker()
    finally:
        set_active_save_id(None)

    welfare.assert_called_once_with()
    language_learning.assert_called_once()
    aliases.assert_called_once()
    assert aliases.call_args.args[1] == "account-a"
    assert state.club_error is None
    assert state.club_profiles[42]["team"]["team_type"] == "club"
