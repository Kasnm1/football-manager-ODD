from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def sources() -> tuple[str, str]:
    return (
        (ROOT / "web" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.css").read_text(encoding="utf-8"),
    )


def test_settings_signature_covers_all_visible_control_values() -> None:
    script, _ = sources()
    signature = script.split("function settingsRenderStateSignature", 1)[1].split(
        "function syncSettingsDialogStatus", 1
    )[0]

    for field in (
        "data_scope_id",
        "app.cheatMode",
        "app.freeShop",
        "app.disableFa",
        "app.godMode",
        "app.compactMoney",
        "app.darkMode",
        "app.reverseProfitLossColors",
        "app.showHiddenAttributes",
        "app.unlimitedBetting",
        "app.worldPlayerGender",
        "settings.money_currency",
        "settings.betting_limit_single",
        "settings.betting_limit_parlay",
        "settings.championship_enabled",
        "settings.odds_scope",
        "settings.odds_days",
        "settings.odds_auto_refresh_seconds",
    ):
        assert field in signature


def test_settings_skip_unchanged_control_writes_and_reset_on_account_change() -> None:
    script, _ = sources()
    renderer = script.split("function renderSettingsControls", 1)[1].split(
        "function render()", 1
    )[0]
    main_renderer = script.split("function render()", 1)[1].split(
        "async function maybeShowIntegrityNotice", 1
    )[0]

    assert "if (!force && app.settingsRenderSignature === signature) return;" in renderer
    assert renderer.index("app.settingsRenderSignature === signature") < renderer.index(
        '$("#cheat-mode").checked'
    )
    assert "renderSettingsControls();" in main_renderer
    settings_open = script.split('$("#settings-button").addEventListener', 1)[1].split(
        '$("#apply-saved-account")', 1
    )[0]
    assert "renderSettingsControls({force:true});" in settings_open
    assert 'app.settingsRenderSignature = null;' in script.split(
        "if (accountScopeChanged)", 1
    )[1].split("const favoriteScopeId", 1)[0]


def test_settings_busy_status_is_independent_of_content_signature() -> None:
    script, css = sources()
    status = script.split("function syncSettingsDialogStatus", 1)[1].split(
        "function setSettingsMutationBusy", 1
    )[0]
    renderer = script.split("function renderSettingsControls", 1)[1].split(
        "function render()", 1
    )[0]
    setup = script.split("function ensureSettingsTabs()", 1)[1].split(
        "function renderOperationRecords", 1
    )[0]

    assert renderer.index("syncSettingsDialogStatus();") < renderer.index(
        "app.settingsRenderSignature === signature"
    )
    assert 'dialog?.setAttribute("aria-busy", String(busy));' in status
    assert 'content?.setAttribute("aria-busy", String(busy));' in status
    assert 'status.textContent = mutationBusy ? uiText("settings.saving") : uiText("settings.refreshing");' in status
    assert 'status.setAttribute("role", "status");' in setup
    assert 'status.setAttribute("aria-live", "polite");' in setup
    assert '.settings-sync-status' in css
    assert '#settings-dialog button:focus-visible' in css


def test_async_settings_controls_publish_busy_and_restore_failed_values() -> None:
    script, _ = sources()
    handlers = script.split('$("#save-betting-limits").addEventListener', 1)[1].split(
        '$("[data-cheat-clear]")', 1
    )[0]

    for key in ("betting-limits", "disable-fa", "money-currency", "god-mode", "championship", "odds-reading"):
        assert f'setSettingsMutationBusy("{key}", true);' in handlers
        assert f'setSettingsMutationBusy("{key}", false);' in handlers
    assert handlers.count('setAttribute("aria-busy", "true")') >= 4
    assert handlers.count("renderSettingsControls({force:true});") >= 3
    assert "const refreshBusy = Boolean(app.state?.refreshing || app.state?.reconciling);" in handlers


def test_settings_backend_remains_authoritative() -> None:
    script, _ = sources()

    for route in (
        "/api/settings/betting-limits",
        "/api/settings/money-currency",
        "/api/settings/championship",
        "/api/settings/odds-reading",
    ):
        assert route in script


def test_market_settings_controls_do_not_overlap_custom_selects() -> None:
    _, styles = sources()

    assert ".settings-content .odds-reading-setting { display:grid; grid-template-columns:minmax(0,1fr) auto;" in styles
    assert ".odds-reading-fields { min-width:0; display:grid;" in styles
    assert ".odds-reading-fields label { min-width:0; align-self:end; display:grid; grid-template-rows:auto 38px;" in styles
    assert ".odds-reading-fields .fmodd-select,.odds-reading-fields .fmodd-select-trigger { height:38px; min-height:38px; }" in styles
    assert ".odds-reading-setting > button {" in styles
    assert ".odds-reading-setting button {" not in styles


def test_player_name_localization_control_only_appears_for_simplified_chinese() -> None:
    script, _ = sources()
    availability = script.split("function playerNameLocalizationAvailable", 1)[1].split(
        "function syncSettingsDialogStatus", 1
    )[0]
    renderer = script.split("function renderSettingsControls", 1)[1].split(
        "function shellDomIndex", 1
    )[0]

    assert '=== "zh-CN"' in availability
    control = script.split("function ensurePlayerNameLocalizationControl", 1)[1].split(
        "function ensurePortraitSourceControl", 1
    )[0]
    assert 'row.hidden = activeUiLocale() !== "zh-CN";' in control
    assert 'id="player-name-localization" checked' not in control
    assert "playerNameLocalizationSetting.hidden = !playerNameLocalizationVisible;" in renderer
    assert "playerNameLocalizationVisible && settings.player_name_localization !== false" in renderer


def test_cjk_interface_font_controls_separate_locale_and_latin_glyphs() -> None:
    script, _ = sources()
    config = script.split("const LOCALE_FONT_CONFIGS", 1)[1].split(
        "const FONT_SIZE_ADJUST", 1
    )[0]
    controls = script.split("function syncLocaleFontControls", 1)[1].split(
        "function ensureFontControls", 1
    )[0]

    for locale in ("en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"):
        assert f'"{locale}": {{selectId:' in config
    english = config.split('"en-GB":', 1)[1].split("},", 1)[0]
    assert 'selectId:"english-font"' in english
    assert "yahei-ui" not in english
    assert '"zh-CN": {selectId:"chinese-font"' in config
    assert '"zh-TW": {selectId:"chinese-font"' in config
    assert '"ko-KR": {selectId:"chinese-font"' in config
    assert '"ja-JP": {selectId:"chinese-font"' in config
    assert 'CJK_FONT_LOCALES.has(locale) && selectId === "english-font"' in controls
    assert 'const fontLocale = isLatinControl ? "en-GB" : locale;' in controls
    assert 'isLatinControl ? "settings.font.latin_and_numbers"' in controls
    assert "label.hidden = !active;" in controls
    assert "labelText.dataset.i18n = labelKey;" in controls

    apply_font = script.split("function applyLocaleFontPreference", 1)[1].split(
        "function storedFontSizeStep", 1,
    )[0]
    latin_first = '"var(--fmodd-english-render-font, var(--fmodd-english-font)), var(--fmodd-english-font), var(--fmodd-locale-font)'
    assert 'const latinKey = locale === "en-GB" ? normalized : storedLocaleFontKey("en-GB");' in apply_font
    assert latin_first in apply_font
    assert "root.dataset.interfaceLatinFont = latinKey;" in apply_font


def test_korean_and_japanese_font_controls_hide_missing_faces_and_keep_distinct_choices() -> None:
    script, styles = sources()
    availability = script.split("const LOCALE_FONT_AVAILABILITY_SAMPLES", 1)[1].split(
        "function localeFontStorageKey", 1,
    )[0]
    controls = script.split("function syncLocaleFontControls", 1)[1].split(
        "function ensureFontControls", 1,
    )[0]

    assert '"arial-unicode-ms": {source:\'"Arial Unicode MS"\'' in script
    assert '"noto-serif-jp": {source:\'"Noto Serif JP"\'' in script
    assert '"ko-KR": "한글 글꼴 선택"' in availability
    assert '"ja-JP": "日本語フォント選択"' in availability
    assert 'fontRasterSignature(`${source},${fallback}`, sample)' in availability
    assert "fallback !== candidate" in availability
    assert "availableLocaleFontKeys(fontLocale)" in controls
    assert "availableKeys.map((fontKey)" in controls
    assert "button, input, select, textarea { font:inherit; }" in styles


def test_interface_font_preferences_are_persisted_per_locale() -> None:
    script, _ = sources()

    assert 'FONT_STORAGE_PREFIX = "fmodd-interface-font-"' in script
    assert 'return `${FONT_STORAGE_PREFIX}${locale}`;' in script
    assert "localStorage.setItem(localeFontStorageKey(fontLocale), key);" in script
    assert "Object.keys(LOCALE_FONT_CONFIGS).forEach" in script
    assert "applyLocaleFontPreference(locale, fontKey);" in script
