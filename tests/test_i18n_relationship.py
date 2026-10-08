from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def relationship_probe() -> dict[str, object]:
    probe = r'''
const fs = require("fs");
const vm = require("vm");
const context = {
  window: {FMODDI18nModules: []},
  document: {documentElement: {dataset: {}}, querySelector: () => null, dispatchEvent: () => {}},
  NodeFilter: {SHOW_TEXT: 4},
  CustomEvent: class CustomEvent {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync("web/i18n.relationship.js", "utf8"), context);
const module = context.window.FMODDI18nModules.at(-1);
vm.runInContext(fs.readFileSync("web/i18n.js", "utf8"), context);
const i18n = context.window.FMODDI18n;
const keys = Object.fromEntries(Object.entries(module.messages).map(([locale, catalog]) => [locale, Object.keys(catalog).sort()]));
i18n.setLocale("en-GB", {persist: false, root: null});
const english = [
  i18n.t("relations3d.title"),
  i18n.t("relations3d.network_summary", {people: 4, links: 7}),
  i18n.t("relations3d.screenshot.saved", {path: "data/screenshots/graph.png"}),
  i18n.t("relations.reason.colleague"),
];
i18n.setLocale("ko-KR", {persist: false, root: null});
const korean = [
  i18n.t("relations3d.title"),
  i18n.t("relations3d.network_summary", {people: 4, links: 7}),
  i18n.t("relations3d.screenshot.saved", {path: "data/screenshots/graph.png"}),
  i18n.t("relations.reason.colleague"),
];
console.log(JSON.stringify({keys, messages: module.messages, english, korean}));
'''
    result = subprocess.run(
        ["node", "-e", probe], cwd=ROOT, check=True, capture_output=True,
        text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def placeholders(value: str) -> set[str]:
    return set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", value))


def test_relationship_catalog_has_locale_and_placeholder_parity() -> None:
    output = relationship_probe()
    keys = output["keys"]
    assert keys["en-GB"] == keys["zh-CN"] == keys["ko-KR"]
    for key in keys["en-GB"]:
        variants = [output["messages"][locale][key] for locale in ("en-GB", "zh-CN", "ko-KR")]
        assert placeholders(variants[0]) == placeholders(variants[1]) == placeholders(variants[2]), key
    assert output["english"] == [
        "3D relationship links",
        "4 people · 7 relationship links",
        "Saved: data/screenshots/graph.png",
        "Colleague",
    ]
    assert output["korean"] == [
        "3D 관계 연결",
        "4명 · 관계 링크 7개",
        "저장됨: data/screenshots/graph.png",
        "동료",
    ]


def test_relationship_scripts_and_renderers_use_semantic_messages() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    relationship_script = html.index('<script src="/i18n.relationship.js"></script>')
    core_script = html.index('<script src="/i18n.js"></script>')
    app_script = html.index('<script src="/app.js"></script>')
    assert relationship_script < core_script < app_script

    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for call in (
        'uiText("relations3d.title")',
        'uiText("relations3d.network_summary"',
        'uiText("relations3d.screenshot.saved"',
        'uiText("relations3d.background.panel_title")',
        'uiText("relations3d.tune.engine_title")',
        'uiText("relations3d.hint.node")',
        'uiText("relations.toast.scope_missing")',
        'relationshipTrainingRoleLabel(role)',
        'uiText(RELATIONSHIP_TRAINING_POSITION_LABELS[position])',
        'uiText("relationship.room.preview_title")',
        'uiText("relationship.room.effect_summary"',
        'uiText("relationship.room.confirm_enter")',
        'uiText("relationship.room.unlock_action")',
        "relationshipTimelineSourceLabel(row.source)",
        "relationshipEventRoleLabel(from)",
        "window.FMODDRelationshipEventOutcomeKeys",
    ):
        assert call in script

    timeline = script.split("function relationshipEventRoleLabel", 1)[1].split(
        "function relationshipTimelinePeopleHtml", 1,
    )[0]
    assert "from?.event_role_label" not in timeline
    assert "row.source?.label||" not in timeline

    body = script.split("function renderRelations3D", 1)[1].split("document.addEventListener(\"click\"", 1)[0]
    # The renderer still contains Chinese comments and stable internal category
    # keys; the semantic-call assertions above cover its user-facing surfaces.
    assert 'uiText("relations3d.title")' in body
