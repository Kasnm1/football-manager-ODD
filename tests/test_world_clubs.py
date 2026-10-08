from __future__ import annotations

import unittest
import json
import struct
import threading
from io import BytesIO
from datetime import date
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import ANY, patch

import tools.world_clubs as world_clubs
from tools.app_paths import active_save_id
from tools.world_clubs import (
    _atomic_write_json, _looks_non_mens, _native_region_from_nation,
    acquire_club, acquired_clubs_for_game_date,
    apply_acquired_club_overrides, build_world_club_directory_index,
    build_world_clubs,
    club_acquisition_valuation, club_investment_performance,
    club_portfolio_metrics, club_portfolio_summary,
    enrich_world_club_reputation_ranks, enrich_world_club_standings,
    load_acquired_clubs, load_native_world_clubs,
    merge_native_world_clubs, paginate_world_clubs,
    recover_acquired_clubs_from_transactions,
    read_native_world_club_detail,
    read_native_world_club_results,
    read_native_world_club_staff,
    resolve_native_team_addresses,
    set_portfolio_group_name,
    sync_acquired_club_manager_record,
)


def test_related_club_squads_discovers_verified_u21_and_u18(monkeypatch):
    first, under_21, under_18, club = 0x1000, 0x2000, 0x3000, 0x9000
    teams = {
        first: {"id": 679, "squad_type_code": 0},
        under_21: {"id": 2000339955, "squad_type_code": 10},
        under_18: {"id": 2000339956, "squad_type_code": 12},
    }

    class FakeReader:
        layout = SimpleNamespace(team_type_offset=0x28, team_vtable_rva=0x500)

        @staticmethod
        def team(address):
            return {**teams[address], "team_type": "club"}

        @staticmethod
        def ptr(address):
            return club if address in {team + 0x30 for team in teams} else 0

        @staticmethod
        def u8(address):
            return {first + 0x28: 0, under_21 + 0x28: 10, under_18 + 0x28: 12}.get(address)

    directory = SimpleNamespace(
        addresses_for_vtable=lambda _name, _rva: tuple(teams),
    )
    monkeypatch.setattr(world_clubs, "database_index_for_reader", lambda _reader: directory)

    rows = world_clubs._related_club_squads(FakeReader(), first)

    assert [(row[1]["id"], row[1]["squad_type_code"]) for row in rows] == [
        (679, 0), (2000339955, 10), (2000339956, 12),
    ]


def test_related_club_squads_prefers_verified_club_linked_team_vector(monkeypatch):
    first, under_21, under_19, club = 0x1000, 0x2000, 0x3000, 0x9000
    begin = 0xA000
    teams = {
        first: {"id": 602, "squad_type_code": 0},
        under_21: {"id": 2000779135, "squad_type_code": 10},
        under_19: {"id": 2000779134, "squad_type_code": 11},
    }

    class FakeReader:
        module_base = 0x100000
        layout = SimpleNamespace(team_type_offset=0x28, team_vtable_rva=0x500)

        @staticmethod
        def team(address):
            return {**teams[address], "team_type": "club"}

        @staticmethod
        def ptr(address):
            if address in {team + 0x30 for team in teams}:
                return club
            if address in teams:
                return 0x100500
            return 0

        @staticmethod
        def u8(address):
            return {
                first + 0x28: 0,
                under_21 + 0x28: 10,
                under_19 + 0x28: 11,
            }.get(address)

        @staticmethod
        def bytes(address, size):
            if address == club + 0x18 and size == 24:
                return struct.pack("<QQQ", begin, begin + 24, begin + 24)
            return None

        @staticmethod
        def ptr_array(address, count):
            assert (address, count) == (begin, 3)
            return [first, under_21, under_19]

    monkeypatch.setattr(
        world_clubs, "database_index_for_reader",
        lambda _reader: (_ for _ in ()).throw(AssertionError("directory fallback should not run")),
    )

    rows = world_clubs._related_club_squads(FakeReader(), first)

    assert [(row[1]["id"], row[1]["squad_type_code"]) for row in rows] == [
        (602, 0), (2000779135, 10), (2000779134, 11),
    ]


def test_acquisition_quote_uses_summary_roster_without_full_profiles(monkeypatch):
    class FakeReader:
        @staticmethod
        def team(address):
            assert address == 0x1000
            return {
                "id": 42, "team_type": "club", "name": "Test Club",
                "short_name": "Test", "reputation": 7000,
            }

        @staticmethod
        def roster_valuation(address):
            assert address == 0x1000
            return [{"id": 7, "ca": 150, "pa": 170}]

        @staticmethod
        def roster(_address):
            raise AssertionError("full roster must not be read for an acquisition quote")

    monkeypatch.setattr(
        world_clubs, "borrow_game_reader", lambda: nullcontext(FakeReader()),
    )
    monkeypatch.setattr(
        "tools.club_reader._club_information",
        lambda _reader, _address: {"finances": {"balance": 1_000_000}},
    )

    result = world_clubs.read_native_world_club_acquisition_quote({
        "id": 42, "name": "Cached Club", "address": "0x1000",
        "competition": "Test League",
    })

    assert result["club"]["name"] == "Test Club"
    assert result["acquisition"]["price"] > 0


def test_player_membership_reads_roster_summary_without_expanding_profile(monkeypatch):
    class FakeReader:
        layout = SimpleNamespace(team_type_offset=None)

        @staticmethod
        def team(address):
            assert address == 0x1000
            return {"id": 42, "team_type": "club"}

        @staticmethod
        def roster(address):
            assert address == 0x1000
            return [{"id": 7, "name": "Player"}, {"id": 8, "name": "Other"}]

    monkeypatch.setattr(
        world_clubs, "borrow_game_reader", lambda: nullcontext(FakeReader()),
    )
    monkeypatch.setattr(
        "tools.club_reader.read_roster_player_profile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("player profile must not be expanded for membership")
        ),
    )

    result = world_clubs.read_native_world_club_player_membership(
        {"id": 42, "address": "0x1000"}, 7,
    )

    assert result == {
        "id": 7, "name": "Player", "squad_team_id": 42,
        "squad_team_address": "0x1000",
    }


def test_world_club_target_relocates_by_uid_before_using_cached_address(monkeypatch):
    stale_address = 0x1000
    fresh_address = 0x2000
    fresh_club = 0x3000

    class FakeReader:
        module_base = 0x100000
        layout = SimpleNamespace(club_vtable_rva=0x500)

        @staticmethod
        def team(address):
            if address == fresh_address:
                return {"id": 42, "team_type": "club", "name": "Fresh Club"}
            return None

        @staticmethod
        def ptr(address):
            return {
                fresh_address + world_clubs.TEAM_CLUB: fresh_club,
                fresh_club: 0x100500,
            }.get(address, 0)

    monkeypatch.setattr(
        world_clubs, "resolve_team_club",
        lambda _reader, hint, team_id: SimpleNamespace(
            team_address=fresh_address, club_address=fresh_club, team_id=team_id,
        ),
    )

    address, team = world_clubs._resolve_world_club_team(
        FakeReader(), {"id": 42, "address": hex(stale_address)},
    )

    assert address == fresh_address
    assert team["name"] == "Fresh Club"


def test_world_club_reads_prefer_uid_resolved_team_over_stale_address(monkeypatch):
    stale_address, fresh_address = 0x1000, 0x2200

    class FakeReader:
        layout = SimpleNamespace()

        @staticmethod
        def team(address):
            assert address == fresh_address
            return {"id": 42, "team_type": "club", "name": "Fresh Club"}

    calls = []

    def resolve(_reader, address, team_id):
        calls.append((address, team_id))
        return SimpleNamespace(
            team_address=fresh_address, club_address=0x3200, team_id=team_id,
        )

    monkeypatch.setattr(world_clubs, "resolve_team_club", resolve)

    address, team = world_clubs._resolve_world_club_team(
        FakeReader(), {"id": 42, "address": hex(stale_address)},
    )

    assert calls == [(stale_address, 42)]
    assert address == fresh_address
    assert team["name"] == "Fresh Club"


def test_world_club_reads_fall_back_to_validated_address_when_uid_directory_fails(monkeypatch):
    class FakeReader:
        layout = SimpleNamespace()

        @staticmethod
        def team(address):
            assert address == 0x1000
            return {"id": 42, "team_type": "club"}

    monkeypatch.setattr(
        world_clubs, "resolve_team_club",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("目录暂不可用")),
    )

    address, team = world_clubs._resolve_world_club_team(
        FakeReader(), {"id": 42, "address": "0x1000"},
    )

    assert address == 0x1000
    assert team["id"] == 42


class WorldClubDirectoryTests(unittest.TestCase):
    def test_prepared_directory_index_preserves_filter_and_search_results(self) -> None:
        directory = {"clubs": [
            {
                "id": 1, "name": "United", "reputation": 9000,
                "continent": "欧洲", "nation": "英格兰",
                "competition": "英超", "competitions": ["英超"],
            },
            {
                "id": 2, "name": "United", "reputation": 7000,
                "continent": "欧洲", "nation": "苏格兰",
                "competition": "苏超", "competitions": ["苏超"],
            },
            {
                "id": 3, "name": "未分类球队", "reputation": 5000,
                "continent": "世界", "nation": "未分类",
                "competition": "未分类", "competitions": [],
            },
        ]}
        prepared = build_world_club_directory_index(directory)
        cases = [
            {}, {"continent": "欧洲"}, {"nation": "英格兰"},
            {"search": "United"}, {"search": "未分类球队"},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                expected = paginate_world_clubs(directory, **arguments)
                actual = paginate_world_clubs(
                    directory, directory_index=prepared, **arguments,
                )
                self.assertEqual(actual, expected)

    def test_world_club_projection_cache_tracks_account_name_overrides(self) -> None:
        import fm_odds_web

        address_overrides: dict[int, str] = {}
        state = SimpleNamespace(
            lock=threading.RLock(),
            world_club_projection_cache_key=None,
            world_club_projection_cache=None,
            _owned_world_club_address_overrides=lambda _save_id: address_overrides,
        )
        directory = {
            "generated_at": "now", "_projection_source_key": ("base",),
            "clubs": [{
                "id": 42, "name": "Original", "short_name": "Original",
                "continent": "欧洲", "nation": "英格兰",
                "competition": "英超", "competitions": ["英超"],
                "reputation": 7000,
            }],
        }
        first = fm_odds_web.LocalOddsState._world_club_directory_projection(
            state, directory, "save-a", [],
        )
        second = fm_odds_web.LocalOddsState._world_club_directory_projection(
            state, directory, "save-a", [],
        )
        self.assertIs(second, first)

        acquired = [{"id": 42, "renamed_short_name": "New"}]
        renamed = apply_acquired_club_overrides(directory, acquired)
        third = fm_odds_web.LocalOddsState._world_club_directory_projection(
            state, renamed, "save-a", acquired,
        )
        self.assertIsNot(third, first)
        self.assertEqual(third["clubs_by_id"][42]["short_name"], "New")

    def test_world_club_directory_reuses_base_build_and_returns_copies(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(), data_version=3,
            world_club_directory_cache_key=None,
            world_club_directory_cache=None,
        )
        output = {"game_date": "2026-08-17"}
        native = {
            "generated_at": "now", "game_key": "fm26",
            "addresses_current": True,
            "clubs": [{
                "id": 42, "name": "Test Club", "short_name": "Test",
                "reputation": 7000, "address": "0x1000",
            }],
        }
        with (
            patch(
                "fm_odds_web.build_world_clubs",
                return_value={"clubs": [{"id": 42, "name": "Base"}]},
            ) as build,
            patch(
                "fm_odds_web.merge_native_world_clubs",
                return_value={"clubs": [{"id": 42, "name": "Test Club"}]},
            ),
            patch("fm_odds_web.public_league_standings", return_value={}),
        ):
            first = fm_odds_web.LocalOddsState._world_club_directory(
                state, output, "save-a", native,
            )
            first["clubs"][0]["name"] = "Mutated"
            second = fm_odds_web.LocalOddsState._world_club_directory(
                state, output, "save-a", native,
            )

        self.assertEqual(build.call_count, 1)
        self.assertEqual(second["clubs"][0]["name"], "Test Club")

    def test_request_scope_binding_is_applied_in_a_fresh_http_thread(self) -> None:
        import fm_odds_web

        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.lock = threading.RLock()
        state.cache_verified = True
        state.output = {
            "save_instance_id": "career-a",
            "account_scope_id": "scope-a",
        }
        state.wallet_ready = False
        state.connection_scope_id = None
        observed = []

        def worker() -> None:
            observed.append(active_save_id())
            observed.append(state._bind_request_scope())
            observed.append(active_save_id())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [None, "scope-a", "scope-a"])

    def test_unidentified_save_is_not_treated_as_verified(self) -> None:
        import fm_odds_web

        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.cache_verified = True
        state.output = {"save_instance_id": "unidentified"}

        self.assertFalse(state._has_verified_save())

    def test_http_entrypoints_bind_scope_before_dispatch(self) -> None:
        import fm_odds_web

        events = []
        handler = fm_odds_web.Handler.__new__(fm_odds_web.Handler)
        handler.state = SimpleNamespace(
            _bind_request_scope=lambda: events.append("bind"),
        )
        handler.path = "/api/state"
        handler._do_GET = lambda: events.append("get")
        handler.do_GET()
        self.assertEqual(events, ["bind", "get"])

        events.clear()
        handler.path = "/api/unknown"
        handler.headers = {"Content-Length": "0"}
        handler.rfile = BytesIO(b"")
        handler._json = lambda status, payload: events.append(
            ("json", int(status), payload),
        )
        handler.do_POST()
        self.assertEqual(events[0], "bind")
        self.assertEqual(events[1][0:2], ("json", 404))

    def test_native_cache_path_uses_explicit_career_scope(self) -> None:
        with patch.object(
            world_clubs, "cache_data_root", return_value=Path("cache-root"),
        ) as cache_root:
            path = world_clubs._native_cache_path("fm26", "career-a")

        cache_root.assert_called_once_with("career-a")
        self.assertEqual(
            path, Path("cache-root") / "world" / "native_clubs_fm26_career-a.json",
        )

    def test_world_scan_worker_binds_account_scope_in_real_thread(self) -> None:
        import fm_odds_web

        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.RLock()
        state.output = {
            "save_instance_id": "career-a",
            "account_scope_id": "scope-a",
        }
        state.world_club_scan_progress = 0
        state.world_club_scan_text = None
        state.world_club_scan_error = None
        state.world_club_scanning = True
        state.world_club_cache = None
        state.world_club_cache_save_id = None
        state._sync_youth_generation_plans = lambda *_args: {}
        observed_scopes = []

        def save_cache(_payload, _save_id):
            observed_scopes.append(active_save_id())

        with (
            patch("fm_odds_web.scan_native_world_clubs", return_value={
                "game_key": "fm26", "clubs": [], "nations": [],
            }),
            patch("fm_odds_web.save_native_world_clubs", side_effect=save_cache),
        ):
            worker = threading.Thread(
                target=state._world_club_scan_worker,
                args=("scope-a", "career-a", []),
            )
            worker.start()
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(observed_scopes, ["scope-a"])
        self.assertFalse(state.world_club_scanning)

    def test_portfolio_group_name_is_trimmed_and_persisted(self) -> None:
        payload = {"schema_version": 1, "clubs": []}
        with patch.object(world_clubs, "update_document") as update:
            update.side_effect = (
                lambda _key, _default, mutator, _scope, **_kwargs: mutator(payload)
            )
            result = set_portfolio_group_name("  星海集团  ", "account-a")
        self.assertEqual(result, "星海集团")
        self.assertEqual(payload["group_name"], "星海集团")
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], "acquired_clubs")
        self.assertEqual(update.call_args.args[3], "account-a")

    def test_group_rename_uses_current_account_scope(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(), _bind_current_save=lambda: "account-a",
        )
        with patch.object(
            fm_odds_web, "set_portfolio_group_name", return_value="星海集团",
        ) as rename:
            result = fm_odds_web.LocalOddsState.rename_owned_group(
                state, {"name": "星海集团"},
            )
        self.assertEqual(result, {"group_name": "星海集团"})
        rename.assert_called_once_with("星海集团", "account-a")

    def test_reputation_ranks_are_calculated_within_current_league_and_nation(self) -> None:
        result = enrich_world_club_reputation_ranks({"clubs": [
            {"id": 1, "name": "Alpha", "competition_id": 10, "competition": "甲级联赛", "nation": "中国", "reputation": 900},
            {"id": 2, "name": "Beta", "competition_id": 10, "competition": "甲级联赛", "nation": "中国", "reputation": 900},
            {"id": 3, "name": "Gamma", "competition_id": 11, "competition": "乙级联赛", "nation": "中国", "reputation": 800},
            {"id": 4, "name": "Delta", "competition_id": 12, "competition": "丙级联赛", "nation": "日本", "reputation": 950},
        ]})
        rows = {row["id"]: row for row in result["clubs"]}
        self.assertEqual((rows[1]["competition_reputation_rank"], rows[1]["competition_reputation_total"]), (1, 2))
        self.assertEqual((rows[2]["competition_reputation_rank"], rows[2]["competition_reputation_total"]), (1, 2))
        self.assertEqual((rows[3]["competition_reputation_rank"], rows[3]["competition_reputation_total"]), (1, 1))
        self.assertEqual((rows[1]["nation_reputation_rank"], rows[1]["nation_reputation_total"]), (1, 3))
        self.assertEqual((rows[2]["nation_reputation_rank"], rows[2]["nation_reputation_total"]), (1, 3))
        self.assertEqual((rows[3]["nation_reputation_rank"], rows[3]["nation_reputation_total"]), (3, 3))
        self.assertEqual((rows[4]["nation_reputation_rank"], rows[4]["nation_reputation_total"]), (1, 1))

    def test_owned_refresh_resolves_only_acquired_team_ids(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.RLock(),
            output={"save_instance_id": "save-a"},
            world_club_scanning=False,
            owned_world_club_addresses={},
            owned_world_club_address_save_id=None,
            _bind_current_save=lambda: "scope-a",
            _data_scope_id=lambda _output: "scope-a",
            _world_club_native_cache=lambda _save_id: {
                "clubs": [
                    {"id": 10, "address": "0x2000"},
                    {"id": 20, "address": "0x3000"},
                    {"id": 30, "address": "0x4000"},
                ],
            },
        )
        calls = []

        def resolve(
            team_ids, hints, *, scan_all_if_unresolved=False,
            known_addresses=None,
        ):
            calls.append((
                set(team_ids), set(hints), scan_all_if_unresolved,
                dict(known_addresses or {}),
            ))
            return {10: "0x5000", 20: "0x6000"}

        with (
            patch(
                "fm_odds_web.load_acquired_clubs",
                return_value={"clubs": [{"id": 10}, {"id": 20}]},
            ),
            patch("fm_odds_web.world_club_team_address_hints", return_value={0x1000}),
            patch("fm_odds_web.resolve_native_team_addresses", side_effect=resolve),
        ):
            result = fm_odds_web.LocalOddsState.refresh_owned_world_clubs(state)

        self.assertEqual(result["refreshed"], 2)
        self.assertEqual(result["missing"], [])
        self.assertFalse(result["partial"])
        self.assertEqual(calls, [(
            {10, 20}, {0x1000, 0x2000, 0x3000}, False,
            {10: "0x2000", 20: "0x3000"},
        )])
        self.assertEqual(state.owned_world_club_addresses, {10: "0x5000", 20: "0x6000"})

    def test_owned_refresh_keeps_resolved_clubs_when_one_is_missing(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.RLock(),
            output={"save_instance_id": "save-a"},
            world_club_scanning=False,
            owned_world_club_addresses={},
            owned_world_club_address_save_id=None,
            _bind_current_save=lambda: "scope-a",
            _data_scope_id=lambda _output: "scope-a",
            _world_club_native_cache=lambda _save_id: {"clubs": []},
        )
        calls = []

        def resolve(team_ids, hints, **kwargs):
            calls.append((set(team_ids), set(hints), kwargs))
            return {10: "0x5000"} if len(calls) == 1 else {}

        with (
            patch(
                "fm_odds_web.load_acquired_clubs",
                return_value={"clubs": [{"id": 10}, {"id": 20}]},
            ),
            patch("fm_odds_web.world_club_team_address_hints", return_value=set()),
            patch(
                "fm_odds_web.resolve_native_team_addresses",
                side_effect=resolve,
            ),
        ):
            result = fm_odds_web.LocalOddsState.refresh_owned_world_clubs(state)

        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(result["missing"], [20])
        self.assertTrue(result["partial"])
        self.assertEqual(state.owned_world_club_addresses, {10: "0x5000"})
        self.assertEqual(calls, [
            ({10, 20}, set(), {
                "scan_all_if_unresolved": False, "known_addresses": {},
            }),
            ({20}, set(), {
                "scan_all_if_unresolved": True, "known_addresses": {},
            }),
        ])

    def test_owned_refresh_full_scan_recovers_stale_saved_address(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.RLock(),
            output={"save_instance_id": "save-a"},
            world_club_scanning=False,
            owned_world_club_addresses={},
            owned_world_club_address_save_id=None,
            _bind_current_save=lambda: "scope-a",
            _data_scope_id=lambda _output: "scope-a",
            _world_club_native_cache=lambda _save_id: {"clubs": []},
        )
        calls = []

        def resolve(team_ids, hints, **kwargs):
            calls.append((set(team_ids), kwargs))
            return {} if len(calls) == 1 else {42: "0x9000"}

        with (
            patch(
                "fm_odds_web.load_acquired_clubs",
                return_value={"clubs": [{"id": 42, "address": "0x1000"}]},
            ),
            patch("fm_odds_web.world_club_team_address_hints", return_value=set()),
            patch("fm_odds_web.resolve_native_team_addresses", side_effect=resolve),
        ):
            result = fm_odds_web.LocalOddsState.refresh_owned_world_clubs(state)

        self.assertEqual(result["missing"], [])
        self.assertEqual(state.owned_world_club_addresses, {42: "0x9000"})
        self.assertEqual(calls, [
            ({42}, {
                "scan_all_if_unresolved": False,
                "known_addresses": {42: "0x1000"},
            }),
            ({42}, {
                "scan_all_if_unresolved": True,
                "known_addresses": {42: "0x1000"},
            }),
        ])

    def test_quick_team_address_resolution_scans_only_hinted_slab(self) -> None:
        module_base = 0x100000
        team_address = 0x1080
        club_address = 0x5000
        team_id = 42
        raw = bytearray(0x200)
        struct.pack_into("<Q", raw, team_address - 0x1000, module_base + 0x200)
        struct.pack_into("<I", raw, team_address - 0x1000 + 0x0C, team_id)

        class FakeLayout:
            module_name = "game_plugin.dll"
            team_vtable_rva = 0x200
            club_vtable_rva = 0x300

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=module_base)

        class FakeReader:
            def __init__(self, *_args):
                pass
                self.layout = FakeLayout()

            @staticmethod
            def ptr(address):
                return {
                    team_address + world_clubs.TEAM_CLUB: club_address,
                    club_address: module_base + 0x300,
                }.get(address, 0)

            @staticmethod
            def u32(address):
                return team_id if address == club_address + 0x0C else 0

        regions = [
            SimpleNamespace(base_address=0x1000, size=len(raw), type=world_clubs.MEM_PRIVATE),
            SimpleNamespace(base_address=0x9000, size=0x100, type=world_clubs.MEM_PRIVATE),
        ]
        reads = []

        def read_memory(_process, address, size):
            reads.append(address)
            if address == 0x1000:
                return bytes(raw[:size])
            raise AssertionError("unhinted region must not be scanned")

        fake_reader = FakeReader()
        fake_reader.layout = FakeLayout()
        fake_reader.module = SimpleNamespace(base_address=module_base)
        fake_reader.process = object()
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(fake_reader),
            ),
            patch("tools.world_clubs.iter_readable_regions", return_value=regions),
            patch("tools.world_clubs.read_process_memory", side_effect=read_memory),
        ):
            result = resolve_native_team_addresses({team_id}, {team_address})

        self.assertEqual(result, {team_id: hex(team_address)})
        self.assertEqual(reads, [0x1000])

    def test_known_team_address_is_validated_without_scanning_a_slab(self) -> None:
        team_id = 42
        team_address = 0x5000

        class FakeLayout:
            module_name = "game_plugin.dll"

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=0x100000)

        class FakeReader:
            def __init__(self, *_args):
                pass

            @staticmethod
            def team(address):
                assert address == team_address
                return {"id": team_id, "team_type": "club"}

        fake_reader = FakeReader()
        fake_reader.layout = FakeLayout()
        fake_reader.module = SimpleNamespace(base_address=0x100000)
        fake_reader.process = object()
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(fake_reader),
            ),
            patch(
                "tools.world_clubs.iter_readable_regions",
                side_effect=AssertionError("validated UID/address must not scan memory"),
            ),
        ):
            result = resolve_native_team_addresses(
                {team_id}, set(), known_addresses={team_id: hex(team_address)},
            )

        self.assertEqual(result, {team_id: hex(team_address)})

    def test_quick_team_address_resolution_can_fallback_to_all_private_regions(self) -> None:
        module_base = 0x100000
        team_address = 0x9080
        club_address = 0xA000
        team_id = 84
        raw = bytearray(0x200)
        struct.pack_into("<Q", raw, team_address - 0x9000, module_base + 0x200)
        struct.pack_into("<I", raw, team_address - 0x9000 + 0x0C, team_id)

        class FakeLayout:
            module_name = "game_plugin.dll"
            team_vtable_rva = 0x200
            club_vtable_rva = 0x300

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=module_base)

        class FakeReader:
            def __init__(self, *_args):
                self.process = object()
                self.module = SimpleNamespace(base_address=module_base)

            @staticmethod
            def ptr(address):
                return {
                    team_address + world_clubs.TEAM_CLUB: club_address,
                    club_address: module_base + 0x300,
                }.get(address, 0)

            @staticmethod
            def u32(address):
                return team_id if address == club_address + 0x0C else 0

        regions = [
            SimpleNamespace(base_address=0x1000, size=0x100, type=world_clubs.MEM_PRIVATE),
            SimpleNamespace(base_address=0x9000, size=len(raw), type=world_clubs.MEM_PRIVATE),
        ]

        def read_memory(_process, address, size):
            if address == 0x1000:
                return bytes(size)
            if address == 0x9000:
                return bytes(raw[:size])
            raise AssertionError(f"unexpected read at {address:#x}")

        fake_reader = FakeReader()
        fake_reader.layout = FakeLayout()
        fake_reader.module = SimpleNamespace(base_address=module_base)
        fake_reader.process = object()
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(fake_reader),
            ),
            patch("tools.world_clubs.iter_readable_regions", return_value=regions),
            patch("tools.world_clubs.read_process_memory", side_effect=read_memory),
        ):
            result = resolve_native_team_addresses(
                {team_id}, set(), scan_all_if_unresolved=True,
            )

        self.assertEqual(result, {team_id: hex(team_address)})

    def test_cached_world_refresh_coalesces_nearby_team_headers(self) -> None:
        module_base = 0x100000
        addresses = (0x2000, 0x2100)
        reputation_offset = 0x86
        raw = bytearray(0x188)
        for index, address in enumerate(addresses):
            offset = address - addresses[0]
            struct.pack_into("<Q", raw, offset, module_base + 0x200)
            struct.pack_into("<I", raw, offset + world_clubs.ENTITY_UID, 40 + index)
            struct.pack_into("<Q", raw, offset + world_clubs.TEAM_CLUB, 0x5000 + index * 0x100)
            struct.pack_into("<Q", raw, offset + world_clubs.TEAM_MANAGER, 0x7000 + index * 0x100)
            struct.pack_into("<H", raw, offset + reputation_offset, 8000 + index)

        class FakeLayout:
            key = "fm26"
            game_version = "26.3.2"
            executable_sha256 = "build-hash"
            display_name = "FM26"
            module_name = "game_plugin.dll"
            team_vtable_rva = 0x200
            team_reputation_offset = reputation_offset

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=module_base)

        class FakeReader:
            def __init__(self, *_args):
                self.process = object()
                self.module = SimpleNamespace(base_address=module_base)

            @staticmethod
            def competition(_address):
                return None

        payload = {
            "schema_version": 4,
            "game_key": "fm26", "game_version": "26.3.2",
            "build_identity": "build-hash", "save_id": "save-a",
            "process_id": 123, "module_base": hex(module_base),
            "clubs": [
                {"id": 40 + index, "name": f"Club {index}", "address": hex(address)}
                for index, address in enumerate(addresses)
            ],
        }
        with (
            patch("tools.world_clubs.select_process_layout", return_value=(123, "fm.exe", FakeLayout())),
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(FakeReader()),
            ),
            patch("tools.world_clubs.read_process_memory", return_value=bytes(raw)) as read,
        ):
            refreshed = world_clubs.refresh_native_world_clubs(payload, "save-a")

        read.assert_called_once_with(ANY, addresses[0], len(raw))
        self.assertEqual(refreshed["scan_mode"], "cached_addresses")
        self.assertEqual([row["reputation"] for row in refreshed["clubs"]], [8001, 8000])

    def test_portfolio_metrics_batch_reuses_one_process_and_core_rosters(self) -> None:
        module_base = 0x100000

        class FakeLayout:
            key = "fm24"
            module_name = "fm.exe"

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=module_base)

        class FakeReader:
            def __init__(self, *_args):
                self.roster_calls = []

            @staticmethod
            def team(address):
                return {"id": address, "team_type": "club", "reputation": 7000}

            def roster_valuation(self, address):
                self.roster_calls.append(address)
                return [{"id": address + 1, "ca": 150, "pa": 170}]

            def roster(self, _address):
                raise AssertionError("full roster must not be read for portfolio cards")

        clubs = [
            {"id": 10, "address": "0xa"},
            {"id": 20, "address": "0x14"},
        ]
        fake_reader = FakeReader()
        fake_reader.layout = FakeLayout()
        fake_reader.module = SimpleNamespace(base_address=module_base)
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(fake_reader),
            ),
            patch("tools.club_reader._club_information", return_value={"finances": {"balance": 1}}),
        ):
            metrics = world_clubs.read_native_world_club_metrics(clubs)

        self.assertEqual(set(metrics), {10, 20})
        self.assertEqual(metrics[10]["player_count"], 1)
        self.assertEqual(metrics[10]["reputation"], 7000)

    def test_portfolio_metrics_keep_finances_when_roster_valuation_fails(self) -> None:
        class FakeReader:
            @staticmethod
            def team(_address):
                return {"id": 42, "team_type": "club", "reputation": 7000}

            @staticmethod
            def roster_valuation(_address):
                raise RuntimeError("阵容暂不可读")

        information = {
            "finances": {"balance": 123, "remaining_transfer_budget": 45},
            "supporters": {"season_ticket_holders": 678},
            "nation": {"id": 1651, "name": "中国"},
        }
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(FakeReader()),
            ),
            patch("tools.club_reader._club_information", return_value=information),
        ):
            metrics = world_clubs.read_native_world_club_metrics([
                {"id": 42, "address": "0x1000"},
            ])

        self.assertEqual(metrics[42]["balance"], 123)
        self.assertEqual(metrics[42]["transfer_budget"], 45)
        self.assertEqual(metrics[42]["fan_count"], 678)
        self.assertEqual(metrics[42]["nation"], {"id": 1651, "name": "中国"})
        self.assertEqual(metrics[42]["reputation"], 7000)
        self.assertTrue(metrics[42]["partial"])
        self.assertNotIn("unavailable", metrics[42])
        self.assertIn("阵容暂不可读", metrics[42]["errors"]["valuation"])

    def test_native_world_club_detail_expands_roster_rows_for_player_cards(self) -> None:
        class FakeLayout:
            key = "fm26"
            module_name = "game_plugin.dll"
            game_date_rva = None
            staff_complete_object_offset = 0x100
            staff_person_vtable_rva = 0x200

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=0x100000)

        roster = [{
            "id": 28025033, "address": "0x2000", "name": "Tom Cairney",
            "ca": 130, "pa": 140, "positions": {"DM": 20},
            "fitness_percent": 77.8, "sharpness_percent": 100,
        }]

        class FakeReader:
            def __init__(self, *_args):
                self.layout = FakeLayout()

            @staticmethod
            def team(_address):
                return {
                    "id": 42, "team_type": "club", "reputation": 7000,
                    "manager_address": "0x3000",
                }

            @staticmethod
            def ptr(address):
                return 0x100000 + 0x200 if address == 0x3100 else 0

            @staticmethod
            def roster(_address):
                return roster

        expanded = {
            "id": 28025033, "name": "Tom Cairney", "address": "0x2000",
            "attributes": {"技术": {"传球": 15}},
            "height_cm": 175, "date_of_birth": "1991-01-20",
            "nationality": "英格兰", "player_contract": {},
        }
        fake_reader = FakeReader()
        fake_reader.module = SimpleNamespace(base_address=0x100000)
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(fake_reader),
            ),
            patch(
                "tools.club_reader._club_information",
                return_value={"nation": {"id": 1651, "name": "中国"}},
            ),
            patch("tools.club_reader.read_roster_player_profile", return_value=expanded) as expand,
        ):
            detail = read_native_world_club_detail({
                "id": 42, "name": "父乐母", "address": "0x1000",
            })

        expand.assert_called_once_with(ANY, roster[0], 0x1000, None, 0x3100)
        self.assertEqual(detail["players"], [expanded])
        self.assertIn("attributes", detail["players"][0])
        self.assertEqual(detail["club"]["nation_id"], 1651)
        self.assertEqual(detail["club"]["nation"], "中国")

    def test_native_world_club_staff_prefers_verified_team_manager_pointer(self) -> None:
        module_base = 0x100000
        manager_object = 0x3000
        manager_person = 0x3100

        class FakeLayout:
            key = "fm26"
            module_name = "game_plugin.dll"
            staff_complete_object_offset = 0x100
            staff_person_vtable_rva = 0x200

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=module_base)

        staff = [{
            "id": 77, "name": "目标主教练", "role": "职务编号 77",
            "job_type": 77, "contract_start_date": "2026-07-01",
        }]

        class FakeReader:
            def __init__(self, *_args):
                self.layout = FakeLayout()

            @staticmethod
            def team(_address):
                return {
                    "id": 42, "team_type": "club",
                    "manager_address": hex(manager_object),
                }

            @staticmethod
            def ptr(address):
                return module_base + 0x200 if address == manager_person else 0

            @staticmethod
            def u32(address):
                return 77 if address == manager_person + world_clubs.ENTITY_UID else 0

        fake_reader = FakeReader()
        fake_reader.module = SimpleNamespace(base_address=module_base)
        with (
            patch(
                "tools.world_clubs.borrow_game_reader",
                return_value=nullcontext(fake_reader),
            ),
            patch("tools.club_reader._scan_staff", return_value=staff),
            patch("tools.club_reader._name", return_value="目标主教练"),
        ):
            result = read_native_world_club_staff({
                "id": 42, "name": "父乐母", "address": "0x1000",
            })

        self.assertEqual(result["manager"], {
            "id": 77, "name": "目标主教练",
            "contract_start_date": "2026-07-01",
            "contract_expiry_date": None,
        })

    def test_owned_club_metrics_frontend_uses_one_batch_request(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        block = script.split("async function performOwnedClubMetricsLoad", 1)[1].split(
            "function renderWorldClubs", 1,
        )[0]
        self.assertIn('request(metricsUrl, {', block)
        self.assertIn("/api/world-clubs/metrics?team_ids=", block)
        self.assertIn("applyEconomySnapshot(response.economy)", block)
        self.assertIn("response.dividends?.paid_count", block)
        self.assertNotIn("/api/world-clubs/detail?team_id=", block)

    def test_world_club_detail_uses_long_timeout_and_lazy_owned_squads(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        helper = script.split("function requestWorldClubDetail", 1)[1].split(
            "async function acquireWorldClub", 1,
        )[0]
        opener = script.split("async function openWorldClubDetail", 1)[1].split(
            "function portfolioControlDeck", 1,
        )[0]
        acquire = script.split("async function acquireWorldClub", 1)[1].split(
            "function renderWorldClubDetailContent", 1,
        )[0]

        self.assertIn("timeoutMs:180000", helper)
        self.assertIn("俱乐部详细资料读取超时", helper)
        self.assertIn("include_related_squads:includeRelatedSquads", helper)
        self.assertIn("if (!force)", helper)
        self.assertIn("ownedClubDetailRequestSequence += 1", helper)
        self.assertIn("includeRelatedSquads:!portfolio || force", opener)
        self.assertIn("force,", opener)
        self.assertIn("blocking:Boolean(force)", opener)
        self.assertIn("cachedOwnedClubDetail(teamId)", opener)
        self.assertIn("ownedClubDetailCacheIsFresh(cached)", opener)
        self.assertIn("if (force) clearOwnedClubDetailCache(teamId)", opener)
        self.assertIn("requestedScope !== ownedClubDetailScopeKey()", opener)
        self.assertIn("requestedRevision !== Number(app.ownedClubDetailRevision", opener)
        self.assertIn(
            "rememberOwnedClubDetail(teamId, detail, {overviewRead:true})", opener,
        )
        self.assertIn("requestWorldClubDetail(teamId)", acquire)

    def test_owned_club_detail_cache_is_session_scoped_and_single_flight(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        helper = script.split("const OWNED_CLUB_DETAIL_FRESH_MS", 1)[1].split(
            "async function acquireWorldClub", 1,
        )[0]
        reset = script.split("function resetAccountScopedWorldState", 1)[1].split(
            "async function loadStateOnce", 1,
        )[0]
        squads = script.split("async function loadOwnedClubSquads", 1)[1].split(
            "async function loadOwnedClubFutureTransfers", 1,
        )[0]

        self.assertIn('String(app.state?.data_scope_id || "")', helper)
        self.assertIn("String(output().save_instance_id", helper)
        self.assertIn("= 5 * 60 * 1000", helper)
        self.assertIn("results:60 * 1000", helper)
        self.assertIn("transfers:2 * 60 * 1000", helper)
        self.assertIn("cached.gameDate === ownedClubDetailGameDate()", helper)
        self.assertIn("ownedClubDetailSectionIsFresh", helper)
        self.assertIn("sectionRevisions", helper)
        self.assertIn("app.ownedClubDetailRequests.get(requestBase)", helper)
        self.assertIn("app.ownedClubDetailRevision += 1", helper)
        self.assertIn("app.ownedClubDetailRequests.delete(requestKey)", helper)
        self.assertIn("app.ownedClubDetailCache.clear()", reset)
        self.assertIn("app.ownedClubDetailRequests.clear()", reset)
        self.assertIn("includeRelatedSquads:true", squads)
        self.assertIn("requestedScope !== ownedClubDetailScopeKey()", squads)
        self.assertIn("requestedRevision !== Number(app.ownedClubDetailRevision", squads)
        self.assertIn("requestedSectionRevision", squads)
        self.assertIn("detail.ownedSquadsLoaded = true", squads)

    def test_owned_club_busy_state_keeps_navigation_and_close_available(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        busy = script.split("function syncWorldClubDetailBusy", 1)[1].split(
            "function beginWorldClubDetailBusy", 1,
        )[0]

        self.assertIn(".close-dialog,[data-owned-tab],[data-owned-squad]", busy)
        self.assertIn('[data-owned-action="player-detail"]', busy)
        self.assertIn('[data-owned-action="load-staff"]', busy)
        self.assertIn("busy && app.worldClubDetailBlocking && !remainsAvailable", busy)
        self.assertIn("worldClubDetailBusyToken", script)

    def test_owned_club_local_rerenders_preserve_scroll_and_focus(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        renderer = script.split("function renderOwnedClubDetailContent", 1)[1].split(
            "function startWorldClubAcquisitionProgress", 1,
        )[0]

        self.assertIn("captureOwnedClubDetailViewState(content)", renderer)
        self.assertIn("restoreOwnedClubDetailViewState(content, viewState)", renderer)
        self.assertIn("preventScroll:true", script)

    def test_owned_club_dividend_is_visible_in_bank_and_portfolio(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        self.assertIn("俱乐部分红", script)
        self.assertIn("本月预计分红", script)
        self.assertIn("上月净利润", script)
        self.assertIn("上月亏损", script)
        self.assertIn("本月预计分红 · 5%", script)
        self.assertIn("dividends.last_paid_total", script)
        self.assertIn("dividends.last_paid_month", script)
        self.assertNotIn("下月起参与", script)

    def test_owned_club_card_debt_action_and_equal_action_sizes(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        styles = (Path(__file__).resolve().parents[1] / "web" / "app.css").read_text(
            encoding="utf-8",
        )
        self.assertIn('data-world-debt="${Number(teamId)}"', script)
        self.assertIn('request(\n      "/api/world-clubs/debt-repay"', script)
        self.assertIn(
            ".owned-club-row-actions .owned-club-row-view,.owned-club-row-sell",
            styles,
        )
        self.assertIn("height:48px;min-height:48px", styles)

    def test_owned_club_financial_sections_share_operations_card(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        renderer = script.split("function renderOwnedClubDetailContent", 1)[1].split(
            "async function openWorldClubDetail", 1,
        )[0]
        financial_card = renderer.split("const financialCard", 1)[1].split(
            "content.innerHTML", 1,
        )[0]
        overview = renderer.split('data-owned-panel="overview"', 1)[1].split(
            'data-owned-panel="operations"', 1,
        )[0]
        operations = renderer.split('data-owned-panel="operations"', 1)[1].split(
            'data-owned-panel="management"', 1,
        )[0]
        self.assertIn('class="owned-operation-card owned-finance-card"', financial_card)
        self.assertIn("${investmentSummary}${financeControls}", financial_card)
        self.assertIn(
            "${renderClubIncomeReport(finances, {collapsible:true})}", financial_card,
        )
        self.assertNotIn("investmentSummary", overview)
        self.assertIn("${financialCard}", operations)
        self.assertIn("FACILITIES", operations)
        self.assertIn("STADIUM", operations)
        self.assertNotIn('data-owned-panel="finance"', renderer)

    def test_owned_club_detail_discards_late_responses_from_previous_club(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        guard = script.split("function isCurrentWorldClubDetail", 1)[1].split(
            "async function loadOwnedClubStaff", 1,
        )[0]
        owned_renderer = script.split(
            "function renderOwnedClubDetailContent", 1,
        )[1].split("function renderWorldClubDetailContent", 1)[0]
        world_renderer = script.split(
            "function renderWorldClubDetailContent", 1,
        )[1].split("async function openWorldClubDetail", 1)[0]
        opener = script.split("async function openWorldClubDetail", 1)[1].split(
            "function portfolioControlDeck", 1,
        )[0]

        self.assertIn("dialog?.open", guard)
        self.assertIn("app.worldClubDetailRequestKey", guard)
        self.assertIn("content?.dataset.worldClubDetailRequestKey", guard)
        self.assertIn("content?.dataset.worldClubDetailTeamId", guard)
        self.assertIn(
            "if (!isCurrentWorldClubDetail(content, detail, teamId)) return;",
            owned_renderer,
        )
        self.assertIn(
            "if (!isCurrentWorldClubDetail(content, detail, teamId)) return;",
            world_renderer,
        )
        self.assertIn("content.dataset.worldClubDetailRequestKey = String(requestKey)", opener)
        self.assertIn("content.dataset.worldClubDetailTeamId = String(Number(teamId))", opener)
        self.assertIn("detail.worldClubDetailRequestKey = requestKey", opener)
        self.assertIn(
            "if (!isCurrentWorldClubDetail(content, detail, teamId)) return;",
            opener,
        )
        rename = script.split(
            "function openOwnedClubRenameDialog", 1,
        )[1].split("function openOwnedStadiumRenameDialog", 1)[0]
        self.assertIn("isCurrentWorldClubDetail(content, detail, teamId)", rename)
        self.assertIn("shortInput.focus()", rename)
        self.assertNotIn("input.focus()", rename)

    def test_owned_club_frontend_handles_suspension_and_refresh_queue(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        card = script.split("function worldClubCard", 1)[1].split(
            "async function performOwnedClubMetricsLoad", 1,
        )[0]
        detail = script.split("function renderWorldClubDetailContent", 1)[1].split(
            "async function openWorldClubDetail", 1,
        )[0]
        metrics = script.split("async function loadOwnedClubMetrics", 1)[1].split(
            "async function refreshOwnedClubCards", 1,
        )[0]
        metrics_load = script.split(
            "async function performOwnedClubMetricsLoad", 1,
        )[1].split("async function loadOwnedClubMetrics", 1)[0]
        finance = script.split(
            "const endpoint = ownedClubFinance ?", 1,
        )[1].split('if (["transfer_budget_out"', 1)[0]
        acquisition = script.split("async function acquireWorldClub", 1)[1].split(
            "function renderWorldClubDetailContent", 1,
        )[0]

        self.assertIn("club.ownership_active === false", card)
        self.assertIn("owned-club-row-suspended", card)
        self.assertIn('class="owned-club-row-sell" disabled', card)
        self.assertIn("content.innerHTML = `<article", detail)
        self.assertIn("detail.ownership_suspended || club?.ownership_active === false", detail)
        self.assertLess(
            detail.index("detail.ownership_suspended"),
            detail.index("if (portfolio)"),
        )
        opener = script.split("async function openWorldClubDetail", 1)[1].split(
            "function portfolioControlDeck", 1,
        )[0]
        self.assertIn("const acquiredClub =", opener)
        self.assertIn("const directoryClub =", opener)
        self.assertIn(
            "const club = (portfolio ? acquiredClub : directoryClub) || acquiredClub || directoryClub;",
            opener,
        )
        self.assertIn("!detail.ownership_suspended", opener)
        self.assertIn("ownedClubMetricsForceQueued", metrics)
        self.assertIn("return app.ownedClubMetricsPromise", metrics)
        self.assertEqual(metrics_load.count("app.ownedClubMetrics.clear()"), 1)
        self.assertNotIn("if (force) {", metrics_load)
        self.assertIn("已保留上次数据", metrics_load)
        self.assertIn("rebuildOwnedClubPortfolioSummaryFromCache()", finance)
        self.assertNotIn("loadOwnedClubMetrics({force:true})", finance)
        self.assertIn("updateOwnedClubCachedMetrics(", finance)
        self.assertIn("client_submission_id:submissionId", acquisition)

    def test_local_finance_updates_keep_cached_detail_and_group_summary_current(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        debt = script.split("async function repayOwnedClubDebt", 1)[1].split(
            "async function loadWorldNations", 1,
        )[0]
        finance_dialog = script.split("async function openOwnedClubFinanceDialog", 1)[1].split(
            "function renderOwnedClubVision", 1,
        )[0]
        state_loader = script.split("async function loadStateOnce", 1)[1].split(
            "function freeServicesEnabled", 1,
        )[0]

        self.assertIn("updateOwnedClubCachedMetrics(teamId, {debt:0}, {debts:[]})", debt)
        self.assertIn("rebuildOwnedClubPortfolioSummaryFromCache()", debt)
        self.assertIn("syncOwnedClubDividendForecasts(nextState.economy)", state_loader)
        self.assertIn("rememberOwnedClubDetailMutation", script)
        self.assertIn("invalidateOwnedClubDetailSections(targetTeamId", script)
        self.assertIn('if (directionKey === "in") return openDialog()', finance_dialog)
        self.assertIn("clearOwnedClubDetailCache(teamId)", script.split(
            "async function sellOwnedClub", 1,
        )[1].split("async function repayOwnedClubDebt", 1)[0])

    def test_world_club_background_scan_reuses_manual_progress_polling(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        load_block = script.split("async function loadWorldClubs", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        polling_block = script.split("function syncWorldClubScanPolling", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        scan_block = script.split("async function scanWorldClubs", 1)[1].split(
            "function worldClubDetailPlayers", 1,
        )[0]
        self.assertIn("syncWorldClubScanPolling(app.worldClubs)", load_block)
        self.assertIn("data?.native_scan?.scanning", polling_block)
        self.assertIn("setInterval", polling_block)
        self.assertIn("response.native_scan", scan_block)
        self.assertNotIn("setInterval", scan_block)

    def test_world_club_refresh_state_is_visible_in_content_and_scan_button(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        styles = (Path(__file__).resolve().parents[1] / "web" / "app.css").read_text(
            encoding="utf-8",
        )
        load_block = script.split("async function loadWorldClubs", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        render_block = script.split("function renderWorldClubs", 1)[1].split(
            "async function sellOwnedClub", 1,
        )[0]
        self.assertIn("app.worldClubsLoading = true;", load_block)
        self.assertIn('if (!background || ["world-clubs", "my-clubs"].includes(app.page))', load_block)
        self.assertIn("renderWorldClubs();\n      renderMyClubs();", load_block)
        self.assertIn("正在刷新", render_block)
        self.assertIn("`正在刷新 ${scan.progress || 0}%`", render_block)
        self.assertNotIn("scan.progress_text ||", render_block)
        self.assertIn('scanButton.classList.toggle("is-refreshing", refreshing)', render_block)
        self.assertIn('#world-club-scan.is-refreshing svg', styles)

    def test_world_club_load_failure_exits_spinner_and_offers_retry(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        load_block = script.split("async function loadWorldClubs", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        render_block = script.split("function renderWorldClubs", 1)[1].split(
            "async function sellOwnedClub", 1,
        )[0]

        self.assertNotIn("timeoutMs:15000", load_block)
        self.assertIn("app.worldClubsError = error.message", load_block)
        self.assertIn("!data && app.worldClubsLoading", render_block)
        self.assertIn("俱乐部读取失败", render_block)
        self.assertIn("data-world-clubs-retry", render_block)

    def test_my_clubs_renders_loading_and_retry_instead_of_a_blank_page(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        load_block = script.split("async function loadWorldClubs", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        render_block = script.split("function renderMyClubs", 1)[1].split(
            "function clubLegacyTierLabel", 1,
        )[0]

        self.assertIn("renderMyClubs();", load_block)
        self.assertIn("if (!index.data)", render_block)
        self.assertIn("正在读取我的集团", render_block)
        self.assertIn("集团资料读取失败", render_block)
        self.assertIn("data-my-clubs-retry", render_block)

    def test_world_club_frontend_indexes_visible_data_and_skips_equal_renders(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        index_block = script.split("function worldClubDataIndex", 1)[1].split(
            "function worldClubContentSignature", 1,
        )[0]
        render_block = script.split("function renderWorldClubs", 1)[1].split(
            "async function sellOwnedClub", 1,
        )[0]

        self.assertIn("app.worldClubIndexSource === data", index_block)
        self.assertIn("facetsByName:new Map", index_block)
        self.assertIn("clubsById:new Map", index_block)
        self.assertIn("dataSignature:JSON.stringify", index_block)
        self.assertIn("worldClubContentSignature(index)", render_block)
        self.assertIn("if (app.worldClubRenderSignature === signature) return", render_block)
        self.assertIn('role="group" aria-label="洲筛选"', render_block)
        self.assertIn('aria-pressed="${row.name === app.worldClubContinent}"', render_block)

    def test_owned_club_frontend_indexes_state_and_skips_equal_renders(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        index_block = script.split("function ownedClubDataIndex", 1)[1].split(
            "function saveOwnedClubOrder", 1,
        )[0]
        signature_block = script.split(
            "function ownedClubRenderStateSignature", 1,
        )[1].split("function syncMyClubsPageStatus", 1)[0]
        render_block = script.split("function renderMyClubs", 1)[1].split(
            "function clubLegacyTierLabel", 1,
        )[0]

        self.assertIn("sources.acquired === acquired", index_block)
        self.assertIn("byId = new Map", index_block)
        self.assertIn("dataSignature:JSON.stringify(acquired)", index_block)
        self.assertIn("version:app.ownedClubIndexVersion", index_block)
        self.assertIn("app.ownedClubPortfolioSummary", signature_block)
        self.assertIn("app.ownedClubMetricsVersion", signature_block)
        self.assertIn("index.version", signature_block)
        self.assertIn("app.ownedClubExpandedIds", signature_block)
        self.assertIn("app.ownedClubReorderMode", signature_block)
        self.assertIn("ownedClubRenderStateSignature(index)", render_block)
        self.assertIn("if (app.ownedClubRenderSignature === signature) return", render_block)
        self.assertIn("const clubs = index.ordered", render_block)

    def test_world_and_owned_club_busy_states_are_accessible(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn('id="page-world-clubs" aria-labelledby="world-clubs-page-title" aria-busy="false"', markup)
        self.assertIn('id="world-club-sync-status"', markup)
        self.assertIn('id="world-club-content" aria-busy="false"', markup)
        self.assertIn('id="page-my-clubs" aria-labelledby="my-clubs-page-title" aria-busy="false"', markup)
        self.assertIn('id="my-clubs-sync-status"', markup)
        self.assertIn('id="my-club-content" aria-busy="false"', markup)
        self.assertIn('aria-label="搜索世界俱乐部"', markup)
        self.assertIn('page?.setAttribute("aria-busy", String(refreshing))', script)
        self.assertIn('root?.setAttribute("aria-busy", String(refreshing))', script)
        self.assertIn("正在刷新旗下俱乐部", script)
        self.assertIn("正在读取经营指标", script)
        self.assertIn(".world-club-sync-status", styles)
        self.assertIn('.owned-club-row-identity[role="button"]:focus-visible', styles)
        reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
        self.assertIn(".world-club-sync-status > i", reduced_motion)
        self.assertIn(".my-clubs-sync-status > i", reduced_motion)

    def test_owned_club_disclosure_supports_keyboard_and_group_rename_focus(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        card = script.split("function worldClubCard", 1)[1].split(
            "function activeOwnedClubs", 1,
        )[0]
        interactions = script.split(
            "function initializeOwnedClubCardInteractions", 1,
        )[1].split("async function performOwnedClubMetricsLoad", 1)[0]
        group_rename = script.split("function openGroupRenameDialog", 1)[1].split(
            "function portfolioToolShell", 1,
        )[0]

        self.assertIn('data-owned-club-disclosure role="button" tabindex="0"', card)
        self.assertIn('aria-controls="owned-club-metrics-${teamId}"', card)
        self.assertIn('event.key !== "Enter" && event.key !== " "', interactions)
        self.assertIn("input.focus()", group_rename)
        self.assertIn("input.select()", group_rename)
        self.assertNotIn("shortInput.focus()", group_rename)

    def test_world_club_route_returns_json_for_runtime_failures(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import Mock
        from fm_odds_web import Handler

        handler = Handler.__new__(Handler)
        handler.path = "/api/world-clubs"
        handler.state = SimpleNamespace(
            public_world_clubs=Mock(side_effect=RuntimeError("world clubs unavailable")),
        )
        handler._bind_request_scope = lambda: None
        responses = []
        handler._json = lambda status, payload: responses.append((status, payload))
        handler.do_GET()
        self.assertEqual(int(responses[0][0]), 503)
        self.assertEqual(responses[0][1]["error_code"], "service_unavailable")
        self.assertIn("world clubs unavailable", responses[0][1]["error"])

    def test_get_routes_have_a_common_json_error_boundary(self) -> None:
        server = (Path(__file__).resolve().parents[1] / "fm_odds_web.py").read_text(
            encoding="utf-8",
        )
        wrapper = server.split("def do_GET(self)", 1)[1].split(
            "def _do_GET(self)", 1,
        )[0]

        self.assertIn("self._do_GET()", wrapper)
        self.assertIn(
            "except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)",
            wrapper,
        )
        self.assertIn("except Exception as error:", wrapper)
        self.assertIn("normalized.status", wrapper)
        self.assertIn("self._localized_error_payload", wrapper)

    def test_get_error_boundary_returns_json_and_ignores_client_disconnects(self) -> None:
        import fm_odds_web

        handler = fm_odds_web.Handler.__new__(fm_odds_web.Handler)
        responses = []

        def fail_read() -> None:
            raise RuntimeError("read failed")

        handler._do_GET = fail_read
        handler._json = lambda status, payload: responses.append((status, payload))
        handler.do_GET()

        self.assertEqual(int(responses[0][0]), 503)
        self.assertEqual(responses[0][1]["error"], "read failed")

        for disconnect in (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            with self.subTest(disconnect=disconnect.__name__):
                handler._do_GET = lambda error=disconnect: (_ for _ in ()).throw(error())
                handler._json = lambda *_args: self.fail("client disconnect must not write JSON")
                handler.do_GET()

    def test_world_pages_reload_stale_or_cross_save_caches(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        show_page = script.split("function showPage", 1)[1].split(
            "function showInitialUsageNotice", 1,
        )[0]
        self.assertIn("!app.worldClubs?.ready", show_page)
        self.assertIn("app.worldClubsScope !== worldScope", show_page)
        self.assertIn("!app.worldNations?.ready", show_page)
        self.assertIn("app.worldNationsScope !== worldScope", show_page)

    def test_world_requests_ignore_late_responses_from_another_save(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        clubs = script.split("async function loadWorldClubs", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        nations = script.split("async function loadWorldNations", 1)[1].split(
            "function syncWorldNationScanPolling", 1,
        )[0]
        for block, scope_field in (
            (clubs, "app.worldClubsScope = requestedScope"),
            (nations, "app.worldNationsScope = requestedScope"),
        ):
            self.assertIn("requestedScope !== currentScope", block)
            self.assertIn("!isSaveConnected()", block)
            self.assertIn(scope_field, block)

    def test_account_change_clears_owned_club_state_before_reloading(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        reset_block = script.split("function resetAccountScopedWorldState", 1)[1].split(
            "async function loadState", 1,
        )[0]
        state_block = script.split("async function loadState", 1)[1].split(
            "function beginPolling", 1,
        )[0]

        self.assertIn("app.worldClubs = null", reset_block)
        self.assertIn("app.ownedClubMetrics.clear()", reset_block)
        self.assertIn("app.ownedClubPortfolioSummary = null", reset_block)
        self.assertIn("previousDataScope !== nextDataScope", state_block)
        self.assertIn("resetAccountScopedWorldState()", state_block)
        self.assertIn('loadWorldClubs({page:1})', state_block)

    def test_owned_club_responses_are_bound_to_the_requested_scope(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        clubs = script.split("async function loadWorldClubs", 1)[1].split(
            "function clubReputationTier", 1,
        )[0]
        metrics = script.split("async function performOwnedClubMetricsLoad", 1)[1].split(
            "function syncWorldClubScanPolling", 1,
        )[0]

        self.assertIn('String(response.data_scope_id || "") !== requestedScope', clubs)
        self.assertIn('String(response.data_scope_id || "") !== scope', metrics)

    def test_empty_owned_club_metrics_echoes_current_account_scope(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(),
            output={"save_instance_id": "save-a"},
            _bind_current_save=lambda: "scope-a",
            _data_scope_id=lambda _output: "scope-a",
        )
        with patch("fm_odds_web.load_acquired_clubs", return_value={"clubs": []}):
            result = fm_odds_web.LocalOddsState.owned_world_club_metrics(state)

        self.assertEqual(result["data_scope_id"], "scope-a")
        self.assertEqual(result["metrics"], {})

    def test_world_club_list_echoes_the_account_scope_it_loaded(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(),
            output={"save_instance_id": "save-a", "game_date": "2026-07-29"},
            acquired_status_enforced=set(),
            _has_verified_save=lambda: True,
            _data_scope_id=lambda _output: "scope-a",
            _world_club_native_cache=lambda _save_id: None,
            _apply_owned_world_club_addresses=lambda directory, _save_id: directory,
            _world_club_scan_public=lambda _save_id: {"ready": False},
        )
        with (
            patch("fm_odds_web.load_acquired_clubs", return_value={"clubs": []}),
            patch("fm_odds_web.public_league_standings", return_value={}),
            patch("fm_odds_web.set_active_save_id") as bind_scope,
        ):
            result = fm_odds_web.LocalOddsState.public_world_clubs(state)

        bind_scope.assert_called_once_with("scope-a")
        self.assertEqual(result["data_scope_id"], "scope-a")
        self.assertEqual(result["acquired"], [])

    def test_world_club_list_does_not_write_chairman_state(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(),
            output={"save_instance_id": "save-a", "game_date": "2026-07-29"},
            acquired_status_enforced=set(),
            _has_verified_save=lambda: True,
            _data_scope_id=lambda _output: "scope-a",
            _world_club_native_cache=lambda _save_id: None,
            _apply_owned_world_club_addresses=lambda directory, _save_id: directory,
            _world_club_scan_public=lambda _save_id: {"ready": False},
        )
        acquired = [{"id": 42, "name": "已收购俱乐部", "address": "0x1000"}]
        directory = {"clubs": [{
            "id": 42, "name": "已收购俱乐部", "address": "0x1000",
            "reputation": 5000, "competition": "测试联赛",
            "competition_id": 7, "nation": "中国", "continent": "亚洲",
            "competitions": ["测试联赛"],
        }]}
        with (
            patch("fm_odds_web.build_world_clubs", return_value=directory),
            patch("fm_odds_web.public_league_standings", return_value={}),
            patch("fm_odds_web.load_acquired_clubs", return_value={"clubs": acquired}),
            patch(
                "fm_odds_web.enforce_acquired_club_chairman_status",
                side_effect=AssertionError("GET 列表不得修改游戏内存"),
            ) as enforce,
        ):
            result = fm_odds_web.LocalOddsState.public_world_clubs(state)

        enforce.assert_not_called()
        self.assertEqual(result["acquired"][0]["id"], 42)

    def test_owned_club_ranks_survive_when_native_directory_cache_is_missing(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(),
            output={"save_instance_id": "save-a", "game_date": "2026-07-29"},
            acquired_status_enforced=set(),
            _has_verified_save=lambda: True,
            _data_scope_id=lambda _output: "scope-a",
            _world_club_native_cache=lambda _save_id: None,
            _apply_owned_world_club_addresses=lambda directory, _save_id: directory,
            _world_club_scan_public=lambda _save_id: {"ready": False},
        )
        directory = {"clubs": [
            {
                "id": 42, "name": "已收购俱乐部", "competition_id": 7,
                "competition": "测试联赛", "nation": "中国", "reputation": 800,
                "competitions": ["测试联赛"],
            },
            {
                "id": 43, "name": "同联赛俱乐部", "competition_id": 7,
                "competition": "测试联赛", "nation": "中国", "reputation": 900,
                "competitions": ["测试联赛"],
            },
        ]}
        standings = {"competitions": [{
            "competition_id": 7, "competition_name": "测试联赛",
            "teams": [
                {"team_id": 43, "position": 1, "points": 24},
                {"team_id": 42, "position": 2, "points": 20},
            ],
        }]}
        acquired = [{"id": 42, "name": "已收购俱乐部"}]
        with (
            patch("fm_odds_web.build_world_clubs", return_value=directory),
            patch("fm_odds_web.public_league_standings", return_value=standings),
            patch("fm_odds_web.load_acquired_clubs", return_value={"clubs": acquired}),
        ):
            result = fm_odds_web.LocalOddsState.public_world_clubs(state)

        owned = result["acquired"][0]
        self.assertEqual((owned["league_position"], owned["league_team_count"]), (2, 2))
        self.assertEqual(
            (owned["competition_reputation_rank"], owned["competition_reputation_total"]),
            (2, 2),
        )
        self.assertEqual(
            (owned["nation_reputation_rank"], owned["nation_reputation_total"]),
            (2, 2),
        )

    def test_owned_club_refresh_uses_targeted_endpoint(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        helper = script.split("async function refreshOwnedClubCards", 1)[1].split(
            "function syncWorldClubScanPolling", 1,
        )[0]
        block = script.split('$("#my-clubs-refresh")?.addEventListener', 1)[1].split(
            "document.addEventListener", 1,
        )[0]
        self.assertIn('request("/api/world-clubs/owned-refresh"', helper)
        self.assertIn("timeoutMs:180000", helper)
        self.assertIn("已保留上次数据", helper)
        self.assertNotIn('request("/api/refresh"', helper)
        self.assertNotIn("waitForOwnedClubSnapshotRefresh", script)
        self.assertNotIn("/api/world-clubs/results?team_id=", helper)
        self.assertNotIn("Promise.allSettled", helper)
        self.assertIn("await loadWorldClubs", helper)
        self.assertIn("await loadOwnedClubMetrics({force:true})", helper)
        self.assertIn("refreshOwnedClubCards()", block)
        self.assertNotIn('request("/api/world-clubs/scan"', block)

    def test_owned_club_detail_reports_active_refresh_instead_of_scan_prompt(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        detail_block = script.split("async function openWorldClubDetail", 1)[1].split(
            "function renderMyClubs", 1,
        )[0]
        refresh_block = script.split('$("#my-clubs-refresh")?.addEventListener', 1)[1].split(
            "document.addEventListener", 1,
        )[0]

        self.assertIn("portfolio && app.ownedClubRefreshLoading", detail_block)
        self.assertIn("<strong>正在刷新</strong>", detail_block)
        self.assertIn("app.ownedClubRefreshLoading = true", refresh_block)
        self.assertIn("app.ownedClubRefreshLoading = false", refresh_block)

    def test_world_club_information_renderer_does_not_depend_on_team_id(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        block = script.split("function renderWorldClubInformation", 1)[1].split(
            "function ownedClubAction", 1,
        )[0]
        self.assertNotIn("teamId", block)

        detail_block = script.split("function renderWorldClubDetailContent", 1)[1].split(
            "async function openWorldClubDetail", 1,
        )[0]
        self.assertIn("const acquired =", detail_block)
        self.assertIn("Number(row.id) === Number(teamId)", detail_block)
        self.assertIn("{showIncomeReport:acquired}", detail_block)

    def test_portfolio_metrics_use_squad_model_instead_of_asking_price_sum(self) -> None:
        metrics = club_portfolio_metrics(
            [{"asking_price": 800_000_000}, {"asking_price": 1_200_000_000}, {}],
            {"finances": {
                "balance": 75_000_000,
                "remaining_transfer_budget": 40_000_000,
                "income_statement": {
                    "income": [
                        {"this_season": 90_000_000, "last_month": 6_000_000},
                        {"this_season": 10_000_000, "last_month": 1_000_000},
                    ],
                    "expenditure": [
                        {"last_month": 2_000_000}, {"last_month": 500_000},
                    ],
                },
                "monthly_summary": [
                    {"month": "2026-06", "profit": 5_000_000},
                    {"month": "2026-07", "profit": -2_000_000},
                ],
                "debts": [{"original_debt": 30_000_000}, {"original_debt": 5_000_000}],
            }, "supporters": {
                "season_ticket_holders": 12_345,
                "social_media_followers": 710_454,
            }},
            {
                "price": 700_000_000,
                "components": {"squad": 450_000_000},
                "scores": {"reputation": 0.8, "squad": 0.75},
                "inferred_fields": ["stadium"],
                "player_count": 3,
            },
        )
        self.assertEqual(metrics["current_valuation"], 700_000_000)
        self.assertEqual(metrics["valuation_scores"]["reputation"], 0.8)
        self.assertEqual(metrics["valuation_inferred_fields"], ["stadium"])
        self.assertEqual(metrics["squad_value"], 450_000_000)
        self.assertEqual(metrics["squad_value_source"], "ca_pa_reputation_model")
        self.assertEqual(metrics["valued_players"], 3)
        self.assertEqual(metrics["player_count"], 3)
        self.assertEqual(metrics["balance"], 75_000_000)
        self.assertEqual(metrics["transfer_budget"], 40_000_000)
        self.assertEqual(metrics["fan_count"], 710_454)
        self.assertEqual(metrics["fan_count_source"], "social_media_followers")
        self.assertEqual(metrics["season_ticket_holders"], 12_345)
        self.assertEqual(metrics["social_media_followers"], 710_454)
        self.assertEqual(metrics["season_revenue"], 100_000_000)
        self.assertEqual(metrics["last_month_profit"], 4_500_000)
        self.assertEqual(metrics["monthly_profits"], [
            {"month": "2026-06", "profit": 5_000_000},
            {"month": "2026-07", "profit": -2_000_000},
        ])
        self.assertEqual(metrics["debt"], 35_000_000)

    def test_portfolio_metric_history_compares_adjacent_month_and_season(self) -> None:
        history, first = world_clubs._update_club_metric_history(
            {}, {"reputation": 2_724, "social_media_followers": 10_089},
            game_date="2026-06-30",
            season_start="2025-07-01", season_end="2026-06-30",
        )
        self.assertIsNone(first["reputation_comparison"])
        self.assertIsNone(first["social_media_followers_comparison"])

        history, second = world_clubs._update_club_metric_history(
            history, {"reputation": 2_800, "social_media_followers": 11_098},
            game_date="2026-07-01",
            season_start="2026-07-01", season_end="2027-06-30",
        )
        self.assertEqual(second["reputation_comparison"], {
            "previous": 2_724, "change": 76, "change_percent": 2.8,
        })
        self.assertEqual(second["social_media_followers_comparison"], {
            "previous": 10_089, "change": 1_009, "change_percent": 10.0,
        })
        self.assertEqual(len(history["months"]), 2)
        self.assertEqual(len(history["seasons"]), 2)

    def test_portfolio_metric_history_excludes_future_timeline_snapshots(self) -> None:
        history = {
            "schema_version": 1,
            "months": [{
                "month": "2026-07", "observed_game_date": "2026-07-20",
                "reputation": 2_900,
            }],
            "seasons": [{
                "season_key": "2026-2027", "season_start": "2026-07-01",
                "season_end": "2027-06-30", "observed_game_date": "2026-07-20",
                "social_media_followers": 12_000,
            }],
        }
        rewound, comparisons = world_clubs._update_club_metric_history(
            history, {"reputation": 2_700, "social_media_followers": 10_000},
            game_date="2026-06-15",
            season_start="2025-07-01", season_end="2026-06-30",
        )
        self.assertIsNone(comparisons["reputation_comparison"])
        self.assertIsNone(comparisons["social_media_followers_comparison"])
        self.assertEqual([row["month"] for row in rewound["months"]], ["2026-06"])
        self.assertEqual(
            [row["season_key"] for row in rewound["seasons"]], ["2025-2026"],
        )

    def test_portfolio_metric_history_sync_is_account_scoped_and_bounded(self) -> None:
        payload = {"schema_version": 1, "group_name": "我的集团", "clubs": [
            {"id": 42, "name": "测试俱乐部"},
        ]}

        def update(mutator, scope_id):
            self.assertEqual(scope_id, "scope-a")
            return mutator(payload)

        with patch("tools.world_clubs._update_portfolio", side_effect=update):
            result = world_clubs.sync_acquired_club_metric_history(
                {42: {"reputation": 2_724, "social_media_followers": 10_089}},
                game_date="2026-07-15",
                season_start="2026-07-01", season_end="2027-06-30",
                scope_id="scope-a",
            )

        self.assertIn(42, result)
        history = payload["clubs"][0]["metric_history"]
        self.assertEqual(history["months"][0]["reputation"], 2_724)
        self.assertEqual(
            history["seasons"][0]["social_media_followers"], 10_089,
        )
        self.assertLessEqual(len(history["months"]), 24)
        self.assertLessEqual(len(history["seasons"]), 4)

    def test_portfolio_metrics_reject_invalid_native_money_statement(self) -> None:
        metrics = club_portfolio_metrics(
            [],
            {"finances": {
                "income_statement": {
                    "income": [{
                        "this_month": 1_124_180_362,
                        "last_month": 3_206_794_824,
                        "this_season": 559,
                        "last_season": 124_518_401,
                    }],
                    "expenditure": [{
                        "this_month": 0,
                        "last_month": 4_294_967_295,
                        "this_season": 1_065_353_216,
                        "last_season": 0,
                    }],
                },
            }},
        )

        self.assertIsNone(metrics["season_revenue"])
        self.assertIsNone(metrics["last_month_profit"])

    def test_portfolio_metrics_keep_valid_income_when_expenditure_is_invalid(self) -> None:
        metrics = club_portfolio_metrics(
            [],
            {"finances": {
                "income_statement": {
                    "income": [{
                        "this_month": 8_000_000,
                        "last_month": 7_000_000,
                        "this_season": 65_000_000,
                        "last_season": 120_000_000,
                    }],
                    "expenditure": [{
                        "this_month": 0,
                        "last_month": 0xFFFFFFFF,
                        "this_season": 0,
                        "last_season": 0,
                    }],
                },
            }},
        )

        self.assertEqual(metrics["season_revenue"], 65_000_000)
        self.assertIsNone(metrics["last_month_profit"])

    def test_owned_club_fan_count_falls_back_to_season_tickets(self) -> None:
        metrics = club_portfolio_metrics(
            [], {"supporters": {
                "season_ticket_holders": 63_000,
                "social_media_followers": None,
            }},
        )
        self.assertEqual(metrics["fan_count"], 63_000)
        self.assertEqual(metrics["fan_count_source"], "season_ticket_holders")

        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        card = script.split("function worldClubCard", 1)[1].split(
            "async function loadOwnedClubMetrics", 1,
        )[0]
        self.assertIn('metrics.fan_count_source === "social_media_followers"', card)
        self.assertIn('? "社交媒体关注" : "季票持有者"', card)
        self.assertIn("ownedClubMetric(fanCountLabel, ownedClubCount(metrics.fan_count))", card)

    def test_portfolio_performance_and_summary_ignore_unreadable_valuations(self) -> None:
        performance = club_investment_performance(
            {"current_valuation": 120_000_000, "balance": 15_000_000},
            {
                "acquisition_price": 100_000_000,
                "acquired_at": "2026-07-01",
                "acquired_game_date": "2026-07-01",
            },
            current_game_date="2026-07-28",
        )
        self.assertEqual(performance["valuation_change"], 20_000_000)
        self.assertEqual(performance["valuation_change_percent"], 20.0)
        self.assertEqual(performance["acquired_at"], "2026-07-01")
        self.assertEqual(performance["holding_days"], 27)

        summary = club_portfolio_summary(
            [
                {"id": 1, "acquisition_price": 100_000_000},
                {"id": 2, "acquisition_price": 900_000_000},
            ],
            {
                1: {
                    "current_valuation": 120_000_000,
                    "balance": 15_000_000,
                    "season_revenue": 8_000_000,
                },
                2: {"unavailable": True},
            },
        )
        self.assertEqual(summary["club_count"], 2)
        self.assertEqual(summary["valued_club_count"], 1)
        self.assertTrue(summary["partial"])
        self.assertEqual(summary["acquisition_cost"], 1_000_000_000)
        self.assertEqual(summary["current_valuation"], 120_000_000)
        self.assertEqual(summary["valuation_change"], 20_000_000)
        self.assertEqual(summary["balance"], 15_000_000)

        legacy = club_portfolio_summary(
            [{"id": 3}],
            {3: {"current_valuation": 50_000_000}},
        )
        self.assertEqual(legacy["valued_club_count"], 1)
        self.assertEqual(legacy["comparable_club_count"], 0)
        self.assertFalse(legacy["partial"])
        self.assertEqual(legacy["current_valuation"], 50_000_000)
        self.assertIsNone(legacy["valuation_change"])

    def test_old_acquisition_snapshot_is_repriced_into_the_current_band(self) -> None:
        acquisition = {
            "id": 42,
            "name": "旧版球队",
            "reputation": 7_000,
            "competition": "英超联赛",
            "acquisition_price": 13_000_000_000,
            "acquisition_valuation": {
                "base_price": 13_000_000_000,
                "competition_premium": 0,
                "price_band": {
                    "minimum": 10_000_000_000,
                    "maximum": 30_000_000_000,
                },
                "finance": {"actual": 2_000_000_000},
            },
        }
        quote = world_clubs.acquisition_rebalance_quote(acquisition)

        self.assertEqual(quote["status"], "available")
        self.assertEqual(quote["fair_price"], 2_350_000_000)
        self.assertEqual(quote["refund"], 10_650_000_000)
        self.assertEqual(quote["new_price_band"], {
            "minimum": 1_000_000_000,
            "maximum": 10_000_000_000,
        })

    def test_old_saudi_premium_is_included_in_acquisition_compensation(self) -> None:
        quote = world_clubs.acquisition_rebalance_quote({
            "reputation": 7_000,
            "competition": "Saudi Pro League",
            "acquisition_price": 83_000_000_000,
            "acquisition_valuation": {
                "base_price": 13_000_000_000,
                "competition_premium": 70_000_000_000,
                "price_band": {
                    "minimum": 80_000_000_000,
                    "maximum": 100_000_000_000,
                },
                "finance": {"actual": 2_000_000_000},
            },
        })

        self.assertEqual(quote["new_competition_premium"], 15_000_000_000)
        self.assertEqual(quote["fair_price"], 17_350_000_000)
        self.assertEqual(quote["refund"], 65_650_000_000)

    def test_negative_balance_overcharge_is_available_as_compensation(self) -> None:
        quote = world_clubs.acquisition_rebalance_quote({
            "reputation": 7_000,
            "competition": "英超联赛",
            "acquisition_price": 5_000_000_000,
            "acquisition_valuation": {
                "base_price": 2_000_000_000,
                "competition_premium": 0,
                "price_band": {
                    "minimum": 1_000_000_000,
                    "maximum": 10_000_000_000,
                },
                "finance": {
                    "actual": 5_000_000_000,
                    "balance_absolute": 4_900_000_000,
                    "transfer_budget": 100_000_000,
                },
                "negative_balance_reset": True,
            },
        })

        self.assertEqual(quote["status"], "available")
        self.assertEqual(quote["fair_price"], 2_000_000_000)
        self.assertEqual(quote["refund"], 3_000_000_000)

    def test_claimed_acquisition_compensation_changes_investment_to_net_cost(self) -> None:
        acquisition = {
            "acquisition_price": 13_000_000_000,
            "acquisition_rebalance": {
                "id": world_clubs.ACQUISITION_REBALANCE_ID,
                "status": "claimed",
                "refund": 10_650_000_000,
                "fair_price": 2_350_000_000,
            },
        }
        performance = club_investment_performance(
            {"current_valuation": 3_000_000_000}, acquisition,
        )

        self.assertEqual(performance["gross_acquisition_price"], 13_000_000_000)
        self.assertEqual(performance["acquisition_refund"], 10_650_000_000)
        self.assertEqual(performance["net_acquisition_cost"], 2_350_000_000)
        self.assertEqual(performance["valuation_change"], 650_000_000)

    def test_legacy_claim_only_receives_incremental_v2_compensation(self) -> None:
        acquisition = {
            "reputation": 6_000,
            "acquisition_price": 687_033_333,
            "acquisition_valuation": {
                "base_price": 687_033_333,
                "competition_premium": 0,
                "pricing_divisor": 1,
                "price_band": {
                    "minimum": 500_000_000,
                    "maximum": 1_000_000_000,
                },
                "finance": {"actual": 0},
            },
            "acquisition_rebalance": {
                "id": "acquisition-price-bands-2026-07-31-v1",
                "status": "claimed",
                "refund": 100_000_000,
                "fair_price": 587_033_333,
            },
        }

        quote = world_clubs.acquisition_rebalance_quote(acquisition)
        performance = club_investment_performance(
            {"current_valuation": quote["fair_price"]}, acquisition,
        )

        self.assertEqual(quote["status"], "available")
        self.assertEqual(quote["claimed_refund"], 100_000_000)
        self.assertEqual(
            quote["refund"],
            acquisition["acquisition_price"] - 100_000_000 - quote["fair_price"],
        )
        self.assertEqual(quote["total_refund"], 100_000_000 + quote["refund"])
        self.assertEqual(performance["acquisition_refund"], 100_000_000)
        self.assertEqual(performance["net_acquisition_cost"], 587_033_333)

    def test_legacy_non_professional_snapshots_receive_only_the_new_status_discount(self) -> None:
        for status, old_divisor, reduction in (
            ({"code": 2, "name": "半职业"}, 8, 2),
            ({"code": 3, "name": "业余"}, 12, 4),
        ):
            with self.subTest(status=status):
                current = club_acquisition_valuation({"status": status}, [], 6_000)
                legacy = dict(current)
                legacy.pop("pricing_model_id", None)
                legacy.pop("pricing_basis", None)
                legacy.pop("status_pricing_divisor", None)
                legacy.pop("reputation_pricing_divisor", None)
                legacy["pricing_divisor"] = old_divisor
                legacy["base_price"] *= reduction
                legacy["competition_premium"] *= reduction
                legacy["price_band"] = {
                    key: value * reduction
                    for key, value in current["price_band"].items()
                }
                quote = world_clubs.acquisition_rebalance_quote({
                    "reputation": 6_000,
                    "acquisition_price": current["price"] * reduction,
                    "acquisition_valuation": legacy,
                })

                self.assertEqual(quote["new_pricing_divisor"], current["pricing_divisor"])
                self.assertEqual(quote["fair_price"], current["price"])
                self.assertEqual(
                    quote["refund"], current["price"] * (reduction - 1),
                )

    def test_acquisition_compensation_claim_is_idempotent(self) -> None:
        import fm_odds_web

        row = {
            "id": 42,
            "name": "旧版球队",
            "reputation": 7_000,
            "competition": "英超联赛",
            "acquisition_price": 13_000_000_000,
            "acquisition_valuation": {
                "base_price": 13_000_000_000,
                "competition_premium": 0,
                "price_band": {
                    "minimum": 10_000_000_000,
                    "maximum": 30_000_000_000,
                },
                "finance": {"actual": 2_000_000_000},
            },
        }
        credited = []
        state = SimpleNamespace(
            lock=threading.RLock(), output={"game_date": "2026-07-31"},
            _bind_current_save=lambda: "scope-a",
        )

        def update(_team_id, updates, _scope):
            row.update(updates)
            return dict(row)

        with (
            patch("fm_odds_web.load_acquired_clubs", side_effect=lambda _scope: {"clubs": [dict(row)]}),
            patch("fm_odds_web.update_acquired_club", side_effect=update),
            patch("fm_odds_web.adjust_bank_balance", side_effect=lambda amount, kind, **details: credited.append((amount, kind, details)) or {"bank_balance": amount}),
            patch("fm_odds_web.public_economy", return_value={"bank_balance": 10_650_000_000}),
        ):
            first = fm_odds_web.LocalOddsState.claim_world_club_acquisition_rebalance(
                state, {"team_id": 42},
            )
            second = fm_odds_web.LocalOddsState.claim_world_club_acquisition_rebalance(
                state, {"team_id": 42},
            )

        self.assertFalse(first["already_claimed"])
        self.assertTrue(second["already_claimed"])
        self.assertEqual(first["compensation"]["refund"], 10_650_000_000)
        self.assertEqual(len(credited), 1)
        self.assertEqual(credited[0][0:2], (
            10_650_000_000, "world_club_acquisition_rebalance_refund",
        ))

    def test_acquisition_compensation_rolls_back_bank_when_record_save_fails(self) -> None:
        import fm_odds_web

        row = {
            "id": 42,
            "name": "旧版球队",
            "reputation": 7_000,
            "competition": "英超联赛",
            "acquisition_price": 13_000_000_000,
            "acquisition_valuation": {
                "base_price": 13_000_000_000,
                "competition_premium": 0,
                "price_band": {
                    "minimum": 10_000_000_000,
                    "maximum": 30_000_000_000,
                },
                "finance": {"actual": 2_000_000_000},
            },
        }
        adjusted = []
        state = SimpleNamespace(
            lock=threading.RLock(), output={"game_date": "2026-07-31"},
            _bind_current_save=lambda: "scope-a",
        )
        with (
            patch("fm_odds_web.load_acquired_clubs", return_value={"clubs": [row]}),
            patch("fm_odds_web.update_acquired_club", side_effect=OSError("disk full")),
            patch("fm_odds_web.adjust_bank_balance", side_effect=lambda amount, kind, **_details: adjusted.append((amount, kind)) or {"bank_balance": amount}),
        ):
            with self.assertRaisesRegex(RuntimeError, "款项已回滚"):
                fm_odds_web.LocalOddsState.claim_world_club_acquisition_rebalance(
                    state, {"team_id": 42},
                )

        self.assertEqual(adjusted, [
            (10_650_000_000, "world_club_acquisition_rebalance_refund"),
            (-10_650_000_000, "world_club_acquisition_rebalance_rollback"),
        ])

    def test_owned_club_frontend_hides_acquisition_compensation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")

        self.assertNotIn("function ownedClubAcquisitionRebalanceBadge", script)
        self.assertNotIn('data-world-acquisition-rebalance=', script)
        self.assertNotIn('"/api/world-clubs/acquisition-rebalance"', script)
        self.assertNotIn("估值补偿", script)
        self.assertNotIn(".owned-rebalance-badge", styles)

    def test_owned_club_frontend_renders_portfolio_value_feedback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("ownedClubPortfolioSummary", script)
        summary = script.split("function portfolioSummaryMetrics", 1)[1].split(
            "function renderGroupInformation", 1,
        )[0]
        self.assertIn('id="my-clubs-summary"', markup)
        self.assertIn('ownedClubMetric("当前估值"', summary)
        self.assertIn('ownedClubMetric("全部俱乐部结余"', summary)
        self.assertIn('ownedClubMetric("累积收购成本"', summary)
        self.assertIn('ownedClubMetric("本赛季营收"', summary)
        self.assertIn('ownedClubMetric("已收购俱乐部数量"', summary)
        self.assertNotIn("owned-portfolio-summary", summary)
        self.assertNotIn('ownedClubMetric("相对收购价"', summary)
        self.assertIn('valuationScore("品牌", "reputation")', script)
        self.assertIn('valuationScore("阵容", "squad")', script)

    def test_unimplemented_owned_club_actions_are_marked_in_the_label(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        helper = script.split("function ownedClubAction", 1)[1].split(
            "function beginOwnedClubOperation", 1,
        )[0]
        self.assertIn('"sponsorship-coming-soon", "club-meeting-coming-soon"', helper)
        self.assertIn("`${uiLegacy(label)}（暂未实装）`", helper)

    def test_owned_club_money_uses_the_configured_currency_formatter(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        money_helper = script.split("function ownedClubMoney", 1)[1].split(
            "function ownedClubCount", 1,
        )[0]
        signed_helper = script.split("function ownedClubSignedMoney", 1)[1].split(
            "function ownedClubDividendEstimate", 1,
        )[0]
        self.assertIn("formatMoney(amount)", money_helper)
        self.assertNotIn("compactMoney", money_helper)
        self.assertIn("formatMoney(Math.abs(amount))", signed_helper)
        self.assertNotIn("compactMoney", signed_helper)

    def test_world_club_acquisition_is_only_exposed_in_detail(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        card_renderer = script.split("function worldClubCard", 1)[1].split(
            "async function loadOwnedClubMetrics", 1,
        )[0]
        detail_renderer = script.split("function renderWorldClubDetailContent", 1)[1].split(
            "async function openWorldClubDetail", 1,
        )[0]
        self.assertNotIn('data-world-acquire=', card_renderer)
        self.assertNotIn('class="world-club-acquire"', card_renderer)
        self.assertIn('data-world-view=', card_renderer)
        self.assertIn('data-world-detail-acquire=', detail_renderer)
        self.assertIn('data-world-detail-acquire=', script)
        self.assertIn('const acquisitionPrice', script)
        self.assertIn('request("/api/world-clubs/acquire"', script)
        acquire_handler = script.split("async function acquireWorldClub", 1)[1].split("\n}", 1)[0]
        self.assertNotIn("fmoddConfirm", acquire_handler)
        self.assertIn("result.warning", acquire_handler)

    def test_world_club_acquisition_shows_thirty_second_progress_feedback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")

        progress_helper = script.split(
            "function startWorldClubAcquisitionProgress", 1,
        )[1].split("async function acquireWorldClub", 1)[0]
        detail_renderer = script.split(
            "function renderWorldClubDetailContent", 1,
        )[1].split("async function openWorldClubDetail", 1)[0]
        self.assertIn("/ 30000", progress_helper)
        self.assertIn("Math.min(99", progress_helper)
        self.assertIn('render(100, "收购完成")', progress_helper)
        self.assertNotIn("setTimeout(resolve, 350)", script)
        self.assertIn("data-world-acquisition-progress", detail_renderer)
        self.assertIn('role="progressbar"', detail_renderer)
        self.assertIn(".world-acquisition-progress", styles)

    def test_world_club_cards_use_plain_names_text_view_and_compact_price(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        card_renderer = script.split("function worldClubCard", 1)[1].split(
            "async function loadOwnedClubMetrics", 1,
        )[0]
        detail_renderer = script.split("function renderWorldClubDetailContent", 1)[1].split(
            "async function openWorldClubDetail", 1,
        )[0]
        self.assertIn("const displayName = club.display_name", card_renderer)
        self.assertIn("club.renamed_short_name || club.short_name", card_renderer)
        self.assertIn("escapeHtml(displayName)", card_renderer)
        self.assertNotIn('data-lucide="eye"', card_renderer)
        self.assertIn('<span>查看</span>', card_renderer)
        self.assertIn("formatPounds(acquisitionPrice)", detail_renderer)
        self.assertNotIn("formatFullMoney(acquisitionPrice)", detail_renderer)

    def test_brand_promotion_keeps_button_reference_across_confirmation(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        handler = script.split(
            '$("#portfolio-brand-submit")?.addEventListener("click", async (event) => {', 1,
        )[1].split("  });", 1)[0]
        button_capture = handler.index("const button = event.currentTarget;")
        confirmation = handler.index("await fmoddConfirm")
        self.assertLess(button_capture, confirmation)
        self.assertIn('runPortfolioToolOperation(button, "正在推广"', handler)
        self.assertIn("if (!result) return;", handler)
        self.assertNotIn("runPortfolioToolOperation(event.currentTarget", handler)

    def test_owned_club_sale_uses_ninety_eight_percent_of_live_valuation(self) -> None:
        import fm_odds_web

        rows = [{
            "id": 42, "name": "测试俱乐部", "sale_restore": {"schema_version": 1},
        }]
        restored = []
        forgotten = []
        credited = []
        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.RLock(),
            output={"save_instance_id": "save-a", "game_date": "2028-05-20"},
            acquired_status_enforced={("save-a", 42)},
            owned_world_club_addresses={42: "0x1000"},
            _bind_current_save=lambda: "scope-a",
            _owned_world_club_target=lambda *_args, **_kwargs: (
                "scope-a", {"save_instance_id": "save-a", "game_date": "2028-05-20"},
                {"id": 42, "name": "测试俱乐部", "address": "0x1000"},
            ),
            _data_scope_id=lambda _output: "scope-a",
            _sync_owned_club_board_hook=lambda _output: {"active": False},
        )

        def remove(team_id, _scope):
            if not any(int(row["id"]) == int(team_id) for row in rows):
                return False
            rows.clear()
            return True

        with (
            patch("fm_odds_web.load_acquired_clubs", side_effect=lambda _scope: {"clubs": list(rows)}),
            patch("fm_odds_web.remove_acquired_club", side_effect=remove),
            patch("fm_odds_web.read_native_world_club_metrics", return_value={42: {"current_valuation": 123_456_789}}),
            patch("fm_odds_web.restore_native_club_after_sale", side_effect=lambda *_args: restored.append(_args) or {"restored": True, "legacy": False, "_rollback": {}}),
            patch("fm_odds_web.forget_club_dividend_tracking", side_effect=forgotten.append),
            patch("fm_odds_web.adjust_bank_balance", side_effect=lambda amount, kind, **details: credited.append((amount, kind, details)) or {"bank_balance": amount}),
        ):
            result = fm_odds_web.LocalOddsState.sell_world_club(state, {"team_id": 42})

        self.assertEqual(result["sale_price"], 120_987_653)
        self.assertEqual(credited[0][0:2], (120_987_653, "world_club_sale"))
        self.assertEqual(forgotten, [42])
        self.assertEqual(result["acquired"], [])
        self.assertTrue(restored)

    def test_owned_club_sale_uses_fmodd_record_when_game_takeover_blocks_restore(self) -> None:
        import fm_odds_web

        rows = [{
            "id": 42, "name": "被游戏收购的俱乐部",
            "acquisition_price": 100_000_000,
            "acquisition_valuation": {
                "price": 100_000_000, "competition_premium": 0,
            },
            "sale_restore": {"schema_version": 1},
        }]
        credited = []
        state = SimpleNamespace(
            lock=threading.RLock(), memory_lock=threading.RLock(),
            output={"save_instance_id": "save-a", "game_date": "2028-05-20"},
            acquired_status_enforced={("save-a", 42)},
            owned_world_club_addresses={42: "0x1000"},
            _bind_current_save=lambda: "scope-a",
            _owned_world_club_target=lambda *_args, **_kwargs: (
                "scope-a", {"save_instance_id": "save-a", "game_date": "2028-05-20"},
                {"id": 42, "name": "被游戏收购的俱乐部", "address": "0x1000"},
            ),
            _data_scope_id=lambda _output: "scope-a",
            _sync_owned_club_board_hook=lambda _output: {"active": False},
        )

        def remove(team_id, _scope):
            if not any(int(row["id"]) == int(team_id) for row in rows):
                return False
            rows.clear()
            return True

        with (
            patch("fm_odds_web.load_acquired_clubs", side_effect=lambda _scope: {"clubs": list(rows)}),
            patch("fm_odds_web.remove_acquired_club", side_effect=remove),
            patch("fm_odds_web.read_native_world_club_metrics", return_value={42: {"unavailable": True}}),
            patch("fm_odds_web.restore_native_club_after_sale", side_effect=RuntimeError("俱乐部主席已变化")) as restore,
            patch("fm_odds_web.forget_club_dividend_tracking"),
            patch("fm_odds_web.adjust_bank_balance", side_effect=lambda amount, kind, **details: credited.append((amount, kind, details)) or {"bank_balance": amount}),
        ):
            result = fm_odds_web.LocalOddsState.sell_world_club(state, {"team_id": 42})

        self.assertEqual(result["sale_price"], 98_000_000)
        self.assertEqual(result["valuation_source"], "fmodd_record")
        self.assertIn("未覆盖新数据", result["warning"])
        self.assertEqual(result["acquired"], [])
        restore.assert_called_once()
        self.assertEqual(credited[0][0:2], (98_000_000, "world_club_sale"))

    def test_owned_club_frontend_confirms_sale_below_view(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        card = script.split("function worldClubCard", 1)[1].split(
            "async function loadOwnedClubMetrics", 1,
        )[0]
        self.assertIn('data-world-sell=', script)
        self.assertIn('request(\n      "/api/world-clubs/sell"', script)
        self.assertIn("function ownedClubSalePrice", script)
        self.assertIn("(valuation - competitionPremium) * 0.98", script)
        self.assertIn('<span>出售</span>', card)
        self.assertNotIn('出售 ·', card)
        self.assertNotIn("saleReady", card)
        self.assertNotIn('data-world-sell="${Number(club.id)}" disabled', card)
        sale = script.split("async function sellOwnedClub", 1)[1].split(
            "async function loadWorldNations", 1,
        )[0]
        self.assertIn("const confirmed = await fmoddConfirm", sale)
        self.assertIn('"确认出售"', sale)
        self.assertIn("if (!confirmed) return", sale)
        self.assertLess(sale.index("fmoddConfirm"), sale.index('"/api/world-clubs/sell"'))

    def test_owned_club_detail_header_shows_revenue_instead_of_ownership(self) -> None:
        from frontend_source import read_frontend_source

        script = read_frontend_source(
            Path(__file__).resolve().parents[1] / "web" / "app.js", mode="raw",
        )
        profile = script.split('content.innerHTML = `<section class="owned-club-profile"', 1)[1]
        profile = profile.split("</section>", 1)[0]
        self.assertIn('ownedClubMetric(uiText("owned.detail.revenue"), money(detail.portfolio_metrics?.season_revenue))', profile)
        self.assertIn('ownedClubMetric(uiText("owned.detail.nation"), localizedClubNation(detail.club || club, info.nation))', profile)
        self.assertIn('ownedClubMetric(uiText("owned.detail.club_nature"), localizedClubNature(status))', profile)
        self.assertNotIn('ownedClubMetric("所有权"', profile)

        overview = script.split('data-owned-panel="overview"', 1)[1].split(
            "</section>", 1,
        )[0]
        self.assertNotIn('ownedClubMetric("士气"', overview)
        self.assertNotIn('ownedClubMetric("主席状态"', overview)

    def test_world_club_detail_orders_cards_and_hides_unowned_income_report(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        block = script.split("function renderWorldClubInformation", 1)[1].split(
            "function ownedClubAction", 1,
        )[0]
        self.assertIn('["平均门票价格", money(finances.average_match_ticket_price)]', block)
        self.assertIn('["门票收入（本月）", money(ticketIncomeThisMonth)]', block)
        self.assertIn('["门票收入（本赛季）", money(ticketIncomeThisSeason)]', block)
        self.assertIn("{showIncomeReport = false}", block)
        self.assertIn("showIncomeReport ? renderClubIncomeReport(finances) : \"\"", block)
        self.assertIn(
            "<div>${facilitiesSection}${financesSection}</div>"
            "<div>${statusSection}${stadiumSection}</div>",
            block,
        )

    def test_target_club_results_reuse_session_fixture_addresses_until_refresh_or_date_change(self) -> None:
        class FakeLayout:
            key = "fm24"
            distribution = "steam"
            executable_sha256 = "layout-hash"
            game_version = "24.4.2"
            game_date_rva = 0x10
            module_name = "fm.exe"

            @staticmethod
            def module(_process):
                return SimpleNamespace(base_address=0x100000)

        class FakeProcessContext:
            def __enter__(self):
                return object()

            def __exit__(self, *_args):
                return False

        class FakeReader:
            def __init__(self, *_args):
                pass

            @staticmethod
            def u32(_address):
                return 1

            @staticmethod
            def team(address):
                return {
                    0x100: {"id": 42, "name": "目标队"},
                    0x110: {"id": 43, "name": "对手队"},
                }.get(address)

            @staticmethod
            def competition(_address):
                return {"id": 7, "name": "测试联赛"}

        fixtures = {
            0x200: SimpleNamespace(
                home_team=0x100, away_team=0x110, competition_season=0x500,
                match_date=date(2026, 7, 24),
            ),
            0x300: SimpleNamespace(
                home_team=0x110, away_team=0x100, competition_season=0x500,
                match_date=date(2026, 7, 27),
            ),
        }
        world_clubs._CLUB_FIXTURE_CACHE.clear()
        current_date = {"value": date(2026, 7, 25)}
        with (
            patch("tools.world_clubs.select_process_layout", return_value=(123, "fm.exe", FakeLayout())),
            patch("tools.world_clubs.open_process", return_value=FakeProcessContext()),
            patch("tools.world_clubs.Reader", FakeReader),
            patch("tools.world_clubs.decode_date", side_effect=lambda _code: current_date["value"]),
            patch("tools.world_clubs.scan_fixture_addresses", return_value=([0x200, 0x300], 4096)) as scan,
            patch("tools.world_clubs.parse_fixture", side_effect=lambda _reader, address: fixtures.get(address)),
            patch("tools.preview_cup_odds.read_completed_results", return_value=[]),
        ):
            first = read_native_world_club_results(
                {"id": 42, "address": "0x100"}, save_identity="save-a",
            )
            second = read_native_world_club_results(
                {"id": 42, "address": "0x100"}, save_identity="save-a",
            )
            forced = read_native_world_club_results(
                {"id": 42, "address": "0x100"}, save_identity="save-a",
                force_refresh=True,
            )
            current_date["value"] = date(2026, 7, 26)
            next_day = read_native_world_club_results(
                {"id": 42, "address": "0x100"}, save_identity="save-a",
            )

        self.assertEqual(scan.call_count, 3)
        self.assertFalse(first["fixture_cache_hit"])
        self.assertTrue(second["fixture_cache_hit"])
        self.assertFalse(forced["fixture_cache_hit"])
        self.assertFalse(next_day["fixture_cache_hit"])
        self.assertEqual(second["fixture_pool_bytes_scanned"], 0)
        self.assertEqual(first["fixture_count"], 2)
        world_clubs._CLUB_FIXTURE_CACHE.clear()

    def test_acquired_clubs_are_persisted_in_the_explicit_account_scope(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.world_clubs.save_data_root", side_effect=lambda scope=None: root / str(scope or "default")):
                acquire_club({"id": 42, "name": "测试俱乐部"}, "account-a")
                self.assertEqual([row["id"] for row in load_acquired_clubs("account-a")["clubs"]], [42])
                self.assertEqual(load_acquired_clubs("account-b")["clubs"], [])

    def test_transaction_history_recovers_clubs_deleted_by_date_rewind(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.world_clubs.save_data_root", side_effect=lambda scope=None: root / str(scope or "default")):
                transactions = [{
                    "type": "world_club_acquisition", "team_id": 42,
                    "team_name": "恢复俱乐部", "amount": -123456,
                    "at": "2026-08-01T12:00:00",
                    "game_date": "2026-07-20",
                }]
                recovered = recover_acquired_clubs_from_transactions(
                    transactions, "account-a",
                )
                repeated = recover_acquired_clubs_from_transactions(
                    transactions, "account-a",
                )
                remaining = load_acquired_clubs("account-a")["clubs"]

        self.assertEqual([row["id"] for row in recovered], [42])
        self.assertEqual(repeated, [])
        self.assertEqual([row["id"] for row in remaining], [42])
        self.assertEqual(remaining[0]["acquisition_price"], 123456)
        self.assertEqual(remaining[0]["acquired_game_date"], "2026-07-20")
        self.assertTrue(remaining[0]["recovered_from_transaction"])

    def test_transaction_history_recovers_paid_recruitment_floor_and_ignores_refund(self) -> None:
        transactions = [
            {
                "id": "upgrade-1", "type": "world_club_facility_upgrade",
                "team_id": 42, "facility": "recruitment", "facility_level": 10,
            },
            {
                "id": "upgrade-2", "type": "world_club_facility_upgrade",
                "team_id": 42, "facility": "recruitment", "facility_level": 11,
            },
            {
                "id": "refund-2", "type": "world_club_facility_upgrade_rollback",
                "team_id": 42, "facility": "recruitment", "facility_level": 11,
                "refund_of": "upgrade-2",
            },
            {
                "id": "training-1", "type": "world_club_facility_upgrade",
                "team_id": 42, "facility": "training", "facility_level": 18,
            },
            {
                "id": "scheduled-recruitment",
                "type": "world_club_facility_upgrade",
                "team_id": 42, "facility": "recruitment",
                "facility_level": 11, "target_level": 20,
                "facility_upgrade_mode": "scheduled_12_day",
            },
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "tools.world_clubs.save_data_root",
                side_effect=lambda scope=None: root / str(scope or "default"),
            ):
                acquire_club({"id": 42, "name": "测试俱乐部"}, "account-a")
                recover_acquired_clubs_from_transactions(transactions, "account-a")
                remaining = load_acquired_clubs("account-a")["clubs"]

        self.assertEqual(
            remaining[0]["facility_level_floors"], {"recruitment": 11},
        )

    def test_recruitment_floor_does_not_cross_a_completed_sale_and_reacquisition(self) -> None:
        transactions = [
            {
                "id": "acquire-1", "type": "world_club_acquisition",
                "team_id": 42, "team_name": "测试俱乐部", "amount": -100,
            },
            {
                "id": "upgrade-1", "type": "world_club_facility_upgrade",
                "team_id": 42, "facility": "recruitment", "facility_level": 14,
            },
            {
                "id": "sale-1", "type": "world_club_sale",
                "team_id": 42, "amount": 90,
            },
            {
                "id": "acquire-2", "type": "world_club_acquisition",
                "team_id": 42, "team_name": "测试俱乐部", "amount": -110,
            },
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch(
                "tools.world_clubs.save_data_root",
                side_effect=lambda scope=None: root / str(scope or "default"),
            ):
                recover_acquired_clubs_from_transactions(transactions, "account-a")
                remaining = load_acquired_clubs("account-a")["clubs"]

        self.assertEqual([row["id"] for row in remaining], [42])
        self.assertNotIn("facility_level_floors", remaining[0])

    def test_date_rewind_suspends_without_deleting_acquisition(self) -> None:
        clubs = [{
            "id": 42, "name": "未来俱乐部",
            "acquired_game_date": "2026-07-20",
        }]

        rewound = acquired_clubs_for_game_date(clubs, "2026-07-19")
        restored = acquired_clubs_for_game_date(clubs, "2026-07-20")

        self.assertFalse(rewound[0]["ownership_active"])
        self.assertEqual(rewound[0]["ownership_suspended_until"], "2026-07-20")
        self.assertTrue(restored[0]["ownership_active"])
        self.assertIsNone(restored[0]["ownership_suspended_until"])
        self.assertNotIn("ownership_active", clubs[0])

    def test_suspended_owned_club_detail_returns_status_without_native_read(self) -> None:
        import fm_odds_web

        state = SimpleNamespace(
            lock=threading.RLock(),
            output={"save_instance_id": "save-a", "game_date": "2026-07-19"},
            _bind_current_save=lambda: "scope-a",
            _data_scope_id=lambda _output: "scope-a",
        )
        suspended = {
            "id": 42, "name": "未来俱乐部", "ownership_active": False,
            "ownership_suspended_until": "2026-07-20",
        }
        with (
            patch("fm_odds_web.account_acquired_clubs", return_value=[suspended]),
            patch(
                "fm_odds_web.read_native_world_club_detail",
                side_effect=AssertionError("挂起记录不应读取或写入游戏内存"),
            ),
        ):
            result = fm_odds_web.LocalOddsState.world_club_detail(state, 42)

        self.assertTrue(result["ownership_suspended"])
        self.assertFalse(result["club"]["ownership_active"])
        self.assertEqual(result["club"]["ownership_suspended_until"], "2026-07-20")

    def test_transaction_recovery_respects_completed_sale(self) -> None:
        transactions = [
            {"type": "world_club_acquisition", "team_id": 42, "amount": -100},
            {"type": "world_club_sale", "team_id": 42, "amount": 98},
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.world_clubs.save_data_root", side_effect=lambda scope=None: root / str(scope or "default")):
                recovered = recover_acquired_clubs_from_transactions(
                    transactions, "account-a",
                )

        self.assertEqual(recovered, [])

    def test_manager_record_accumulates_readable_results_without_duplicates(self) -> None:
        results = [
            {"date": "2026-07-01", "competition": "联赛", "opponent": "甲队", "score": "2-0", "home_away": "主", "result": "胜"},
            {"date": "2026-07-05", "competition": "联赛", "opponent": "乙队", "score": "1-1", "home_away": "客", "result": "平"},
            {"date": "2026-07-09", "competition": "杯赛", "opponent": "丙队", "score": "0-1", "home_away": "主", "result": "负"},
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.world_clubs.save_data_root", side_effect=lambda scope=None: root / str(scope or "default")):
                acquire_club({"id": 42, "name": "测试俱乐部"}, "account-a")
                first = sync_acquired_club_manager_record(42, 10, results, "account-a")
                repeated = sync_acquired_club_manager_record(42, 10, results, "account-a")
                added = sync_acquired_club_manager_record(
                    42, 10,
                    [*results, {"date": "2026-07-12", "competition": "联赛", "opponent": "丁队", "score": "3-0", "home_away": "主", "result": "胜"}],
                    "account-a",
                )
        self.assertEqual(first["total"], {"wins": 1, "draws": 1, "losses": 1})
        self.assertEqual(repeated["total"], first["total"])
        self.assertEqual(added["total"], {"wins": 2, "draws": 1, "losses": 1})

    def test_manager_change_resets_and_ignores_pre_change_results(self) -> None:
        results = [
            {"date": "2026-07-01", "competition": "联赛", "opponent": "甲队", "score": "2-0", "home_away": "主", "result": "胜"},
            {"date": "2026-07-05", "competition": "联赛", "opponent": "乙队", "score": "1-1", "home_away": "客", "result": "平"},
        ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("tools.world_clubs.save_data_root", side_effect=lambda scope=None: root / str(scope or "default")):
                acquire_club({"id": 42, "name": "测试俱乐部"}, "account-a")
                sync_acquired_club_manager_record(42, 10, results, "account-a")
                changed = sync_acquired_club_manager_record(42, 20, results, "account-a")
                after = sync_acquired_club_manager_record(
                    42, 20,
                    [*results, {"date": "2026-07-10", "competition": "联赛", "opponent": "丙队", "score": "0-1", "home_away": "主", "result": "负"}],
                    "account-a",
                )
        self.assertEqual(changed["total"], {"wins": 0, "draws": 0, "losses": 0})
        self.assertEqual(after["season"], {"wins": 0, "draws": 0, "losses": 1})
        self.assertEqual(after["total"], after["season"])

    def test_acquisition_valuation_uses_elite_band_and_maximum_quality_scores(self) -> None:
        information = {
            "facilities": {
                "training": 20, "youth": 20,
                "junior_coaching": 20, "youth_recruitment": 20,
            },
            "finances": {"balance": -50_000_000, "remaining_transfer_budget": 20_000_000},
            "stadium": {
                "capacity": 100_000, "expansion_capacity": 100_000,
                "state_raw": 0, "pitch_condition": 200,
            },
        }
        roster = [
            {"id": index, "ca": 200, "pa": 200, "loan_status": "loaned_in"}
            for index in range(1, 26)
        ]
        result = club_acquisition_valuation(information, roster, 10_000)
        self.assertEqual(result["finance"]["actual"], 20_000_000)
        self.assertEqual(result["finance"]["balance_asset"], 0)
        self.assertEqual(result["finance"]["floor"], 10_000_000_000)
        self.assertEqual(result["finance"]["protected"], 10_000_000_000)
        self.assertEqual(result["price_band"], {
            "minimum": 30_000_000_000, "maximum": 60_000_000_000,
        })
        self.assertEqual(result["multiplier"], 1.0)
        self.assertEqual(result["scores"]["squad"], 1.0)
        self.assertEqual(result["scores"]["facilities"], 1.0)
        self.assertEqual(result["scores"]["stadium"], 1.0)
        self.assertEqual(result["price"], 57_900_000_000)
        self.assertLessEqual(result["price"], result["price_band"]["maximum"])
        self.assertTrue(result["negative_balance_reset"])

    def test_saudi_pro_league_adds_fifteen_billion_to_acquisition_and_sale(self) -> None:
        import fm_odds_web

        ordinary = club_acquisition_valuation({}, [], 7_000, "英超联赛")
        for competition in ("沙特超级联赛", "沙特职业联赛", "Saudi Pro League"):
            with self.subTest(competition=competition):
                saudi = club_acquisition_valuation({}, [], 7_000, competition)
                self.assertEqual(saudi["competition_premium"], 15_000_000_000)
                self.assertEqual(saudi["price"], ordinary["price"] + 15_000_000_000)
                self.assertEqual(
                    saudi["price_band"]["minimum"],
                    ordinary["price_band"]["minimum"] + 15_000_000_000,
                )
                self.assertEqual(
                    fm_odds_web.owned_club_sale_price({
                        "current_valuation": saudi["price"],
                        "competition_premium": saudi["competition_premium"],
                    }),
                    ordinary["price"] * 98 // 100 + 15_000_000_000,
                )

    def test_non_professional_clubs_are_cheaper_to_buy_and_sell(self) -> None:
        import fm_odds_web

        professional = club_acquisition_valuation(
            {"status": {"code": 1, "name": "职业"}}, [], 6_000,
        )
        for status in ({"code": 2, "name": "半职业"}, {"name": "Semi-Professional"}):
            with self.subTest(status=status):
                semi_pro = club_acquisition_valuation(
                    {"status": status}, [], 6_000,
                )
                self.assertEqual(semi_pro["price"], professional["price"] // 8)
                self.assertEqual(semi_pro["multiplier"], 1 / 16)
                self.assertEqual(semi_pro["pricing_divisor"], 16)
                self.assertEqual(semi_pro["pricing_basis"], "semi_professional")
                self.assertEqual(semi_pro["reputation_pricing_divisor"], 1)
                self.assertEqual(
                    semi_pro["price_band"]["minimum"],
                    500_000_000 // 16,
                )
                self.assertEqual(
                    fm_odds_web.owned_club_sale_price({
                        "current_valuation": semi_pro["price"],
                        "competition_premium": semi_pro["competition_premium"],
                    }),
                    semi_pro["price"] * 98 // 100,
                )
                rebalance = world_clubs.acquisition_rebalance_quote({
                    "reputation": 6_000,
                    "acquisition_price": semi_pro["price"],
                    "acquisition_valuation": semi_pro,
                })
                self.assertEqual(rebalance["fair_price"], semi_pro["price"])
                self.assertEqual(rebalance["refund"], 0)

        for status in ({"code": 3, "name": "业余"}, {"name": "Amateur"}):
            with self.subTest(status=status):
                amateur = club_acquisition_valuation(
                    {"status": status}, [], 6_000,
                )
                self.assertEqual(amateur["price"], professional["price"] // 24)
                self.assertEqual(amateur["multiplier"], 1 / 48)
                self.assertEqual(amateur["pricing_divisor"], 48)
                self.assertEqual(amateur["pricing_basis"], "amateur")
                self.assertEqual(amateur["reputation_pricing_divisor"], 1)
                self.assertEqual(
                    fm_odds_web.owned_club_sale_price({
                        "current_valuation": amateur["price"],
                        "competition_premium": amateur["competition_premium"],
                    }),
                    amateur["price"] * 98 // 100,
                )

        professional_saudi = club_acquisition_valuation(
            {"status": {"code": 1}}, [], 6_000, "Saudi Pro League",
        )
        semi_pro_saudi = club_acquisition_valuation(
            {"status": {"code": 2}}, [], 6_000, "Saudi Pro League",
        )
        self.assertEqual(
            semi_pro_saudi["competition_premium"],
            professional_saudi["competition_premium"] // 8,
        )
        self.assertEqual(semi_pro_saudi["price"], professional_saudi["price"] // 8)

    def test_low_reputation_professional_clubs_use_two_to_six_fold_discounts(self) -> None:
        cases = {
            6_000: 2,
            4_500: 3,
            3_000: 4,
            1_500: 5,
            500: 6,
        }
        for reputation, divisor in cases.items():
            with self.subTest(reputation=reputation):
                result = club_acquisition_valuation(
                    {"status": {"code": 1, "name": "职业"}}, [], reputation,
                )
                self.assertEqual(result["pricing_basis"], "professional")
                self.assertEqual(result["status_pricing_divisor"], 1)
                self.assertEqual(result["reputation_pricing_divisor"], divisor)
                self.assertEqual(result["pricing_divisor"], divisor)

                saudi = club_acquisition_valuation(
                    {"status": {"code": 1, "name": "职业"}}, [],
                    reputation, "Saudi Pro League",
                )
                self.assertEqual(
                    saudi["competition_premium"], 15_000_000_000 // divisor,
                )

        elite = club_acquisition_valuation(
            {"status": {"code": 1, "name": "职业"}}, [], 7_000,
        )
        self.assertEqual(elite["pricing_divisor"], 1)
        unreadable = club_acquisition_valuation(
            {"status": {"code": 1, "name": "职业"}}, [], None,
        )
        self.assertEqual(unreadable["pricing_divisor"], 1)

    def test_current_low_reputation_price_snapshot_needs_no_rebalance(self) -> None:
        valuation = club_acquisition_valuation(
            {"status": {"code": 1, "name": "职业"}}, [], 4_500,
        )
        quote = world_clubs.acquisition_rebalance_quote({
            "reputation": 4_500,
            "acquisition_price": valuation["price"],
            "acquisition_valuation": valuation,
        })

        self.assertEqual(quote["status"], "current")
        self.assertEqual(quote["fair_price"], valuation["price"])
        self.assertEqual(quote["refund"], 0)

    def test_squad_quality_is_capped_at_one(self) -> None:
        roster = [{"id": index, "ca": 999, "pa": 999} for index in range(1, 40)]
        invalid = club_acquisition_valuation({}, roster, 0)
        self.assertEqual(invalid["player_count"], 0)
        valid = club_acquisition_valuation(
            {"finances": {"balance": 1}},
            [{"id": index, "ca": 200, "pa": 200} for index in range(1, 40)],
            0,
        )
        self.assertEqual(valid["scores"]["squad"], 1.0)
        self.assertEqual(valid["components"]["squad"], 500_000)
        self.assertGreaterEqual(valid["price"], 5_000_000 // 6)
        self.assertLessEqual(valid["price"], 25_000_000 // 6)

    def test_elite_squad_quality_maps_raw_80_to_display_90(self) -> None:
        self.assertEqual(world_clubs._display_squad_quality(0.80), 0.90)
        roster = [
            {"id": index, "ca": 160, "pa": 160}
            for index in range(1, 26)
        ]
        result = club_acquisition_valuation({}, roster, 9_000)
        self.assertEqual(result["scores"]["squad"], 0.9133)

    def test_missing_elite_roster_uses_reputation_as_display_score(self) -> None:
        result = club_acquisition_valuation({}, [], 9_000)
        self.assertEqual(result["scores"]["squad"], 0.90)
        self.assertIn("squad", result["inferred_fields"])

    def test_facilities_quality_scales_within_the_reputation_band(self) -> None:
        def value(level: int) -> int:
            facilities = {
                "training": level, "youth": level,
                "junior_coaching": level, "youth_recruitment": level,
            }
            result = club_acquisition_valuation({"facilities": facilities}, [], 0)
            return result["components"]["facilities"]

        self.assertEqual(value(7), 700_000 // 6)
        self.assertEqual(value(8), 800_000 // 6)
        self.assertEqual(value(14), 1_400_000 // 6)
        self.assertEqual(value(20), 2_000_000 // 6)

    def test_brand_baseline_uses_continuous_reputation_bands(self) -> None:
        information = {"finances": {"balance": 1}}
        expected = {
            4_000: 83_333_334,
            7_000: 1_000_000_000,
            8_500: 10_000_000_000,
            10_000: 49_500_000_000,
        }
        for reputation, brand_value in expected.items():
            with self.subTest(reputation=reputation):
                result = club_acquisition_valuation(information, [], reputation)
                self.assertEqual(result["components"]["brand"], brand_value)

    def test_owned_brand_promotion_prices_are_two_times_previous_band_prices(self) -> None:
        from fm_odds_web import owned_club_brand_quote, owned_club_brand_quotes

        self.assertEqual(owned_club_brand_quote(4_000, 50)["price"], 16_000_000)
        self.assertEqual(owned_club_brand_quote(7_000, 100)["price"], 120_000_000)
        self.assertEqual(owned_club_brand_quote(9_500, 300)["price"], 1_440_000_000)
        self.assertEqual(
            [quote["requested"] for quote in owned_club_brand_quotes(7_000)],
            [50, 100, 300],
        )
        with self.assertRaisesRegex(ValueError, "推广规格"):
            owned_club_brand_quote(7_000, 20)

    def test_finance_floor_handles_missing_values_and_never_undercuts_assets(self) -> None:
        missing = club_acquisition_valuation({}, [], 7_000)
        self.assertEqual(missing["finance"]["actual"], 0)
        self.assertEqual(missing["finance"]["floor"], 2_000_000_000)
        self.assertEqual(missing["finance"]["protected"], 2_000_000_000)
        self.assertEqual(missing["inferred_fields"], ["squad", "facilities", "stadium"])
        self.assertGreaterEqual(missing["price"], missing["price_band"]["minimum"])
        observed = club_acquisition_valuation({
            "finances": {
                "balance": -120_000_000_000,
                "remaining_transfer_budget": 40_000_000_000,
            },
        }, [], 9_500)
        self.assertEqual(observed["finance"]["actual"], 40_000_000_000)
        self.assertEqual(observed["finance"]["balance_asset"], 0)
        self.assertEqual(observed["finance"]["protected"], 40_000_000_000)
        self.assertGreaterEqual(observed["price"], 40_000_000_000)
        self.assertLessEqual(observed["price"], observed["price_band"]["maximum"])

    def test_fm24_negative_balance_is_not_priced_as_acquisition_asset(self) -> None:
        result = club_acquisition_valuation({
            "finances": {
                "balance": -2_000_000_000,
                "remaining_transfer_budget": 100_000_000,
            },
        }, [], 7_000)

        self.assertEqual(result["finance"]["balance_absolute"], 2_000_000_000)
        self.assertEqual(result["finance"]["balance_asset"], 0)
        self.assertEqual(result["finance"]["actual"], 100_000_000)
        self.assertTrue(result["negative_balance_reset"])
        self.assertLessEqual(result["price"], result["price_band"]["maximum"])

    def test_reputation_bands_set_expected_total_price_ranges(self) -> None:
        cases = {
            500: (5_000_000 // 6, 25_000_000 // 6),
            1_500: (25_000_000 // 5, 100_000_000 // 5),
            3_000: (100_000_000 // 4, 250_000_000 // 4),
            4_500: (250_000_000 // 3, 500_000_000 // 3),
            6_000: (500_000_000 // 2, 1_000_000_000 // 2),
            7_500: (1_000_000_000, 10_000_000_000),
            8_750: (10_000_000_000, 30_000_000_000),
            9_500: (30_000_000_000, 60_000_000_000),
        }
        for reputation, expected_band in cases.items():
            with self.subTest(reputation=reputation):
                result = club_acquisition_valuation({}, [], reputation)
                self.assertEqual(
                    (result["price_band"]["minimum"], result["price_band"]["maximum"]),
                    expected_band,
                )
                self.assertGreaterEqual(result["price"], expected_band[0])
                self.assertLessEqual(result["price"], expected_band[1])

    def test_ordinary_reputation_clubs_use_tempered_acquisition_prices(self) -> None:
        league_one_like = club_acquisition_valuation({}, [], 4_500)
        championship_like = club_acquisition_valuation({}, [], 6_000)

        self.assertGreaterEqual(league_one_like["price"], 100_000_000)
        self.assertLessEqual(league_one_like["price"], 135_000_000)
        self.assertGreaterEqual(championship_like["price"], 300_000_000)
        self.assertLessEqual(championship_like["price"], 400_000_000)

    def test_groups_teams_and_sorts_by_reputation(self) -> None:
        output = {
            "competition_formats": [{
                "competition_id": 101,
                "competition_name": "J1联赛",
                "competition_kind": "league",
                "stages": [{"teams": [
                    {"id": 2, "name": "乙队", "address": "0x2", "reputation": 700},
                    {"id": 1, "name": "甲队", "address": "0x1", "reputation": 900},
                ]}],
            }],
            "season_results": [{
                "date": "2028-01-02", "competition_name": "J1联赛",
                "home": {"id": 1, "name": "甲队"}, "away": {"id": 2, "name": "乙队"},
                "home_goals": 2, "away_goals": 0,
            }],
        }
        result = build_world_clubs(output)
        self.assertEqual([row["id"] for row in result["clubs"]], [1, 2])
        self.assertEqual(result["clubs"][0]["continent"], "亚洲")
        self.assertEqual(result["clubs"][0]["nation"], "日本")
        self.assertEqual(result["clubs"][0]["competition_id"], 101)
        self.assertEqual(result["clubs"][0]["recent_results"][0]["result"], "胜")

    def test_excludes_national_teams_and_prefers_league_over_cup(self) -> None:
        output = {
            "competition_formats": [
                {
                    "competition_name": "国家队资格赛",
                    "competition_kind": "national",
                    "stages": [{"teams": [{"id": 99, "name": "国家队"}]}],
                },
                {
                    "competition_id": 20,
                    "competition_name": "天皇杯",
                    "competition_kind": "cup",
                    "stages": [{"teams": [{"id": 10, "name": "京都不死鸟", "reputation": 500}]}],
                },
                {
                    "competition_id": 10,
                    "competition_name": "J1联赛",
                    "competition_kind": "league",
                    "stages": [{"teams": [{"id": 10, "name": "京都不死鸟", "reputation": 600}]}],
                },
                {
                    "competition_id": 11,
                    "competition_name": "日本18岁以下甲级联赛",
                    "competition_kind": "league",
                    "stages": [{"teams": [{"id": 11, "name": "京都不死鸟", "reputation": 100}]}],
                },
            ],
        }
        result = build_world_clubs(output)
        self.assertEqual([row["id"] for row in result["clubs"]], [10])
        self.assertEqual(result["clubs"][0]["competition"], "J1联赛")
        self.assertEqual(result["clubs"][0]["competition_id"], 10)
        self.assertEqual(result["clubs"][0]["competitions"], ["J1联赛", "天皇杯"])

    def test_league_position_requires_matching_competition_and_team(self) -> None:
        directory = {"clubs": [
            {"id": 1, "name": "甲队", "competition_id": 10},
            {"id": 2, "name": "乙队", "competition_id": 20},
            {"id": 3, "name": "杯赛队", "competition_id": 99},
            {
                "id": 4, "name": "皇家马德里足球俱乐部",
                "continent": "欧洲", "nation": "西班牙",
                "competition": "西班牙足球甲级联赛",
            },
        ]}
        result = enrich_world_club_standings(directory, {"competitions": [
            {
                "competition_id": 10, "competition_name": "J1联赛",
                "teams": [
                    {"team_id": 2, "position": 1, "points": 12},
                    {"team_id": 1, "position": 2, "points": 9},
                ],
            },
            {
                "competition_id": 67, "competition_name": "西甲联赛",
                "teams": [{"team_id": 4, "position": 1, "points": 51}],
            },
            {
                "competition_id": 1301394, "competition_name": "欧冠联赛",
                "teams": [{"team_id": 4, "position": 9, "points": 12}],
            },
        ]})
        rows = {row["id"]: row for row in result["clubs"]}
        self.assertEqual(rows[1]["league_position"], 2)
        self.assertEqual(rows[1]["league_team_count"], 2)
        self.assertEqual(rows[1]["league_points"], 9)
        self.assertNotIn("league_position", rows[2])
        self.assertNotIn("league_position", rows[3])
        self.assertEqual(rows[4]["competition_id"], 67)
        self.assertEqual(rows[4]["competition"], "西甲联赛")
        self.assertEqual(rows[4]["league_position"], 1)

    def test_native_merge_keeps_domestic_league_and_filters_only_explicit_non_mens_markers(self) -> None:
        self.assertTrue(_looks_non_mens("Kyoto Women", "League"))
        self.assertTrue(_looks_non_mens("Club U19", "Youth League"))
        self.assertTrue(_looks_non_mens("富勒姆", "英格兰18岁以下甲级联赛"))
        self.assertTrue(_looks_non_mens("Fulham", "England U-21 League"))
        self.assertFalse(_looks_non_mens("Argentinos Juniors", "Primera División"))
        result = merge_native_world_clubs(
            {"generated_at": "now", "clubs": [{
                "id": 1, "name": "京都不死鸟", "competition": "J1联赛",
                "competitions": ["J1联赛"], "continent": "亚洲", "nation": "日本",
                "reputation": 600, "recent_results": [],
            }]},
            {"clubs": [{
                "id": 1, "name": "京都不死鸟", "competition": "天皇杯",
                "competitions": ["天皇杯"], "continent": "亚洲", "nation": "日本",
                "reputation": 610, "recent_results": [], "source": "native_scan",
            }, {
                "id": 2, "name": "河内FC", "competition": "越南甲级联赛",
                "competitions": ["越南甲级联赛"], "continent": "亚洲", "nation": "越南",
                "reputation": 300, "recent_results": [], "source": "native_scan",
            }, {
                "id": 3, "name": "京都不死鸟", "competition": "日本18岁以下甲级联赛",
                "competitions": ["日本18岁以下甲级联赛"], "continent": "亚洲", "nation": "日本",
                "reputation": 200, "recent_results": [], "source": "native_scan",
            }]},
        )
        self.assertEqual([row["id"] for row in result["clubs"]], [1, 2])
        self.assertEqual(result["clubs"][0]["competition"], "J1联赛")
        self.assertEqual(result["clubs"][0]["reputation"], 610)

    def test_acquired_name_override_does_not_mutate_native_directory(self) -> None:
        directory = {"clubs": [{"id": 42, "name": "原名称", "short_name": "原名称"}]}
        result = apply_acquired_club_overrides(
            directory, [{
                "id": 42,
                "renamed_name": "新完整名称",
                "renamed_short_name": "新简称",
            }],
        )
        self.assertEqual(result["clubs"][0]["name"], "新完整名称")
        self.assertEqual(result["clubs"][0]["short_name"], "新简称")
        self.assertEqual(result["clubs"][0]["display_name"], "新简称")
        self.assertTrue(result["clubs"][0]["name_overridden"])
        self.assertEqual(directory["clubs"][0]["name"], "原名称")
        self.assertEqual(directory["clubs"][0]["short_name"], "原名称")

    def test_china_nation_aliases_share_one_filter_and_facet(self) -> None:
        directory = {"clubs": [
            {
                "id": 1, "name": "原生扫描球队", "continent": "亚洲",
                "nation": "中华人民共和国", "competition": "中国甲级联赛",
                "reputation": 500,
            },
            {
                "id": 2, "name": "赛事目录球队", "continent": "亚洲",
                "nation": "中国", "competition": "中超联赛", "reputation": 600,
            },
        ]}
        result = paginate_world_clubs(directory, continent="亚洲", nation="中华人民共和国")
        self.assertEqual([row["id"] for row in result["clubs"]], [1, 2])
        self.assertEqual({row["nation"] for row in result["clubs"]}, {"中国"})
        asia = next(row for row in result["facets"] if row["name"] == "亚洲")
        self.assertEqual(asia["nations"], [{"name": "中国", "count": 2}])

    def test_pagination_never_returns_more_than_fifteen_cards(self) -> None:
        directory = {"clubs": [
            {
                "id": index, "name": f"Club {index}", "continent": "亚洲",
                "nation": "日本" if index <= 35 else "越南", "competition": "League",
                "competitions": ["League"], "reputation": 1000 - index,
            }
            for index in range(1, 66)
        ]}
        first = paginate_world_clubs(directory, page=1, page_size=99)
        self.assertEqual(len(first["clubs"]), 15)
        self.assertEqual(first["pagination"]["page_count"], 5)
        self.assertEqual(first["pagination"]["total"], 65)
        self.assertEqual(first["clubs"][0]["reputation_rank"], 1)
        self.assertEqual(first["clubs"][0]["competition_count"], 1)
        japan = paginate_world_clubs(directory, continent="亚洲", nation="日本", page=2)
        self.assertEqual(len(japan["clubs"]), 15)
        search = paginate_world_clubs(directory, search="Club 64")
        self.assertEqual([row["id"] for row in search["clubs"]], [64])

    def test_common_chinese_alias_prioritizes_senior_club_name(self) -> None:
        directory = {"clubs": [
            {
                "id": 679, "name": "曼彻斯特城", "continent": "欧洲",
                "nation": "英格兰", "competition": "英格兰足球超级联赛",
                "competitions": ["英格兰足球超级联赛"], "reputation": 9150,
            },
            {
                "id": 9001, "name": "曼城青年队", "continent": "欧洲",
                "nation": "英格兰", "competition": "英格兰青年联赛",
                "competitions": ["英格兰青年联赛"], "reputation": 9500,
            },
        ]}
        result = paginate_world_clubs(directory, search="曼城")
        self.assertEqual([row["id"] for row in result["clubs"]], [679, 9001])

    def test_same_name_clubs_are_qualified_by_competition_not_merged(self) -> None:
        directory = {"clubs": [
            {
                "id": 426430, "name": "纽卡斯尔喷气机", "continent": "大洋洲",
                "nation": "澳大利亚", "competition": "澳大利亚足球超级联赛",
                "competitions": ["澳大利亚足球超级联赛"], "reputation": 4900,
            },
            {
                "id": 15068483, "name": "纽卡斯尔喷气机", "continent": "大洋洲",
                "nation": "澳大利亚", "competition": "新南威尔士足球超级联赛",
                "competitions": ["新南威尔士足球超级联赛"], "reputation": 3250,
            },
        ]}
        result = paginate_world_clubs(directory, search="纽卡斯尔喷气机")
        self.assertEqual(result["pagination"]["total"], 2)
        self.assertEqual(
            [row["display_name"] for row in result["clubs"]],
            [
                "纽卡斯尔喷气机 · 澳大利亚足球超级联赛",
                "纽卡斯尔喷气机 · 新南威尔士足球超级联赛",
            ],
        )

    def test_unassigned_clubs_are_searchable_but_not_a_browse_category(self) -> None:
        directory = {"clubs": [
            {
                "id": 1, "name": "已归属俱乐部", "continent": "亚洲",
                "nation": "日本", "competition": "J1联赛", "reputation": 500,
            },
            {
                "id": 2, "name": "无归属俱乐部", "continent": "未分类",
                "nation": "未分类", "competition": "未分类", "reputation": 100,
            },
        ]}
        browse = paginate_world_clubs(directory)
        self.assertEqual([row["id"] for row in browse["clubs"]], [1])
        self.assertEqual([row["name"] for row in browse["facets"]], ["亚洲"])
        self.assertNotIn("未分类", str(browse["facets"]))
        search = paginate_world_clubs(directory, search="无归属")
        self.assertEqual([row["id"] for row in search["clubs"]], [2])

    def test_loading_cache_never_starts_a_scan(self) -> None:
        identity = {
            "game_key": "fm26", "game_version": "26.3.2", "build_identity": "build",
            "save_id": "save-1", "process_id": 10, "module_base": "0x1000",
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text(json.dumps({"schema_version": 4, **identity, "clubs": []}), encoding="utf-8")
            layout = type("Layout", (), {"key": "fm26"})()
            with (
                patch("tools.world_clubs._native_cache_path", return_value=path),
                patch("tools.world_clubs.select_process_layout", return_value=(10, "fm.exe", layout)),
                patch("tools.world_clubs._runtime_identity", return_value=identity),
                patch("tools.world_clubs.scan_native_world_clubs", side_effect=AssertionError("unexpected scan")),
            ):
                payload = load_native_world_clubs("save-1")
        self.assertIsNotNone(payload)
        self.assertTrue(payload["addresses_current"])

    def test_atomic_cache_write_retries_windows_file_contention(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cache.json"
            path.write_text('{"old":true}', encoding="utf-8")
            from os import replace as real_replace
            attempts = 0

            def flaky_replace(source: str | Path, target: str | Path) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise PermissionError(5, "access denied")
                real_replace(source, target)

            with (
                patch("tools.world_clubs.os.replace", side_effect=flaky_replace),
                patch("tools.world_clubs.time.sleep"),
            ):
                _atomic_write_json(path, {"ready": True})
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(attempts, 2)
        self.assertTrue(payload["ready"])

    def test_fm_nation_ids_map_to_continents_before_code_fallbacks(self) -> None:
        self.assertEqual(_native_region_from_nation(771, "")[0], "欧洲")
        self.assertEqual(_native_region_from_nation(145, "")[0], "亚洲")
        self.assertEqual(_native_region_from_nation(45, "")[0], "非洲")
        self.assertEqual(_native_region_from_nation(1435, "")[0], "大洋洲")
        self.assertEqual(_native_region_from_nation(1657, "")[0], "南美洲")


if __name__ == "__main__":
    unittest.main()
