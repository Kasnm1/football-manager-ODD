from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def _probe() -> dict[str, object]:
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
    i18n.t("legacy.active"),
    i18n.t("legacy.profile.loaned_to", {club:"Ajax"}),
    i18n.t("legacy.career_since_start"),
    i18n.t("legacy.best.recommended", {count:3}),
    i18n.t("legacy.best.slot_cleared"),
  ];
}
console.log(JSON.stringify({messages:module.messages, output}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_legacy_best_event_catalogs_have_locale_and_placeholder_parity() -> None:
    result = _probe()
    messages = result["messages"]
    keys = set(messages["en-GB"])
    assert keys == set(messages["zh-CN"]) == set(messages["ko-KR"])
    selected = {
        key for key in keys
        if key.startswith("legacy.best.")
        or key in {"legacy.club.other", "legacy.history_member", "legacy.career_since_start", "legacy.none"}
    }
    assert selected
    for key in selected:
        placeholders = [
            set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", messages[locale][key]))
            for locale in ("en-GB", "zh-CN", "ko-KR")
        ]
        assert placeholders[0] == placeholders[1] == placeholders[2], key


def test_legacy_best_event_representative_runtime_copy() -> None:
    output = _probe()["output"]
    assert output["en-GB"] == [
        "In squad",
        "Loaned to Ajax",
        "Since the start of the save",
        "Recommended 3 players by position rating",
        "Position cleared",
    ]
    assert output["ko-KR"] == [
        "스쿼드에 있음",
        "Ajax로 임대",
        "저장 파일 시작 이후",
        "포지션 평점으로 3명을 추천했습니다",
        "포지션을 비웠습니다",
    ]


def test_legacy_best_event_handlers_use_semantic_copy() -> None:
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    segments = {
        "legacyPrimaryClub": source.split("function legacyPrimaryClub", 1)[1].split("function legacyMonthDate", 1)[0],
        "legacyClubTenureForClub": source.split("function legacyClubTenureForClub", 1)[1].split("function legacyClubTenure", 1)[0],
        "legacyTransferClubDetails": source.split("function legacyTransferClubDetails", 1)[1].split("function legacyPortraitMarkup", 1)[0],
        "saveLegacyBestTeam": source.split("async function saveLegacyBestTeam", 1)[1].split("function recommendLegacyBestTeam", 1)[0],
        "recommendLegacyBestTeam": source.split("function recommendLegacyBestTeam", 1)[1].split("function assignLegacyBestTeamPlayer", 1)[0],
        "assignLegacyBestTeamPlayer": source.split("function assignLegacyBestTeamPlayer", 1)[1].split("function renderLegacyWorldSearch", 1)[0],
    }
    for token in (
        'uiText("legacy.profile.loaned_to"',
        'uiText("legacy.career_since_start")',
        'uiText("legacy.best.save_failed")',
        'uiText("legacy.best.recommended"',
        'uiText("legacy.best.player_added")',
        'uiText("legacy.best.slot_cleared")',
    ):
        assert token in "\n".join(segments.values())
    assert not re.search(r"[\u4e00-\u9fff]", "\n".join(segments.values()))
