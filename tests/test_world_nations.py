from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import tools.world_clubs as world_clubs
from fm_odds_web import LocalOddsState
from tools.game_layout import (
    FM24_240_LAYOUT, FM24_241_LAYOUT, FM24_EPIC_LAYOUT, FM24_LAYOUT,
    FM24_XGP_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE,
)


class FakeNationReader:
    def __init__(self) -> None:
        self.module_base = 0x100000
        self.layout = SimpleNamespace(
            key="fm26",
            nation_men_container_offset=0x108,
            nation_youth_rating_offset=0x864,
            nation_vtable_rva=0x180,
            national_team_vtable_rva=0x200,
            staff_complete_object_offset=0x100,
            staff_person_vtable_rva=0x300,
        )
        self.process = object()
        self.youth_rating = 135

    def ptr(self, address: int) -> int:
        return {
            0x1000 + 0x108: 0x2000,
            0x2000: self.module_base + 0x180,
            0x2000 + 0x18: 0x3000,
            0x3000: 0x4000,
            0x3008: 0x4100,
            0x6100: self.module_base + 0x300,
        }.get(address, 0)

    def u32(self, address: int) -> int:
        return {
            0x1000 + 0x0C: 765,
            0x6100 + 0x0C: 35015767,
        }.get(address, 0)

    def u8(self, address: int) -> int | None:
        if address == 0x2000 + 0x864:
            return self.youth_rating
        return None

    def team(self, address: int) -> dict | None:
        if address != 0x4080:
            return None
        return {
            "id": 765,
            "name": "英格兰",
            "address": hex(address),
            "manager_address": "0x6000",
            "team_type": "national",
            "reputation": 9350,
        }


class FakeLayout:
    key = "fm26"
    module_name = "game_plugin.dll"
    staff_complete_object_offset = 0x100
    staff_person_vtable_rva = 0x300
    nation_men_container_offset = 0x108
    nation_youth_rating_offset = 0x864
    nation_vtable_rva = 0x180

    def module(self, _process):
        return SimpleNamespace(base_address=0x100000)


def _national_team_raw() -> bytes:
    raw = bytearray(0x100)
    raw[0x80:0x88] = (0x100000 + 0x200).to_bytes(8, "little")
    return bytes(raw)


def test_world_nation_pagination_and_price() -> None:
    rows = [
        {"id": 1, "name": "日本", "continent": "亚洲", "youth_rating": 112},
        {"id": 2, "name": "英格兰", "continent": "欧洲", "youth_rating": 135},
    ]
    result = world_clubs.paginate_world_nations(rows, continent="欧洲", page_size=99)
    assert [row["name"] for row in result["nations"]] == ["英格兰"]
    assert result["pagination"]["page_size"] == 15
    assert result["facets"] == [
        {"name": "亚洲", "count": 1},
        {"name": "欧洲", "count": 1},
    ]
    assert world_clubs.nation_youth_investment_price(99) == 10_000_000
    assert world_clubs.nation_youth_investment_price(135) == 120_226_443
    assert world_clubs.nation_youth_investment_price(199) == 10_000_000_000
    assert world_clubs.nation_youth_investment_price(200) is None
    assert world_clubs.nation_youth_investment_quote(126, 5) == {
        "requested_increase": 5, "increase": 5,
        "before": 126, "after": 131, "price": 372_426_171,
    }
    assert world_clubs.nation_youth_investment_quote(126, -1) == {
        "requested_increase": -1, "increase": -1,
        "before": 126, "after": 125, "price": 32_282_712,
    }
    assert world_clubs.nation_youth_investment_quote(126, -5) == {
        "requested_increase": -5, "increase": -5,
        "before": 126, "after": 121, "price": 186_213_086,
    }
    assert world_clubs.nation_youth_investment_quote(196, 5) == {
        "requested_increase": 5, "increase": 4,
        "before": 196, "after": 200, "price": 36_170_484_070,
    }


def test_public_world_nations_reads_only_the_current_page_details() -> None:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "save-1"
    nations = [
        {
            "id": index, "name": f"国家{index:02d}", "continent": "亚洲",
            "address": hex(0x1000 + index), "detail_scanned": False,
        }
        for index in range(1, 17)
    ]
    native = {"addresses_current": True, "nations": nations}
    state._world_club_native_cache = lambda _save_id: native
    state._world_club_scan_public = lambda _save_id: {"ready": True, "addresses_current": True}

    def summaries(rows: list[dict]) -> dict[int, dict]:
        return {
            int(row["id"]): {
                "reputation": 9000 + int(row["id"]),
                "manager": f"教练{row['id']}",
                "detail_scanned": True,
                "detail_error": None,
            }
            for row in rows
        }

    with patch("fm_odds_web.read_native_world_nation_summaries", side_effect=summaries) as read:
        result = state.public_world_nations(page=1, page_size=15)

    assert len(read.call_args.args[0]) == 15
    assert result["nations"][0]["reputation"] == 9001
    assert result["nations"][0]["manager"] == "教练1"
    assert nations[0]["detail_scanned"] is True
    assert nations[15]["detail_scanned"] is False


def test_world_nation_players_filters_primary_nationality_and_paginates() -> None:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._bind_current_save = lambda: "save-1"
    state._data_scope_id = lambda _output: "save-1"
    state._world_club_native_cache = lambda _save_id: {
        "nations": [{"id": 765, "name": "英格兰", "continent": "欧洲"}],
    }
    result_page = {
        "players": [{"id": 7, "name": "Test Player", "pa": 190}],
        "pagination": {
            "page": 2, "page_size": 30, "page_count": 3,
            "total": 65, "from": 31, "to": 60,
        },
        "filters": {}, "index": {"source": "session_person_cache"},
    }

    with patch("fm_odds_web.search_world_players", return_value=result_page) as search:
        result = state.world_nation_players(
            765, ranking="world_reputation", page=2, page_size=99,
        )

    assert result["ranking"] == "world_reputation"
    assert result["ranking_label"] == "世界声望"
    assert result["pagination"]["page"] == 2
    search.assert_called_once_with(
        "", page=2, page_size=30, sort_by="world_reputation",
        sort_order="desc", gender="men", nationality_id=765,
    )


def test_world_nation_player_price_ranking_discloses_verified_field() -> None:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._bind_current_save = lambda: "save-1"
    state._data_scope_id = lambda _output: "save-1"
    state._world_club_native_cache = lambda _save_id: {
        "nations": [{"id": 765, "name": "英格兰", "continent": "欧洲"}],
    }
    empty_page = {
        "players": [],
        "pagination": {
            "page": 1, "page_size": 30, "page_count": 1,
            "total": 0, "from": 0, "to": 0,
        },
        "filters": {}, "index": {"source": "session_person_cache"},
    }

    with patch("fm_odds_web.search_world_players", return_value=empty_page):
        result = state.world_nation_players(765, ranking="asking_price")

    assert result["ranking_label"] == "挂牌价"
    assert "独立当前身价字段尚未验证" in result["price_note"]


def test_world_scan_rebuilds_legacy_club_cache_without_nations() -> None:
    state = SimpleNamespace(
        lock=threading.RLock(),
        memory_lock=threading.Lock(),
        output={"save_instance_id": "different-save"},
        world_club_scan_progress=0,
        world_club_scan_text="",
        world_club_scan_error=None,
        world_club_scanning=True,
        world_club_cache=None,
        world_club_cache_save_id=None,
        _data_scope_id=lambda _output: "different-save",
    )
    cached = {
        "addresses_current": True,
        "clubs": [{"id": 1, "address": "0x1000"}],
    }
    rebuilt = {
        "clubs": [{"id": 1, "address": "0x1000"}],
        "nations": [{"id": 765, "address": "0x2000"}],
    }

    with (
        patch("fm_odds_web.refresh_native_world_clubs") as refresh,
        patch(
            "fm_odds_web.scan_native_world_clubs",
            side_effect=lambda *_args: rebuilt if state.memory_lock.locked() else pytest.fail(
                "world scan must own the memory lock"
            ),
        ) as scan,
        patch("fm_odds_web.save_native_world_clubs") as save,
        patch("fm_odds_web.set_active_save_id"),
    ):
        LocalOddsState._world_club_scan_worker(
            state, "scope-1", "save-1", [], cached,
        )

    refresh.assert_not_called()
    scan.assert_called_once()
    save.assert_called_once_with(rebuilt, "save-1")
    assert state.world_club_cache is rebuilt
    assert state.world_club_scan_error is None
    assert state.world_club_scanning is False


def test_same_generation_layouts_share_default_nation_offsets() -> None:
    for layout in (
        FM24_LAYOUT, FM24_EPIC_LAYOUT, FM24_240_LAYOUT,
        FM24_241_LAYOUT, FM24_XGP_LAYOUT,
    ):
        assert (layout.nation_men_container_offset, layout.nation_youth_rating_offset) == (0x108, 0x78C)
    for layout in (FM26_LAYOUT, FM26_XGP_TEMPLATE):
        assert (layout.nation_men_container_offset, layout.nation_youth_rating_offset) == (0x108, 0x864)


def test_nation_row_rejects_wrong_mens_container_type() -> None:
    reader = FakeNationReader()
    original_ptr = reader.ptr
    reader.ptr = lambda address: 0xDEADBEEF if address == 0x2000 else original_ptr(address)
    assert world_clubs._nation_row(reader, 0x1000, "欧洲", "英格兰") is None


def test_reads_nation_youth_rating_reputation_and_manager() -> None:
    reader = FakeNationReader()
    nation = {"id": 765, "name": "英格兰", "continent": "欧洲", "address": "0x1000"}
    with (
        patch("tools.world_clubs.select_process_layout", return_value=(1, "fm.exe", FakeLayout())),
        patch(
            "tools.world_clubs.borrow_game_reader",
            return_value=nullcontext(reader),
        ),
        patch("tools.world_clubs.read_process_memory", return_value=_national_team_raw()),
        patch("tools.club_reader._name", return_value="Thomas Tuchel"),
    ):
        detail = world_clubs.read_native_world_nation_detail(nation)
    assert detail["nation"]["youth_rating"] == 135
    assert detail["nation"]["reputation"] == 9350
    assert detail["manager"] == {"id": 35015767, "name": "Thomas Tuchel"}
    assert detail["investment"]["price"] == 120_226_443
    assert [row["increase"] for row in detail["investment"]["options"]] == [-5, -1, 1, 5]


def test_nation_investment_writes_one_point_with_readback() -> None:
    reader = FakeNationReader()
    nation = {"id": 765, "name": "英格兰", "address": "0x1000"}

    def write(_process, address: int, value: bytes) -> None:
        assert address == 0x2000 + 0x864
        reader.youth_rating = value[0]

    with (
        patch("tools.world_clubs.select_process_layout", return_value=(1, "fm.exe", FakeLayout())),
        patch("tools.world_clubs.open_process", return_value=nullcontext(object())) as open_game,
        patch("tools.world_clubs.Reader", return_value=reader),
        patch("tools.world_clubs.write_process_memory", side_effect=write),
    ):
        result = world_clubs.invest_native_world_nation_youth(
            nation, expected_youth_rating=135,
        )
        assert result["before"] == 135
        assert result["after"] == 136
        with pytest.raises(RuntimeError, match="已变化"):
            world_clubs.invest_native_world_nation_youth(
                nation, expected_youth_rating=135,
            )
    open_game.assert_called_with(1, write_memory=True)


def test_nation_investment_write_failure_keeps_original_value() -> None:
    reader = FakeNationReader()
    nation = {"id": 765, "name": "英格兰", "address": "0x1000"}
    with (
        patch("tools.world_clubs.select_process_layout", return_value=(1, "fm.exe", FakeLayout())),
        patch("tools.world_clubs.open_process", return_value=nullcontext(object())) as open_game,
        patch("tools.world_clubs.Reader", return_value=reader),
        patch(
            "tools.world_clubs.write_process_memory",
            side_effect=OSError("Windows error 5"),
        ) as write,
        pytest.raises(RuntimeError, match="原值未变化"),
    ):
        world_clubs.invest_native_world_nation_youth(
            nation, expected_youth_rating=135,
        )
    open_game.assert_called_once_with(1, write_memory=True)
    write.assert_called_once()
    assert reader.youth_rating == 135


def test_nation_investment_writes_multiple_points_with_readback() -> None:
    reader = FakeNationReader()
    reader.youth_rating = 196
    nation = {"id": 765, "name": "England", "address": "0x1000"}

    def write(_process, _address: int, value: bytes) -> None:
        reader.youth_rating = value[0]

    with (
        patch("tools.world_clubs.select_process_layout", return_value=(1, "fm.exe", FakeLayout())),
        patch("tools.world_clubs.open_process", return_value=nullcontext(object())),
        patch("tools.world_clubs.Reader", return_value=reader),
        patch("tools.world_clubs.write_process_memory", side_effect=write),
    ):
        result = world_clubs.invest_native_world_nation_youth(
            nation, expected_youth_rating=196, increase=4,
        )

    assert result["increase"] == 4
    assert result["after"] == 200


def test_nation_investment_can_lower_rating_with_readback() -> None:
    reader = FakeNationReader()
    reader.youth_rating = 126
    nation = {"id": 765, "name": "England", "address": "0x1000"}

    def write(_process, _address: int, value: bytes) -> None:
        reader.youth_rating = value[0]

    with (
        patch("tools.world_clubs.select_process_layout", return_value=(1, "fm.exe", FakeLayout())),
        patch("tools.world_clubs.open_process", return_value=nullcontext(object())),
        patch("tools.world_clubs.Reader", return_value=reader),
        patch("tools.world_clubs.write_process_memory", side_effect=write),
    ):
        result = world_clubs.invest_native_world_nation_youth(
            nation, expected_youth_rating=126, increase=-5,
        )

    assert result["increase"] == -5
    assert result["after"] == 121


def _world_nation_state() -> tuple[LocalOddsState, dict, dict]:
    state = object.__new__(LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.RLock()
    state.output = {"save_instance_id": "save-1"}
    state._bind_current_save = lambda: None
    state._data_scope_id = lambda _output: "save-1"
    nation = {"id": 765, "name": "英格兰", "address": "0x1000", "youth_rating": 135}
    native = {"addresses_current": True, "nations": [nation]}
    state._world_club_native_cache = lambda _save_id: native
    return state, native, nation


def test_world_nation_investment_charges_and_updates_cache() -> None:
    state, native, nation = _world_nation_state()
    detail = {
        "nation": {**nation, "youth_rating": 135},
        "national_team": {"name": "英格兰"},
    }
    payment = {"amount": 120_226_443, "wallet": 120_226_443, "bank": 0}
    with (
        patch("fm_odds_web.read_native_world_nation_detail", return_value=detail),
        patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
        patch(
            "fm_odds_web.invest_native_world_nation_youth",
            return_value={"before": 135, "after": 136},
        ),
        patch(
            "fm_odds_web.public_economy",
            return_value={"general_balance": 880_000_000, "casino_balance": 20_000_000},
        ),
        patch("fm_odds_web.save_native_world_clubs") as save_cache,
    ):
        result = state.invest_world_nation(
            {"nation_id": 765, "expected_youth_rating": 135},
        )
    charge.assert_called_once()
    assert charge.call_args.args[:2] == (120_226_443, "world_nation_youth_investment")
    save_cache.assert_called_once_with(native, "save-1")
    assert nation["youth_rating"] == 136
    assert result["native"]["after"] == 136
    assert result["economy"] == {
        "general_balance": 880_000_000, "casino_balance": 20_000_000,
    }


def test_world_nation_investment_refunds_when_memory_write_fails() -> None:
    state, _native, nation = _world_nation_state()
    detail = {
        "nation": {**nation, "youth_rating": 135},
        "national_team": {"name": "英格兰"},
    }
    payment = {"amount": 120_226_443, "wallet": 120_226_443, "bank": 0}
    with (
        patch("fm_odds_web.read_native_world_nation_detail", return_value=detail),
        patch("fm_odds_web.charge_combined_funds", return_value=payment),
        patch(
            "fm_odds_web.invest_native_world_nation_youth",
            side_effect=RuntimeError("write failed"),
        ),
        patch("fm_odds_web.refund_combined_funds") as refund,
        pytest.raises(RuntimeError, match="write failed"),
    ):
        state.invest_world_nation(
            {"nation_id": 765, "expected_youth_rating": 135},
        )
    refund.assert_called_once()
    assert refund.call_args.args[:2] == (
        payment, "world_nation_youth_investment_rollback",
    )
    assert nation["youth_rating"] == 135


def test_world_nation_decrease_charges_half_price() -> None:
    state, native, nation = _world_nation_state()
    detail = {
        "nation": {**nation, "youth_rating": 126},
        "national_team": {"name": "England"},
    }
    payment = {"amount": 186_213_086, "wallet": 186_213_086, "bank": 0}
    with (
        patch("fm_odds_web.read_native_world_nation_detail", return_value=detail),
        patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
        patch(
            "fm_odds_web.invest_native_world_nation_youth",
            return_value={"before": 126, "after": 121, "increase": -5},
        ) as write_rating,
        patch("fm_odds_web.public_economy", return_value={}),
        patch("fm_odds_web.save_native_world_clubs"),
    ):
        result = state.invest_world_nation({
            "nation_id": 765, "expected_youth_rating": 126, "increase": -5,
        })

    assert charge.call_args.args[:2] == (
        186_213_086, "world_nation_youth_investment",
    )
    write_rating.assert_called_once_with(
        nation, expected_youth_rating=126, increase=-5,
    )
    assert result["native"]["after"] == 121
    assert native["nations"][0]["youth_rating"] == 121


def test_world_nation_frontend_and_routes_are_wired() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    markup = (root / "web" / "index.html").read_text(encoding="utf-8")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    stylesheet = (root / "web" / "app.css").read_text(encoding="utf-8")
    server = (root / "fm_odds_web.py").read_text(encoding="utf-8")
    assert 'data-page="world-nations"' in markup
    assert 'id="world-nation-detail-dialog"' in markup
    assert 'class="world-club-card world-nation-card"' in script
    assert 'class="world-club-grid world-nation-grid"' in script
    assert 'if (page === "world-nations")' in script
    assert "投资国家队青训" in script
    assert 'data-youth-increase="${requestedIncrease}"' in script
    assert "optionButton(-5)" in script
    assert "optionButton(-1)" in script
    assert 'querySelectorAll("[data-world-nation-invest]")' in script
    assert "applyEconomySnapshot(result.economy);" in script
    assert '${youth} → ${youth + 1}' not in script
    detail_renderer = script[
        script.index("function renderWorldNationDetail("):
        script.index("function isCurrentWorldNationDetail(")
    ]
    assert 'data-lucide="trending-up"' not in detail_renderer
    assert ".world-nation-investment button span{color:inherit}" in stylesheet
    assert "/api/world-nations/detail" in script
    assert "/api/world-nations/players" in script
    assert "/api/world-nations/invest" in script
    assert '"/api/world-nations": _get_world_nations_route' in server
    assert '"/api/world-nations/players": _get_world_nation_players_route' in server
    assert '"/api/world-nations/invest": "invest_world_nation"' in server


def test_world_nation_player_ranking_ui_has_tabs_pagination_and_player_drilldown() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    stylesheet = (root / "web" / "app.css").read_text(encoding="utf-8")

    assert 'data-world-nation-ranking="${key}"' in script
    assert '["world_reputation", "世界声望"]' in script
    assert "按主要国籍统计，每页 30 人" not in script
    assert 'page_size:"30"' in script
    assert 'data-world-nation-ranking-page=' in script
    assert 'data-world-nation-player=' in script
    assert 'openWorldPlayerDetail(Number(row.dataset.worldNationPlayer))' in script
    assert 'world-nation-ranking-rank${rankTone}' in script
    assert 'world-nation-ranking-avatar' not in script
    assert 'world-nation-ranking-avatar' not in stylesheet
    assert '<span><strong>${escapeHtml(playerName)}</strong><small>' in script
    assert 'worldNationRankingProgress(player, ranking)' in script
    assert 'role="tablist" aria-label="排行榜类型"' in script
    assert 'aria-label="国家球员榜单分页"' in script
    assert '.world-nation-ranking-row:hover,.world-nation-ranking-row:focus-visible' in stylesheet
    assert '.world-nation-ranking-rank.podium-1 b' in stylesheet
    assert '.world-nation-ranking-metric>i b' in stylesheet
    assert '.world-nation-detail-dialog{width:min(1040px,94vw)}' in stylesheet


def test_world_nation_load_failure_exits_spinner_and_offers_retry() -> None:
    script = ((Path(__file__).resolve().parents[1] / "src") / "web" / "app.js").read_text(
        encoding="utf-8",
    )
    load = script.split("async function loadWorldNations", 1)[1].split(
        "function syncWorldNationScanPolling", 1,
    )[0]
    render = script.split("function renderWorldNations", 1)[1].split(
        "function renderWorldNationDetail", 1,
    )[0]

    assert "app.worldNationsError = null" in load
    assert "app.worldNationsError = error.message" in load
    assert "renderWorldNations();" in load
    assert "!data && app.worldNationsLoading" in render
    assert "世界国家读取失败" in render
    assert "data-world-nations-retry" in render


def test_world_nation_frontend_indexes_visible_rows_and_skips_unchanged_dom() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    index = script.split("function worldNationDataIndex", 1)[1].split(
        "function worldNationContentSignature", 1,
    )[0]
    signature = script.split("function worldNationContentSignature", 1)[1].split(
        "function syncWorldNationPageStatus", 1,
    )[0]
    render = script.split("function renderWorldNations", 1)[1].split(
        "function renderWorldNationDetail", 1,
    )[0]
    detail = script.split("async function openWorldNationDetail", 1)[1].split(
        "async function scanWorldNations", 1,
    )[0]
    detail_actions = script.split("function bindWorldNationDetailActions", 1)[1].split(
        "async function openWorldNationDetail", 1,
    )[0]
    reset = script.split("function resetAccountScopedWorldState", 1)[1].split(
        "async function loadStateOnce", 1,
    )[0]

    assert "app.worldNationIndexSource === data" in index
    assert "nationsById:new Map" in index
    assert "facetTotal:facets.reduce" in index
    assert "dataSignature = JSON.stringify" in index
    assert "index.version" in signature
    assert "index.dataSignature" not in signature
    assert "app.worldNationRenderSignature === signature" in render
    assert "worldNationDataIndex().nationsById.get" in detail
    assert detail.count("invalidateWorldNationDataIndex();") + detail_actions.count("invalidateWorldNationDataIndex();") >= 2
    assert "app.worldNationIndexSource = null" in reset
    assert "app.worldNationIndexVersion = 0" in reset
    assert "app.worldNationRenderSignature = null" in reset


def test_world_nation_busy_keyboard_and_aria_contracts() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    markup = (root / "web" / "index.html").read_text(encoding="utf-8")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    stylesheet = (root / "web" / "app.css").read_text(encoding="utf-8")
    load = script.split("async function loadWorldNations", 1)[1].split(
        "function syncWorldNationScanPolling", 1,
    )[0]
    status = script.split("function syncWorldNationPageStatus", 1)[1].split(
        "function worldNationCard", 1,
    )[0]
    render = script.split("function renderWorldNations", 1)[1].split(
        "function renderWorldNationDetail", 1,
    )[0]

    assert 'aria-labelledby="world-nations-page-title"' in markup
    assert 'id="world-nation-sync-status"' in markup
    assert 'id="world-nation-content" aria-busy="false"' in markup
    assert '"world.search_nations"' in markup
    assert 'aria-labelledby="world-nation-detail-title"' in markup
    assert '"dialog.close_nation_details"' in markup
    assert "requestKey !== app.worldNationRequestKey" in load
    assert 'setAttribute("aria-busy", String(refreshing))' in status
    assert "worldNationIdleDisabled" in status
    assert 'role="group" aria-label="' in render
    assert 'aria-pressed="${!app.worldNationContinent}"' in render
    assert 'class="world-nation-ranking-table" role="table"' in render
    assert 'aria-label="上一页"' in render
    assert 'aria-label="下一页"' in render
    assert ".world-nation-sync-status" in stylesheet
    assert "#world-nation-scan.is-refreshing svg" in stylesheet
    assert ".world-nation-sync-status > i" in stylesheet
