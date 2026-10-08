from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACK_LOCALES = (
    "zh-TW", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT",
)
PACK_SOURCES = {
    "zh-TW": "/i18n.tw.js",
    "de-DE": "/i18n.de.js",
    "es-ES": "/i18n.es.js",
    "fr-FR": "/i18n.fr.js",
    "ru-RU": "/i18n.ru.js",
    "ja-JP": "/i18n.ja.js",
    "pt-BR": "/i18n.pt-BR.js",
    "pt-PT": "/i18n.pt-PT.js",
}


def _probe() -> dict[str, object]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const html = fs.readFileSync(root + "/web/index.html", "utf8");
const sources = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => match[1]);
const context = {
  window:{FMODDI18nModules:[], FMODDLocalePacks:{}},
  document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}},
  NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {},
};
vm.createContext(context);
for (const source of sources) {
  if (!source.startsWith("/i18n") || source === "/i18n.js") continue;
  vm.runInContext(fs.readFileSync(root + "/web" + source, "utf8"), context, {filename:source});
}
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context, {filename:"/i18n.js"});
const locales = ["zh-TW", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const keys = Object.fromEntries(["en-GB", ...locales].map((locale) => [locale, context.window.FMODDI18n.catalogKeys(locale)]));
const placeholders = {};
for (const locale of locales) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  placeholders[locale] = Object.fromEntries(keys["en-GB"].map((key) => [
    key,
    [...context.window.FMODDI18n.t(key).matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((match) => match[1]).sort(),
  ]));
}
context.window.FMODDI18n.setLocale("en-GB", {persist:false, root:null});
placeholders["en-GB"] = Object.fromEntries(keys["en-GB"].map((key) => [
  key,
  [...context.window.FMODDI18n.t(key).matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g)].map((match) => match[1]).sort(),
]));
const qualityKeys = [
  "dialog.manual.subtitle",
  "settings.cheats.god_mode.note",
  "inventory.use_subtitle_flu",
  "owned.youth.son_note",
  "credit.terms.interest",
  "relationship.room.guidance_rule",
  "shop.confirm_purchase",
  "shop.attribute_note",
  "bank.sugar_daddy.confirm",
];
const reviewedUiKeys = [
  "wallet.title",
  "league.rank",
  "history.card_view",
  "calendar.weekday.sun",
  "calendar.weekday.mon",
  "sponsor.annual_fee",
  "credit.attribute.passing",
  "credit.attribute.vision",
  "world.minimum",
  "world.maximum",
  "training.graduating",
  "training.studying",
  "activity.participate",
  "activity.counselling",
  "world.sort_by",
  "legacy.profile.stats.career",
  "world.rebuilding",
  "world.unnamed_club",
  "portfolio.finances",
  "portfolio.expand",
  "portfolio.collapse",
  "relations.reason.siblings",
  "relations.reason.children",
  "relations3d.bg.section.lighting",
  "relations3d.bg.section.banner",
  "relationship.role.scientist",
  "training.archive.facility.treadmill",
  "legacy.moment.tag.bicycle_kick",
  "mail.correspondent.referee_fallback",
  "bank.credit_rules",
];
const samples = {"en-GB":Object.fromEntries(qualityKeys.map((key) => [key, context.window.FMODDI18n.t(key)]))};
const englishReviewedUi = Object.fromEntries(reviewedUiKeys.map((key) => [key, context.window.FMODDI18n.t(key)]));
const englishLeaks = {};
const untranslatedReviewedUi = {};
const allLocales = ["en-GB", "zh-CN", "ko-KR", ...locales];
const itemContractKeys = keys["en-GB"].filter((key) => (
  /^item\.[^.]+\.(?:name|description)$/.test(key)
  || /^item\.(?:tier|family|note)\./.test(key)
));
const rawCatalogs = Object.fromEntries(allLocales.map((locale) => [locale, Object.assign(
  {},
  ...context.window.FMODDI18nModules.map((module) => module.messages?.[locale] || {}),
  context.window.FMODDLocalePacks[locale]?.messages || {},
)]));
const governanceKeys = [
  "integrity.committee",
  "mail.type.integrity.body",
  "settings.cheats.disable_fa",
];
const governanceCopy = Object.fromEntries(allLocales.map((locale) => [
  locale,
  Object.fromEntries(governanceKeys.map((key) => [key, rawCatalogs[locale][key]])),
]));
const compactActionKeys = ["portfolio.more_data", "portfolio.collapse_data", "portfolio.card.relative_to_price"];
const compactActionCopy = Object.fromEntries(allLocales.map((locale) => [
  locale,
  Object.fromEntries(compactActionKeys.map((key) => [key, rawCatalogs[locale][key]])),
]));
const outrightKeys = [...new Set([
  ...keys["en-GB"].filter((key) => key.startsWith("championship.")),
  "bet.championship",
  "betting.outrights",
  "betting.outrights_singles_only",
  "betting.championship_added",
  "betting.championship_mix",
  "betting.championship_only",
  "refresh.stage.championship_markets",
  "settings.markets.outrights",
  "settings.markets.outrights_note",
  "settings.markets.outrights_enabled",
  "settings.markets.outrights_disabled",
  "refund.description",
])];
const outrightCopy = Object.fromEntries(allLocales.map((locale) => [
  locale,
  Object.fromEntries(outrightKeys.map((key) => [key, rawCatalogs[locale][key]])),
]));
const englishRawKeys = keys["en-GB"];
const rawKeyDiffs = Object.fromEntries(locales.map((locale) => {
  const localeKeys = new Set(Object.keys(rawCatalogs[locale]));
  const englishKeys = new Set(englishRawKeys);
  return [locale, {
    missing: englishRawKeys.filter((key) => !localeKeys.has(key)),
    extra: [...localeKeys].filter((key) => !englishKeys.has(key)).sort(),
  }];
}));
const missingItemKeys = Object.fromEntries(allLocales.map((locale) => [
  locale,
  itemContractKeys.filter((key) => !String(rawCatalogs[locale][key] || "").trim()),
]));
const englishFunctionWords = /\b(?:the|this|that|these|those|with|without|before|after|while|when|from|into|only|cannot|could|would|should|will|has|have|was|were|are|and)\b/i;
const bettingMarketDomain = /^(?:bet|betting|championship|history|manager|market|operations|refresh|request|settings|shell|slip|topbar)\./;
const bettingMarketKeys = keys["en-GB"].filter((key) => (
  bettingMarketDomain.test(key)
  && (/market/.test(key) || /\bmarkets?\b/i.test(rawCatalogs["en-GB"][key] || ""))
));
const bettingMarketCopy = {};
for (const locale of ["zh-CN", "zh-TW", "ko-KR", "ja-JP"]) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  bettingMarketCopy[locale] = Object.fromEntries(
    bettingMarketKeys.map((key) => [key, context.window.FMODDI18n.t(key)]),
  );
}
for (const locale of locales) {
  const pack = context.window.FMODDLocalePacks[locale]?.messages || {};
  samples[locale] = Object.fromEntries(qualityKeys.map((key) => [key, pack[key] || null]));
  untranslatedReviewedUi[locale] = reviewedUiKeys.filter((key) => pack[key] === englishReviewedUi[key]);
  englishLeaks[locale] = Object.entries(pack)
    .filter(([, value]) => englishFunctionWords.test(
      String(value).replace(/\{[A-Za-z_][A-Za-z0-9_]*\}/g, ""),
    ))
    .map(([key, value]) => [key, value]);
}
console.log(JSON.stringify({sources, keys, placeholders, samples, englishLeaks, untranslatedReviewedUi, itemContractKeys, missingItemKeys, rawKeyDiffs, bettingMarketCopy, governanceCopy, compactActionCopy, outrightCopy}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_extension_locale_packs_load_before_core_and_cover_the_english_catalog() -> None:
    output = _probe()
    sources = output["sources"]
    core_index = sources.index("/i18n.js")
    expected_keys = output["keys"]["en-GB"]
    assert expected_keys
    for locale in PACK_LOCALES:
        source = PACK_SOURCES[locale]
        assert sources.index(source) < core_index
        assert output["keys"][locale] == expected_keys
        assert output["rawKeyDiffs"][locale] == {"missing": [], "extra": []}


def test_extension_locale_pack_placeholders_match_authoritative_english() -> None:
    output = _probe()
    english = output["placeholders"]["en-GB"]
    for locale in PACK_LOCALES:
        assert output["placeholders"][locale] == english


def test_extension_locale_pack_long_sentences_are_real_target_language_copy() -> None:
    output = _probe()
    english = output["samples"]["en-GB"]
    source_script = re.compile(r"[\u3400-\u9fff\uac00-\ud7a3\u3040-\u30ff]")
    simplified_only = re.compile(r"[这为个们类项显关开门务处续进过实样层场员资户认备览网边变将团总从与选时还没发应]")
    for locale in ("de-DE", "es-ES", "fr-FR", "ru-RU", "pt-BR", "pt-PT"):
        for key, value in output["samples"][locale].items():
            assert value and value != english[key], (locale, key)
            assert not source_script.search(value), (locale, key, value)
            assert "interface:" not in value.casefold(), (locale, key, value)
    for key, value in output["samples"]["ja-JP"].items():
        assert value and value != english[key], ("ja-JP", key)
        assert not simplified_only.search(value), ("ja-JP", key, value)
        assert "日本語表示：" not in value, (key, value)
    for key, value in output["samples"]["zh-TW"].items():
        assert value and value != english[key], ("zh-TW", key)
        assert not simplified_only.search(value), ("zh-TW", key, value)


def test_shop_item_metadata_is_complete_in_every_locale() -> None:
    output = _probe()
    assert len(output["itemContractKeys"]) == 81
    assert output["missingItemKeys"] == {
        locale: [] for locale in ("en-GB", "zh-CN", "ko-KR", *PACK_LOCALES)
    }
    architecture = (ROOT / "docs" / "I18N_ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "物品以稳定 SKU 识别" in architecture
    assert "物品的名称、描述、等级和效果提示" in architecture


def test_extension_locale_packs_do_not_generate_or_duplicate_fake_copy() -> None:
    banned_tokens = (
        "semanticFallback",
        "familyRules",
        "byFamily(",
        "phraseRules",
        "wordRules",
        "translateLegacyText",
        "日本語表示：",
        "Русский интерфейс:",
        "данные готовы",
        "элемент элемент",
        "История клуба: {",
        "Активности: {",
        "Тренировки: {",
        "Отношения: {",
        "Мир: {",
        "Ставки: {",
        "Daten bereit",
        "datos listos",
    )
    key_pattern = re.compile(r'"([a-z][A-Za-z0-9_.-]+)"\s*:')
    source_names = {
        locale: source.removeprefix("/i18n.").removesuffix(".js")
        for locale, source in PACK_SOURCES.items()
    }
    for locale in PACK_LOCALES:
        source = (ROOT / "web" / f"i18n.{source_names[locale]}.js").read_text(encoding="utf-8")
        for token in banned_tokens:
            assert token not in source, (locale, token)
        keys = key_pattern.findall(source)
        duplicates = sorted(key for key in set(keys) if keys.count(key) > 1)
        assert duplicates == [], (locale, duplicates[:20])
        if locale == "ru-RU":
            assert source.count("элемент") <= 20


def test_extension_locale_packs_have_no_english_function_word_fallbacks() -> None:
    output = _probe()
    assert output["englishLeaks"] == {locale: [] for locale in PACK_LOCALES}


def test_extension_locale_packs_translate_reviewed_short_ui_labels() -> None:
    output = _probe()
    assert output["untranslatedReviewedUi"] == {locale: [] for locale in PACK_LOCALES}


def test_football_association_copy_uses_each_locales_governing_body_term() -> None:
    output = _probe()
    expected_terms = {
        "en-GB": ("Football Association Disciplinary Committee", "The Football Association", "Disable FA penalties"),
        "zh-CN": ("足协纪律委员会", "足协已作出", "关闭足协"),
        "zh-TW": ("足協紀律委員會", "足協已作出", "關閉足協"),
        "ko-KR": ("축구협회 징계위원회", "축구협회가", "축구협회 징계 비활성화"),
        "de-DE": ("Disziplinarausschuss des Fußballverbands", "Der Fußballverband", "Sanktionen des Fußballverbands deaktivieren"),
        "es-ES": ("Comité Disciplinario de la Federación de Fútbol", "La Federación de Fútbol", "Desactivar las sanciones de la Federación de Fútbol"),
        "fr-FR": ("Commission de discipline de la Fédération de football", "La Fédération de football", "Désactiver les sanctions de la Fédération de football"),
        "ru-RU": ("Дисциплинарный комитет Футбольной федерации", "Футбольная федерация", "Отключить санкции Футбольной федерации"),
        "ja-JP": ("サッカー協会懲戒委員会", "サッカー協会が", "サッカー協会による処分を無効化"),
        "pt-BR": ("Comitê Disciplinar da Federação de Futebol", "A Federação de Futebol", "Desativar sanções da Federação de Futebol"),
        "pt-PT": ("Conselho de Disciplina da Federação de Futebol", "A Federação de Futebol", "Desativar sanções da Federação de Futebol"),
    }
    keys = (
        "integrity.committee",
        "mail.type.integrity.body",
        "settings.cheats.disable_fa",
    )
    for locale, expected in expected_terms.items():
        actual = output["governanceCopy"][locale]
        for key, fragment in zip(keys, expected, strict=True):
            assert fragment in actual[key], (locale, key, actual[key])


def test_owned_club_disclosure_actions_are_compact_and_natural() -> None:
    output = _probe()
    expected = {
        "en-GB": ("More", "Less", "vs purchase price"),
        "zh-CN": ("更多", "收起", "相较收购价"),
        "zh-TW": ("更多", "收起", "相較收購價"),
        "ko-KR": ("더보기", "접기", "인수가 대비"),
        "de-DE": ("Mehr", "Weniger", "ggü. Kaufpreis"),
        "es-ES": ("Más", "Menos", "vs. precio de compra"),
        "fr-FR": ("Plus", "Moins", "vs prix d’achat"),
        "ru-RU": ("Подробнее", "Свернуть", "От цены покупки"),
        "ja-JP": ("詳細", "折りたたむ", "買収価格比"),
        "pt-BR": ("Mais", "Menos", "vs. preço de compra"),
        "pt-PT": ("Mais", "Menos", "vs. preço de compra"),
    }
    for locale, (more, less, relative_to_price) in expected.items():
        assert output["compactActionCopy"][locale] == {
            "portfolio.more_data": more,
            "portfolio.collapse_data": less,
            "portfolio.card.relative_to_price": relative_to_price,
        }


def test_traditional_chinese_pack_uses_taiwanese_ui_and_football_terms() -> None:
    output = _probe()
    source = (ROOT / "web" / "i18n.tw.js").read_text(encoding="utf-8")
    message_source = source.split("const messages = Object.freeze({", 1)[1].split(
        "\n  });\n  const legacy", 1
    )[0]
    assert output["keys"]["zh-TW"] == output["keys"]["en-GB"]
    for expected in (
        '"settings.locale.option.tw": "繁體中文"',
        '"settings.currency.label": "貨幣"',
        '"position.gk": "守門員（GK）"',
        '"selection.draw": "和局"',
        '"betting.parlay_label": "過關"',
        '"bank.loading_note": "合併銀行與錢包交易明細…"',
        '"settings.font.size": "介面字級"',
    ):
        assert expected in source
    for mainland in (
        "简体", "软件", "文件夹", "加载", "设置", "默认", "缓存", "搜索", "连接",
        "门将", "赔率", "傢俱樂部", "質量", "反饋", "實時", "合同", "後臺", "型別",
        "校驗", "響應", "運營", "本地服務",
    ):
        assert mainland not in message_source


def test_east_asian_betting_market_terms_are_localized_by_context() -> None:
    copy = _probe()["bettingMarketCopy"]
    assert copy["ja-JP"]["history.bet_item"] == "ベット種別：{market}"
    assert copy["ko-KR"]["history.bet_item"] == "베팅 항목: {market}"
    assert copy["zh-CN"]["request.unsupported_market"] == "不支持的投注玩法：{market}"
    assert copy["zh-TW"]["request.unsupported_market"] == "不支援的投注玩法：{market}"
    assert not any("市場" in value for value in copy["ja-JP"].values())
    assert not any(
        term in value
        for value in copy["ko-KR"].values()
        for term in ("시장", "마켓")
    )


def test_outright_betting_copy_uses_reviewed_football_terms_in_every_locale() -> None:
    copy = _probe()["outrightCopy"]
    preferred_terms = {
        "en-GB": "Outright",
        "zh-CN": "冠军盘",
        "zh-TW": "冠軍盤",
        "ko-KR": "우승팀 베팅",
        "de-DE": "Turniersieger",
        "es-ES": "apuestas al ganador",
        "fr-FR": "paris sur le vainqueur",
        "ru-RU": "ставки на победителя",
        "ja-JP": "優勝予想",
        "pt-BR": "apostas no vencedor",
        "pt-PT": "apostas no vencedor",
    }
    for locale, preferred in preferred_terms.items():
        values = copy[locale]
        assert all(values.values()), (locale, values)
        assert preferred.casefold() in values["betting.outrights"].casefold(), (
            locale,
            values["betting.outrights"],
        )

    banned_terms = {
        "ko-KR": ("우승 시장", "아웃라이트"),
        "de-DE": ("outright", "siegermarkt"),
        "es-ES": ("outright", "mercados de ganador"),
        "fr-FR": ("outright", "marchés de vainqueur"),
        "ru-RU": ("прямые рынки", "рынки победителей"),
        "ja-JP": ("優勝オッズ", "優勝ベット"),
        "pt-BR": ("mercados definitivos", "mercados de vencedor"),
        "pt-PT": ("mercados definitivos", "mercados de vencedor"),
    }
    for locale, banned in banned_terms.items():
        visible_copy = "\n".join(copy[locale].values()).casefold()
        assert not any(term.casefold() in visible_copy for term in banned), (locale, visible_copy)

    reviewed_copy = {
        "es-ES": {
            "championship.cup_period": "Comienza el {date} · {count} equipos",
            "championship.final_settlement": "Fecha de liquidación final: {date}",
            "championship.league_settlement": "Fecha de liquidación de la liga: {date}",
            "championship.locked": "Campeón confirmado · apuestas cerradas",
            "championship.schedule_pending": "Calendario por confirmar",
            "championship.team_choice": "Selección del campeón de {competition}",
        },
        "fr-FR": {
            "championship.cup_period": "Début le {date} · {count} équipes",
            "championship.final_settlement": "Date du règlement final : {date}",
            "championship.league_settlement": "Date de règlement du championnat : {date}",
            "championship.team_choice": "Sélection du vainqueur de {competition}",
        },
        "ru-RU": {
            "championship.crowned": "Победитель определён",
            "championship.league_period": "с {start} по {end}",
        },
        "pt-BR": {
            "championship.cup_period": "Início em {date} · {count} equipes",
            "championship.schedule_pending": "Calendário a confirmar",
            "championship.team_choice": "Seleção do campeão de {competition}",
        },
        "pt-PT": {
            "championship.cup_period": "Início em {date} · {count} equipas",
            "championship.schedule_pending": "Calendário por confirmar",
            "championship.team_choice": "Seleção do campeão de {competition}",
        },
    }
    for locale, expected in reviewed_copy.items():
        assert {key: copy[locale][key] for key in expected} == expected
