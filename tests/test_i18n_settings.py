from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")
SETTINGS_LOCALES = ("en-GB", "zh-CN", "ko-KR", "pt-BR", "pt-PT")
SETTINGS_LOCALE_OPTION_KEYS = (
    "settings.locale.option.pt_br",
    "settings.locale.option.pt_pt",
)


def _probe() -> dict[str, object]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.settings.js", "utf8"), context);
for (const file of ["i18n.pt-BR.js", "i18n.pt-PT.js"]) {
  vm.runInContext(fs.readFileSync(root + "/web/" + file, "utf8"), context, {filename:file});
}
const module = context.window.FMODDI18nModules[0];
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const catalogs = Object.fromEntries(["en-GB", "zh-CN", "ko-KR", "pt-BR", "pt-PT"].map((locale) => [
  locale,
  Object.assign({}, module.messages?.[locale] || {}, context.window.FMODDLocalePacks?.[locale]?.messages || {}),
]));
const result = {messages:module.messages, catalogs, examples:{}};
for (const locale of ["en-GB", "ko-KR", "pt-BR", "pt-PT"]) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  result.examples[locale] = context.window.FMODDI18n.t("operations.location", {location:"/api/state"});
}
console.log(JSON.stringify(result));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_settings_catalog_has_locale_and_placeholder_parity() -> None:
    output = _probe()
    catalogs = output["catalogs"]
    source_keys = set(output["messages"]["en-GB"])
    for locale in SETTINGS_LOCALES:
        assert source_keys <= set(catalogs[locale])
    for key in source_keys:
        parameters = [
            set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", catalogs[locale][key]))
            for locale in SETTINGS_LOCALES
        ]
        assert all(value == parameters[0] for value in parameters), key
    assert all(key in catalogs["en-GB"] for key in SETTINGS_LOCALE_OPTION_KEYS)
    assert output["examples"]["en-GB"] == "Location: /api/state"
    assert output["examples"]["ko-KR"] == "위치: /api/state"
    assert set(output["examples"]) == {"en-GB", "ko-KR", "pt-BR", "pt-PT"}
    assert output["examples"]["pt-BR"] != output["examples"]["en-GB"]
    assert output["examples"]["pt-PT"] != output["examples"]["en-GB"]


def test_settings_and_operation_renderers_use_semantic_copy() -> None:
    source = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    for call in (
        "UI_LOCALE_NATIVE_NAMES",
        'uiText("settings.currency.label")',
        'uiText("settings.colours.reverse")',
        'uiText("settings.font.size")',
        'labelKey:"settings.font.family.segoe_ui"',
        '"settings.font.language"',
        '"settings.font.changed";',
        'toast(uiText(messageKey',
        'uiText("settings.markets.seconds_8")',
        'uiText("settings.account.switch_failed")',
        'button.dataset.i18n = "manager.merge_accounts";',
        'uiText("settings.storage.clear_cache.confirm")',
        'uiText("settings.cheats.wallet_cleared")',
        'uiText("operations.clear.confirm")',
        'uiText("settings.portraits.title")',
        'uiText("settings.update.ignore")',
        'uiText("operations.failed_summary")',
        'uiText("operations.empty_note")',
        'toLocaleString(activeUiLocale()',
        'uiText(group.labelKey)',
        'if (app.storageInfo) renderStorageInfo(app.storageInfo);',
    ):
        assert call in source
    for legacy in (
        'root.setAttribute("aria-label", "最近任务与操作记录")',
        'tab.textContent = group.label;',
        'return date.toLocaleString("zh-CN"',
        'toast(scopeId ? "已切换到所选存档账户" : "已恢复自动识别存档")',
        '确定清空本机保存的任务与操作记录吗？此操作不会影响游戏存档。',
        'toast(`中文字体已切换为 ${event.target.selectedOptions[0]?.textContent || "默认字体"}`)',
        'toast(enabled ? "冠军盘已开启，下次刷新盘口时恢复" : "冠军盘已关闭")',
    ):
        assert legacy not in source
    for key in (
        "settings.font.family.yahei_ui",
        "settings.font.family.jhenghei",
        "settings.font.family.simhei",
        "settings.font.family.source_han_sans_sc",
    ):
        assert f'data-i18n="{key}"' in html
    for unsupported in ('value="nsimsun"', 'value="noto-sans-sc"'):
        assert unsupported not in html
