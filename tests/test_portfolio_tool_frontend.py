from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
DARK_CSS = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")


def _between(start: str, end: str) -> str:
    return SCRIPT.split(start, 1)[1].split(end, 1)[0]


def test_portfolio_tool_rejects_stale_shared_content_publication():
    current = _between(
        "function portfolioToolRequestIsCurrent", "function syncPortfolioToolBusy"
    )
    opening = _between("async function openPortfolioTool", "function ownedClubRenderStateSignature")

    assert "requestKey) === Number(app.portfolioToolRequestKey)" in current
    assert "app.portfolioTool === kind" in current
    assert '$("#portfolio-tool-dialog")?.open' in current
    assert "const requestKey = ++app.portfolioToolRequestKey;" in opening
    assert 'loadPortfolioYouth(content, requestKey)' in opening
    assert 'loadPortfolioBrand(content, Number(clubs[0].id), requestKey)' in opening
    assert 'loadPortfolioRelations(content, requestKey)' in opening
    assert "const nextRequestKey = ++app.portfolioToolRequestKey;" in SCRIPT

    for name, next_name, kind in (
        ("loadPortfolioRelations", "portfolioSummaryMetrics", "relations"),
        ("loadPortfolioYouth", "renderPortfolioBrand", "youth"),
        ("loadPortfolioBrand", "openPortfolioTool", "brand"),
    ):
        loader = _between(f"async function {name}", f"function {next_name}")
        assert f'portfolioToolRequestIsCurrent(requestKey, "{kind}")' in loader


def test_portfolio_writes_share_one_dialog_level_single_flight():
    helper = _between("async function runPortfolioToolOperation", "function portfolioRelationTypeOptions")
    tools = _between("function renderPortfolioRelations", "function ownedClubRenderStateSignature")

    assert "beginPortfolioToolBusy(label, {requestKey, blocking:true})" in helper
    assert "if (!finish)" in helper
    assert "`portfolio-tool:${requestKey}`" in helper
    assert "finally" in helper
    assert tools.count("runPortfolioToolOperation(") == 8
    assert "runOwnedClubOperation(" not in tools

    for endpoint in (
        "/api/world-clubs/relations",
        "/api/world-clubs/youth-plan/arm",
        "/api/world-clubs/youth-plan/cancel",
        "/api/world-clubs/youth-plan/reopen",
        "/api/world-clubs/youth-plan/apply",
        "/api/world-clubs/brand-promote",
    ):
        assert endpoint in tools


def test_relation_read_uses_long_timeout_and_reuses_same_scope_request():
    loader = _between("async function loadPortfolioRelations", "function portfolioSummaryMetrics")
    reset = _between("function resetAccountScopedWorldState", "async function loadStateOnce")

    assert "app.portfolioRelationsRequest" in loader
    assert "pending.scope !== scope" in loader
    assert "const relations = await pending.promise" in loader
    assert "timeoutMs:180000" in loader
    assert "俱乐部关系读取超时" in loader
    assert "app.portfolioRelationsRequest = null" in reset


def test_youth_write_stays_busy_through_authoritative_overview_refresh():
    youth = _between("function renderPortfolioYouth", "async function loadPortfolioYouthOverview")
    for endpoint in (
        "/api/world-clubs/youth-plan/arm",
        "/api/world-clubs/youth-plan/cancel",
        "/api/world-clubs/youth-plan/reopen",
        "/api/world-clubs/youth-plan/apply",
    ):
        endpoint_at = youth.index(endpoint)
        overview_at = youth.index('/api/world-clubs/youth-overview', endpoint_at)
        assert endpoint_at < overview_at
        assert "runPortfolioToolOperation(" in youth[max(0, endpoint_at - 220) : endpoint_at]


def test_portfolio_busy_state_restores_preexisting_disabled_controls():
    sync = _between("function syncPortfolioToolBusy", "function beginPortfolioToolBusy")
    reset = _between("function resetAccountScopedWorldState", "async function loadStateOnce")

    assert 'dialog?.setAttribute("aria-busy", String(busy))' in sync
    assert 'content?.setAttribute("aria-busy", String(busy))' in sync
    assert 'querySelectorAll("button,input,select,textarea")' in sync
    assert "portfolioToolWasDisabled" in sync
    assert "delete control.dataset.portfolioToolWasDisabled" in sync
    assert "app.portfolioToolRequestKey += 1" in reset
    assert "app.portfolioToolBlocking = false" in reset
    assert 'if (app.portfolioToolBlocking) event.preventDefault();' in SCRIPT


def test_portfolio_dialog_exposes_busy_keyboard_motion_and_dark_theme_semantics():
    assert (
        'id="portfolio-tool-dialog" class="portfolio-tool-dialog" '
        'aria-labelledby="portfolio-tool-title"'
    ) in HTML
    assert 'aria-describedby="portfolio-tool-subtitle portfolio-tool-status"' in HTML
    assert 'id="portfolio-tool-status" class="portfolio-tool-status" role="status" aria-live="polite" hidden' in HTML
    assert 'id="portfolio-tool-content" class="portfolio-tool-content" aria-busy="false"' in HTML
    assert ".portfolio-tool-dialog button:focus-visible" in CSS
    assert ".portfolio-tool-dialog summary:focus-visible" in CSS
    assert ".portfolio-tool-status>i{animation:none}" in CSS
    assert "font-size:13px!important" in CSS
    assert ':root[data-theme="dark"] .portfolio-tool-status' in DARK_CSS
