from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def sources() -> tuple[str, str, str]:
    return (
        (ROOT / "web" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.css").read_text(encoding="utf-8"),
    )


def test_mail_uses_exact_source_versioned_index_and_id_lookup() -> None:
    script, _, _ = sources()
    index = script.split("function mailDataIndex()", 1)[1].split(
        "function renderMail()", 1
    )[0]
    click = script.split('$("#mail-content").addEventListener', 1)[1].split(
        '$("#settings-button")', 1
    )[0]

    assert "sources?.records === records" in index
    assert "sources.pagination === pagination" in index
    assert "const dataSignature = JSON.stringify([records, pagination]);" in index
    assert "previous?.dataSignature === dataSignature" in index
    assert "recordsById:new Map" in index
    assert "mailDataIndex().recordsById.get" in click


def test_mail_signature_skips_unchanged_dom_and_preserves_busy_error_states() -> None:
    script, _, _ = sources()
    renderer = script.split("function renderMail()", 1)[1].split(
        "async function loadMailPage", 1
    )[0]
    loader = script.split("async function loadMailPage", 1)[1].split(
        "function mailToastCard", 1
    )[0]

    assert 'String(app.state?.data_scope_id || "")' in renderer
    assert "index.version" in renderer
    assert "moneyRenderSignature()" in renderer
    assert "app.mailError" in renderer
    assert "if (app.mailRenderSignature === signature) return;" in renderer
    assert renderer.index("if (app.mailRenderSignature === signature) return;") < renderer.index(
        "records.map((item)"
    )
    assert 'dialog?.setAttribute("aria-busy", String(busy));' in renderer
    assert 'content.setAttribute("aria-busy", String(busy));' in renderer
    assert "app.mailLoading = true;" in loader
    assert 'app.mailError = "";' in loader
    assert "app.mailLoading = false;" in loader
    assert loader.count("renderMail();") >= 2


def test_mail_toast_header_uses_mail_title_or_type_specific_fallback() -> None:
    script, _, _ = sources()
    toast = script.split("function mailToastCard", 1)[1].split(
        "function enqueueMailToasts", 1
    )[0]

    assert 'localizedMailField(mail, "title").trim()' in toast
    assert "const label = localizedTitle" in toast
    assert "? localizedTitle" in toast
    assert 'const settlement = mailType === "bet_profit";' in toast
    assert 'relief ? "复活资金发放" : "注单结算到账"' not in toast


def test_global_statistics_dialog_has_loading_error_and_retry_contract() -> None:
    script, html, css = sources()
    state = script.split("function renderGlobalCareerState()", 1)[1].split(
        "async function openGlobalCareerStatistics", 1
    )[0]
    loader = script.split("async function loadGlobalCareerStatistics", 1)[1].split(
        "async function openGlobalCareerStatistics", 1
    )[0]

    assert 'id="global-career-dialog" class="global-career-dialog" aria-labelledby="global-career-title" aria-busy="false"' in html
    assert 'id="global-career-content" class="global-career-content" role="tabpanel" aria-live="polite" aria-busy="false"' in html
    assert 'dialog.setAttribute("aria-busy", String(busy));' in state
    assert 'content.setAttribute("aria-busy", String(busy));' in state
    assert "data-global-career-retry" in state
    assert "if (app.globalStatisticsLoading) return;" in loader
    assert "app.globalStatisticsError = error.message" in loader
    assert '$("#global-career-content")?.addEventListener' in script
    assert ".global-career-error button:focus-visible" in css


def test_mail_and_operation_log_expose_keyboard_and_live_status_semantics() -> None:
    script, html, css = sources()
    operation = script.split("function renderOperationRecords", 1)[1].split(
        "const SETTINGS_TAB_GROUPS", 1
    )[0]

    assert 'id="mail-dialog" class="utility-dialog" aria-labelledby="mail-dialog-title" aria-busy="false"' in html
    assert 'id="mail-sync-status" role="status" aria-live="polite" hidden' in html
    assert 'id="mail-content" class="mail-content" aria-busy="false"' in html
    assert '.mail-item-button:focus-visible' in css
    assert '.mail-pagination button:focus-visible' in css
    assert 'summary.setAttribute("role", "status");' in operation
    assert 'root.setAttribute("role", "list");' in operation
    assert 'role="listitem"' in operation
    assert 'aria-hidden="true"' in operation
    assert '.operation-log-content{min-height:0;overflow-y:auto' in css
    assert 'scrollbar-gutter:stable' in css
    assert 'data-active-tab="operations"' in css


def test_global_profit_colors_follow_betting_history_convention() -> None:
    _, _, css = sources()

    assert ".global-career-metrics article.profit strong { color:var(--profit-red); }" in css
    assert ".global-career-metrics article.loss strong { color:var(--loss-green); }" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ".operation-log-row.running > svg" in css
    assert ".mail-state .spinner" in css
