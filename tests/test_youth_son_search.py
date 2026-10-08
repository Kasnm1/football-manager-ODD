"""Isolated son discovery regression; never opens FM or account files."""
from __future__ import annotations

import struct
from datetime import date
from types import SimpleNamespace

import pytest
from tools import youth_intake as youth
from tools.database_index import DatabaseIndex


def make_search(monkeypatch, *, wrong_club=False):
    team, club, person, player, contract = 0x5000, 0x6000, 0x7000, 0x6F00, 0x8000
    old_person, invalid_person = 0x7100, 0x7200
    layout = SimpleNamespace(
        key="fm26", team_vtable_rva=0x200,
        player_person_offset=0x100, player_and_non_player_person_offset=0x180,
        actual_player_vtable_rvas=(0x300,), player_and_non_player_vtable_rvas=(0x400,),
    )
    def header(uid):
        return struct.pack("<QI", 0x100900, 0) + struct.pack("<I", uid)
    person_raw = bytearray(0xB0)
    struct.pack_into("<Q", person_raw, 0xA8, contract)
    contract_raw = struct.pack("<QQQ", 0x100A00, person, team)
    class Directory:
        def __init__(self):
            self.forced = []
        def addresses(self, name, *, force=False):
            self.forced.append((name, force))
            return (person, old_person, invalid_person)
        def player_targets(self):
            # Models normal group classification omitting the valid new person.
            return {}
    directory = Directory()
    class Reader:
        module_base = 0x100000
        string_cache = {"old": "cached"}
        def __init__(self):
            self.layout = layout
            self.invalidations = 0
        def invalidate_prefetch(self):
            self.invalidations += 1
        def _fixed_size_snapshots(self, addresses, size):
            rows = {
                person: header(101) if size == 0x10 else bytes(person_raw),
                old_person: header(100), invalid_person: header(102),
                contract: contract_raw,
            }
            return {a: rows[a] for a in addresses if a in rows}
        def ptr(self, address):
            return {player: 0x100300, team: 0x100200,
                    team + youth.TEAM_CLUB: club + int(wrong_club)}.get(address, 0)
        def u32(self, address):
            return 42 if address == team + 0x0C else 0
    reader = Reader()
    monkeypatch.setattr(youth, "resolve_team_club", lambda *_a, **_k: SimpleNamespace(
        team_address=team, club_address=club))
    monkeypatch.setattr(youth, "_validated_player_person", lambda _r, a, uid: (
        person if (a, uid) == (player, 101) else pytest.fail("unsafe player selected")))
    monkeypatch.setattr(youth, "_player", lambda *_a, **_k: {
        "id": 101, "name": "New Youth", "address": hex(player)})
    monkeypatch.setattr(youth, "_movement_snapshot", lambda *_a: {"loan": None})
    monkeypatch.setattr(youth, "_attach_database_index", lambda _r: directory)
    monkeypatch.setattr(youth, "_roster_snapshot", lambda *_a: [])
    plan = {"global_player_baseline_complete": True,
            "global_player_baseline_ids": [100], "processed_player_ids": []}
    return reader, directory, plan


def test_manual_search_finds_player_missing_from_normal_directory(monkeypatch):
    reader, directory, plan = make_search(monkeypatch)
    args = (reader, plan, 42, "0x5000", date(2030, 12, 2))
    assert youth._youth_candidate_source_players(*args) == []
    found = youth._youth_candidate_source_players(*args, deep_search=True)
    assert [row["id"] for row in found] == [101]
    assert found[0]["candidate_source"] == "primary_contract"
    assert directory.forced == [("person", True)]
    assert reader.invalidations == 1
    assert not reader.string_cache


def test_manual_search_rejects_other_club_and_baseline_players(monkeypatch):
    reader, directory, plan = make_search(monkeypatch, wrong_club=True)
    assert youth._youth_candidate_source_players(
        reader, plan, 42, "0x5000", date(2030, 12, 2), deep_search=True) == []
    targets = youth._fresh_youth_player_targets(reader, directory, plan)
    assert set(targets) == {101}  # Existing UID 100 and invalid object 102 excluded.


def test_contract_recovery_survives_unreadable_roster(monkeypatch):
    reader, _directory, plan = make_search(monkeypatch)
    def unreadable(*_args):
        raise RuntimeError("roster unavailable")
    monkeypatch.setattr(youth, "_roster_snapshot", unreadable)
    found = youth._youth_candidate_source_players(
        reader, plan, 42, "0x5000", date(2030, 12, 2), deep_search=True)
    assert [row["id"] for row in found] == [101]


def test_roster_error_is_preserved_when_contract_recovery_finds_nothing(monkeypatch):
    reader, _directory, plan = make_search(monkeypatch, wrong_club=True)
    def unreadable(*_args):
        raise RuntimeError("roster unavailable")
    monkeypatch.setattr(youth, "_roster_snapshot", unreadable)
    with pytest.raises(RuntimeError, match="roster unavailable"):
        youth._youth_candidate_source_players(
            reader, plan, 42, "0x5000", date(2030, 12, 2), deep_search=True)


def test_deep_search_does_not_expand_legacy_plan_without_global_baseline(monkeypatch):
    reader, directory, plan = make_search(monkeypatch)
    plan["global_player_baseline_complete"] = False
    assert youth._fresh_youth_player_targets(reader, directory, plan) == {}
    assert directory.forced == []


def test_ambiguous_player_uid_is_rejected(monkeypatch):
    reader, directory, plan = make_search(monkeypatch)
    directory.addresses = lambda *_a, **_k: (0x7000, 0x7300)
    reader._fixed_size_snapshots = lambda *_a: {
        a: struct.pack("<QII", 0x100900, 0, 101) for a in (0x7000, 0x7300)}
    reader.ptr = lambda a: 0x100300 if a in (0x6F00, 0x7200) else 0
    monkeypatch.setattr(youth, "_validated_player_person", lambda _r, a, _uid: a + 0x100)
    assert youth._fresh_youth_player_targets(reader, directory, plan) == {}


def test_manual_son_apply_requests_deep_search_without_clearing_reservation(monkeypatch):
    plan = {"status": "waiting", "selected_player_id": 101,
            "locked_candidate_ids": [101], "armed_at": "original"}
    monkeypatch.setattr(youth, "_load", lambda _s: {
        "plans": {"42:academy_son": plan}})
    monkeypatch.setattr(youth, "youth_plans_for_team", lambda *_a: {"academy_son": plan})
    calls = []
    monkeypatch.setattr(youth, "finalize_academy_son_attributes", lambda *a, **kw: (
        calls.append(kw) or {"finalized": False, "reason": "waiting"}))
    result = youth.apply_youth_plan("scope", 42, "0x5000", "academy_son", "2030-12-02",
                                   manager_id=7)
    assert result["status"] == "waiting"
    assert calls[0]["deep_search"] is True
    assert calls[0]["allow_shortfall"] is True
    assert plan["selected_player_id"] == 101
    assert plan["locked_candidate_ids"] == [101]


def test_registered_table_can_be_explicitly_refreshed():
    calls = []
    index = object.__new__(DatabaseIndex)
    index._refresh_table = lambda name, **kw: (
        calls.append((name, kw)) or SimpleNamespace(addresses=(0x7000,)))
    assert index.addresses("person", force=True) == (0x7000,)
    assert calls == [("person", {"force": True})]
