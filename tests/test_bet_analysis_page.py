from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


def test_bet_analysis_page_preserves_renderer_output_and_host_boundary() -> None:
    script = r'''
const crypto = require("crypto");
const fs = require("fs");
const vm = require("vm");
const root = process.argv[1];
const pageSource = fs.readFileSync(`${root}/web/bet_analysis_page.js`, "utf8");
const appSource = fs.readFileSync(`${root}/web/app.js`, "utf8");
const context = {window:{}};
vm.runInNewContext(pageSource, context);
const start = appSource.indexOf("function renderBetAnalysis()");
let cursor = appSource.indexOf("{", start), depth = 0, end = cursor;
for (; cursor < appSource.length; cursor += 1) { const ch = appSource[cursor]; if (ch === "{") depth += 1; else if (ch === "}" && --depth === 0) { end = cursor + 1; break; } }
const wrapper = appSource.slice(start, end);
const text = (key, params = {}) => `${key}:${JSON.stringify(params)}`;
const samples = [
  {name:"empty", app:{betAnalysis:null,analysisRange:"days-10",analysisLineMode:"period"}, length:82, hash:"9da41199339d11510b5f35beb7480f14ea892087e43ca6edc95f53ee8dab9f32"},
  {name:"days-period", app:{analysisRange:"days-10",analysisLineMode:"period",betAnalysis:{daily:[{date:"2028-06-01",profit:10,bets:1},{date:"2028-06-02",profit:-4,bets:2}],tickets:[],net_profit:6,roi:1.2,total_stake:20,settled_bets:2,total_payout:26,win_rate:50,pending_exposure:3,maximum_profit:10,longest_win_streak:1,maximum_loss:-4,longest_loss_streak:1,current_streak:-1,wins:1,losses:1,pushes:0}}, length:5095, hash:"b30419ffbc23dee8258fd542c49d29b073a5199bd7f7301285c99fdde9f1c19e"},
  {name:"bets-cumulative", app:{analysisRange:"bets-20",analysisLineMode:"cumulative",betAnalysis:{daily:[],tickets:[{date:"2028-06-01",profit:-3,bets:1},{date:"2028-06-03",profit:9,bets:1}],net_profit:6,roi:2,total_stake:12,settled_bets:2,total_payout:18,win_rate:50,pending_exposure:0,maximum_profit:9,longest_win_streak:1,maximum_loss:-3,longest_loss_streak:1,current_streak:1,wins:1,losses:1,pushes:0}}, length:5119, hash:"c301e72b209009dd8029825104d98989f5caa3eeb88b54b0668ec5eb864e2abc"},
];
const results = samples.map((sample) => {
  Object.assign(context, {app:sample.app,uiText:text,formatFullMoney:(value) => `M${Number(value || 0).toFixed(2)}`,round:(value) => Math.round(value * 100) / 100,escapeHtml:(value) => String(value),formatChartAxisMoney:(value) => `A${Number(value).toFixed(1)}`});
  vm.runInNewContext(`${wrapper}; result = renderBetAnalysis();`, context);
  const output = context.result;
  return {name:sample.name, equalsPage:output === context.window.FMODDBetAnalysisPage.render({analysis:sample.app.betAnalysis,range:sample.app.analysisRange,analysisLineMode:sample.app.analysisLineMode,uiText:context.uiText,formatFullMoney:context.formatFullMoney,round:context.round,escapeHtml:context.escapeHtml,formatChartAxisMoney:context.formatChartAxisMoney}), length:output.length, hash:crypto.createHash("sha256").update(output).digest("hex"), expectedLength:sample.length, expectedHash:sample.hash};
});
console.log(JSON.stringify({results, pageHasAppReference:/\bapp\./.test(pageSource)}));
'''
    result = subprocess.run(
        ["node", "-e", script, str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert not payload["pageHasAppReference"]
    for sample in payload["results"]:
        assert sample["equalsPage"]
        assert sample["length"] == sample["expectedLength"]
        assert sample["hash"] == sample["expectedHash"]


def test_bet_analysis_page_loads_before_the_history_controller() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app_script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert html.index('src="/bet_analysis_page.js"') < html.index('src="/app.js"')
    assert "window.FMODDBetAnalysisPage.render({" in app_script
    assert 'event.target.closest("[data-analysis-line]")' in app_script
    assert 'event.target.closest("[data-analysis-range]")' in app_script
