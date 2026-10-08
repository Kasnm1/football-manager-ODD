"""Execute the actual frontend functions with fake HTTP and DOM adapters."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = (Path(__file__).resolve().parents[1] / "src")


def function_source(start, end):
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    return start + script.split(start, 1)[1].split(end, 1)[0]


def run_js(source):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the isolated frontend harness")
    result = subprocess.run(
        [node, "-"], input="const assert = require('node:assert/strict');\n" + source,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_brand_promotion_updates_quotes_without_another_http_request():
    function = function_source("function renderPortfolioBrand", "async function loadPortfolioBrand")
    run_js('''
    const clubs = [{id:10, name:"Club", reputation:8000}];
    const listeners = {};
    const content = {innerHTML:"", querySelector:() => ({value:"50"})};
    const $ = selector => ({addEventListener:(_event, callback) => {listeners[selector] = callback;}});
    const uiText = key => key;
    const activeOwnedClubs = () => clubs;
    const activeUiLocale = () => "en-GB";
    const escapeHtml = String, formatPounds = String;
    const renderIcons = () => {}, renderMyClubs = () => {}, toast = () => {};
    const fmoddConfirm = async () => true;
    const portfolioToolRequestIsCurrent = () => true;
    const applyEconomySnapshot = () => {};
    const app = {portfolioToolRequestKey:1};
    const calls = [];
    const request = async (url) => {
      calls.push(url);
      return {native:{after:8050, applied:50}, quotes:[], economy:{}};
    };
    const runPortfolioToolOperation = async (_button, _label, task) => task();
    const loadPortfolioBrand = async () => {calls.push("brand-quote");};
    ''' + function + '''
    (async () => {
      renderPortfolioBrand(content, {team_id:10, team_name:"Club", reputation:8000,
        quotes:[{requested:50, applied:50, price:100, before:8000, after:8050}]}, 1);
      await listeners["#portfolio-brand-submit"]({currentTarget:{}});
      assert.deepEqual(calls, ["/api/world-clubs/brand-promote"]);
      assert.equal(clubs[0].reputation, 8050);
      assert.ok(content.innerHTML.includes("8,050 / 10000"));
      assert.ok(content.innerHTML.includes("portfolio.brand_maxed"));
    })().catch(error => {console.error(error); process.exitCode=1;});
    ''')


def test_partial_metrics_merges_group_totals_and_rejects_other_scope():
    functions = function_source("async function performOwnedClubMetricsLoad", "async function loadOwnedClubMetrics")
    functions += function_source("function rebuildOwnedClubPortfolioSummaryFromCache", "function updateOwnedClubCachedMetrics")
    run_js('''
    const app = {state:{data_scope_id:"scope-a"}, ownedClubMetricsScope:"scope-a",
      ownedClubMetrics:new Map([[10,{balance:100,current_valuation:500,net_acquisition_cost:300}]]),
      ownedClubMetricsVersion:0, ownedClubMetricsRequestKey:0, ownedClubMetricsLoading:false};
    const activeOwnedClubs = () => [{id:10},{id:20}];
    const renderMyClubs = () => {}, renderBank = () => {}, toast = () => {};
    const uiText = key => key, formatFullMoney = String;
    const applyEconomySnapshot = () => {};
    const loadOwnedClubMetrics = () => {throw new Error("unexpected reread");};
    const calls = [];
    let response = {data_scope_id:"scope-a", metrics_complete:false,
      metrics:{20:{balance:200,current_valuation:600,net_acquisition_cost:400}}, summary:null};
    const request = async url => {calls.push(url); return response;};
    ''' + functions + '''
    (async () => {
      assert.equal(await performOwnedClubMetricsLoad(), true);
      assert.deepEqual(calls, ["/api/world-clubs/metrics?team_ids=20"]);
      assert.equal(app.ownedClubMetrics.get(10).balance, 100);
      assert.equal(app.ownedClubMetrics.get(20).balance, 200);
      assert.equal(app.ownedClubPortfolioSummary.balance, 300);
      assert.equal(app.ownedClubPortfolioSummary.current_valuation, 1100);
      assert.equal(app.ownedClubPortfolioSummary.club_count, 2);
      assert.equal(app.ownedClubMetricsLoading, false);
      response = {data_scope_id:"scope-b", metrics:{10:{balance:999}}, summary:{balance:999}};
      await performOwnedClubMetricsLoad({force:true});
      assert.equal(app.ownedClubMetrics.get(10).balance, 100);
      assert.equal(app.ownedClubPortfolioSummary.balance, 300);
    })().catch(error => {console.error(error); process.exitCode=1;});
    ''')


def test_refresh_uses_embedded_metrics_instead_of_a_third_http_request():
    function = function_source("async function refreshOwnedClubCards", "function syncWorldClubScanPolling")
    run_js('''
    const calls = [];
    const snapshot = {data_scope_id:"scope-a", metrics:{10:{balance:100}}};
    const app = {worldClubPage:1, ownedClubMetricsPromise:null};
    const uiText = key => key;
    const request = async (url, options) => {
      calls.push(url);
      assert.equal(JSON.parse(options.body).include_metrics, true);
      return {refreshed:1, metrics_snapshot:snapshot};
    };
    const clearOwnedClubDetailCache = () => {};
    const loadWorldClubs = async () => {calls.push("/api/world-clubs");};
    const performOwnedClubMetricsLoad = async options => {
      assert.equal(options.force, true);
      assert.equal(options.snapshot, snapshot);
      return true;
    };
    const loadOwnedClubMetrics = async () => {throw new Error("unexpected metrics HTTP request");};
    ''' + function + '''
    (async () => {
      assert.equal((await refreshOwnedClubCards()).refreshed, 1);
      assert.deepEqual(calls, ["/api/world-clubs/owned-refresh", "/api/world-clubs"]);
    })().catch(error => {console.error(error); process.exitCode=1;});
    ''')
