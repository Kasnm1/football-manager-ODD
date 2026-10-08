from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tools.training_ground import FACILITIES


ROOT = Path(__file__).resolve().parents[1]


def facilities_probe() -> dict[str, object]:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.facilities.js", "utf8"), context);
const module = context.window.FMODDI18nModules[0];
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const i18n = context.window.FMODDI18n;
const keys = Object.fromEntries(Object.entries(module.messages).map(([locale, catalog]) => [locale, Object.keys(catalog).sort()]));
i18n.setLocale("en-GB", {persist:false, root:null});
const english = [i18n.t("canteen.ban_all_confirm", {count:4}), i18n.tp("canteen.people", 2), i18n.translateLegacy("解锁建设")];
i18n.setLocale("ko-KR", {persist:false, root:null});
const korean = [i18n.t("canteen.ban_all_confirm", {count:4}), i18n.tp("canteen.people", 2), i18n.translateLegacy("解锁建设")];
console.log(JSON.stringify({keys, messages:module.messages, english, korean}));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def placeholders(value: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def test_facility_view_labels_are_available_in_every_supported_locale() -> None:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const html = fs.readFileSync(root + "/web/index.html", "utf8");
const sources = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => match[1]);
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
for (const source of sources) {
  if (!source.startsWith("/i18n") || source === "/i18n.js") continue;
  vm.runInContext(fs.readFileSync(root + "/web" + source, "utf8"), context, {filename:source});
}
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const locales = ["en-GB","zh-CN","zh-TW","ko-KR","de-DE","es-ES","fr-FR","ru-RU","ja-JP","pt-BR","pt-PT"];
const labels = {};
for (const locale of locales) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  labels[locale] = ["facilities.view.compact"].map((key) => context.window.FMODDI18n.t(key));
}
console.log(JSON.stringify(labels));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    labels = json.loads(result.stdout)
    expected_keys = {"facilities.view.compact"}
    assert set(labels) == {"en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"}
    for locale, values in labels.items():
        assert all(value and value not in expected_keys for value in values), (locale, values)


def test_facilities_catalog_has_locale_and_placeholder_parity() -> None:
    output = facilities_probe()
    keys = output["keys"]
    assert keys["en-GB"] == keys["zh-CN"] == keys["ko-KR"]
    for key in keys["en-GB"]:
        variants = [output["messages"][locale][key] for locale in ("en-GB", "zh-CN", "ko-KR")]
        assert placeholders(variants[0]) == placeholders(variants[1]) == placeholders(variants[2]), key
    assert output["english"] == [
        "Deny all 4 first-team players access to the canteen for 3 days? Each decision can be revoked.",
        "2 people",
        "Unlock construction",
    ]
    assert output["korean"] == [
        "현재 1군 선수 4명의 식당 출입을 3일간 금지할까요? 각 결정을 철회할 수 있습니다.",
        "2명",
        "건설 잠금 해제",
    ]


def test_counselling_roster_copy_and_unlock_confirmation_are_localised() -> None:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const html = fs.readFileSync(root + "/web/index.html", "utf8");
const sources = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => match[1]);
const context = {window:{FMODDI18nModules:[]}, document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}}, NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {}};
vm.createContext(context);
for (const source of sources) {
  if (!source.startsWith("/i18n") || source === "/i18n.js") continue;
  vm.runInContext(fs.readFileSync(root + "/web" + source, "utf8"), context, {filename:source});
}
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context);
const locales = ["en-GB","zh-CN","zh-TW","ko-KR","de-DE","es-ES","fr-FR","ru-RU","ja-JP","pt-BR","pt-PT"];
const messages = {};
for (const locale of locales) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  messages[locale] = {
    description: context.window.FMODDI18n.t("activity.counselling_roster_description"),
    action: context.window.FMODDI18n.t("activity.start_counselling"),
    confirmation: context.window.FMODDI18n.t("activity.confirm_counselling_unlock", {price:"£100K", player:"Alex"}),
  };
}
console.log(JSON.stringify(messages));
'''
    result = subprocess.run(
        ["node", "-e", probe, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    messages = json.loads(result.stdout)
    assert messages["zh-CN"] == {
        "description": "选择有不满的球员进行心理辅导",
        "action": "开始谈话",
        "confirmation": "确认支付 £100K 为 Alex 解锁必定清除不满？",
    }
    for locale, copy in messages.items():
        assert copy["description"] and copy["description"] != "activity.counselling_roster_description", locale
        assert copy["action"] and copy["action"] != "activity.start_counselling", locale
        assert "£100K" in copy["confirmation"] and "Alex" in copy["confirmation"], locale
        assert "\n" not in copy["confirmation"], locale

    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    metadata = script.split("activityMeta.psychological_counseling =", 1)[1].split(";", 1)[0]
    assert 'action:uiText("activity.start_counselling")' in metadata


def test_career_discussion_room_uses_the_requested_name() -> None:
    output = facilities_probe()
    messages = output["messages"]
    traditional = (ROOT / "web" / "i18n.tw.js").read_text(encoding="utf-8")

    assert messages["en-GB"]["activity.node_retirement"] == "Career discussion room"
    assert messages["zh-CN"]["activity.node_retirement"] == "生涯交流室"
    assert messages["ko-KR"]["activity.node_retirement"] == "커리어 대화실"
    assert '"activity.node_retirement": "生涯交流室"' in traditional


def test_black_room_description_uses_the_short_random_injury_copy() -> None:
    output = facilities_probe()
    messages = output["messages"]

    assert messages["en-GB"]["activity.black_room_description"] == "Add a random injury to the player"
    assert messages["zh-CN"]["activity.black_room_description"] == "为球员添加随机伤病"
    assert messages["ko-KR"]["activity.black_room_description"] == "선수에게 무작위 부상을 추가합니다"


def test_migrated_facility_renderers_use_semantic_messages_with_bounded_data_allowlist() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for key in (
        'uiPlural("canteen.people", players.length)',
        'uiText("canteen.banned_remaining", {days:remaining})',
        'uiText("canteen.per_game_day"',
        'uiText("canteen.open", {name:uiText(plan.nameKey)})',
        'uiText("canteen.ban_all_confirm", {count:players.length})',
        'uiText("training.no_available_position_players", {position:restrictedPositionLabel})',
        'uiText("training.active_attribute_status"',
        'uiText("training.estimated_over_pa"',
        'uiPlural("training.position_roster_count", filteredPlayers.length)',
        'uiText(group.labelKey)',
        'uiText("activity.floor_entertainment_eyebrow")',
        'uiText("activity.floor_talk_room_eyebrow")',
        'uiText("activity.floor_media_eyebrow")',
        'uiText("activity.floor_international_eyebrow")',
        'uiText("activity.floor_intelligence_eyebrow")',
    ):
        assert key in script

    for literal in (
        'eyebrow:"PLAYER LOUNGE"',
        'eyebrow:"PRIVATE TALKS"',
        'eyebrow:"MEDIA CENTRE"',
        'eyebrow:"INTERNATIONAL DESK"',
        'eyebrow:"MATCH INTELLIGENCE"',
    ):
        assert literal not in script

    # These three migrated renderers may retain only API/user values (names,
    # descriptions, and fixture data) and stable CSS/data attributes. None is
    # a Chinese UI literal, so the explicit allowlist is intentionally empty.
    allowed_game_or_user_data: tuple[str, ...] = ()
    for function in ("canteenRosterPanel", "renderHospital", "renderInventory"):
        body = re.search(rf"function {function}\([^)]*\) \{{(.*?)(?=\nfunction |\Z)", script, re.S)
        assert body, function
        residual = re.findall(r"[一-龥]+", body.group(1))
        assert residual == list(allowed_game_or_user_data), (function, residual)


def test_training_ground_catalog_and_renderers_use_current_facility_semantic_keys() -> None:
    output = facilities_probe()
    expected = {
        f"training.facility.{sku}.{field}"
        for sku in FACILITIES
        for field in ("name", "description")
    }
    assert expected
    for locale in ("en-GB", "zh-CN", "ko-KR"):
        assert expected <= set(output["keys"][locale]), locale
        assert all(str(output["messages"][locale][key]).strip() for key in expected), locale

    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for call in (
        "trainingFacilityName(item.sku,item)",
        "trainingFacilityDescription(item.sku,item)",
        "trainingFacilityDescription(facility.sku,item)",
        "trainingFacilityName(facility.sku,product)",
        "trainingArchiveFacilityLabel(details.facility_sku,details.facility_name)",
    ):
        assert call in script
    for bypass in (
        "<strong>${escapeHtml(item.name)}</strong>",
        "<small>${escapeHtml(item.description)}</small>",
        "<span>${escapeHtml(product.name || facility.sku)}</span>",
    ):
        assert bypass not in script
