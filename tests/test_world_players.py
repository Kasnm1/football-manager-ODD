from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import struct
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import fm_odds_web
from tools.club_reader import _world_player_table_rows
import tools.club_reader as club_reader
from tools.game_layout import (
    FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT, FM24_LAYOUT,
    FM24_XGP_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE,
)


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_previous_club_layout_is_enabled_for_verified_builds() -> None:
    assert FM26_LAYOUT.person_previous_club_offset == 0x108
    assert FM24_LAYOUT.person_previous_club_offset == 0x138
    assert FM24_EPIC_LAYOUT.person_previous_club_offset == 0x138
    assert FM26_XGP_TEMPLATE.person_previous_club_offset is None
    for layout in (
        FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_XGP_LAYOUT,
    ):
        assert layout.person_previous_club_offset is None


def test_fm24_epic_exposes_verified_nationality_and_relation_writes() -> None:
    assert club_reader._nationality_layout_supported(FM24_EPIC_LAYOUT) is True
    assert club_reader._person_relation_layout_supported(FM24_EPIC_LAYOUT) is True
    assert club_reader._nationality_layout_supported(FM24_XGP_LAYOUT) is False
    assert club_reader._person_relation_layout_supported(FM24_XGP_LAYOUT) is False


def test_world_player_previous_club_distinguishes_value_none_and_unavailable() -> None:
    person = 0x2000
    club = 0x3000
    module_base = 0x100000
    layout = SimpleNamespace(
        person_previous_club_offset=0x108, club_vtable_rva=0x2200,
    )

    class Reader:
        def __init__(self, pointer_raw: bytes | None, *, valid: bool = True):
            self.layout = layout
            self.module_base = module_base
            self.pointer_raw = pointer_raw
            self.valid = valid

        def bytes(self, address, size):
            assert address == person + 0x108
            assert size == 8
            return self.pointer_raw

        def ptr(self, address):
            assert address == club
            return module_base + (0x2200 if self.valid else 0x9999)

        def u32(self, address):
            assert address == club + club_reader.ENTITY_UID
            return 791

        def fm_string_at(self, address):
            return "东京绿茵" if address == club + club_reader.CLUB_NAME_SHORT else None

    available = club_reader._read_world_player_previous_club(
        Reader(struct.pack("<Q", club)), person,
    )
    assert available == {
        "previous_club_state": "available",
        "previous_club_id": 791,
        "previous_club_name": "东京绿茵",
    }
    explicit_none = club_reader._read_world_player_previous_club(
        Reader(struct.pack("<Q", 0)), person,
    )
    assert explicit_none["previous_club_state"] == "none"
    assert explicit_none["previous_club_name"] is None
    unreadable = club_reader._read_world_player_previous_club(
        Reader(None), person,
    )
    assert unreadable["previous_club_state"] == "unavailable"
    invalid = club_reader._read_world_player_previous_club(
        Reader(struct.pack("<Q", club), valid=False), person,
    )
    assert invalid["previous_club_state"] == "unavailable"


def test_world_player_api_returns_session_cached_search_results():
    state = SimpleNamespace(
        lock=threading.RLock(),
        memory_lock=threading.RLock(),
        output={"save_instance_id": "save-a"},
        _has_verified_save=lambda: True,
        _data_scope_id=lambda _output: "scope-a",
    )
    cached = {
        "players": [{"id": 7, "name": "测试球员"}],
        "pagination": {
            "page": 1, "page_size": 30, "page_count": 1,
            "total": 1, "from": 1, "to": 1,
        },
        "filters": {"search": "测试"},
        "index": {"source": "session_person_cache", "player_count": 58000},
    }
    with (
        patch("fm_odds_web.search_world_players", return_value=cached) as search,
        patch("fm_odds_web.player_alias_uids_for_query", return_value=()) as aliases,
        patch("fm_odds_web.apply_player_aliases") as apply_names,
    ):
        result = fm_odds_web.LocalOddsState.public_world_players(
            state, search="测试", page=1, page_size=30,
            sort_by="ca", sort_order="asc",
        )

    search.assert_called_once_with(
        "测试", page=1, page_size=30, force_refresh=False,
        sort_by="ca", sort_order="asc", candidate_uids=(),
    )
    aliases.assert_called_once_with("scope-a", "测试")
    apply_names.assert_called_once_with(cached, "scope-a")
    assert result["ready"] is True
    assert result["data_scope_id"] == "scope-a"
    assert result["index"]["source"] == "session_person_cache"


def test_world_player_chinese_alias_search_passes_stable_uid_candidates():
    state = SimpleNamespace(
        lock=threading.RLock(), memory_lock=threading.RLock(),
        output={"save_instance_id": "save-a"},
        _has_verified_save=lambda: True,
        _data_scope_id=lambda _output: "scope-a",
    )
    response = {
        "players": [{"id": 7, "name": "Koki Saito"}],
        "pagination": {"page": 1, "page_size": 12, "page_count": 1, "total": 1},
        "index": {"source": "session_person_cache_localized_uid"},
    }
    with patch(
        "fm_odds_web.player_alias_uids_for_query", return_value=(7,),
    ), patch(
        "fm_odds_web.search_world_players", return_value=response,
    ) as search, patch("fm_odds_web.apply_player_aliases"):
        result = fm_odds_web.LocalOddsState.public_world_players(
            state, search="齐藤光毅", page_size=12,
        )

    search.assert_called_once_with(
        "齐藤光毅", page=1, page_size=12, force_refresh=False,
        sort_by="pa", sort_order="desc", candidate_uids=(7,),
    )
    assert result["players"][0]["id"] == 7


def test_world_player_api_forwards_nationality_age_ca_and_pa_filters():
    state = SimpleNamespace(
        lock=threading.RLock(), memory_lock=threading.RLock(),
        output={"save_instance_id": "save-a"},
        _has_verified_save=lambda: True,
        _data_scope_id=lambda _output: "scope-a",
    )
    response = {
        "players": [],
        "pagination": {"page": 1, "page_size": 30, "page_count": 1, "total": 0},
        "filters": {}, "index": {"source": "session_person_cache"},
    }
    with (
        patch("fm_odds_web.search_world_players", return_value=response) as search,
        patch("fm_odds_web.apply_player_aliases"),
    ):
        fm_odds_web.LocalOddsState.public_world_players(
            state, nationality_id=765, min_age=18, max_age=23,
            min_ca=120, max_ca=160, min_pa=150, max_pa=190,
        )

    search.assert_called_once_with(
        "", page=1, page_size=30, force_refresh=False,
        sort_by="pa", sort_order="desc", nationality_id=765,
        min_age=18, max_age=23, min_ca=120, max_ca=160,
        min_pa=150, max_pa=190,
    )


def test_world_player_route_parses_directory_filter_query_values():
    calls = []
    handler = SimpleNamespace(
        state=SimpleNamespace(
            public_world_players=lambda **kwargs: calls.append(kwargs) or {"ready": True},
        ),
    )

    result = fm_odds_web._get_world_players_route(handler, {}, {
        "nationality_id": ["765"], "min_age": ["18"], "max_age": ["23"],
        "min_ca": ["120"], "max_ca": ["160"],
        "min_pa": ["150"], "max_pa": ["190"],
    })

    assert result == {"ready": True}
    assert calls[0]["nationality_id"] == 765
    assert calls[0]["min_age"] == 18
    assert calls[0]["max_age"] == 23
    assert calls[0]["min_ca"] == 120
    assert calls[0]["max_ca"] == 160
    assert calls[0]["min_pa"] == 150
    assert calls[0]["max_pa"] == 190


def test_world_player_action_is_foreground_timed_and_keeps_memory_gate():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = fm_odds_web.ForegroundPriorityLock()
    state.last_operation_performance = {}
    state._bind_current_save = lambda: "scope-a"
    events = []

    def action():
        events.append(("action", state.memory_lock.locked()))
        return {"changed": True}

    with (
        patch("fm_odds_web.set_active_save_id"),
        patch(
            "fm_odds_web.read_world_player_profile",
            side_effect=lambda player_id: {
                "id": player_id, "name": "测试球员",
            },
        ),
        patch("fm_odds_web.apply_player_aliases"),
    ):
        result = state._world_player_action_result(
            7, "world_player_test", action,
        )

    assert events == [("action", True)]
    assert result["result"] == {"changed": True}
    assert state.last_operation_performance["operation"] == "world_player_test"
    assert state.last_operation_performance["succeeded"] is True
    assert not state.memory_lock.locked()


def test_world_player_search_reports_the_database_index_root_cause():
    reader = SimpleNamespace(
        database_index_provider=lambda: None,
        database_index_error_provider=lambda: "native person table header is invalid",
    )
    @contextmanager
    def borrowed():
        yield reader

    with patch.object(
        club_reader, "borrow_game_reader", return_value=borrowed(),
    ):
        with pytest.raises(RuntimeError, match="native person table header is invalid"):
            club_reader.search_world_players()


def test_world_player_index_phase_skips_visible_page_live_hydration():
    nation_id = next(iter(club_reader.NATION_NAMES))
    directory = SimpleNamespace(
        search_players=lambda *_args, **_kwargs: {
            "players": [{
                "id": 7, "name": "测试球员", "ca": 121, "pa": 154,
                "nationality_id": nation_id,
            }],
            "pagination": {"page": 1, "page_size": 30, "page_count": 1, "total": 1},
            "filter_options": {"nationality_ids": [nation_id]},
            "index": {"source": "session_person_cache"},
        },
    )
    reader = SimpleNamespace(
        database_index_provider=lambda: directory,
        database_index_error_provider=lambda: "",
        layout=SimpleNamespace(key="fm26", person_flags_offset=0x18),
    )

    @contextmanager
    def borrowed():
        yield reader

    with (
        patch.object(club_reader, "borrow_game_reader", return_value=borrowed()),
        patch.object(club_reader, "_world_player_table_rows") as hydrate,
    ):
        result = club_reader.search_world_players(live_summary=False)

    hydrate.assert_not_called()
    assert result["players"][0]["ca"] == 121
    assert result["players"][0]["nationality"] == club_reader.NATION_NAMES[nation_id]
    assert result["filter_options"]["nationalities"] == [
        {"id": nation_id, "name": club_reader.NATION_NAMES[nation_id]},
    ]
    assert result["index"]["live_summary"] is False


def test_world_player_index_phase_uses_foreground_priority_and_records_timing():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = fm_odds_web.ForegroundPriorityLock()
    state.last_operation_performance = {}
    state.output = {"save_instance_id": "save-a", "game_date": "2026-08-31"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "scope-a"
    response = {
        "players": [{"id": 7, "name": "测试球员"}],
        "pagination": {"page": 1, "page_size": 30, "page_count": 1, "total": 1},
        "index": {"source": "session_person_cache"},
    }

    with (
        patch("fm_odds_web.search_world_players", return_value=response) as search,
        patch("fm_odds_web.set_active_save_id"),
        patch("fm_odds_web.apply_player_aliases"),
    ):
        result = state.public_world_players(live_summary=False)

    search.assert_called_once_with(
        "", page=1, page_size=30, force_refresh=False,
        sort_by="pa", sort_order="desc", live_summary=False,
    )
    assert result["game_date"] == "2026-08-31"
    assert state.last_operation_performance["operation"] == "world_player_search"
    assert state.last_operation_performance["succeeded"] is True
    assert not state.memory_lock.locked()


def test_world_player_page_is_wired_to_cached_search_endpoint():
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    dark_styles = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")
    server = (ROOT / "fm_odds_web.py").read_text(encoding="utf-8")

    assert 'data-page="world-players"' in markup
    assert 'id="page-world-players"' in markup
    assert 'id="world-player-search"' in markup
    assert 'id="world-player-filters"' in markup
    assert 'id="world-player-nationality"' in markup
    assert 'id="world-player-min-age"' in markup
    assert 'id="world-player-max-age"' in markup
    assert 'id="world-player-min-ca"' in markup
    assert 'id="world-player-max-ca"' in markup
    assert 'id="world-player-min-pa"' in markup
    assert 'id="world-player-max-pa"' in markup
    assert 'class="world-player-gender-setting" hidden' in markup
    assert 'id="world-player-gender" aria-label="选择世界球员范围"' in markup
    assert '<option value="men">男足球员</option>' in markup
    assert '<option value="women">女足球员</option>' in markup
    assert '<option value="all">全部球员</option>' in markup
    assert 'control.hidden = !(data?.ready && data.gender_supported === true)' in script
    assert ".world-player-gender-setting" in styles
    assert ".world-player-gender-setting" in dark_styles
    settings_block = script.split("function ensureSettingsTabs", 1)[1].split(
        "function bindEvents", 1,
    )[0]
    assert "world-player-gender-setting" not in settings_block
    assert '"world-players": () => renderWorldPlayers()' in script
    assert 'request(`/api/world-players?${query}`)' in script
    assert '["nationality_id", app.worldPlayerNationalityId]' in script
    assert '["min_age", app.worldPlayerMinAge]' in script
    assert '["max_age", app.worldPlayerMaxAge]' in script
    assert '["min_ca", app.worldPlayerMinCa]' in script
    assert '["max_ca", app.worldPlayerMaxCa]' in script
    assert '["min_pa", app.worldPlayerMinPa]' in script
    assert '["max_pa", app.worldPlayerMaxPa]' in script
    assert 'data?.filter_options?.nationalities || []' in script
    assert 'live:live ? "1" : "0"' in script
    assert "void hydrateWorldPlayerPage(query, requestKey, hydrationKey, requestedScope)" in script
    assert "worldPlayerSummaryCache: new Map()" in script
    assert '"正在更新当前页资料"' in script
    assert 'worldPlayerSort: "pa"' in script
    assert 'sort_by:app.worldPlayerSort || "pa"' in script
    assert 'data-world-player-sort="${field}"' in script
    assert 'worldPlayerSortHeader("ca", "CA", filters)' in script
    assert 'worldPlayerSortHeader("pa", "PA", filters)' in script
    assert 'worldPlayerSortHeader("asking_price", "挂牌价", filters)' in script
    assert 'sort_by=_query_value(query, "sort_by") or "pa"' in server
    assert 'loadWorldPlayers({page:1, force:true})' in script
    assert 'setTimeout(() => loadWorldPlayers({page:1}), 160)' in script
    assert '<strong>正在读取</strong>' in script
    assert 'role="columnheader">年龄' in script
    assert 'role="columnheader">位置' in script
    assert "首次加载会批量读取姓名" not in script
    assert "会话级姓名索引" not in script
    assert "姓名缓存" not in script
    assert "搜索只过滤本地缓存" not in script
    assert "重建索引" not in markup
    assert 'app.worldPlayersRebuilding ? "重建中" : "正在读取"' in script
    assert ".world-player-row" in styles
    assert '"/api/world-players": _get_world_players_route' in server
    assert '"/api/world-players/detail"' in server
    for endpoint in (
        '"/api/world-players/name"',
        '"/api/world-players/edit"',
        '"/api/world-players/names"',
        '"/api/world-players/contract"',
        '"/api/world-players/language"',
        '"/api/world-players/preferred-move"',
        '"/api/world-players/primary-nationality"',
        '"/api/world-players/unhappiness/clear"',
        '"/api/world-players/second-nationality"',
        '"/api/world-players/second-nationality/remove"',
        '"/api/world-players/injury/add"',
        '"/api/world-players/injury/remove"',
        '"/api/world-players/retirement"',
        '"/api/world-players/person-relation"',
    ):
        assert endpoint in server
    assert '"/api/world-players/edit": "edit_world_player"' in server
    assert 'data-world-player-detail' in script
    assert 'openWorldPlayerEditor' in script
    assert 'data-edit-world-player' not in script
    assert 'const playerNameEditButton = options.worldPlayer ? ""' in script
    assert 'if (options.worldPlayer) return;' in script
    assert 'saveWorldPlayerContract' in script
    assert 'saveWorldPlayerNames' in script
    assert 'data-world-player-name-field' in script
    assert 'data-world-player-contract-field' in script
    assert 'data-world-player-contract-status-bit' in script
    assert 'data-world-player-person-relation-add' not in script
    assert 'data-world-player-person-relation-remove' not in script
    assert 'saveWorldPlayerPersonRelation' not in script
    assert '<h3>人物关系</h3>' not in script
    assert 'data-world-player-injury-add' in script
    assert 'addWorldPlayerInjury' in script
    assert 'data-world-player-nationality-remove' in script
    assert 'removeWorldPlayerSecondNationality' in script
    assert 'data-world-player-primary-nationality-save' in script
    assert 'saveWorldPlayerPrimaryNationality' in script
    assert 'data-world-player-contract-bonus-amount' in script
    assert 'bonuses_and_clauses' in script
    assert 'capabilities.contract_happiness' in script
    assert 'playing_time_happiness' in script
    assert 'expected_raw: player.editor?.expected_raw?.contract || ""' in script
    assert 'data-world-player-unhappiness-clear' in script
    assert 'clearWorldPlayerUnhappiness' in script
    assert 'asking_price' in script
    assert 'nationality_eligibility' in script
    assert '请先备份存档' in script
    assert 'expected_raw' in script


def test_world_player_frontend_indexes_visible_rows_and_skips_equal_renders():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index_block = script.split("function worldPlayerDataIndex", 1)[1].split(
        "function worldPlayerRenderStateSignature", 1,
    )[0]
    render_block = script.split("function renderWorldPlayers", 1)[1].split(
        "function clubReputationTier", 1,
    )[0]

    assert "app.worldPlayerIndexSource === data" in index_block
    assert "playersById:new Map" in index_block
    assert "dataSignature:JSON.stringify" in index_block
    assert "version:app.worldPlayerIndexVersion" in index_block
    assert "worldPlayerRenderStateSignature(index)" in render_block
    assert "if (app.worldPlayerRenderSignature === signature) return" in render_block
    assert "const players = index.players" in render_block
    assert "const filters = index.filters" in render_block
    assert "moneyRenderSignature()" in script
    assert "index.version" in script
    assert "worldPlayerDataIndex().playersById.get" in script
    assert "invalidateWorldPlayerDataIndex()" in script


def test_world_player_page_exposes_busy_keyboard_and_table_semantics():
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'id="page-world-players" aria-labelledby="world-players-page-title" aria-busy="false"' in markup
    assert 'id="world-player-sync-status"' in markup
    assert 'id="world-player-content" aria-busy="false"' in markup
    assert '"nav.world"' in markup
    assert 'page?.setAttribute("aria-busy", String(busy))' in script
    assert 'root?.setAttribute("aria-busy", String(busy))' in script
    assert '"world.players.rebuilding"' in script
    assert '"world.players.loading"' in script
    assert '"world.players.list_aria"' in script
    assert '"world.players.list_aria"' in script
    assert 'event.key === "Enter" || event.key === " "' in script
    assert ".world-player-sync-status" in styles
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".world-player-sync-status > i" in reduced_motion
    assert ".world-player-row { transition:none; }" in reduced_motion


def test_world_player_visible_page_reads_fmrte_style_summary_fields():
    player = 0x1000
    person = player + 0x30
    nation = 0x3000
    contract = 0x4000
    team_address = 0x5000
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", game_date_rva=0x100,
        actual_player_vtable_rvas=(0x1700,),
        player_and_non_player_vtable_rvas=(0x1800,),
        player_person_offset=0x30,
        player_and_non_player_person_offset=0x40,
        player_ca_offset=0x200, player_pa_offset=0x202,
        player_ca_bytes=2, player_positions_offset=0x208,
        person_nationality_offset=0x68,
        person_date_of_birth_offset=0x88,
        person_date_of_birth_day_year=False,
    )

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base

        def prefetch(self, _address, _size):
            return None

        def ptr(self, address):
            return {
                player: module_base + 0x1700,
                person + 0x68: nation,
                person + 0xA8: contract,
                contract + 0x08: person,
                contract + 0x10: team_address,
            }.get(address)

        def u32(self, address):
            return {
                module_base + 0x100: (2026 << 16) | 1,
                person + 0x0C: 7,
                person + 0x88: (2000 << 16) | 1,
                nation + 0x0C: 1651,
                player + 0x234: 12_500_000,
            }.get(address)

        def u16(self, address):
            return {player + 0x200: 121, player + 0x202: 154}.get(address)

        def u8(self, _address):
            return None

        def bytes(self, address, size):
            if address == player + 0x208 and size == 15:
                return bytes((1, 1, 1, 16, 1, 18, 1, 16, 1, 1, 1, 1, 1, 1, 1))
            return None

        def team(self, address):
            if address == team_address:
                return {"id": 42, "name": "测试足球俱乐部", "short_name": "测试俱乐部"}
            return None

    result = _world_player_table_rows(Reader(), [{
        "id": 7, "name": "测试球员", "address": hex(player),
        "object_type": "actual_player",
    }])

    assert result == [{
        "id": 7, "name": "测试球员", "address": hex(player),
        "object_type": "actual_player", "age": 26, "ca": 121, "pa": 154,
        "nationality_id": 1651, "nationality": "巴西", "nationality_code": "BRA",
        "team_id": 42, "team_name": "测试俱乐部", "asking_price": 12_500_000,
        "positions": ["DC", "DM", "MC"],
        "position_ratings": {"DC": 16, "DM": 18, "MC": 16},
        "summary_live": True,
    }]


def test_world_player_page_resets_on_account_scope_change():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    reset = script.split("function resetAccountScopedWorldState()", 1)[1].split(
        "function ", 1,
    )[0]

    assert "app.worldPlayersRequestKey += 1" in reset
    assert "app.worldPlayers = null" in reset
    assert "app.worldPlayersScope = null" in reset
    assert "app.worldPlayerIndexSource = null" in reset
    assert "app.worldPlayerIndex = null" in reset
    assert "app.worldPlayerIndexVersion = 0" in reset
    assert "app.worldPlayerRenderSignature = null" in reset
    assert "app.worldPlayerHydrationRequestKey += 1" in reset
    assert "app.worldPlayerSummaryCache.clear()" in reset


def test_world_player_detail_uses_uid_relocation_and_live_identity_checks():
    source = (ROOT / "tools" / "club_reader.py").read_text(encoding="utf-8")
    index = (ROOT / "tools" / "database_index.py").read_text(encoding="utf-8")
    assert "def _world_player_index_target" in source
    assert "directory.player_for_uid(player_id)" in source
    assert "_validated_player_person(reader, address, player_id)" in source
    assert "球员 Person 对象与当前数据库目录不一致" in source
    assert "def player_for_uid" in index


def test_world_player_editor_stages_scalar_and_nested_changes_transactionally():
    source = (ROOT / "tools" / "club_reader.py").read_text(encoding="utf-8")
    assert "def update_world_player_fields" in source
    assert "球员旧值签名缺失，请重新打开球员详情" in source
    assert "_apply_verified_memory_changes(" in source
    assert "球员编辑失败且原值回滚校验异常" in source


def test_world_player_editor_rechecks_old_values_and_reads_back_nested_fields():
    player = 0x1000
    person = 0x1040
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", actual_player_vtable_rvas=(0x1700,),
        player_and_non_player_vtable_rvas=(0x1800,),
        player_person_offset=0x40, player_and_non_player_person_offset=0x50,
        player_ca_offset=0x200, player_ca_bytes=2, player_pa_offset=0x202,
        player_positions_offset=0x150, player_attributes_offset=0x15F,
        person_hidden_attributes_offset=0x70, attribute_display_bias=0,
        player_height_offset=None, player_weight_offset=None,
        player_fitness_offset=None, player_sharpness_offset=None,
        player_fatigue_offset=None, player_morale_offset=None,
        player_home_reputation_offset=None, player_current_reputation_offset=None,
        player_world_reputation_offset=0x262,
        person_date_of_birth_offset=None,
    )

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                player + 0x150: bytearray([1, 1, 1, 16, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]),
                player + 0x15F: bytearray([50] * 54),
                player + 0x200: bytearray((120).to_bytes(2, "little")),
                player + 0x202: bytearray((150).to_bytes(2, "little")),
                player + 0x262: bytearray((4000).to_bytes(2, "little")),
                person + 0x70: bytearray([10] * 8),
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def u8(self, address):
            raw = self._read(address, 1)
            return raw[0] if raw else None

        def u16(self, address):
            raw = self._read(address, 2)
            return int.from_bytes(raw, "little") if raw else None

        def u32(self, address):
            if address == person + 0x0C:
                return 7
            raw = self._read(address, 4)
            return int.from_bytes(raw, "little") if raw else None

        def ptr(self, address):
            return {player: module_base + 0x1700}.get(address)

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    original_raw = bytes(reader.memory[player + 0x15F])
    original_positions = bytes(reader.memory[player + 0x150])
    original_hidden = bytes(reader.memory[person + 0x70])
    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_fields(
            7,
            {
                "ca": 125,
                "positions": {"DC": 18},
                "attributes": {"技术:传球": 15},
                "hidden_attributes": {"雄心": 20, "左脚": 18},
            },
            {
                "ca": 120,
                "positions": {"DC": 16},
                "attributes": {"技术:传球": 10},
                "hidden_attributes": {"雄心": 10, "左脚": 10},
                "expected_raw": {
                    "positions": original_positions.hex(),
                    "attributes": original_raw.hex(),
                    "person_hidden_attributes": original_hidden.hex(),
                },
            },
        )
    assert result["write_count"] == 4
    assert int.from_bytes(reader.memory[player + 0x200], "little") == 125
    assert reader.memory[player + 0x150][3] == 18
    assert reader.memory[player + 0x15F][0x16 - 0x0F] == 75
    assert reader.memory[player + 0x15F][0x27 - 0x0F] == 90
    assert reader.memory[person + 0x70][1] == 20

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)):
        with pytest.raises(ValueError, match="CA 已发生变化"):
            club_reader.update_world_player_fields(
                7, {"ca": 130}, {"ca": 120, "expected_raw": {}}
            )


def test_world_player_contract_editor_writes_scalars_and_rejects_stale_raw():
    player = 0x1000
    person = 0x1040
    contract = 0x2000
    team = 0x3000
    module_base = 0x100000
    layout = SimpleNamespace(key="fm26")

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            raw = bytearray(0xC8)
            raw[0x20:0x24] = (1000).to_bytes(4, "little")
            raw[0x38:0x3C] = (200).to_bytes(4, "little")
            raw[0x44:0x48] = ((2026 << 16) | 1).to_bytes(4, "little")
            raw[0x48:0x4C] = ((2028 << 16) | 183).to_bytes(4, "little")
            raw[0x4C:0x50] = ((2026 << 16) | 1).to_bytes(4, "little")
            raw[0x54] = 3
            raw[0x57] = 1
            raw[0x5D] = 5
            raw[0xC3] = 1
            self.memory = {contract: raw}

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def ptr(self, address):
            return {
                person + 0xA8: contract,
                contract + 0x08: person,
                contract + 0x10: team,
            }.get(address)

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    original = bytes(reader.memory[contract])
    expected = {
        "expected_raw": original.hex(),
            "contract": {
                "wage_per_week": 1000,
                "expiry_date": "2028-07-01",
                "agreed_playing_time": 3,
                "squad_number": 5,
                "transfer_status_raw": 1,
                "contract_type": 1,
        },
    }
    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_contract(
            7,
            {
                "wage_per_week": 2500,
                "expiry_date": "2029-07-01",
                "agreed_playing_time": 5,
                "squad_number": 9,
                "transfer_status_raw": 9,
            },
            expected,
        )
    assert result["write_count"] == 5
    assert int.from_bytes(reader.memory[contract][0x20:0x24], "little") == 2500
    assert int.from_bytes(reader.memory[contract][0x48:0x4C], "little") == ((2029 << 16) | 182)
    assert reader.memory[contract][0x54] == 5
    assert reader.memory[contract][0x5D] == 9
    assert reader.memory[contract][0x57] == 9

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)):
        with pytest.raises(ValueError):
            club_reader.update_world_player_contract(
                7, {"wage_per_week": 3000}, {"expected_raw": "00", "contract": {"wage_per_week": 2500}},
            )


def test_world_player_scalar_editor_writes_asking_price_and_nationality_eligibility():
    player = 0x1000
    person = 0x1040
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", player_positions_offset=0x150, player_attributes_offset=0x15F,
        person_hidden_attributes_offset=0x70, player_ca_offset=0x200,
        player_ca_bytes=2, player_pa_offset=0x202, player_height_offset=None,
        player_weight_offset=None, player_fitness_offset=None,
        player_sharpness_offset=None, player_fatigue_offset=None,
        player_morale_offset=None, player_home_reputation_offset=None,
        player_current_reputation_offset=None, player_world_reputation_offset=None,
        person_date_of_birth_offset=None,
    )

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                player + 0x150: bytearray([1] * 15),
                player + 0x15F: bytearray([50] * 54),
                person + 0x70: bytearray([10] * 8),
                player + 0x234: bytearray((1234).to_bytes(4, "little")),
                player + 0x26E: bytearray([80]),
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    expected = {
        "asking_price": 1234,
        "nationality_eligibility": 80,
        "expected_raw": {
            "positions": bytes(reader.memory[player + 0x150]).hex(),
            "attributes": bytes(reader.memory[player + 0x15F]).hex(),
            "person_hidden_attributes": bytes(reader.memory[person + 0x70]).hex(),
        },
    }
    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_fields(
            7,
            {"asking_price": 2500, "nationality_eligibility": 83},
            expected,
        )
    assert result["write_count"] == 2
    assert int.from_bytes(reader.memory[player + 0x234], "little") == 2500
    assert reader.memory[player + 0x26E][0] == 83

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)):
        with pytest.raises(ValueError):
            club_reader.update_world_player_fields(
                7, {"asking_price": 3000}, {"asking_price": 1234, "expected_raw": {}},
            )


def test_world_player_editor_coalesces_nearby_prewrite_fields():
    base = 0x1000
    raw = bytearray(0x300)
    raw[0x40:0x44] = b"ABCD"
    raw[0x80:0x82] = b"XY"

    class Reader:
        def __init__(self):
            self.calls = []

        def bytes(self, address, size):
            self.calls.append((int(address), int(size)))
            start = int(address) - base
            return bytes(raw[start:start + int(size)])

    reader = Reader()
    result = club_reader._read_relative_memory_fields(
        reader, base, {"first": (0x40, 4), "second": (0x80, 2)},
    )

    assert result == {"first": b"ABCD", "second": b"XY"}
    assert reader.calls == [(base + 0x40, 0x42)]


def test_world_player_editor_falls_back_when_wide_snapshot_is_unreadable():
    base = 0x1000
    values = {(base + 0x40, 4): b"ABCD", (base + 0x80, 2): b"XY"}

    class Reader:
        def __init__(self):
            self.calls = []

        def bytes(self, address, size):
            key = (int(address), int(size))
            self.calls.append(key)
            return values.get(key)

    reader = Reader()
    result = club_reader._read_relative_memory_fields(
        reader, base, {"first": (0x40, 4), "second": (0x80, 2)},
    )

    assert result == {"first": b"ABCD", "second": b"XY"}
    assert reader.calls == [
        (base + 0x40, 0x42),
        (base + 0x40, 4),
        (base + 0x80, 2),
    ]


def test_world_player_training_projection_cache_uses_complete_raw_key_and_copies():
    layout = SimpleNamespace(key="fm26", attribute_display_bias=0)
    raw = bytes([50] * 54)
    positions = bytes([1, 1, 1, 1, 1, 1, 1, 20, 1, 1, 1, 1, 1, 1, 1])
    attributes = {"技术": {"传球": 10}}
    with club_reader._PLAYER_TRAINING_CA_CACHE_LOCK:
        club_reader._PLAYER_TRAINING_CA_CACHE.clear()

    with patch.object(
        club_reader, "fm26_recommended_ca",
        wraps=club_reader.fm26_recommended_ca,
    ) as recommended:
        first = club_reader._player_training_ca_snapshot(
            layout, raw, positions, attributes,
        )
        first["costs"].clear()
        second = club_reader._player_training_ca_snapshot(
            layout, raw, positions, attributes,
        )
        changed = club_reader._player_training_ca_snapshot(
            layout, bytes([55]) + raw[1:], positions, attributes,
        )

    assert recommended.call_count == 2
    assert second["costs"]
    assert changed["recommended_ca"] != second["recommended_ca"]


def test_world_player_name_editor_updates_existing_utf8_payloads_only():
    player = 0x1000
    person = 0x1040
    full_block = 0x3000
    first_entry = 0x3100
    first_block = 0x3200
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", person_full_name_offset=0x40,
        person_first_name_offset=0x50, person_last_name_offset=0x58,
        person_common_name_offset=0x60,
    )

    def string_block(base, value):
        raw = value.encode("utf-8")
        return base + 12, bytearray(
            (len(raw) + 9).to_bytes(8, "little")
            + (1).to_bytes(4, "little")
            + len(raw).to_bytes(4, "little")
            + raw + b"\0"
        )

    full_pointer, full_raw = string_block(full_block, "张三")
    first_pointer, first_raw = string_block(first_block, "Amy")

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.string_cache = {}
            self.memory = {
                person + 0x40: bytearray(full_pointer.to_bytes(8, "little")),
                person + 0x50: bytearray(first_entry.to_bytes(8, "little")),
                first_entry: bytearray(first_pointer.to_bytes(8, "little")),
                full_block: full_raw,
                first_block: first_raw,
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def ptr(self, address):
            raw = self._read(address, 8)
            return int.from_bytes(raw, "little") if raw else None

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    expected = {"names": {"full_name": "张三", "first_name": "Amy"}}
    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_names(
            7, {"full_name": "李四", "first_name": "Bob"}, expected,
        )
    assert result["write_count"] == 2
    assert reader.bytes(full_pointer + 4, 6) == "李四".encode("utf-8")
    assert reader.bytes(first_pointer + 4, 3) == b"Bob"

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)):
        with pytest.raises(ValueError):
            club_reader.update_world_player_names(
                7, {"full_name": "王五"}, {"names": {"full_name": "旧名"}},
            )


def test_world_player_primary_nationality_editor_replaces_validated_pointer():
    player = 0x1000
    person = 0x1040
    current_nation = 0x3000
    target_nation = 0x4000
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", distribution="steam", person_nationality_offset=0x68,
        person_relationships_offset=0x78, nation_men_container_offset=0x108,
        nation_vtable_rva=0x1200,
    )

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                person + 0x68: bytearray(current_nation.to_bytes(8, "little")),
                current_nation + club_reader.ENTITY_UID: bytearray((769).to_bytes(4, "little")),
                target_nation + club_reader.ENTITY_UID: bytearray((1651).to_bytes(4, "little")),
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def ptr(self, address):
            raw = self._read(address, 8)
            return int.from_bytes(raw, "little") if raw else None

        def u32(self, address):
            raw = self._read(address, 4)
            return int.from_bytes(raw, "little") if raw else None

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)), \
         patch.object(club_reader, "_validated_nationality_target", return_value=current_nation), \
         patch.object(club_reader, "_nation_address_for_uid", return_value=target_nation) as resolve_nation, \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_primary_nationality(
            7, 1651, 769, nation_address=target_nation,
        )

    assert result == {"player_id": 7, "before_nation_id": 769, "nation_id": 1651, "changed": True}
    resolve_nation.assert_called_once_with(reader, module_base, 1651, target_nation)
    assert reader.ptr(person + 0x68) == target_nation


def test_world_player_contract_editor_updates_existing_bonus_record():
    person = 0x1040
    contract = 0x2000
    bonus_begin = 0x3000
    module_base = 0x100000
    layout = SimpleNamespace(key="fm26")
    raw = bytearray(0xC8)
    raw[0x08:0x10] = person.to_bytes(8, "little")
    raw[0x5A] = 98
    raw[0x5C] = 60
    raw[0x68:0x70] = bonus_begin.to_bytes(8, "little")
    raw[0x70:0x78] = (bonus_begin + 8).to_bytes(8, "little")
    raw[0x78:0x80] = (bonus_begin + 16).to_bytes(8, "little")

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                person + 0xA8: bytearray(contract.to_bytes(8, "little")),
                contract: raw,
                bonus_begin: bytearray(struct.pack("<ihh", 1000, -1, 32)),
            }

        def _read(self, address, size):
            for base, data in self.memory.items():
                if base <= address and address + size <= base + len(data):
                    start = address - base
                    return bytes(data[start:start + size])
            return None

        def _write(self, address, value):
            for base, data in self.memory.items():
                if base <= address and address + len(value) <= base + len(data):
                    start = address - base
                    data[start:start + len(value)] = value
                    return
            self.memory[address] = bytearray(value)

        def bytes(self, address, size):
            return self._read(address, size)

        def ptr(self, address):
            raw_value = self._read(address, 8)
            return int.from_bytes(raw_value, "little") if raw_value else None

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, value):
        reader._write(int(address), bytes(value))

    expected = {
        "expected_raw": {"contract": bytes(raw).hex(), "contract_bonuses": bytes(reader.memory[bonus_begin]).hex()},
        "contract": {"bonuses_and_clauses": [{"index": 0, "type": 32, "amount": 1000, "number": None}]},
    }
    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, 0x1000, person)), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_contract(
            7,
            {
                "contract_type": 1,
                "happiness": 90,
                "playing_time_happiness": 50,
                "bonuses_and_clauses": [{"index": 0, "amount": 1500}],
            },
            {
                **expected,
                "contract": {
                    **expected["contract"],
                    "contract_type": 0,
                    "happiness": 98,
                    "playing_time_happiness": 60,
                },
            },
        )

    assert result["write_count"] == 4
    assert reader.bytes(contract + 0xC3, 1) == b"\x01"
    assert reader.bytes(contract + 0x5A, 1) == b"Z"
    assert reader.bytes(contract + 0x5C, 1) == b"2"
    assert struct.unpack("<ihh", reader.bytes(bonus_begin, 8)) == (1500, -1, 32)


def test_world_player_joined_club_date_uses_generation_offset_and_writes_with_guard():
    player = 0x1000
    person = 0x3000
    module_base = 0x100000
    joined_address = person + 0x14C
    original_raw = ((2024 << 16) | 120).to_bytes(4, "little")
    layout = SimpleNamespace(
        key="fm24", player_positions_offset=0x150,
        player_attributes_offset=0x15F, person_hidden_attributes_offset=0x70,
        player_ca_offset=0x200, player_ca_bytes=2, player_pa_offset=0x202,
        player_height_offset=None, player_weight_offset=None,
        player_fitness_offset=None, player_sharpness_offset=None,
        player_fatigue_offset=None, player_morale_offset=None,
        player_home_reputation_offset=None, player_current_reputation_offset=None,
        player_world_reputation_offset=None, person_date_of_birth_offset=None,
        person_date_of_birth_day_year=False, attribute_display_bias=0,
    )

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                player + 0x150: bytearray([1] * 15),
                player + 0x15F: bytearray([50] * 54),
                person + 0x70: bytearray([10] * 8),
                joined_address: bytearray(original_raw),
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def invalidate_prefetch(self):
            return None

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    expected = {
        "joined_club_date": "2024-04-29",
        "expected_raw": {"person_joined_club_date": original_raw.hex()},
    }
    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", return_value=({}, player, person)), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_fields(
            7, {"joined_club_date": "2025-01-15"}, expected,
        )

    assert result["write_count"] == 1
    assert int.from_bytes(reader.memory[joined_address], "little") == ((2025 << 16) | 15)


def test_world_player_person_relation_appends_validated_parent_record():
    source_player, target_player = 0x1000, 0x2000
    source_person, target_person = 0x3000, 0x4000
    container, begin, end, capacity = 0x5000, 0x6000, 0x6010, 0x6020
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", distribution="steam", person_relationships_offset=0x78,
    )

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                source_person + 0x78: bytearray(container.to_bytes(8, "little")),
                target_person + 0x78: bytearray((0).to_bytes(8, "little")),
                container: bytearray(struct.pack("<QQQ", begin, end, capacity)),
                begin: bytearray(struct.pack("<QHBBBBBB", 0x7000, 0, 3, 1, 100, 79, 0, 0xFF)),
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def ptr(self, address):
            raw = self._read(address, 8)
            return int.from_bytes(raw, "little") if raw else 0

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    def target(_reader, player_id):
        return ({}, source_player, source_person) if int(player_id) == 7 else ({}, target_player, target_person)

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", side_effect=target), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_person_relation(7, 8, "parent")

    assert result["changed"] is True
    assert reader.bytes(container, 24) == struct.pack("<QQQ", begin, end + 16, capacity)
    record = reader.bytes(end, 16)
    assert struct.unpack_from("<QH", record)[0:2] == (target_person, 1)
    assert record[10:14] == bytes((3, 1, 100, 79))


def test_world_player_person_relation_removes_matching_child_record():
    source_player, target_player = 0x1000, 0x2000
    source_person, target_person = 0x3000, 0x4000
    container, begin, end, capacity = 0x5000, 0x6000, 0x6010, 0x6020
    module_base = 0x100000
    layout = SimpleNamespace(
        key="fm26", distribution="steam", person_relationships_offset=0x78,
    )
    relation = struct.pack("<QHBBBBBB", target_person, 3, 3, 1, 100, 79, 0, 0xFF)

    class Reader:
        def __init__(self):
            self.layout = layout
            self.module_base = module_base
            self.memory = {
                source_person + 0x78: bytearray(container.to_bytes(8, "little")),
                container: bytearray(struct.pack("<QQQ", begin, end, capacity)),
                begin: bytearray(relation),
            }

        def _read(self, address, size):
            for base, raw in self.memory.items():
                if base <= address and address + size <= base + len(raw):
                    start = address - base
                    return bytes(raw[start:start + size])
            return None

        def _write(self, address, raw):
            for base, data in self.memory.items():
                if base <= address and address + len(raw) <= base + len(data):
                    start = address - base
                    data[start:start + len(raw)] = raw
                    return
            self.memory[address] = bytearray(raw)

        def bytes(self, address, size):
            return self._read(address, size)

        def ptr(self, address):
            raw = self._read(address, 8)
            return int.from_bytes(raw, "little") if raw else 0

    reader = Reader()

    @contextmanager
    def writable_reader():
        yield reader, object(), SimpleNamespace(base_address=module_base)

    def write(_process, address, raw):
        reader._write(int(address), bytes(raw))

    def target(_reader, player_id):
        return ({}, source_player, source_person) if int(player_id) == 7 else ({}, target_player, target_person)

    with patch.object(club_reader, "_writable_game_reader", writable_reader), \
         patch.object(club_reader, "_world_player_index_target", side_effect=target), \
         patch.object(club_reader, "write_process_memory", side_effect=write):
        result = club_reader.update_world_player_person_relation(7, 8, "child", remove=True)

    assert result["removed"] is True
    assert reader.bytes(container, 24) == struct.pack("<QQQ", begin, begin, capacity)
