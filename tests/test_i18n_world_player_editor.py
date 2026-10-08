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
    i18n.t("economy.salary.schedule_weekly"),
    i18n.t("world.editor.confirm_language", {player:"Alex", level:8}),
    i18n.t("world.player_detail.contract_type_value", {value:7}),
    i18n.t("world.player_detail.item_count", {count:2}),
  ];
}
console.log(JSON.stringify({messages:module.messages, output}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_world_player_editor_catalogs_have_locale_and_placeholder_parity() -> None:
    result = _probe()
    messages = result["messages"]
    keys = set(messages["en-GB"])
    assert keys == set(messages["zh-CN"]) == set(messages["ko-KR"])
    selected = {
        key for key in keys
        if key.startswith(("economy.salary.", "world.editor.", "world.player_detail."))
        or key == "legacy.name.edit_title"
    }
    assert selected
    for key in selected:
        placeholders = [
            set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", messages[locale][key]))
            for locale in ("en-GB", "zh-CN", "ko-KR")
        ]
        assert placeholders[0] == placeholders[1] == placeholders[2], key


def test_world_player_editor_representative_runtime_copy() -> None:
    output = _probe()["output"]
    assert output["en-GB"] == [
        "Selected weekly salary payments to the bank",
        "Change Alex's language proficiency to 8?",
        "Type 7",
        "2 items",
    ]
    assert output["ko-KR"] == [
        "주급을 은행으로 지급하도록 선택했습니다",
        "Alex의 언어 숙련도를 8로 변경할까요?",
        "유형 7",
        "2개",
    ]


def test_world_player_editor_renderers_use_semantic_copy_and_keep_raw_values() -> None:
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = source.index("async function updateSalarySchedule")
    end = source.index("function iconRenderRoots", start)
    editor = source[start:end]
    for token in (
        '"economy.salary.schedule_weekly"',
        'uiText("world.editor.confirm_language"',
        'uiText("world.editor.memory_warning")',
        'uiText("world.editor.contract_section")',
        'uiText("world.player_detail.transfer_status")',
        'uiText("legacy.name.placeholder")',
        'uiText("legacy.name.saved")',
    ):
        assert token in editor
    # Only game-provided hidden-attribute names remain as Han literals; they
    # select the valid lower bound and must not become translated display copy.
    residual = re.findall(r"[\u4e00-\u9fff]+", editor)
    assert residual == ["适应性", "雄心", "忠诚", "抗压能力", "职业素养", "体育精神", "情绪控制", "争论"]
    # These compatibility values are part of the API contract, not labels.
    for option in ("cancel", "six_months", "two_years", "ten_years", "twenty_years"):
        assert f'value="{option}"' in editor
    assert 'join("、")' in editor
