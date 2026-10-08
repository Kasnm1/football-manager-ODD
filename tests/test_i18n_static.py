from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")
LOCALES = ("en-GB", "zh-CN", "ko-KR")
ATTRIBUTE_KEYS = {
    "aria-label": "data-i18n-aria-label",
    "placeholder": "data-i18n-placeholder",
    "title": "data-i18n-title",
    "alt": "data-i18n-alt",
}
FONT_LABELS = {
    "微软雅黑",
    "微软正黑体",
    "黑体",
    "宋体",
    "新宋体",
    "楷体",
    "仿宋",
    "等线",
    "谷歌思源黑体",
    "思源黑体",
}


class _HTMLProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[dict[str, object]] = []
        self.stack: list[int] = []
        self.text_nodes: list[tuple[str, tuple[dict[str, str], ...]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        current = {name: value or "" for name, value in attrs}
        index = len(self.elements)
        self.elements.append({"tag": tag, "attrs": current, "parent": self.stack[-1] if self.stack else None})
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(index)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        current = {name: value or "" for name, value in attrs}
        self.elements.append({"tag": tag, "attrs": current, "parent": self.stack[-1] if self.stack else None})

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, -1, -1):
            if self.elements[self.stack[position]]["tag"] == tag:
                del self.stack[position:]
                break

    def handle_data(self, data: str) -> None:
        self.text_nodes.append((data, tuple(self.elements[index]["attrs"] for index in self.stack)))


def _catalog() -> dict[str, object]:
    script = r'''
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const context = {window: {FMODDI18nModules: []}};
vm.createContext(context);
const files = [
  "i18n.static.js", "i18n.shell.js", "i18n.commerce.js", "i18n.items.js", "i18n.settings.js",
  "i18n.facilities.js", "i18n.world.js", "i18n.enums.js", "i18n.relationship.js",
];
for (const file of files) {
  vm.runInContext(fs.readFileSync(root + "/web/" + file, "utf8"), context, {filename:file});
}
const combined = {messages:{"en-GB":{}, "zh-CN":{}, "ko-KR":{}}, legacy:{"en-GB":{}, "zh-CN":{}, "ko-KR":{}}};
for (const module of context.window.FMODDI18nModules) {
  for (const locale of Object.keys(combined.messages)) {
    Object.assign(combined.messages[locale], module.messages?.[locale] || {});
    Object.assign(combined.legacy[locale], module.legacy?.[locale] || {});
  }
}
console.log(JSON.stringify(combined));
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


def test_static_catalog_is_complete_and_isomorphic() -> None:
    module = _catalog()
    messages = module["messages"]
    assert set(messages) == set(LOCALES)
    key_sets = {locale: set(messages[locale]) for locale in LOCALES}
    assert key_sets["en-GB"] == key_sets["zh-CN"] == key_sets["ko-KR"]
    for key in key_sets["en-GB"]:
        values = [messages[locale][key] for locale in LOCALES]
        assert all(isinstance(value, str) and value for value in values), key
        assert _parameters(values[0]) == _parameters(values[1]) == _parameters(values[2]), key
    assert set(module["legacy"]) == set(LOCALES)


def test_index_keys_and_localized_attributes_have_catalog_entries_and_fallbacks() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    module = _catalog()
    parser = _HTMLProbe()
    parser.feed(html)
    for element in parser.elements:
        attrs = element["attrs"]
        assert isinstance(attrs, dict)
        for attribute, key_attribute in ATTRIBUTE_KEYS.items():
            if key_attribute in attrs:
                assert attribute in attrs
                key = attrs[key_attribute]
                assert all(key in module["messages"][locale] for locale in LOCALES), key
        if "data-i18n" in attrs:
            key = attrs["data-i18n"]
            assert all(key in module["messages"][locale] for locale in LOCALES), key


def test_settings_latest_news_link_opens_the_official_website() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    configurator = script.split("function configureOfficialNewsLink", 1)[1].split(
        "applyColorTheme(app.darkMode", 1
    )[0]
    assert 'link.href = "https://fmodd.com/";' in configurator
    assert 'icon.dataset.lucide = "globe-2";' in configurator
    assert 'label.dataset.i18n = "settings.latest_news";' in configurator
    assert "link.replaceChildren(icon, label);" in configurator
    assert "configureOfficialNewsLink();" in script


def test_index_has_no_nested_i18n_nodes_or_unkeyed_cjk_user_text() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    parser = _HTMLProbe()
    parser.feed(html)
    for index, element in enumerate(parser.elements):
        attrs = element["attrs"]
        assert isinstance(attrs, dict)
        if "data-i18n" not in attrs:
            continue
        parent = element["parent"]
        while parent is not None:
            parent_attrs = parser.elements[parent]["attrs"]
            assert isinstance(parent_attrs, dict)
            assert "data-i18n" not in parent_attrs, (index, parent, attrs.get("data-i18n"))
            parent = parser.elements[parent]["parent"]

    residuals: list[str] = []
    for data, ancestors in parser.text_nodes:
        text = data.strip()
        if not text or not re.search(r"[\u4e00-\u9fff]", text):
            continue
        if any(frame.get("data-i18n") for frame in ancestors):
            continue
        if text in FONT_LABELS:
            continue
        residuals.append(text)
    assert residuals == []

    for element in parser.elements:
        attrs = element["attrs"]
        assert isinstance(attrs, dict)
        for attribute in ATTRIBUTE_KEYS:
            value = attrs.get(attribute, "")
            if re.search(r"[\u4e00-\u9fff]", value):
                assert ATTRIBUTE_KEYS[attribute] in attrs, (attribute, value)


def test_index_loads_all_localization_modules_before_core_and_app() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    sources = re.findall(r'<script\s+src="([^"]+)"', html)
    expected = [
        "/i18n.static.js",
        "/i18n.shell.js",
        "/i18n.commerce.js",
        "/i18n.items.js",
        "/i18n.settings.js",
        "/i18n.facilities.js",
        "/i18n.world.js",
        "/i18n.ko.js",
        "/i18n.relationship.js",
        "/i18n.tw.js",
        "/i18n.de.js",
        "/i18n.es.js",
        "/i18n.fr.js",
        "/i18n.ru.js",
        "/i18n.ja.js",
        "/i18n.pt-BR.js",
        "/i18n.pt-PT.js",
        "/i18n.enums.js",
        "/i18n.js",
        "/bet_analysis_page.js",
        "/app.js",
    ]
    assert sources[-len(expected):] == expected
