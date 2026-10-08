from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import patch

import fmodd_desktop


ROOT = Path(__file__).resolve().parents[1]
CHINESE = re.compile(r"[\u3400-\u9fff]")


def _host_catalog_keys(catalog_name: str) -> set[str]:
    source = (ROOT / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
    match = re.search(
        rf"private static readonly Dictionary<string, string> {catalog_name}UserText = "
        rf"new Dictionary<string, string>\s*\{{(?P<body>.*?)\n        \}};",
        source,
        re.DOTALL,
    )
    assert match, f"{catalog_name}UserText catalog is missing"
    return set(re.findall(r'\{\s*"([^"]+)"\s*,', match.group("body")))


def _python_strings_outside_catalog(source: str) -> list[str]:
    tree = ast.parse(source)
    catalog = next(
        node for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "DESKTOP_MESSAGE_CATALOGS"
                for target in node.targets)
    )
    first, last = catalog.lineno, catalog.end_lineno
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and not (first <= node.lineno <= last)
    ]


def test_desktop_message_catalogs_have_key_parity_and_complete_locales() -> None:
    catalogs = fmodd_desktop.DESKTOP_MESSAGE_CATALOGS
    assert set(catalogs) == set(fmodd_desktop.SUPPORTED_DESKTOP_LOCALES)
    expected_keys = set(catalogs["en-GB"])
    assert expected_keys
    assert all(set(catalog) == expected_keys for catalog in catalogs.values())

    host_keys = _host_catalog_keys("English")
    assert host_keys
    assert _host_catalog_keys("Chinese") == host_keys
    assert _host_catalog_keys("TraditionalChinese") == host_keys
    assert _host_catalog_keys("Korean") == host_keys
    assert _host_catalog_keys("German") == host_keys
    assert _host_catalog_keys("Spanish") == host_keys
    assert _host_catalog_keys("French") == host_keys
    assert _host_catalog_keys("Russian") == host_keys
    assert _host_catalog_keys("Japanese") == host_keys
    assert _host_catalog_keys("BrazilianPortuguese") == host_keys
    assert _host_catalog_keys("EuropeanPortuguese") == host_keys


def test_desktop_locale_uses_the_existing_settings_contract_and_falls_back_to_english() -> None:
    with patch("fmodd_desktop.load_settings", return_value={"ui_locale": "zh-CN"}):
        assert fmodd_desktop.resolve_desktop_locale() == "zh-CN"
    with patch("fmodd_desktop.load_settings", return_value={"ui_locale": "ko-KR"}):
        assert fmodd_desktop.resolve_desktop_locale() == "ko-KR"
    for locale in ("zh-TW", "de-DE", "es-ES", "fr-FR", "ru-RU", "ja-JP", "pt-BR", "pt-PT"):
        with patch("fmodd_desktop.load_settings", return_value={"ui_locale": locale}):
            assert fmodd_desktop.resolve_desktop_locale() == locale
    with patch("fmodd_desktop.load_settings", return_value={"ui_locale": "it-IT"}):
        assert fmodd_desktop.resolve_desktop_locale() == "en-GB"
    with patch("fmodd_desktop.load_settings", side_effect=OSError("settings unavailable")):
        assert fmodd_desktop.resolve_desktop_locale() == "en-GB"


def test_desktop_messages_translate_catalogued_text_but_preserve_diagnostics_verbatim() -> None:
    assert fmodd_desktop.desktop_message("en-GB", "startup.already_running") == (
        "FMODD is already running. Do not start it again."
    )
    assert fmodd_desktop.desktop_message("zh-CN", "startup.already_running") == (
        "FMODD 已在运行，请勿重复启动。"
    )
    assert fmodd_desktop.desktop_message("zh-TW", "startup.already_running") == (
        "FMODD 已在執行，請勿重複啟動。"
    )
    assert fmodd_desktop.desktop_message("ko-KR", "startup.already_running") == (
        "FMODD가 이미 실행 중입니다. 다시 시작하지 마세요."
    )
    diagnostic = "WinError 5: C:\\private-path"
    rendered = fmodd_desktop.desktop_message(
        "ko-KR", "startup.host_start_failed", error=diagnostic,
    )
    assert diagnostic in rendered


def test_host_uses_localized_keys_for_every_owned_startup_and_file_dialog_surface() -> None:
    source = (ROOT / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
    for key in (
        "startup.initialising",
        "startup.webview_failed",
        "dialog.portrait_xml_title",
        "dialog.portrait_xml_filter",
        "dialog.portrait_graphics_root",
        "dialog.data_root",
    ):
        assert f'UserText("{key}")' in source
    assert "args.Length > 3 ? args[3] : \"en-GB\"" in source
    assert "[address, str(profile_path), str(icon_path), locale]" in (
        ROOT / "fmodd_desktop.py"
    ).read_text(encoding="utf-8")


def test_no_chinese_host_literal_escapes_the_localized_catalogs() -> None:
    python_source = (ROOT / "fmodd_desktop.py").read_text(encoding="utf-8")
    assert not any(CHINESE.search(value) for value in _python_strings_outside_catalog(python_source))

    host_source = (ROOT / "desktop" / "WebViewHost.cs").read_text(encoding="utf-8")
    catalog = re.search(
        r"// BEGIN LOCALIZED USER TEXT.*?// END LOCALIZED USER TEXT",
        host_source,
        re.DOTALL,
    )
    assert catalog
    outside_catalog = host_source[:catalog.start()] + host_source[catalog.end():]
    assert not CHINESE.search(outside_catalog)
