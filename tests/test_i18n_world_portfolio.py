from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def world_probe() -> dict[str, object]:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.world.js", "utf8"), context);
const module = context.window.FMODDI18nModules[0];
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const i18n = context.window.FMODDI18n;
const keys = Object.fromEntries(Object.entries(module.messages).map(([locale, catalog]) => [locale, Object.keys(catalog).sort()]));
i18n.setLocale("en-GB", {persist:false, root:null});
const english = [i18n.t("world.players.range_invalid", {label:"Age"}), i18n.t("portfolio.brand_confirm", {price:"£500", team:"Ajax", before:"4,000", after:"4,050"}), i18n.t("world.nation.ranking_page", {page:2, total:4, count:91}), i18n.t("portfolio.order.saved", {club:"Ajax", position:2}), i18n.t("portfolio.more_data"), i18n.t("portfolio.collapse_data"), i18n.t("portfolio.card.relative_to_price")];
i18n.setLocale("ko-KR", {persist:false, root:null});
const korean = [i18n.t("world.players.range_invalid", {label:"나이"}), i18n.t("portfolio.brand_confirm", {price:"£500", team:"Ajax", before:"4,000", after:"4,050"}), i18n.t("world.nation.ranking_page", {page:2, total:4, count:91}), i18n.t("portfolio.order.saved", {club:"Ajax", position:2}), i18n.t("portfolio.more_data"), i18n.t("portfolio.collapse_data"), i18n.t("portfolio.card.relative_to_price")];
console.log(JSON.stringify({keys, messages:module.messages, english, korean}));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def placeholders(value: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def test_world_portfolio_catalogs_have_locale_and_placeholder_parity() -> None:
    output = world_probe()
    keys = output["keys"]
    assert keys["en-GB"] == keys["zh-CN"] == keys["ko-KR"]
    for key in keys["en-GB"]:
        values = [output["messages"][locale][key] for locale in ("en-GB", "zh-CN", "ko-KR")]
        assert placeholders(values[0]) == placeholders(values[1]) == placeholders(values[2])


def test_world_portfolio_representative_english_and_korean_output() -> None:
    output = world_probe()
    assert output["english"] == [
        "Age: minimum cannot be greater than maximum",
        "Invest £500 to raise “Ajax” reputation from 4,000 to 4,050?",
        "Page 2 / 4 · 91 players",
        "Ajax moved to position 2; order saved",
        "More", "Less", "vs purchase price",
    ]
    assert output["korean"] == [
        "나이: 최솟값은 최댓값보다 클 수 없습니다",
        "투자 금액 £500. “Ajax”의 명성을 4,000에서 4,050으로 올릴까요?",
        "2 / 4페이지 · 선수 91명",
        "Ajax를 2번째로 이동했고 순서를 저장했습니다",
        "더보기", "접기", "인수가 대비",
    ]


def test_world_portfolio_renderers_use_semantic_copy_and_active_locale_helpers() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for token in (
        'uiText("world.players.range_invalid", {label})',
        'uiText("world.nation.reputation")',
        'uiText("world.nation.ranking_page"',
        'uiText("portfolio.sell.confirm"',
        'uiText("portfolio.order.saved"',
        'uiText("portfolio.brand_confirm"',
        'uiText("portfolio.youth_confirm_son"',
        'toLocaleString(activeUiLocale())',
    ):
        assert token in script
    # Game-provided nation/category values stay raw so they are never mistaken for UI copy.
    start = script.index("function worldClubAssignment")
    end = script.index("function ownedClubLeagueLabel", start)
    residual = [
        value for value in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', script[start:end])
        if re.search(r"[\u4e00-\u9fff]", value)
    ]
    assert set(residual) <= {"", "未分类", "未知", "世界", "world.not_read"}


def test_legacy_best_xi_renderer_uses_world_catalog_keys() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = script.index("function renderLegacyBestTeamPitchStats")
    end = script.index("function legacyBestTeamRemapAssignments", start)
    for key in (
        "legacy.best.goalkeeper_key_stats",
        "legacy.best.career_scope_note",
        "legacy.best.scope_aria",
        "legacy.best.remove_player",
        "legacy.best.candidate_meta",
        "legacy.best.all_candidates_selected",
    ):
        assert f'"{key}"' in script[start:end]


def test_legacy_profile_glance_uses_world_catalog_keys() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = script.index("function renderLegacyProfileGlance")
    end = script.index("function renderLegacySeasonSection", start)
    for key in (
        "legacy.profile.glance_contribution",
        "legacy.profile.glance_appearances",
        "legacy.profile.owner_club",
        "legacy.profile.loaned_to",
    ):
        assert f'"{key}"' in script[start:end]
