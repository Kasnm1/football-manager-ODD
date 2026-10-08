"""Offline regressions for group reads; every FM/account boundary is mocked."""
from contextlib import ExitStack, nullcontext
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import fm_odds_web
from tools import club_reader, world_clubs


def metrics_fixture(month="2026-07"):
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-a", "game_date": "2026-07-29"}
    state._bind_current_save = lambda: "scope-a"
    state._data_scope_id = lambda _output: "scope-a"
    state._world_club_native_cache = lambda _save: None
    state._owned_world_club_address_overrides = lambda _save: {}
    state._apply_owned_world_club_addresses = lambda directory, _save: directory
    stack = ExitStack()
    owned = [{"id": i, "name": f"Club {i}"} for i in range(1, 21)]
    stack.enter_context(patch("fm_odds_web.account_acquired_clubs", return_value=owned))
    stack.enter_context(patch("fm_odds_web.load_economy", return_value={"club_dividend_last_settlement_month": month}))
    stack.enter_context(patch("fm_odds_web.sync_acquired_club_metric_history", return_value={}))
    stack.enter_context(patch("fm_odds_web.owned_club_league_standing", return_value=None))
    stack.enter_context(patch("fm_odds_web.owned_club_dividend_projection", return_value=None))
    stack.enter_context(patch("fm_odds_web.public_economy", return_value={}))
    stack.enter_context(patch("fm_odds_web.archive_club_dividend_mail"))
    native = stack.enter_context(patch("fm_odds_web.read_native_world_club_metrics", side_effect=lambda clubs: {
        row["id"]: {"balance": row["id"] * 100, "transfer_budget": 100, "current_valuation": 500}
        for row in clubs
    }))
    settle = stack.enter_context(patch("fm_odds_web.settle_club_dividends", return_value={"paid_count": 0, "forecasts": []}))
    return stack, state, native, settle


def test_partial_metrics_reads_one_of_twenty_clubs_without_settling_full_month():
    stack, state, native, settle = metrics_fixture()
    with stack:
        result = state.owned_world_club_metrics(team_ids=(7,))
    assert [row["id"] for row in native.call_args.args[0]] == [7]
    assert set(result["metrics"]) == {"7"}
    assert result["metrics_complete"] is False
    assert result["summary"] is None
    settle.assert_not_called()


def test_partial_metrics_at_month_change_expands_to_all_clubs_for_dividends():
    stack, state, native, settle = metrics_fixture(month="2026-06")
    with stack:
        result = state.owned_world_club_metrics(team_ids=(7,))
    assert len(native.call_args.args[0]) == 20
    assert result["metrics_complete"] is True
    assert result["summary"]["club_count"] == 20
    assert len(settle.call_args.args[1]) == 20
    assert set(settle.call_args.args[2]) == set(range(1, 21))


def test_partial_metrics_rejects_club_outside_account_before_native_read():
    stack, state, native, settle = metrics_fixture()
    with stack, pytest.raises(ValueError):
        state.owned_world_club_metrics(team_ids=(21,))
    native.assert_not_called()
    settle.assert_not_called()


def test_owned_refresh_can_return_metrics_after_rebinding_addresses():
    state = SimpleNamespace(
        lock=threading.RLock(), memory_lock=threading.RLock(),
        output={"save_instance_id": "save-a"}, world_club_scanning=False,
        _bind_current_save=lambda: "scope-a", _data_scope_id=lambda _out: "scope-a",
        _world_club_native_cache=lambda _save: None,
    )
    def metrics():
        assert state.owned_world_club_addresses == {10: "0x5000"}
        return {"data_scope_id": "scope-a", "metrics": {"10": {"balance": 100}}}
    state.owned_world_club_metrics = MagicMock(side_effect=metrics)
    with (
        patch("fm_odds_web.account_acquired_clubs", return_value=[{"id": 10}]),
        patch("fm_odds_web.world_club_team_address_hints", return_value=set()),
        patch("fm_odds_web.resolve_native_team_addresses", return_value={10: "0x5000"}),
    ):
        result = fm_odds_web.LocalOddsState.refresh_owned_world_clubs(state, {"include_metrics": True})
    assert result["metrics_snapshot"]["metrics"]["10"]["balance"] == 100
    state.owned_world_club_metrics.assert_called_once_with()


@pytest.mark.parametrize("layout_key", ["fm24", "fm26"])
def test_staff_batch_matches_single_team_results_with_one_pool_walk(layout_key):
    # Twenty teams and 100 people, including unrelated staff and multi-team contracts.
    teams = set(range(1000, 1020))
    contracts = {person: [person * 16] for person in range(1, 101)}
    membership = {person * 16 + 0x10: 1000 + person % 25 for person in range(1, 101)}
    contracts[1].append(0xA000)
    membership[0xA000 + 0x10] = 1002
    reader = SimpleNamespace(layout=SimpleNamespace(key=layout_key), ptr=lambda address: membership.get(address, 0))
    entries = [(person, values[0]) for person, values in contracts.items()]
    def candidate(_reader, person, team, _manager, **_kwargs):
        if team not in {membership[c + 0x10] for c in contracts[person]}:
            return None
        return {"id": person, "name": f"Person {person:03}"}
    with (
        patch.object(club_reader, "_staff_scan_entries", return_value=entries) as pool,
        patch.object(club_reader, "_person_contract_candidates", side_effect=lambda _reader, person: contracts[person]),
        patch.object(club_reader, "_staff_candidate", side_effect=candidate) as parse,
    ):
        expected = {team: club_reader._scan_staff(reader, team, 0) for team in teams}
        old_walks, old_parses = pool.call_count, parse.call_count
        pool.reset_mock(); parse.reset_mock()
        actual = club_reader._scan_staff_for_teams(reader, teams)
        new_walks, new_parses = pool.call_count, parse.call_count
    assert actual == expected
    assert (old_walks, new_walks) == (20, 1)
    if layout_key == "fm26":
        assert old_parses == 2000
        assert new_parses == 81


def test_group_people_reuses_reader_and_skips_finances_and_valuation():
    reader = SimpleNamespace(
        layout=SimpleNamespace(game_date_rva=None, staff_complete_object_offset=0, staff_person_vtable_rva=None),
        module=SimpleNamespace(base_address=0), ptr=lambda _address: 0,
        roster=lambda address: [{"id": address, "name": "Player", "ca": 100}],
    )
    def resolve(_reader, club):
        if club["id"] == 30:
            raise ValueError("俱乐部地址已失效，请重新扫描俱乐部")
        return club["id"] * 100, {"id": club["id"]}
    with (
        patch.object(world_clubs, "borrow_game_reader", return_value=nullcontext(reader)) as borrow,
        patch.object(world_clubs, "_resolve_world_club_team", side_effect=resolve),
        patch.object(club_reader, "_scan_staff_for_teams", return_value={1000: [], 2000: []}) as staff,
        patch.object(club_reader, "read_roster_player_profile", side_effect=lambda _reader, row, *_args: {**row, "fitness": 95}) as profiles,
        patch.object(club_reader, "_club_information") as finances,
        patch.object(world_clubs, "club_acquisition_valuation") as value,
    ):
        result = world_clubs.read_native_world_club_group_people([{"id": i} for i in (10, 20, 30)])
    borrow.assert_called_once_with()
    staff.assert_called_once_with(reader, {1000, 2000})
    assert profiles.call_count == 2
    assert result[10]["players"][0]["fitness"] == 95
    assert result[30]["error"]
    assert result[20]["staff"] == []
    finances.assert_not_called(); value.assert_not_called()


def test_group_people_keeps_one_failed_club_and_player_manager_override():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock(); state.memory_lock = threading.RLock()
    state.output = {}
    state._bind_current_save = lambda: "scope-a"
    state._owned_world_club_targets = MagicMock(side_effect=ValueError("missing"))
    state._owned_world_club_target = MagicMock(side_effect=[("scope-a", {}, {"id": 10}), ValueError("missing")])
    with (
        patch("fm_odds_web.account_acquired_clubs", return_value=[{"id": 10}, {"id": 20}]),
        patch("fm_odds_web.player_manager_for_team", return_value={"id": 77, "name": "Manager"}),
        patch("fm_odds_web.read_native_world_club_group_people", return_value={10: {
            "players": [{"id": 1}], "staff": [{"id": 77}], "manager": None,
        }}) as read,
    ):
        result = state.owned_world_club_group_people()
    read.assert_called_once_with([{"id": 10}])
    assert result["player_count"] == 1
    assert result["staff_count"] == 1
    assert result["clubs"][0]["staff"][0]["is_manager"] is True
    assert result["clubs"][1]["error"] == "missing"
