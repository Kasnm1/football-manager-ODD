from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCALES = ("en-GB", "zh-CN", "ko-KR")


def _probe() -> dict[str, object]:
    script = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const html = fs.readFileSync(root + "/web/index.html", "utf8");
const sources = [...html.matchAll(/<script\s+src="([^"]+)"/g)].map((match) => match[1]);
const context = {
  window:{FMODDI18nModules:[]},
  document:{documentElement:{dataset:{}}, querySelector:()=>null, dispatchEvent:()=>{}},
  NodeFilter:{SHOW_TEXT:4}, CustomEvent:class CustomEvent {},
};
vm.createContext(context);
for (const source of sources) {
  if (!source.startsWith("/i18n") || source === "/i18n.js") continue;
  vm.runInContext(fs.readFileSync(root + "/web" + source, "utf8"), context, {filename:source});
}
const modules = context.window.FMODDI18nModules.map((entry) => entry.messages || {});
vm.runInContext(fs.readFileSync(root + "/web/i18n.js", "utf8"), context, {filename:"/i18n.js"});
const keyed = [...html.matchAll(/data-i18n(?:-aria-label|-placeholder|-title|-alt)?="([^"]+)"/g)].map((match) => match[1]);
const missing = {};
for (const locale of ["en-GB", "zh-CN", "ko-KR"]) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  missing[locale] = [...new Set(keyed.filter((key) => context.window.FMODDI18n.t(key) === key))].sort();
}
context.window.FMODDI18n.setLocale("ko-KR", {persist:false, root:null});
const particles = {
  vowelObject:context.window.FMODDI18n.t("manager.unemployed.message", {manager:"민수"}),
  batchimObject:context.window.FMODDI18n.t("manager.unemployed.message", {manager:"박"}),
  vowelRoute:context.window.FMODDI18n.t("manager.switching_game", {version:"FM24"}),
  batchimRoute:context.window.FMODDI18n.t("manager.switching_game", {version:"FM26"}),
};
const cjkLabels = {};
for (const locale of ["zh-CN", "zh-TW", "ko-KR", "ja-JP"]) {
  context.window.FMODDI18n.setLocale(locale, {persist:false, root:null});
  cjkLabels[locale] = {
    saveChanges:context.window.FMODDI18n.t("common.save_changes"),
    shopEyebrow:context.window.FMODDI18n.t("page.shop.eyebrow"),
    relationshipReport:context.window.FMODDI18n.t("training.archive.relationship.kicker"),
    abilityKicker:context.window.FMODDI18n.t("world.player_detail.kicker_ability"),
  };
}
const unresolvedParticles = [];
for (const module of context.window.FMODDI18nModules) {
  for (const [key, template] of Object.entries(module.messages?.["ko-KR"] || {})) {
    if (!/을\(를\)|이\(가\)|은\(는\)|과\(와\)|\(으\)로/.test(template)) continue;
    const parameters = Object.fromEntries(
      [...template.matchAll(/\{([A-Za-z0-9_]+)\}/g)].map((match) => [match[1], "민수"]),
    );
    const rendered = context.window.FMODDI18n.t(key, parameters);
    if (/을\(를\)|이\(가\)|은\(는\)|과\(와\)|\(으\)로/.test(rendered)) unresolvedParticles.push(key);
  }
}
console.log(JSON.stringify({sources, modules, missing, particles, cjkLabels, unresolvedParticles}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _parameters(value: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def test_every_loaded_domain_catalog_has_locale_and_parameter_parity() -> None:
    output = _probe()
    modules = output["modules"]
    assert modules
    for module in modules:
        assert set(module) == set(LOCALES)
        key_sets = {locale: set(module[locale]) for locale in LOCALES}
        assert all(keys == key_sets["en-GB"] for keys in key_sets.values())
        for key in key_sets["en-GB"]:
            values = {locale: module[locale][key] for locale in LOCALES}
            assert all(isinstance(value, str) and value for value in values.values()), key
            assert all(_parameters(value) == _parameters(values["en-GB"]) for value in values.values()), key


def test_html_semantic_keys_resolve_and_catalogs_load_before_core() -> None:
    output = _probe()
    sources = output["sources"]
    assert sources.index("/i18n.js") < sources.index("/app.js")
    for source in sources:
        if source.startswith("/i18n.") and source not in {"/i18n.js"}:
            assert sources.index(source) < sources.index("/i18n.js")
    assert output["missing"] == {locale: [] for locale in LOCALES}


def test_korean_particles_are_resolved_without_parenthetical_placeholders() -> None:
    output = _probe()
    particles = output["particles"]
    assert "“민수”를" in particles["vowelObject"]
    assert "“박”을" in particles["batchimObject"]
    assert particles["vowelRoute"] == "FM24로 전환하는 중"
    assert particles["batchimRoute"] == "FM26으로 전환하는 중"
    assert all("(" not in value for value in particles.values())
    assert output["unresolvedParticles"] == []


def test_cjk_interfaces_keep_english_kickers_without_de_localising_controls() -> None:
    output = _probe()
    assert output["cjkLabels"] == {
        "zh-CN": {
            "saveChanges": "保存修改",
            "shopEyebrow": "CLUB ECONOMY",
            "relationshipReport": "TRAINING COOPERATION REPORT",
            "abilityKicker": "ABILITY",
        },
        "zh-TW": {
            "saveChanges": "儲存修改",
            "shopEyebrow": "CLUB ECONOMY",
            "relationshipReport": "TRAINING COOPERATION REPORT",
            "abilityKicker": "ABILITY",
        },
        "ko-KR": {
            "saveChanges": "변경 사항 저장",
            "shopEyebrow": "CLUB ECONOMY",
            "relationshipReport": "TRAINING COOPERATION REPORT",
            "abilityKicker": "ABILITY",
        },
        "ja-JP": {
            "saveChanges": "変更を保存",
            "shopEyebrow": "CLUB ECONOMY",
            "relationshipReport": "TRAINING COOPERATION REPORT",
            "abilityKicker": "ABILITY",
        },
    }
