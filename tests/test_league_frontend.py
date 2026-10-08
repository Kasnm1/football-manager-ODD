from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_league_index_signature_and_managed_team_context_are_reused() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    league = script.split("function leagueData()", 1)[1].split(
        "function championshipEnabled", 1,
    )[0]
    index = league.split("function leagueDataIndex()", 1)[1].split(
        "function leagueSelectionStorageKey", 1,
    )[0]
    board = league.split("function leagueBoard", 1)[1].split(
        "function leagueRenderStateSignature", 1,
    )[0]
    renderer = league.split("function renderLeague()", 1)[1]

    assert "app.leagueIndexSource === data" in index
    assert "competitionsById:new Map" in index
    assert "function leagueBoard(competition, competitions, slot, managed)" in league
    assert "managedTeamIds()" not in board
    assert "const signature = leagueRenderStateSignature" in renderer
    assert "if (app.leagueRenderSignature === signature) return;" in renderer
    assert renderer.index("app.leagueRenderSignature === signature") < renderer.index("root.innerHTML =")
    assert 'localStorage.getItem(leagueSelectionStorageKey()) !== serialized' in league


def test_championship_index_covers_settlement_teams_and_render_state() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    championship = script.split("function championshipEnabled()", 1)[1].split(
        "const EMPTY_TRAINING_DATA", 1,
    )[0]
    index = championship.split("function championshipDataIndex()", 1)[1].split(
        "function championshipSettlementDate", 1,
    )[0]
    renderer = championship.split("function renderChampionship()", 1)[1]

    assert "sources?.data === data" in index
    assert "sources.catalog === catalog" in index
    assert "competitionsBySeasonKey" in index
    assert "teamsByCompetitionId" in index
    assert "championshipDataIndex().competitionsBySeasonKey.get" in championship
    assert "index.teamsByCompetitionId.get(competitionId)?.get" in renderer
    assert "const signature = championshipRenderStateSignature" in renderer
    assert "if (app.championshipRenderSignature === signature) return;" in renderer
    assert renderer.index("app.championshipRenderSignature === signature") < renderer.index("root.innerHTML =")
    assert 'aria-pressed="${chosen}"' in renderer
    assert 'role="group" aria-label="${escapeHtml(uiText("championship.team_choice"' in renderer


def test_league_and_championship_publish_busy_keyboard_and_motion_feedback() -> None:
    markup = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

    assert 'aria-labelledby="league-page-title" aria-busy="false"' in markup
    assert 'id="league-sync-status" role="status" aria-live="polite" hidden' in markup
    assert 'id="league-refresh-button"' in markup
    assert 'data-i18n-aria-label="world.refresh" aria-busy="false"' in markup
    assert 'role="group" aria-label="Display competition count" data-i18n-aria-label="league.display_count"' in markup
    assert 'aria-label="Outright markets" data-i18n-aria-label="bet.championship" aria-busy="false"' in markup
    assert ".league-page-controls .segmented button:focus-visible" in styles
    assert ".league-refresh-button:focus-visible" in styles
    assert "font-size:13px" in styles.split(".league-refresh-button {", 1)[1].split("}", 1)[0]
    assert ".league-board>header select:focus-visible" in styles
    assert ".championship-fixture-row:focus-visible" in styles
    assert ".championship-team:focus-visible" in styles
    reduced_motion = styles.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".league-sync-status > i" in reduced_motion
    assert '.league-refresh-button[aria-busy="true"] svg' in reduced_motion
    assert ".championship-team" in reduced_motion


def test_league_manual_refresh_uses_standings_refresh_and_busy_state() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    renderer = script.split("function renderLeague()", 1)[1].split(
        "function championshipEnabled", 1,
    )[0]
    handler = script.split(
        '$("#league-refresh-button").addEventListener("click"', 1,
    )[1].split('$("#canteen-dock-tabs")', 1)[0]

    assert 'refreshButton.disabled = busy' in renderer
    assert 'refreshButton.setAttribute("aria-busy", String(busy))' in renderer
    assert 'request("/api/league/refresh"' in handler
    assert "if (response.started)" in handler
    assert 'beginRefreshStatusPolling("fast"' in handler


def test_league_rows_do_not_style_unverified_position_zones() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    board = script.split("function leagueBoard", 1)[1].split(
        "function leagueRenderStateSignature", 1,
    )[0]

    assert "top-zone" not in board
    assert "bottom-zone" not in board
    assert "positionClass" not in board
    assert 'class="managed"' in board


def test_league_page_background_matches_feature_pages_in_both_themes() -> None:
    styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
    dark_styles = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")

    league_page = styles.split(".league-page {", 1)[1].split("}", 1)[0]
    feature_page = styles.split(".feature-page {", 1)[1].split("}", 1)[0]
    dark_league_page = dark_styles.split(
        ':root[data-theme="dark"] .league-page {', 1,
    )[1].split("}", 1)[0]
    dark_feature_page = dark_styles.split(
        ':root[data-theme="dark"] .feature-page {', 1,
    )[1].split("}", 1)[0]

    background = lambda block: block.split("background:", 1)[1].split(";", 1)[0]

    assert background(league_page) == background(feature_page)
    assert background(dark_league_page) == background(dark_feature_page)
