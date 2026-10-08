from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CompetitionNavigationFrontendTests(unittest.TestCase):
    def test_friendly_competitions_follow_grouped_competitions_without_icon(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        groups_end = script.index('  }).join("");', script.index("function renderCompetitions()"))
        friendly_entry = script.index('data-competition="friendly"', groups_end)
        championship_entry = script.index('data-competition="championship"', friendly_entry)

        self.assertLess(groups_end, friendly_entry)
        self.assertLess(friendly_entry, championship_entry)
        self.assertIn("<span>友谊赛事</span>", script[groups_end:championship_entry])
        self.assertNotIn(
            'data-lucide="handshake"', script[groups_end:championship_entry],
        )

    def test_friendly_competition_labels_use_the_navigation_name(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertNotIn('"友谊赛"', script)
        self.assertGreaterEqual(script.count('"友谊赛事"'), 2)

    def test_competition_blacklist_is_persistent_hidden_and_recoverable(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        navigation = script.split("function renderCompetitions()", 1)[1].split(
            "function compareCompetitionReputation", 1,
        )[0]

        self.assertIn("hiddenCompetitions: new Map()", script)
        self.assertIn('request("/api/favorites/hidden-competitions"', script)
        self.assertIn('data-hide-competition="${id}"', navigation)
        self.assertIn('data-restore-competition="${id}"', navigation)
        self.assertIn('label:"已隐藏赛事"', navigation)
        self.assertIn('data-lucide="trash-2"', navigation)
        self.assertIn('data-lucide="undo-2"', navigation)
        self.assertIn("!app.hiddenCompetitions.has(Number(match.competition_id))", script)
        self.assertIn("app.hiddenCompetitions.has(Number(result.competition_id))", script)
        self.assertIn(
            '!app.hiddenCompetitions.has(Number(item.competition_id))',
            script,
        )
        self.assertIn(".hide-competition-toggle { color:#a6b0aa; opacity:0;", styles)
        self.assertIn(".competition-child-row:hover .hide-competition-toggle", styles)

    def test_hide_action_removes_unplaced_selections_but_not_bet_history(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        navigation = script.split("function renderCompetitions()", 1)[1].split(
            "function compareCompetitionReputation", 1,
        )[0]

        self.assertIn(
            "app.selections = app.selections.filter((selection) => Number(selection.competition_id) !== competitionId);",
            navigation,
        )
        self.assertNotIn("app.state.bets", navigation)

    def test_betting_competition_and_team_names_use_result_title(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        navigation = script.split("function renderCompetitions()", 1)[1].split(
            "function compareCompetitionReputation", 1,
        )[0]
        fixture_row = script.split("function fixtureRow(match)", 1)[1].split(
            "function quickButton", 1,
        )[0]

        self.assertIn('data-competition="${id}" type="button" title="${escapeHtml(competition.name)}"', navigation)
        self.assertIn('class="competition-name fixture-detail-toggle"', fixture_row)
        self.assertIn('title="${escapeHtml(fixtureLabel)}"', fixture_row)
        self.assertEqual(fixture_row.count('title="查看战绩"'), 2)

    def test_group_stage_fixture_labels_include_group_name(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        fixture_row = script.split("function fixtureRow(match)", 1)[1].split(
            "function quickButton", 1,
        )[0]

        self.assertIn(
            'const groupLabel = !match.knockout ? competitionGroupLabel(match) : "";',
            fixture_row,
        )
        self.assertIn(
            'const groupStageLabel = groupLabel ? ` · ${groupLabel}小组赛` : "";',
            fixture_row,
        )
        self.assertIn(
            "const fixtureLabel = competitionName + groupStageLabel + knockoutLeg;",
            fixture_row,
        )
        self.assertIn("function competitionGroupLabel(match)", script)
        self.assertIn("groupIndex >= 255", script)

    def test_only_live_flat_odds_use_a_neutral_symbol_and_tinted_surface(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        quick_button = script.split("function quickButton", 1)[1].split(
            "function renderInlineMarket", 1,
        )[0]

        self.assertIn(
            'app.pricingMode === "casino" && match.market_phase === "live"',
            quick_button,
        )
        self.assertIn(
            'liveMovement && movement === "flat" ? "live-flat" : ""',
            quick_button,
        )
        self.assertIn('movement === "fall" ? "↓" : "−"', quick_button)
        self.assertIn('<small aria-hidden="true">${arrow}</small>', quick_button)
        self.assertIn(
            "#page-betting .odds-button.quick-choice.odds-flat.live-flat:not(.chosen)",
            styles,
        )
        self.assertIn("background:#f7fafc;", styles)
        self.assertIn("color:#52728c;", styles)

    def test_previous_filter_loads_latest_seven_market_dates_before_today(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('data-date-filter="previous"', index)
        self.assertIn('data-date-filter="previous" aria-pressed="false">之前</button>', index)
        self.assertNotIn('data-date-filter="yesterday"', index)
        self.assertLess(index.index('data-date-filter="previous"'), index.index('data-date-filter="today"'))
        self.assertIn('app.selectedDate === "previous"', script)
        self.assertIn('await loadSeasonResults();', script)
        self.assertIn('function historicalMatches()', script)
        self.assertIn('String(result.date || "") < gameDate', script)
        self.assertIn('const HISTORICAL_MARKET_DATE_LIMIT = 7;', script)
        self.assertIn('const previousDate = activeHistoricalDate();', script)
        self.assertIn('.slice(0, HISTORICAL_MARKET_DATE_LIMIT);', script)
        self.assertIn('String(result.date || "") === previousDate', script)
        self.assertNotIn('const yesterday = dayBefore(gameDate);', script)

    def test_previous_navigation_nests_competitions_under_historical_dates(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        navigation = script.split("function renderCompetitions()", 1)[1].split(
            "function compareCompetitionReputation", 1,
        )[0]
        history = script.split("function historicalMatches()", 1)[1].split(
            "function historicalMatchFromResult", 1,
        )[0]

        self.assertIn("function historicalAvailableDates()", history)
        self.assertIn("function activeHistoricalDate()", history)
        self.assertIn("function historicalNavigationMatches(includeHidden = false, requestedDate = activeHistoricalDate())", history)
        self.assertIn("historicalResultCandidates({includeCompetition:false, includeHidden})", history)
        self.assertIn("const currentScopedMatches = bettingNavigationMatches(false);", navigation)
        self.assertIn("const scopeMatchesIncludingHidden = bettingNavigationMatches(true);", navigation)
        self.assertIn("app.scope, app.selectedCompetition, app.selectedDate", navigation)
        self.assertIn('championshipAvailable && app.selectedDate !== "previous"', navigation)
        self.assertIn('data-historical-date="${historicalDate}"', navigation)
        self.assertIn('class="historical-date-children"', navigation)
        self.assertIn('if (app.selectedHistoricalDate === "") return "";', history)
        self.assertIn(
            'app.selectedHistoricalDate = historicalDate === activeHistoricalDate() ? "" : historicalDate;',
            navigation,
        )
        self.assertIn('app.selectedCompetition = "all";', navigation)
        self.assertIn('app.selectedDate === "previous" ? "betting.rail_date" : "betting.rail_competition"', navigation)
        self.assertIn('.historical-date-parent.active', styles)
        self.assertIn('.historical-date-children', styles)

    def test_historical_fixture_keeps_score_context_and_marks_correct_choices(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        fixture_row = script.split("function fixtureRow(match)", 1)[1].split(
            "function competitionGroupLabel", 1,
        )[0]
        quick_button = script.split("function quickButton", 1)[1].split(
            "function inlineMarketRenderStateSignature", 1,
        )[0]
        market_button = script.split("function marketButton", 1)[1].split(
            "function selectionFor", 1,
        )[0]

        self.assertIn('class="team-goals"', fixture_row)
        self.assertIn('class="score-separator"', fixture_row)
        self.assertNotIn('class="half-score"', fixture_row)
        self.assertNotIn(')}球</small>', fixture_row)
        self.assertIn('const scoreSeparator = historical ? `<small class="score-separator">:</small>` : "";', fixture_row)
        self.assertIn('const phaseBadge = historical ? ""', fixture_row)
        self.assertIn('${escapeHtml(match.kickoff_time || "--:--")}${phaseBadge}', fixture_row)
        self.assertIn('quickButton(match, "home", odds.home)', fixture_row)
        self.assertIn('formatOdds(odds, "1X2")', quick_button)
        self.assertIn('formatOdds(selection.odds, selection.market)', market_button)
        self.assertIn('function historicalSelectionCorrect', script)
        self.assertIn('result-correct', script)
        self.assertIn('#page-betting .odds-button.result-correct', styles)
        self.assertIn('#page-betting .market-choice.result-correct', styles)
        self.assertIn('#page-betting .slider-add.result-correct', styles)
        self.assertNotIn('function historicalQuickButton', script)
        self.assertNotIn('历史盘口 · 只读', script)
        self.assertNotIn('正确选项', script)
        self.assertNotIn('昨日赛事不能下注', script)

    def test_historical_precision_sliders_start_at_the_completed_result(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        aligner = script.split("function alignHistoricalGoalSliders", 1)[1].split(
            "function bindSliderMarkets", 1,
        )[0]
        binder = script.split("function bindSliderMarkets", 1)[1].split(
            "function lineOptions", 1,
        )[0]

        self.assertIn("const result = match?.historical_result;", aligner)
        self.assertIn("setValue('[data-slider=\"home\"]', home);", aligner)
        self.assertIn("setValue('[data-slider=\"away\"]', away);", aligner)
        self.assertIn("setValue('[data-slider=\"total\"]', home + away);", aligner)
        self.assertIn("setValue('[data-slider=\"score-home\"]', home, true);", aligner)
        self.assertIn("setValue('[data-slider=\"score-away\"]', away, true);", aligner)
        self.assertIn("halfHome + halfAway >= 3 ? 4", aligner)
        self.assertIn("secondHome + secondAway >= 3 ? 4", aligner)
        self.assertIn("alignHistoricalGoalSliders(container, match);", binder)

    def test_inline_market_toggle_preserves_the_clicked_fixture_position(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        handler = script.split("async function handleFixturesClick(event)", 1)[1].split(
            "function fixtureListRenderSignature", 1,
        )[0]
        toggle = handler.split('  const row = event.target.closest(".fixture-row");', 1)[1]

        self.assertIn('fixtures.querySelector("[data-inline-market]")?.remove();', toggle)
        self.assertIn('row.insertAdjacentHTML("afterend"', toggle)
        self.assertIn('fixtures.dataset.listScrollTop = String(fixtures.scrollTop);', toggle)
        self.assertIn('fixtures.classList.toggle("detail-view", Boolean(nextMatch));', toggle)
        self.assertIn("fixtures.scrollTop = Number(fixtures.dataset.listScrollTop || 0);", toggle)
        self.assertIn("fixtures.scrollTop = 0;", toggle)
        self.assertNotIn("renderFixtures();", toggle)
        self.assertIn(".date-group.selected-date-group { content-visibility:visible; }", styles)
        self.assertIn("overflow-anchor:none", styles)
        self.assertIn("scrollbar-gutter:stable", styles)
        self.assertIn(".fixtures.detail-view .fixture-row.selected { position:relative;", styles)
        self.assertIn(".fixtures.detail-view .date-group:not(.selected-date-group)", styles)

    def test_inline_market_wheel_scrolls_the_fixture_list(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        handler = script.split("function handleInlineMarketWheel(event)", 1)[1].split(
            "async function handleFixturesClick(event)", 1,
        )[0]

        self.assertIn('event.target.closest(".inline-market")', handler)
        self.assertIn("fixtures.scrollTop += delta;", handler)
        self.assertIn("event.preventDefault();", handler)
        self.assertIn(
            '$("#fixtures").addEventListener("wheel", handleInlineMarketWheel, {passive:false});',
            script,
        )

    def test_background_fixture_render_preserves_detail_scroll_position(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        renderer = script.split("function renderFixtures()", 1)[1].split(
            "function fixtureRow(match)", 1,
        )[0]

        self.assertIn("const detailMatch = app.selectedMatch;", renderer)
        self.assertIn("const detailScrollTop = detailMatch ? fixtures.scrollTop : null;", renderer)
        self.assertIn("requestAnimationFrame(() =>", renderer)
        self.assertIn("app.selectedMatch === detailMatch", renderer)
        self.assertIn("fixtures.scrollTop = detailScrollTop;", renderer)
        self.assertNotIn("if (app.selectedMatch) fixtures.scrollTop = 0;", renderer)

    def test_betting_workspace_constrains_the_fixture_scroll_container(self):
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn(
            "#page-betting.active { display:grid; grid-template-rows:auto minmax(0,1fr); height:calc(100vh - 112px); min-height:0; overflow:hidden; }",
            styles,
        )
        self.assertIn(
            "#page-betting .workspace { height:auto; min-height:0; align-items:stretch; }",
            styles,
        )
        self.assertIn(
            "#page-betting .competition-rail,#page-betting .event-panel,#page-betting .betslip { height:100%; min-height:0; }",
            styles,
        )

    def test_fixture_detail_row_animates_without_sticky_overlay(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        animation = script.split("function animateFixtureRowTransition", 1)[1].split(
            "async function handleFixturesClick", 1,
        )[0]

        self.assertIn("const flight = row.cloneNode(true);", animation)
        self.assertIn('flight.classList.add("fixture-row-flight", "selected");', animation)
        self.assertIn("const to = row.getBoundingClientRect();", animation)
        self.assertIn("requestAnimationFrame(() =>", animation)
        self.assertIn('flight.classList.add("fixture-row-flight-arriving");', animation)
        self.assertIn('row.classList.remove("fixture-row-transition-target");', animation)
        self.assertIn("window.setTimeout(cleanup, 110);", animation)
        self.assertIn("transition:transform 100ms ease-out,opacity 100ms ease-out,box-shadow 100ms ease-out", styles)
        self.assertIn(".fixture-row-transition-target { opacity:0; transition:opacity 100ms ease-out; }", styles)
        self.assertIn(".fixture-row-flight.fixture-row-flight-arriving { opacity:0;", styles)
        self.assertIn(".fixtures.fixture-transitioning .inline-market { opacity:0; }", styles)
        self.assertIn(".fixtures.detail-view .fixture-row.selected { position:relative;", styles)
        self.assertNotIn(".fixtures.detail-view .fixture-row.selected { position:sticky;", styles)

    def test_betting_slip_keeps_place_button_visible_with_internal_scroll(self):
        styles = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn("#page-betting .betslip {\n    display: flex;\n    flex-direction: column;\n    min-height: 0;\n    overflow: hidden;", styles)
        self.assertIn("flex: 1 1 auto;\n    min-height: 0;\n    max-height: none;\n    overflow-x: hidden;\n    overflow-y: auto;", styles)
        self.assertIn("#page-betting .betslip .slip-controls", styles)
        self.assertIn("flex: 0 0 auto;\n    min-height: 0;\n    overflow: visible;", styles)
        self.assertNotIn("max-height: 50%;\n    overflow-y: auto;", styles)
        self.assertIn("#page-betting .betslip .place-button", styles)
        self.assertIn("height: calc(100vh - 20px);", styles)


if __name__ == "__main__":
    unittest.main()
