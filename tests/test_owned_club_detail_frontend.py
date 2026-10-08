from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
DARK_CSS = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SCRIPT.split(start, 1)[1].split(end, 1)[0]


def test_detail_operations_promote_to_one_dialog_level_single_flight():
    dispatch = _between(
        "async function runOwnedClubOperation",
        "function ownedClubMetric",
    )
    detail = _between(
        "async function runWorldClubDetailOperation",
        "function ownedClubMetric",
    )

    assert 'button?.closest?.("#world-club-detail-content")' in dispatch
    assert "return runWorldClubDetailOperation(button, label, task, operationKey);" in dispatch
    assert "beginWorldClubDetailBusy(label, {requestKey, blocking:true})" in detail
    assert "{skipDetailScope:true}" in detail
    assert "world-club-detail:${requestKey}:${operationKey || label}" in detail
    assert "finally" in detail


def test_detail_busy_state_restores_existing_control_state_after_rerender():
    sync = _between("function syncWorldClubDetailBusy", "function beginWorldClubDetailBusy")
    reset = _between("function resetAccountScopedWorldState", "async function loadStateOnce")
    owned_render = _between("function renderOwnedClubDetailContent", "function startWorldClubAcquisitionProgress")
    directory_render = _between("function renderWorldClubDetailContent", "async function openWorldClubDetail")

    assert 'dialog?.setAttribute("aria-busy", String(busy))' in sync
    assert 'content?.setAttribute("aria-busy", String(busy))' in sync
    assert 'querySelectorAll("button,input,select,textarea")' in sync
    assert "worldClubDetailWasDisabled" in sync
    assert "delete control.dataset.worldClubDetailWasDisabled" in sync
    assert "syncWorldClubDetailBusy();" in owned_render
    assert "syncWorldClubDetailBusy();" in directory_render
    assert "app.worldClubDetailRequestKey = Number(app.worldClubDetailRequestKey || 0) + 1" in reset
    assert "app.worldClubDetailBlocking = false" in reset


def test_detail_initial_read_and_escape_are_busy_aware():
    opening = _between("async function openWorldClubDetail", "function portfolioYouthArrangementLabel")

    assert "const finishBusy = beginWorldClubDetailBusy(" in opening
    assert "{requestKey, blocking:Boolean(force)}" in opening
    assert "finally" in opening
    assert "finishBusy?.();" in opening
    assert 'if (app.worldClubDetailBlocking) event.preventDefault();' in SCRIPT


def test_finance_submit_keeps_detail_single_flight_through_secondary_dialog():
    opening = _between("async function openOwnedClubFinanceDialog", "function renderOwnedClubVision")
    submit = _between('$("#money-input-form").addEventListener', '$("#credit-max")')

    assert "triggerButton:button" in opening
    assert "ownedClubFinance?.triggerButton" in submit
    assert "runOwnedClubOperation(" in submit
    assert "finance:${ownedClubFinance.teamId}:${ownedClubFinance.field}:${ownedClubFinance.direction}" in submit


def test_finance_card_exposes_sugar_daddy_selector_and_version_gate():
    card = _between("const sugarDaddyLabels =", "content.innerHTML =")

    assert "主席团资助类型" in card
    assert "data-owned-sugar-daddy-select" in card
    assert "/api/world-clubs/sugar-daddy" in SCRIPT
    assert "expected:current" in SCRIPT
    assert "当前版本未映射" in card
    assert "sugarDaddyLabels[sugarDaddyValue]" in card
    assert "finances.sugar_daddy_name ||" not in card


def test_facility_upgrade_uses_multi_level_twelve_day_plan_dialog():
    from frontend_source import read_frontend_source

    upgrade = _between(
        "function upgradeOwnedClubFacility",
        "async function openOwnedClubFinanceDialog",
    )
    render = _between("const facilityButton =", "const stadiumQuotes =")

    assert 'id="owned-facility-upgrade-dialog"' in HTML
    assert 'id="owned-facility-upgrade-levels"' in HTML
    assert "每 12 个游戏日提升 1 级" not in HTML
    assert "facility_upgrade_quotes" in upgrade
    assert "levels:Number(selected.levels)" in upgrade
    assert "Number.isFinite(days) && days > 0" in upgrade
    assert "selected?.days" in upgrade
    assert "每 12 个游戏日提升 1 级" not in upgrade
    assert "fmoddConfirm" not in upgrade
    assert "facilities[facilityField] =" not in upgrade
    raw = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
    raw_upgrade = raw.split("function upgradeOwnedClubFacility", 1)[1].split(
        "async function openOwnedClubFinanceDialog", 1,
    )[0]
    assert "owned_actions_patch" in raw_upgrade
    assert "Object.entries(patch)" in raw_upgrade
    assert "facility_upgrade_plans" in render
    assert "计划中" in render
    assert "安排升级" in render
    assert ".owned-facility-upgrade-note" not in CSS


def test_sugar_daddy_save_matches_fund_action_width():
    assert ".owned-finance-controls .owned-club-action{min-width:72px}" in CSS
    assert ".owned-sugar-daddy-control .owned-club-action{width:72px;min-width:72px;flex:0 0 72px" in CSS


def test_detail_dialog_exposes_accessibility_and_motion_contract():
    assert (
        'id="world-club-detail-dialog" class="club-detail-dialog world-club-detail-dialog" '
        'aria-labelledby="world-club-detail-title"'
    ) in HTML
    assert 'aria-describedby="world-club-detail-subtitle world-club-detail-status"' in HTML
    assert 'id="world-club-detail-status" class="world-club-detail-status" role="status" aria-live="polite" hidden' in HTML
    assert 'id="world-club-detail-content" class="club-detail-content" aria-busy="false"' in HTML
    assert ".world-club-detail-dialog button:focus-visible" in CSS
    assert ".world-club-detail-dialog summary:focus-visible" in CSS
    assert ".world-club-detail-status>i{animation:none}" in CSS
    assert ':root[data-theme="dark"] .world-club-detail-status' in DARK_CSS
