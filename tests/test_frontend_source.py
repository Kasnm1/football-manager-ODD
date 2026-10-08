from __future__ import annotations

from pathlib import Path

import pytest

from frontend_source import read_frontend_source


ROOT = Path(__file__).resolve().parents[1]


def test_raw_and_zh_cn_views_are_distinct_for_real_i18n_markup():
    raw = read_frontend_source(ROOT / "web" / "index.html", mode="raw")
    projected = read_frontend_source(ROOT / "web" / "index.html", mode="zh-CN")

    assert 'data-i18n-aria-label="bet.slip.clear"' in raw
    assert 'aria-label="清空投注单"' in projected
    assert 'data-i18n-aria-label="bet.slip.clear"' not in projected
    assert raw != projected


def test_raw_mode_does_not_project_javascript_translation_calls():
    raw = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
    projected = read_frontend_source(ROOT / "web" / "app.js", mode="zh-CN")

    assert 'uiText("betting.slip_cleared")' in raw
    assert "投注单已清空" in projected
    assert raw != projected


def test_frontend_source_rejects_unknown_mode():
    with pytest.raises(ValueError, match="unsupported frontend source mode"):
        read_frontend_source(ROOT / "web" / "index.html", mode="other")
