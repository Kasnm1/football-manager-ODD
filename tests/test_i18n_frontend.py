from pathlib import Path
import re
import subprocess


ROOT = (Path(__file__).resolve().parents[1] / "src")
SUPPORTED_LOCALES = (
    "en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP",
    "pt-BR", "pt-PT",
)
LOCALE_PACK_SOURCES = (
    "/i18n.tw.js", "/i18n.de.js", "/i18n.es.js", "/i18n.fr.js", "/i18n.ru.js", "/i18n.ja.js",
    "/i18n.pt-BR.js", "/i18n.pt-PT.js",
)
NATIVE_LOCALE_NAMES = (
    ("en-GB", "English"), ("zh-CN", "简体中文"), ("zh-TW", "繁體中文"),
    ("ja-JP", "日本語"), ("ko-KR", "한국어"), ("de-DE", "Deutsch"),
    ("es-ES", "Español"), ("fr-FR", "Français"), ("ru-RU", "Русский"),
    ("pt-BR", "Português (Brasil)"), ("pt-PT", "Português (Portugal)"),
)


def object_keys(source: str, start: str, end: str) -> set[str]:
    body = source.split(start, 1)[1].split(end, 1)[0]
    return set(re.findall(r'(?:^|,)\s*"([^"\\]+)"\s*:', body, flags=re.MULTILINE))


def sources() -> tuple[str, str, str, str, str]:
    return (
        (ROOT / "web" / "i18n.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "i18n.ko.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.css").read_text(encoding="utf-8"),
    )


def test_i18n_runtime_is_loaded_before_application_script() -> None:
    i18n, korean, _script, html, _css = sources()
    relationship = html.index('script src="/i18n.relationship.js"')
    core = html.index('script src="/i18n.js"')
    app = html.index('script src="/app.js"')
    assert relationship < core < app
    assert all(html.index(f'script src="{source}"') < core for source in LOCALE_PACK_SOURCES)
    assert 'DEFAULT_LOCALE = "en-GB"' in i18n
    for locale in SUPPORTED_LOCALES:
        assert f'"{locale}"' in i18n.split("const SUPPORTED_LOCALES", 1)[1].split("];", 1)[0]
    assert 'window.FMODDI18n' in i18n
    assert 'window.FMODDKoreanCatalog' in korean


def test_ui_locale_is_scoped_to_settings_and_server_authoritative() -> None:
    i18n, _korean, script, _html, css = sources()
    assert 'const UI_LOCALE_STORAGE_KEY' in script
    assert 'settings.ui_locale' in script
    assert '/api/settings/ui-locale' in script
    assert 'window.location.reload()' not in i18n
    assert 'syncAuthoritativeLocale' in i18n
    display_group = script.split('key:"display"', 1)[1].split('key:"cheats"', 1)[0]
    general_group = script.split('key:"general"', 1)[1].split('key:"markets"', 1)[0]
    assert '".ui-locale-setting"' in display_group
    assert '".ui-locale-setting"' not in general_group
    assert '.ui-locale-setting > .fmodd-select { width:180px;' in css
    locale_control = script.split("function ensureUiLocaleControl()", 1)[1].split("function ensureProfitLossColorControl()", 1)[0]
    assert 'id="ui-locale" data-menu-max-height="440"' in locale_control
    assert "UI_LOCALE_NATIVE_NAMES.map" in locale_control
    assert 'translate="no"' in locale_control
    assert "const requestedMaxHeight = Number(select.dataset.menuMaxHeight);" in script
    assert "Math.min(menu.scrollHeight, maxMenuHeight, window.innerHeight - margin * 2)" in script
    for locale, name in NATIVE_LOCALE_NAMES:
        assert f'["{locale}", "{name}"]' in script
    assert 'language_note' not in script
    assert '切换后将重新载入界面，不会重启服务或修改游戏数据' not in script
    assert '文字粗细' not in i18n


def test_english_shell_has_long_label_layout_guardrails() -> None:
    i18n, _korean, _script, _html, css = sources()
    assert '"home.connect": "Connect save"' in i18n
    assert '[data-locale="en-GB"],[data-locale="zh-TW"],[data-locale="ko-KR"],[data-locale="de-DE"],[data-locale="es-ES"],[data-locale="fr-FR"],[data-locale="ru-RU"],[data-locale="ja-JP"],[data-locale="pt-BR"],[data-locale="pt-PT"]' in css
    assert '.ui-locale-setting' in css


def test_support_author_uses_qr_for_simplified_chinese_and_external_links_elsewhere() -> None:
    _i18n, _korean, script, html, css = sources()
    locale_sources = "\n".join(
        (ROOT / "web" / filename).read_text(encoding="utf-8")
        for filename in (
            "i18n.static.js", "i18n.tw.js", "i18n.de.js", "i18n.es.js",
            "i18n.fr.js", "i18n.ru.js", "i18n.ja.js", "i18n.pt-BR.js", "i18n.pt-PT.js",
        )
    )
    assert 'id="support-author-button" class="support-zh-cn-control"' in html
    assert 'id="support-kofi-link" class="support-kofi-control"' in html
    assert 'href="https://ko-fi.com/fmodd"' in html
    assert 'target="_blank" rel="noopener noreferrer" aria-label="Ko-fi"' in html
    assert 'class="kofi-mark"' in html
    assert '<span>Ko-fi</span>' in html
    assert 'id="support-other-link" class="support-other-control"' in html
    assert html.count('href="https://fmodd.com/donate"') == 3
    assert 'data-i18n="settings.other_ways">Other ways</span>' in html
    for translation in (
        '"Other ways"', '"其他方式"', '"다른 방법"', '"Weitere Möglichkeiten"',
        '"Otras formas"', '"Autres moyens"', '"Другие способы"', '"その他の方法"', '"Outras formas"',
    ):
        assert translation in locale_sources
    assert '[data-locale]:not([data-locale="zh-CN"]) .support-setting .support-zh-cn-control { display:none; }' in css
    assert '[data-locale]:not([data-locale="zh-CN"]) .support-setting :is(.support-kofi-control,.support-other-control) { display:inline-flex; }' in css
    assert '$("#support-author-button").addEventListener("click"' in script


def test_usage_notice_can_be_suppressed_for_seven_days_without_explaining_the_period() -> None:
    _i18n, _korean, script, html, css = sources()
    notice_html = html.split('<dialog id="usage-notice-dialog"', 1)[1].split("</dialog>", 1)[0]
    show_notice = script.split("function showInitialUsageNotice()", 1)[1].split("function lotteryData", 1)[0]
    confirm_notice = script.split('$("#usage-notice-confirm").addEventListener', 1)[1].split(
        '$("#manager-select")', 1,
    )[0]

    assert 'id="usage-notice-dismiss" type="checkbox"' in notice_html
    assert 'data-i18n="notice.dismiss">Don\'t show again</span>' in notice_html
    assert 'href="https://fmodd.com/" target="_blank" rel="noopener noreferrer"' in notice_html
    assert 'class="usage-notice-support-link" href="https://fmodd.com/donate" target="_blank" rel="noopener noreferrer" data-i18n="notice.support_link"' in notice_html
    assert 'data-i18n="notice.support_before"' in notice_html
    assert 'data-i18n="notice.support_after"' in notice_html
    assert "7 days" not in notice_html
    assert "7 天" not in notice_html
    assert "USAGE_NOTICE_DISMISS_DURATION_MS = 7 * 24 * 60 * 60 * 1000" in script
    assert "dismissedUntil > Date.now()" in show_notice
    assert 'localStorage.removeItem(USAGE_NOTICE_DISMISSED_UNTIL_STORAGE_KEY)' in show_notice
    assert '$("#usage-notice-dismiss")?.checked' in confirm_notice
    assert "Date.now() + USAGE_NOTICE_DISMISS_DURATION_MS" in confirm_notice
    assert ".usage-notice-dismiss {" in css

    static_catalog = (ROOT / "web" / "i18n.static.js").read_text(encoding="utf-8")
    for expected in (
        "欢迎使用 FMODD",
        "本工具中的投注、赔率等功能仅用于丰富 Football Manager 的游戏体验",
        "请注意，本工具的部分功能会修改游戏数据，可能造成存档损坏或数据丢失，请在使用前备份存档。",
        "严禁任何个人或组织以本工具名义进行收费售卖、付费授权或捆绑销售。",
        "FMODD 仍在持续更新和完善中",
        "如果 FMODD 让您玩得更尽兴，欢迎前往设置页",
        "您的支持是作者最大的更新动力。",
    ):
        assert expected in static_catalog


def test_first_launch_offers_language_selection_inside_the_usage_notice() -> None:
    _i18n, _korean, script, html, css = sources()
    notice_html = html.split('<dialog id="usage-notice-dialog"', 1)[1].split("</dialog>", 1)[0]
    show_notice = script.split("function showInitialUsageNotice()", 1)[1].split("function lotteryData", 1)[0]
    locale_saver = script.split("async function saveUiLocaleSelection", 1)[1].split(
        "function ensureProfitLossColorControl", 1,
    )[0]

    assert 'id="usage-notice-language-setting" class="usage-notice-language-setting" hidden' in notice_html
    assert notice_html.index('id="usage-notice-language-setting"') < notice_html.index(
        'class="usage-notice-dismiss"',
    )
    assert 'id="usage-notice-language" data-menu-max-height="440"' in notice_html
    for locale, name in NATIVE_LOCALE_NAMES:
        assert f'<option value="{locale}"' in notice_html
        assert f'>{name}</option>' in notice_html
    assert "localStorage.getItem(UI_LOCALE_STORAGE_KEY)" in show_notice
    assert "!window.FMODDI18n?.SUPPORTED_LOCALES?.includes(storedLocale)" in show_notice
    assert "languageSetting.hidden = !firstLaunch" in show_notice
    assert 'request("/api/settings/ui-locale"' in locale_saver
    assert "window.FMODDI18n?.syncAuthoritativeLocale(next)" in locale_saver
    assert 'option.removeAttribute("data-i18n")' in script
    assert 'option.textContent = name' in script
    assert '$("#usage-notice-language")?.addEventListener("change"' in script
    assert ".usage-notice-language-setting[hidden] { display:none; }" in css


def test_core_pages_apply_translations_after_dynamic_render() -> None:
    _i18n, _korean, script, _html, _css = sources()
    assert 'window.FMODDI18n?.apply(document.querySelector(`#page-${app.page}`));' in script
    assert "every feature can use" in script


def test_betting_navigation_translates_dynamic_labels_and_quantity_templates() -> None:
    i18n, _korean, script, _html, _css = sources()
    for source in (
        '"全部赛事":"All competitions"',
        '"联赛赛事":"League competitions"',
        '"杯赛赛事":"Cup competitions"',
        '"国家队赛事":"International competitions"',
        '"没有符合条件的比赛":"No matching fixtures"',
        '"游戏时间":"Game time"',
    ):
        assert source in i18n
    assert 'uiText("betting.upcoming", {days:Number(output().odds_days' in script
    assert 'label.textContent = settled > 0 ? uiPlural("topbar.up_to_date_settled", settled) : uiText("topbar.up_to_date")' in script


def test_korean_catalog_covers_the_existing_english_transition_catalog() -> None:
    i18n, korean, _script, _html, _css = sources()
    english_legacy = object_keys(i18n, "const legacyEnglish = Object.freeze({", "\n  });")
    korean_legacy = object_keys(korean, "const legacy = Object.freeze({", "\n  });")
    assert english_legacy
    assert korean_legacy == english_legacy
    assert '"shell.desktop": "FMODD 데스크톱"' in korean


def test_locale_switch_is_in_place_and_plural_categories_are_not_collapsed() -> None:
    i18n, _korean, script, _html, _css = sources()
    assert "function setLocale(value" in i18n
    assert "document.documentElement.dataset.locale = locale" in i18n
    assert "`${key}.${rule}`" in i18n
    assert "rule === \"one\"" not in i18n
    assert "window.location.reload()" not in script
    assert '.toLocaleLowerCase("zh-CN")' not in script
    assert '.localeCompare(String(right.name || ""), "zh-CN")' not in script
    assert ".localeCompare(String(right.name || \"\"), activeUiLocale())" in script


def test_page_headings_are_semantic_and_do_not_repeat_the_screenshot_label() -> None:
    i18n, korean, _script, html, css = sources()
    page_keys = (
        "shop", "car_lottery", "inventory", "bank", "activity", "hospital",
        "training", "canteen", "club", "hall_of_fame", "relations",
        "world_clubs", "world_players", "world_nations", "my_clubs",
    )
    for page in page_keys:
        for suffix in ("eyebrow", "title"):
            key = f'page.{page}.{suffix}'
            assert i18n.count(f'"{key}"') == 2
            assert f'"{key}"' in korean
            assert f'data-i18n="{key}"' in html
    assert '"page.my_clubs.eyebrow": "CLUB PORTFOLIO"' in i18n
    assert '"page.my_clubs.title": "My Group"' in i18n
    assert '>MY GROUP<' not in html
    assert "i18n-duplicate-eyebrow" in i18n
    assert ".i18n-duplicate-eyebrow" in css
    assert ':is(.dialog-head-actions,.page-tabs) { flex-wrap:wrap; max-width:100%; }' in css
    assert '.page-tabs button { height:auto; min-height:34px;' in css


def test_i18n_core_supports_domain_catalogs_and_dynamic_dom_updates() -> None:
    i18n, _korean, _script, _html, _css = sources()
    assert "window.FMODDI18nModules" in i18n
    assert "buildMessageCatalog" in i18n
    assert "window.FMODDLocalePacks" in i18n
    assert "function observe(root = document.body)" in i18n
    assert "new MutationObserver" in i18n
    assert "formatNumber, formatDate, compare" in i18n
    assert 'const selector = `[data-i18n-${attribute}]`;' in i18n
    for attribute in ("placeholder", "title", "alt"):
        assert f'[\"{attribute}\",' in i18n


def test_dynamic_translation_does_not_rewrite_an_already_translated_text_node() -> None:
    i18n, _korean, _script, _html, _css = sources()
    translator = i18n.split("function translateTextNode(node)", 1)[1].split(
        "function updatePageHeadingSemantics", 1,
    )[0]

    assert "if (current !== translated) node.nodeValue" in translator
    assert "if (translated !== source) node.nodeValue" not in translator


def test_observer_does_not_restore_static_keys_over_dynamic_text_updates() -> None:
    i18n, _korean, _script, _html, _css = sources()
    observer = i18n.split("translationObserver = new MutationObserver", 1)[1].split(
        "translationObserver.observe", 1,
    )[0]

    assert 'record.type === "characterData"' in observer
    assert '!parent.closest("[data-i18n]")' in observer
    assert '!node.parentElement.closest("[data-i18n]")' in observer
    assert 'else if (record.target instanceof Element) observerRoots.add(record.target)' not in observer


def test_explicit_page_translation_preserves_runtime_text_on_keyed_elements() -> None:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const document = {
  documentElement:{dataset:{}}, body:null, querySelector:()=>null, dispatchEvent:()=>{},
  createTreeWalker:()=>({currentNode:null,nextNode:()=>false}),
};
const context = {
  window:{FMODDI18nModules:[],FMODDLocalePacks:{}}, document,
  NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const element = {
  dataset:{i18n:"page.club.title"}, textContent:"Club Management",
  matches:(selector)=>selector === "[data-i18n]", querySelectorAll:()=>[],
  hasAttribute:()=>false, getAttribute:()=>null, setAttribute:()=>{}, querySelector:()=>null,
};
context.window.FMODDI18n.setLocale("zh-CN", {persist:false,root:element});
if (element.textContent !== "\u6267\u6559\u7ba1\u7406") throw new Error("static keyed text did not translate");
const dynamicText = "\u6211\u7684\u4ff1\u4e50\u90e8 \u00b7 \u67cf\u592a\u9633\u795e";
element.textContent = dynamicText;
for (const locale of context.window.FMODDI18n.SUPPORTED_LOCALES) {
  context.window.FMODDI18n.setLocale(locale, {persist:false,root:element});
  if (element.textContent !== dynamicText) {
    throw new Error(`dynamic text was overwritten for ${locale}`);
  }
}
'''
    subprocess.run(
        ["node", "-e", probe, str(ROOT)],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
