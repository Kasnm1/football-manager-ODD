from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tools.club_reader import STAFF_JOB_TYPES
from tools.club_vision import BOARD_IMPORTANCE_OPTIONS, CLUB_VISION_TYPE_NAMES
from tools.initial_data_audit import NATION_NAMES
from tools.preferred_moves import FM24_VERIFIED_PREFERRED_MOVES
from tools.training_ground import FACILITIES


ROOT = Path(__file__).resolve().parents[1]
LOCALES = (
    "en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR",
    "ru-RU", "ja-JP", "pt-BR", "pt-PT",
)


def _catalogs() -> dict[str, dict[str, str]]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const locales = ["en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const extensionLocales = locales.filter((locale) => !["en-GB", "zh-CN", "ko-KR"].includes(locale));
const context = {window:{FMODDI18nModules:[],FMODDLocalePacks:Object.fromEntries(extensionLocales.map((locale) => [locale,{messages:{},legacy:{}}]))}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.enums.js", "utf8"), context);
const moduleMessages = context.window.FMODDI18nModules[0].messages;
console.log(JSON.stringify(Object.fromEntries(locales.map((locale) => [locale,
  moduleMessages[locale] || Object.fromEntries(Object.entries(context.window.FMODDLocalePacks[locale].messages).filter(([key]) => !/^(?:nation|continent|training\.attribute(?:_group)?|relationship\.event|home\.connection|club\.nature|stadium\.(?:state|pitch_type))\./.test(key)))
]))));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _nation_catalogs() -> dict[str, dict[str, str]]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const locales = ["en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const extensionLocales = locales.filter((locale) => !["en-GB", "zh-CN", "ko-KR"].includes(locale));
const context = {window:{FMODDI18nModules:[],FMODDLocalePacks:Object.fromEntries(extensionLocales.map((locale) => [locale,{messages:{},legacy:{}}]))}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.enums.js", "utf8"), context);
const moduleMessages = context.window.FMODDI18nModules[1].messages;
console.log(JSON.stringify(Object.fromEntries(locales.map((locale) => [locale,
  moduleMessages[locale] || Object.fromEntries(Object.entries(context.window.FMODDLocalePacks[locale].messages).filter(([key]) => /^(?:nation|continent)\./.test(key)))
]))));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _training_attribute_catalogs() -> dict[str, object]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const locales = ["en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const extensionLocales = locales.filter((locale) => !["en-GB", "zh-CN", "ko-KR"].includes(locale));
const context = {window:{FMODDI18nModules:[],FMODDLocalePacks:Object.fromEntries(extensionLocales.map((locale) => [locale,{messages:{},legacy:{}}]))}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.enums.js", "utf8"), context);
const moduleMessages = context.window.FMODDI18nModules[2].messages;
const catalogs = Object.fromEntries(locales.map((locale) => [locale,
  moduleMessages[locale] || Object.fromEntries(Object.entries(context.window.FMODDLocalePacks[locale].messages).filter(([key]) => /^training\.attribute(?:_group)?\./.test(key)))
]));
console.log(JSON.stringify({
  catalogs,
  sourceKeys:context.window.FMODDTrainingAttributeSourceKeys,
  groupSourceKeys:context.window.FMODDTrainingAttributeGroupSourceKeys,
}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _relationship_event_catalogs() -> dict[str, dict[str, str]]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const locales = ["en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const extensionLocales = locales.filter((locale) => !["en-GB", "zh-CN", "ko-KR"].includes(locale));
const context = {window:{FMODDI18nModules:[],FMODDLocalePacks:Object.fromEntries(extensionLocales.map((locale) => [locale,{messages:{},legacy:{}}]))}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.enums.js", "utf8"), context);
const moduleMessages = context.window.FMODDI18nModules[3].messages;
console.log(JSON.stringify(Object.fromEntries(locales.map((locale) => [locale,
  moduleMessages[locale] || Object.fromEntries(Object.entries(context.window.FMODDLocalePacks[locale].messages).filter(([key]) => /^relationship\.event\./.test(key)))
]))));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _connection_catalogs() -> dict[str, dict[str, str]]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const locales = ["en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const extensionLocales = locales.filter((locale) => !["en-GB", "zh-CN", "ko-KR"].includes(locale));
const context = {window:{FMODDI18nModules:[],FMODDLocalePacks:Object.fromEntries(extensionLocales.map((locale) => [locale,{messages:{},legacy:{}}]))}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.enums.js", "utf8"), context);
const moduleMessages = context.window.FMODDI18nModules[4].messages;
console.log(JSON.stringify(Object.fromEntries(locales.map((locale) => [locale,
  moduleMessages[locale] || Object.fromEntries(
    Object.entries(context.window.FMODDLocalePacks[locale].messages)
      .filter(([key]) => /^home\.connection\./.test(key))
  )
]))));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _club_information_catalogs() -> dict[str, dict[str, str]]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const locales = ["en-GB", "zh-CN", "zh-TW", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"];
const extensionLocales = locales.filter((locale) => !["en-GB", "zh-CN", "ko-KR"].includes(locale));
const context = {window:{FMODDI18nModules:[],FMODDLocalePacks:Object.fromEntries(extensionLocales.map((locale) => [locale,{messages:{},legacy:{}}]))}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(root + "/web/i18n.enums.js", "utf8"), context);
const moduleMessages = context.window.FMODDI18nModules[5].messages;
console.log(JSON.stringify(Object.fromEntries(locales.map((locale) => [locale,
  moduleMessages[locale] || Object.fromEntries(
    Object.entries(context.window.FMODDLocalePacks[locale].messages)
      .filter(([key]) => /^(?:club\.nature|stadium\.(?:state|pitch_type))\./.test(key))
  )
]))));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def _club_nation_renderer_probe() -> dict[str, str]:
    script = r'''
const fs = require("fs"), vm = require("vm"), root = process.argv[1];
const appSource = fs.readFileSync(root + "/web/app.js", "utf8");
const helper = appSource.slice(
  appSource.indexOf("function localizedNationName"),
  appSource.indexOf("function localizedContinentName"),
);
const labels = {
  "nation.name.765":"England",
  "nation.name.1651":"People's Republic of China",
  "world.not_read":"Not read",
  "world.unnamed_nation":"Unnamed nation",
};
const context = {
  window:{FMODDNationSourceIds:{"英格兰":765,"中国":1651}},
  uiText:(key) => labels[key] || key,
  activeUiLocale:() => "en-GB",
};
vm.createContext(context);
vm.runInContext(helper, context);
console.log(JSON.stringify({
  pending:vm.runInContext('localizedClubNation({nation:"未分类"})', context),
  verified:vm.runInContext('localizedClubNation({nation:"未分类"},{id:1651,name:"中国"})', context),
  indexed:vm.runInContext('localizedClubNation({nation_id:765,nation:"英格兰"})', context),
}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)], check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_dynamic_enum_catalogs_cover_every_supported_locale_and_stable_id() -> None:
    catalogs = _catalogs()
    assert tuple(catalogs) == LOCALES
    expected = {
        *(f"staff.job.{value}" for value in STAFF_JOB_TYPES),
        *(f"training.preferred_move.{value}" for value in FM24_VERIFIED_PREFERRED_MOVES),
        *(f"owned.vision.target.{value}" for value in CLUB_VISION_TYPE_NAMES),
        *(f"owned.vision.importance.{value}" for value, _label in BOARD_IMPORTANCE_OPTIONS),
        *(f"owned.vision.category.{value}" for value in (
            "competition", "finance", "reputation", "recruitment", "youth",
            "style", "stadium", "other",
        )),
    }
    assert all(set(catalogs[locale]) == expected for locale in LOCALES)
    assert all(all(str(value).strip() for value in catalogs[locale].values()) for locale in LOCALES)


def test_korean_dynamic_enum_copy_replaces_reported_chinese_values() -> None:
    korean = _catalogs()["ko-KR"]
    assert korean["staff.job.64"] == "유소년 육성 책임자"
    assert korean["staff.job.88"] == "기술 이사"
    assert korean["staff.job.20"] == "수석 코치"
    assert korean["training.preferred_move.0"] == "왼쪽 측면으로 드리블"
    assert korean["owned.vision.target.40"] == "재정적 자립 달성"
    assert korean["owned.vision.importance.6"] == "어느 정도 중요"
    assert not any(re.search(r"[\u3400-\u9fff\u3040-\u30ff]", value) for value in korean.values())


def test_nation_catalogs_cover_all_fm_ids_and_replace_reported_korean_values() -> None:
    catalogs = _nation_catalogs()
    expected = {
        *(f"nation.name.{identifier}" for identifier in NATION_NAMES),
        *(f"continent.name.{value}" for value in (
            "africa", "asia", "europe", "north_america", "south_america",
            "oceania", "other",
        )),
    }
    assert tuple(catalogs) == LOCALES
    assert all(set(catalogs[locale]) == expected for locale in LOCALES)
    korean = catalogs["ko-KR"]
    assert korean["nation.name.1651"] == "브라질"
    assert korean["nation.name.769"] == "프랑스"
    assert korean["nation.name.771"] == "독일"
    assert korean["nation.name.796"] == "스페인"
    assert korean["nation.name.776"] == "이탈리아"
    assert korean["continent.name.south_america"] == "남아메리카"
    assert korean["continent.name.europe"] == "유럽"
    assert not any(re.search(r"[\u3400-\u9fff\u3040-\u30ff]", value) for value in korean.values())


def test_training_attribute_catalog_covers_every_facility_target_and_locale() -> None:
    output = _training_attribute_catalogs()
    catalogs = output["catalogs"]
    source_keys = output["sourceKeys"]
    expected_sources = {
        key.split(":", 1)[-1]
        for facility in FACILITIES.values()
        for key in facility.get("attributes", [])
    }
    assert set(source_keys) == expected_sources
    expected_keys = {
        *source_keys.values(),
        *output["groupSourceKeys"].values(),
    }
    assert tuple(catalogs) == LOCALES
    assert all(set(catalogs[locale]) == expected_keys for locale in LOCALES)
    assert all(all(str(value).strip() for value in catalogs[locale].values()) for locale in LOCALES)


def test_reported_training_targets_use_reviewed_terms_in_every_locale() -> None:
    catalogs = _training_attribute_catalogs()["catalogs"]
    expected = {
        "en-GB": ("Pace", "Acceleration"),
        "zh-CN": ("速度", "爆发力"),
        "zh-TW": ("速度", "爆發力"),
        "ko-KR": ("주력", "순간 속도"),
        "de-DE": ("Schnelligkeit", "Antritt"),
        "es-ES": ("Velocidad", "Aceleración"),
        "fr-FR": ("Vitesse", "Accélération"),
        "ru-RU": ("Темп", "Ускорение"),
        "ja-JP": ("スピード", "加速力"),
        "pt-BR": ("Ritmo", "Aceleração"),
        "pt-PT": ("Ritmo", "Aceleração"),
    }
    for locale, (pace, acceleration) in expected.items():
        assert catalogs[locale]["training.attribute.pace"] == pace
        assert catalogs[locale]["training.attribute.acceleration"] == acceleration

    source_script = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
    for locale in ("en-GB", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "pt-BR", "pt-PT"):
        assert not any(source_script.search(value) for value in catalogs[locale].values()), locale


def test_relationship_event_codes_have_locale_complete_display_labels() -> None:
    catalogs = _relationship_event_catalogs()
    expected_keys = {
        *(f"relationship.event.role.{value}" for value in (
            "mentor", "coach", "analyst", "scientist", "student", "player",
            "player_a", "player_b", "manager", "activity_target", "counselor",
            "counselee", "trainee",
        )),
        *(f"relationship.event.outcome.{value}" for value in (
            "unlocked", "relieved", "opened_up", "no_effect", "rupture",
        )),
        *(f"relationship.event.source.{value}" for value in (
            "counselling", "staff_drinks", "staff_hot_spring",
            "staff_football_game", "staff_massage",
        )),
    }
    assert tuple(catalogs) == LOCALES
    assert all(set(catalogs[locale]) == expected_keys for locale in LOCALES)
    assert all(all(str(value).strip() for value in catalogs[locale].values()) for locale in LOCALES)
    assert catalogs["en-GB"]["relationship.event.source.counselling"] == "Counselling"
    assert catalogs["en-GB"]["relationship.event.outcome.relieved"] == "Fully relieved"
    assert catalogs["en-GB"]["relationship.event.role.counselee"] == "Counselled player"
    source_script = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
    for locale in ("en-GB", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "pt-BR", "pt-PT"):
        assert not any(source_script.search(value) for value in catalogs[locale].values()), locale


def test_connection_messages_cover_every_status_and_supported_locale() -> None:
    catalogs = _connection_catalogs()
    expected_keys = {
        f"home.connection.{code}"
        for code in (
            "connecting", "connected", "retained", "managed_team_missing",
            "manager_missing", "read_failed", "game_detected", "offline",
        )
    }
    assert tuple(catalogs) == LOCALES
    assert all(set(catalogs[locale]) == expected_keys for locale in LOCALES)
    assert all(
        all(str(value).strip() for value in catalogs[locale].values())
        for locale in LOCALES
    )
    assert catalogs["en-GB"]["home.connection.managed_team_missing"] == (
        "The save was detected, but the current manager’s team has not been "
        "identified yet. Wait for the save to finish loading, then connect again."
    )

    source_script = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
    for locale in (
        "en-GB", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "pt-BR", "pt-PT",
    ):
        assert not any(
            source_script.search(value) for value in catalogs[locale].values()
        ), locale


def test_club_information_enums_are_locale_complete_and_do_not_leak_chinese() -> None:
    catalogs = _club_information_catalogs()
    expected_keys = {
        *(f"club.nature.{code}" for code in (1, 2, 3)),
        *(f"stadium.state.{code}" for code in (1, 2, 6, 11, 16)),
        *(f"stadium.pitch_type.{code}" for code in range(1, 9)),
    }
    assert tuple(catalogs) == LOCALES
    assert all(set(catalogs[locale]) == expected_keys for locale in LOCALES)
    assert all(
        all(str(value).strip() for value in catalogs[locale].values())
        for locale in LOCALES
    )
    english = catalogs["en-GB"]
    assert english["club.nature.1"] == "Professional"
    assert english["stadium.state.1"] == "Very good"
    assert english["stadium.pitch_type.8"] == "Hybrid grass (natural and artificial)"

    source_script = re.compile(r"[\u3400-\u9fff\u3040-\u30ff]")
    for locale in (
        "en-GB", "ko-KR", "de-DE", "es-ES", "fr-FR", "ru-RU", "pt-BR", "pt-PT",
    ):
        assert not any(
            source_script.search(value) for value in catalogs[locale].values()
        ), locale


def test_club_nation_prefers_verified_uid_and_never_mislabels_pending_data() -> None:
    assert _club_nation_renderer_probe() == {
        "pending": "Not read",
        "verified": "People's Republic of China",
        "indexed": "England",
    }


def test_non_cjk_dynamic_enum_catalogs_never_contain_chinese_fallbacks() -> None:
    cjk = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7a3]")
    for catalogs in (_catalogs(), _nation_catalogs()):
        for locale in ("en-GB", "de-DE", "es-ES", "fr-FR", "ru-RU", "pt-BR", "pt-PT"):
            assert not any(cjk.search(value) for value in catalogs[locale].values()), locale


def test_dynamic_renderers_use_stable_enum_ids_instead_of_raw_chinese_labels() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'localizedEnumLabel("staff.job", person?.job_type' in script
    assert "const bit = item?.bit ?? item?.preferred_move_bit" in script
    assert "activeUiLocale() === \"zh-CN\" && source" in script
    assert 'localizedEnumLabel("owned.vision.target", type' in script
    assert "const personDetail = isRosterStaff ? localizedStaffRole(player)" in script
    assert "[Number(item.bit),localizedPreferredMove(item)]" in script
    assert "localizedVisionImportance(option.value)" in script
    assert "localizedVisionTarget(row)" in script
    assert "const nationName = localizedNationName(nation)" in script
    assert "localizedContinentName(row.name)" in script
    assert "localizedContinentName(nation.continent)" in script
    assert "window.FMODDNationSourceIds?.[source]" in script
    assert "window.FMODDTrainingAttributeSourceKeys?.[source]" in script
    assert "localizedTrainingAttribute(selectedTargetKey)" in script
    assert 'escapeHtml(localizedTrainingAttribute(key))}</option>' in script
    assert "localizedTrainingAttributeGroup(group)" in script
    assert "const displayName = localizedTrainingAttribute(row.key || row.name)" in script
    assert "localizedTrainingAttributeGroup(row.group)" in script
    assert "localizedClubNature(status)" in script
    assert "localizedStadiumState(stadium)" in script
    assert "localizedStadiumStateFromRaw(quote.after)" in script
    assert "localizedStadiumPitchType(stadium)" in script
    assert "localizedStadiumPitchType(option.value)" in script
    assert "localizedClubNation(club, metrics.nation)" in script
    assert "localizedClubNation(detail.club || club, info.nation)" in script
    assert "localizedClubNation(detail.club || club, detail.club_information?.nation)" in script
    assert "localizedNationName(club.nation)" not in script
    world_club_information = script.split("function renderWorldClubInformation", 1)[1].split(
        "function ownedClubAction", 1,
    )[0]
    assert "status.name" not in world_club_information
    assert "stadium.state_name" not in world_club_information
    assert "stadium.pitch_type_name" not in world_club_information
    assert 'String(selectedTargetKey).split(":",2).pop()' not in script
    assert 'key.split(":",2)[1]' not in script
    assert "/i18n.enums.js" in (ROOT / "web" / "index.html").read_text(encoding="utf-8")
