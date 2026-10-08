from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")
SCRIPT = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
DARK_CSS = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SCRIPT.split(start, 1)[1].split(end, 1)[0]


def test_nation_detail_uses_monotonic_request_generation_and_scope_binding():
    opening = _between("async function openWorldNationDetail", "async function scanWorldNations")
    reset = _between("function resetAccountScopedWorldState", "async function loadStateOnce")

    assert "Number(app.worldNationDetailRequestKey || 0) + 1" in opening
    assert "worldNationDetailRequestKey = requestKey" in opening
    assert "worldNationDetailRequestKey = String(requestKey)" in opening
    assert "worldNationDetailNationId = String(Number(nationId))" in opening
    assert "app.worldNationDetailRequestKey = Number(app.worldNationDetailRequestKey || 0) + 1" in reset
    assert "app.worldNationDetailBusy = false" in reset


def test_nation_detail_read_and_investment_share_one_dialog_single_flight():
    opening = _between("async function openWorldNationDetail", "async function scanWorldNations")
    busy = _between("function syncWorldNationDetailBusy", "function beginWorldNationDetailBusy")
    operation = _between("async function runWorldNationDetailOperation", "function bindWorldNationDetailActions")
    actions = _between("function bindWorldNationDetailActions", "async function openWorldNationDetail")

    assert 'dialog?.setAttribute("aria-busy", String(busy))' in busy
    assert 'content?.setAttribute("aria-busy", String(busy))' in busy
    assert "worldNationDetailWasDisabled" in busy
    assert "delete control.dataset.worldNationDetailWasDisabled" in busy
    assert "beginWorldNationDetailBusy(label, {requestKey, blocking:true})" in operation
    assert "finally" in operation
    assert "runWorldNationDetailOperation(activeButton" in actions
    assert "world-nation-invest:${requestKey}:${nationId}:${requestedIncrease}" in actions
    assert "await openWorldNationDetail(nationId)" not in actions
    assert "bindWorldNationDetailActions(content, detail, nationId, requestKey)" in opening


def test_nation_detail_rejects_stale_publication_and_reapplies_lock_after_refresh():
    current = _between("function isCurrentWorldNationDetail", "function syncWorldNationDetailBusy")
    actions = _between("function bindWorldNationDetailActions", "async function openWorldNationDetail")

    assert "app.worldNationDetailRequestKey" in current
    assert "worldNationDetailRequestKey" in current
    assert "worldNationDetailNationId" in current
    assert "Number(app.worldNationDetailRequestKey || 0) !== Number(requestKey)" in actions
    assert "content.innerHTML = renderWorldNationDetail(detail)" in actions
    assert "syncWorldNationDetailBusy();" in actions


def test_nation_detail_dialog_exposes_busy_keyboard_and_theme_contract():
    assert (
        'id="world-nation-detail-dialog" class="club-detail-dialog world-nation-detail-dialog" '
        'aria-labelledby="world-nation-detail-title"'
    ) in HTML
    assert 'aria-describedby="world-nation-detail-subtitle world-nation-detail-status"' in HTML
    assert 'id="world-nation-detail-status" class="world-nation-detail-status" role="status" aria-live="polite" hidden' in HTML
    assert 'id="world-nation-detail-content" class="club-detail-content" aria-busy="false"' in HTML
    assert 'if (app.worldNationDetailBlocking) event.preventDefault();' in SCRIPT
    assert ".world-nation-detail-dialog button:focus-visible" in CSS
    assert ".world-nation-detail-status>i{animation:none}" in CSS
    assert ':root[data-theme="dark"] .world-nation-detail-status' in DARK_CSS

