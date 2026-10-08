from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _catalog_probe() -> dict[str, object]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.world.js", "utf8"), context);
const module = context.window.FMODDI18nModules[0];
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const i18n = context.window.FMODDI18n;
const output = {};
for (const locale of ["en-GB", "zh-CN", "ko-KR"]) {
  i18n.setLocale(locale, {persist:false, root:null});
    output[locale] = [
    i18n.t("legacy.home.loading_title"),
    i18n.t("legacy.world_search.placeholder"),
    i18n.t("legacy.player_card.current_value"),
    i18n.t("legacy.profile.stats.total"),
    i18n.t("legacy.profile.stats.season_option", {start:"25", end:"26"}),
    i18n.t("legacy.moment.title"),
    i18n.t("legacy.moment.native_goal_multiple", {count:2}),
  ];
}
console.log(JSON.stringify({messages:module.messages, output}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_legacy_hall_catalog_has_locale_and_placeholder_parity() -> None:
    result = _catalog_probe()
    messages = result["messages"]
    keys = set(messages["en-GB"])
    assert keys == set(messages["zh-CN"]) == set(messages["ko-KR"])
    selected = {key for key in keys if key.startswith("legacy.home.") or key.startswith("legacy.world_search.") or key.startswith("legacy.name.") or key.startswith("legacy.status.") or key.startswith("legacy.moment.") or key.startswith("legacy.preference.") or key.startswith("legacy.profile.stats.")}
    assert selected
    for key in selected:
        placeholders = [
            set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", messages[locale][key]))
            for locale in ("en-GB", "zh-CN", "ko-KR")
        ]
        assert placeholders[0] == placeholders[1] == placeholders[2], key


def test_legacy_hall_representative_runtime_copy() -> None:
    output = _catalog_probe()["output"]
    assert output["en-GB"] == [
        "Preparing player records",
        "Search world players to keep watching them",
        "Current value",
        "Total",
        "25–26 season",
        "Record a match moment",
        "Read 2 native goals · enter the matching minute",
    ]
    assert output["zh-CN"] == [
        "正在整理人物记录",
        "搜索全世界球员并加入持续关注",
        "当前身价",
        "总数据",
        "25–26赛季",
        "记录比赛时刻",
        "已读取 2 个原生进球 · 请填写对应分钟",
    ]
    assert output["ko-KR"] == [
        "선수 기록을 정리하는 중",
        "전 세계 선수를 검색해 계속 관심 목록에 추가",
        "현재 가치",
        "총 데이터",
        "25–26 시즌",
        "경기 순간 기록",
        "원본 득점 2개를 읽음 · 해당 분을 입력하세요",
    ]


def test_legacy_hall_renderers_use_semantic_copy_and_keep_tag_values_stable() -> None:
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for token in (
        'uiText("legacy.home.loading_title")',
        'uiText("legacy.home.scope_aria")',
        'uiText("legacy.world_search.placeholder")',
        'uiText("legacy.name.saved")',
        'uiText("legacy.status.update_incomplete")',
        'uiText("legacy.moment.native_goal_multiple", {count:lateGoals.length})',
        'uiText("legacy.moment.contribution_required")',
        'uiText(tagKeys[tag])',
    ):
        assert token in source
    assert 'value="${escapeHtml(tag)}"' in source
    assert 'checked.includes("进球")' in source

    home = source.split("function renderClubLegacy()", 1)[1].split("async function searchLegacyWorldPlayers", 1)[0]
    residual = [
        value for value in re.findall(r'"([^"\\]*(?:\\.[^"\\]*)*)"', home)
        if re.search(r"[\u4e00-\u9fff]", value)
    ]
    assert residual == []
