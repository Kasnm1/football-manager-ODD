import json
from pathlib import Path

import pytest

from tools.update_check import (
    UpdateCheckError,
    check_for_updates,
    compare_versions,
)


ROOT = (Path(__file__).resolve().parents[1] / "src")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self, _limit):
        return self.payload


def _manifest(**overrides):
    payload = {
        "schemaVersion": 1,
        "version": "2.6.7beta",
        "releaseDate": "2026-09-11",
        "downloadPage": "https://fmodd.com/download",
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("V2.6.6beta", "2.6.6beta", 0),
        ("2.6.6beta", "2.6.6", -1),
        ("2.6.7beta", "2.6.6", 1),
        ("2.6.6rc1", "2.6.6beta9", 1),
    ],
)
def test_compare_versions(left, right, expected):
    assert compare_versions(left, right) == expected


def test_check_for_updates_returns_official_release_details():
    result = check_for_updates(
        "V2.6.6beta",
        opener=lambda _request, timeout: _Response(_manifest()),
    )

    assert result == {
        "current_version": "2.6.6beta",
        "latest_version": "2.6.7beta",
        "release_date": "2026-09-11",
        "download_page": "https://fmodd.com/download",
        "update_available": True,
    }


def test_check_for_updates_rejects_non_official_download_page():
    with pytest.raises(UpdateCheckError, match="invalid_download_page"):
        check_for_updates(
            "2.6.6beta",
            opener=lambda _request, timeout: _Response(
                _manifest(downloadPage="https://example.com/download")
            ),
        )


def test_update_check_api_route_uses_product_version(monkeypatch):
    import fm_odds_web as web

    observed = []
    expected = {"current_version": "2.6.6beta", "update_available": False}
    monkeypatch.setattr(
        web,
        "check_for_updates",
        lambda version: observed.append(version) or expected,
    )

    result = web.build_api_routes().dispatch("GET", "/api/update-check", object())

    assert result is not None
    assert result.payload == expected
    assert observed == [web.PRODUCT_VERSION]


def test_frontend_checks_for_updates_silently_on_startup():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    checker = script.split("async function checkForUpdates", 1)[1].split(
        "function operationRecordTime", 1
    )[0]
    startup = script.rsplit("initializeCustomSelects();", 1)[1]

    assert "({silent = false} = {})" in checker
    assert "app.updateCheckFailed = !silent;" in checker
    assert "checkForUpdates({silent:true});" in startup
    assert "toast(" not in checker


def test_frontend_replaces_check_with_download_and_marks_both_update_entry_points():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    renderer = script.split("function renderUpdateCheckResult", 1)[1].split(
        "async function checkForUpdates", 1
    )[0]

    assert "app.updateCheckResult?.update_available" in renderer
    assert "localStorage.getItem(IGNORED_UPDATE_VERSION_STORAGE_KEY) !== latestVersion" in renderer
    assert '$("#settings-button")?.classList.toggle("update-available", updateAvailable);' in renderer
    assert 'download.classList.toggle("update-available", updateAvailable);' in renderer
    assert "button.hidden = Boolean(result.update_available);" in renderer
    assert "download.hidden = !result.update_available;" in renderer
    assert "ignore.hidden = !updateAvailable;" in renderer
    assert "#settings-button.update-available::after" in styles
    assert ".update-actions #update-download-link.update-available::after" in styles


def test_frontend_can_ignore_only_the_current_update_release():
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    control = script.split("function ensureUpdateCheckControl", 1)[1].split(
        "function renderUpdateCheckResult", 1
    )[0]
    events = script.split('$("#check-for-updates")?.addEventListener', 1)[1].split(
        '$("#ui-locale")?.addEventListener', 1
    )[0]

    assert 'id="ignore-update" data-i18n="settings.update.ignore" hidden' in control
    assert control.index('id="update-download-link"') < control.index('id="ignore-update"')
    assert '$("#ignore-update")?.addEventListener("click"' in events
    assert "localStorage.setItem(IGNORED_UPDATE_VERSION_STORAGE_KEY, version);" in events
    assert "renderUpdateCheckResult();" in events
