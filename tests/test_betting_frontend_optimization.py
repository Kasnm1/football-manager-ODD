from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest
from frontend_source import read_frontend_source


ROOT = (Path(__file__).resolve().parents[1] / "src")
SCRIPT = read_frontend_source(ROOT / "web" / "app.js", mode="raw")
PROJECTED_SCRIPT = read_frontend_source(ROOT / "web" / "app.js", mode="zh-CN")
HTML = read_frontend_source(ROOT / "web" / "index.html", mode="raw")
CSS = read_frontend_source(ROOT / "web" / "app.css", mode="raw")


def _between(start: str, end: str, *, source: str = SCRIPT) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def test_betting_index_reuses_match_and_competition_maps_by_source_identity():
    index = _between("function bettingDataIndex()", "function invalidateBettingDataIndex()")

    assert "sources?.matches === matchRows" in index
    assert "sources.competitions === competitionRows" in index
    assert "matchesByKey = new Map" in index
    assert "competitionsById = new Map" in index
    assert "const competitionDataSignature = JSON.stringify" in index
    assert "competitionVersion" in index
    assert "bettingDataIndex().matchesByKey.get(key)" in SCRIPT
    assert "const matchMap = bettingDataIndex().matchesByKey;" in SCRIPT


def test_competition_rail_signature_covers_visible_navigation_state():
    renderer = _between("function renderCompetitions()", "function compareCompetitionReputation")

    assert "app.state?.data_scope_id" in renderer
    assert "app.state?.data_version" in renderer
    assert "app.scope, app.selectedCompetition" in renderer
    assert "app.expandedCompetitionGroups" in renderer
    assert "app.favoriteTeamIds" in renderer
    assert "app.favoriteCompetitionIds" in renderer
    assert "app.hiddenCompetitions.values()" in renderer
    assert "championshipIndex.navigationSignature" in renderer
    assert "bettingIndex.competitionVersion" in renderer
    assert renderer.index("app.competitionRenderSignature === renderSignature") < renderer.index(
        "const counts = new Map();"
    )


def test_bet_slip_signature_skips_unchanged_dom_and_keeps_busy_state_live():
    signature = _between("function slipRenderStateSignature", "function renderSlip()")
    renderer = _between("function renderSlip()", "function clientSubmissionId()")

    assert "app.state?.data_scope_id" in signature
    assert "app.state?.data_version" in signature
    assert "app.state?.betting_ready" in signature
    assert "app.betSubmissionBusy" in signature
    assert "app.mode, app.passCode" in signature
    assert "settings.money_rate" in signature
    assert "settings.money_symbol" in signature
    assert "item.market_version" in signature
    assert "item.odds" in signature
    assert renderer.index('slip?.setAttribute("aria-busy"') < renderer.index(
        "app.slipRenderSignature === renderSignature"
    )
    assert renderer.index('placeButton?.setAttribute("aria-busy"') < renderer.index(
        "app.slipRenderSignature === renderSignature"
    )
    assert "app.slipRenderSignature = renderSignature;" in renderer


def test_inline_market_signature_preserves_expensive_catalog_until_visible_state_changes():
    signature = _between(
        "function inlineMarketRenderStateSignature", "function renderInlineMarket()",
    )
    renderer = _between("function renderInlineMarket()", "function sliderMarkets")

    assert "app.state?.data_scope_id" in signature
    assert "app.state?.data_version" in signature
    assert "app.state?.betting_ready" in signature
    assert "app.liveQuoteSignatures.get(key)" in signature
    assert "app.marketDetailsLoading.has(key)" in signature
    assert "app.pricingMode" in signature
    assert "app.marketTab" in signature
    assert "app.showHiddenAttributes" in signature
    assert "app.favoriteMarketKeys" in signature
    assert "app.selections" in signature
    assert "app.scoreSliderValues" in signature
    assert "!matchMarketsLoaded(match)" in signature
    assert "app.inlineMarketSource === match" in renderer
    assert "app.inlineMarketContainer === container" in renderer
    assert "app.inlineMarketRenderSignature === renderSignature" in renderer
    assert renderer.index("app.inlineMarketRenderSignature === renderSignature") < renderer.index(
        'const oneXTwo = ["home", "draw", "away"]'
    )
    assert "app.inlineMarketSource = null" in SCRIPT
    assert "app.inlineMarketContainer = null" in SCRIPT
    assert "app.inlineMarketRenderSignature = null" in SCRIPT
    assert "function syncInlineMarketRenderSignature" in SCRIPT
    assert "syncInlineMarketRenderSignature(match, container);" in SCRIPT
    assert "syncInlineMarketRenderSignature();" in SCRIPT


def test_betting_page_exposes_busy_selection_and_keyboard_contracts():
    fixture = _between("function fixtureRow(match)", "function quickButton")
    quick = _between("function quickButton", "function renderInlineMarket")
    market = _between("function marketButton", "function selectionFor")

    assert 'id="page-betting" aria-labelledby="event-title" aria-busy="false"' in HTML
    assert 'id="competition-list" aria-labelledby="competition-rail-title" aria-busy="false"' in HTML
    assert 'class="event-panel" aria-labelledby="event-title" aria-busy="false"' in HTML
    assert 'class="betslip" aria-labelledby="slip-title" aria-busy="false"' in HTML
    assert 'id="slip-count" aria-live="polite"' in HTML
    assert HTML.index('id="reuse-last-bet"') < HTML.index('id="clear-slip"')
    assert '"bet.slip.reuse"' in HTML
    assert 'data-lucide="rotate-ccw"' in HTML
    assert '"bet.slip.clear"' in HTML
    assert '"bet.slip.type"' in HTML
    assert 'id="place-button" aria-busy="false" disabled' in HTML
    assert "data-fixture-detail-toggle" in fixture
    assert 'aria-expanded="${Boolean(selected)}"' in fixture
    assert 'aria-controls="${panelId}"' in fixture
    assert 'aria-pressed="${chosen}"' in quick
    assert 'aria-pressed="${selected}"' in market
    assert 'class="market-category-tabs" role="tablist"' in SCRIPT
    assert 'type="button" role="tab"' in SCRIPT
    assert 'aria-selected="${activeMarketTab === key}"' in SCRIPT
    assert 'aria-pressed="${favorite}"' in SCRIPT
    assert 'id="inline-market-catalog"' in SCRIPT
    assert 'role="tabpanel"' in SCRIPT
    assert "#page-betting .fixture-detail-toggle:focus-visible" in CSS
    assert "#page-betting .remove-selection:focus-visible" in CSS
    assert "#page-betting .place-button:focus-visible" in CSS
    assert "#page-betting .fixture-row," in CSS
    assert "#page-betting .odds-button:hover:not(:disabled) { transform: none; }" in CSS


def test_reuse_last_bet_restores_one_submission_with_current_prices():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to execute the bet reuse behavior")
    latest = "function latestBetRound()" + _between(
        "function latestBetRound()", "function currentChampionshipSelection",
        source=PROJECTED_SCRIPT,
    )
    reuse = "function reuseLatestBetRound()" + _between(
        "function reuseLatestBetRound()", "function renderSlip()",
        source=PROJECTED_SCRIPT,
    )
    probe = f"""
let app = {{
  state:{{betting_ready:true,bets:[
    {{type:"single",client_submission_id:"new",stake:12,fixture_date:"2030-01-01",competition_id:1,home_id:2,away_id:3,market:"1X2",selection_code:"home"}},
    {{type:"single",client_submission_id:"new",stake:12,fixture_date:"2030-01-01",competition_id:1,home_id:2,away_id:3,market:"BTTS",selection_code:"yes"}},
    {{type:"single",client_submission_id:"old",stake:99,fixture_date:"2029-01-01",competition_id:1,home_id:4,away_id:5,market:"1X2",selection_code:"away"}},
  ]}},
  mode:"single",passCode:null,selections:[],stake:"",betSubmissionRetry:{{id:"retry"}},
}};
const match = {{fixture_date:"2030-01-01",competition_id:1,home:{{id:2}},away:{{id:3}}}};
const matches = () => [match];
const matchKey = (item) => [item.fixture_date,item.competition_id,item.home.id,item.away.id].join("|");
const selectionMatchKey = (item) => [item.fixture_date,item.competition_id,item.home_id,item.away_id].join("|");
const selectionFor = (_match,market,selection_code,line) => ({{market,selection_code,line,odds:market === "1X2" ? 2.5 : 1.8}});
const currentChampionshipSelection = () => null;
const maximumStakeForSlip = () => 10;
const stakeInput = {{value:""}};
const $ = (selector) => selector === "#stake-input" ? stakeInput : null;
const toSpendableDisplayMoney = (value) => value;
let refreshed = false;
const refreshSelectionUI = () => {{ refreshed = true; }};
let message = "";
const toast = (value) => {{ message = value; }};
{latest}
{reuse}
const round = latestBetRound();
if (round.selections.length !== 2 || round.stake !== 12 || round.mode !== "single") process.exit(1);
reuseLatestBetRound();
if (!refreshed || app.selections.length !== 2 || app.selections[0].odds !== 2.5) process.exit(2);
if (app.stake !== 10 || stakeInput.value !== "10" || app.betSubmissionRetry !== null) process.exit(3);
if (!message.includes("注额已按可用余额调整")) process.exit(4);
"""
    subprocess.run(
        [node, "-e", probe], check=True, capture_output=True, text=True, timeout=2,
    )
