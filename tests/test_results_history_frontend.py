from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def frontend_sources() -> tuple[str, str, str]:
    return (
        (ROOT / "web" / "app.js").read_text(encoding="utf-8"),
        (ROOT / "web" / "index.html").read_text(encoding="utf-8"),
        (ROOT / "web" / "app.css").read_text(encoding="utf-8"),
    )


def test_history_uses_exact_source_versioned_index_and_id_lookup() -> None:
    script, _, _ = frontend_sources()
    index = script.split("function historyDataIndex()", 1)[1].split(
        "function historyAnalysisDataIndex()", 1
    )[0]

    assert "app.historyIndexSource === bets" in index
    assert "const dataSignature = JSON.stringify(bets);" in index
    assert "previous?.dataSignature === dataSignature" in index
    assert "pendingBets:bets.filter" in index
    assert "settledBets:bets.filter" in index
    assert "betsById:new Map" in index


def test_history_signature_covers_visible_state_before_dom_generation() -> None:
    script, _, _ = frontend_sources()
    signature = script.split("function historyRenderStateSignature", 1)[1].split(
        "function renderHistory()", 1
    )[0]
    history = script.split("function renderHistory()", 1)[1].split(
        "function renderManualRefundDialog()", 1
    )[0]

    for field in (
        "data_scope_id",
        "data_version",
        "index.version",
        "analysisIndex.version",
        "app.historyTab",
        "app.historyView",
        "app.historyMode",
        "app.analysisRange",
        "app.analysisLineMode",
        "index.hasChampionshipBets ? championshipDataIndex().dataSignature",
        "app.resultSearchRefundIds",
        "app.resultSearchIssues",
    ):
        assert field in signature
    assert history.index("if (app.historyRenderSignature === signature) return;") < history.index(
        "const pendingTicket ="
    )


def test_results_use_date_competition_and_team_indices() -> None:
    script, _, _ = frontend_sources()
    index = script.split("function resultsDataIndex()", 1)[1].split(
        "function resultMatchesActiveFilters", 1
    )[0]
    filter_source = script.split("function resultFilterSource", 1)[1].split(
        "function filteredResults", 1
    )[0]

    assert "const byDate = new Map();" in index
    assert "const byCompetition = new Map();" in index
    assert "const byTeam = new Map();" in index
    assert "const dataSignature = JSON.stringify(results);" in index
    assert "previous?.dataSignature === dataSignature" in index
    assert "index.byDate.get(String(app.resultsDate))" in filter_source
    assert "index.byCompetition.get(Number(app.selectedCompetition))" in filter_source


def test_results_and_calendar_skip_unchanged_dom_rebuilds() -> None:
    script, _, _ = frontend_sources()
    calendar = script.split("function renderResultsCalendar()", 1)[1].split(
        "function resultsRenderStateSignature", 1
    )[0]
    results = script.split("function renderResults()", 1)[1].split(
        "function toast", 1
    )[0]

    assert "if (app.resultsCalendarSignature === signature) return;" in calendar
    assert "const resultDates = filteredResultDates(index);" in calendar
    assert "filteredResults(false)" not in calendar
    assert "if (app.resultsRenderSignature === signature)" in results
    assert results.index("if (app.resultsRenderSignature === signature)") < results.index(
        "const grouped = new Map();"
    )


def test_refund_dialog_reuses_history_lookup_but_keeps_server_quotes() -> None:
    script, _, _ = frontend_sources()
    dialog = script.split("function renderManualRefundDialog()", 1)[1].split(
        "async function openManualRefundDialog", 1
    )[0]
    loader = script.split("async function openManualRefundDialog", 1)[1].split(
        "const EMPTY_SEASON_RESULTS", 1
    )[0]

    assert "const bets = historyIndex.betsById;" in dialog
    assert "JSON.stringify(options)" in dialog
    assert "Number(option.refund || 0)" in dialog
    assert "Number(option.fee || 0)" in dialog
    assert "response.all_options || response.options || []" in loader


def test_history_results_and_refund_dialogs_expose_busy_and_keyboard_semantics() -> None:
    script, html, css = frontend_sources()

    assert 'id="history-dialog" aria-labelledby="history-dialog-title" aria-busy="false"' in html
    assert 'class="history-tabs" role="tablist"' in html
    assert 'role="tab" aria-selected="true" aria-controls="history-content"' in html
    assert 'id="history-content" class="history-content" role="tabpanel" aria-busy="false"' in html
    assert 'id="results-dialog" aria-labelledby="results-title" aria-describedby="results-subtitle" aria-busy="false"' in html
    assert 'id="calendar-button" aria-expanded="false" aria-controls="results-calendar"' in html
    assert 'id="manual-refund-dialog" class="manual-refund-dialog" aria-labelledby="manual-refund-title"' in html
    assert 'id="manual-refund-confirm" class="primary-action" type="button" aria-busy="false"' in html
    assert 'button.setAttribute("aria-selected", String(active));' in script
    assert 'button?.setAttribute("aria-expanded", String(app.calendarOpen));' in script
    assert '.history-tabs button:focus-visible' in css
    assert '#calendar-button:focus-visible' in css
    assert '.manual-refund-row:focus-within' in css
    assert '@media (prefers-reduced-motion: reduce)' in css
