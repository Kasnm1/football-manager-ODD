from __future__ import annotations

import unittest
import json
import subprocess

from frontend_source import read_frontend_source
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProfitAnalysisColorTests(unittest.TestCase):
    def test_profit_is_red_and_loss_is_green(self):
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn("--profit-red:#c9362e", css)
        self.assertIn("--loss-green:#08784a", css)
        self.assertIn(".analysis-bar-profit { fill:var(--profit-red-soft); }", css)
        self.assertIn(".analysis-bar-loss { fill:var(--loss-green-soft); }", css)
        self.assertIn(".analysis-profit-line.profit { stroke:var(--profit-red); }", css)
        self.assertIn(".analysis-profit-line.loss { stroke:var(--loss-green); }", css)

    def test_display_setting_can_reverse_profit_and_loss_colors(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        dark_css = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")
        head = html.split("<head>", 1)[1].split("</head>", 1)[0]
        display_group = script.split('key:"display"', 1)[1].split(
            'key:"cheats"', 1,
        )[0]

        self.assertIn('fmodd-profit-loss-colors-reversed', head)
        self.assertLess(
            head.index("fmodd-profit-loss-colors-reversed"),
            head.index('href="/app.css"'),
        )
        self.assertIn('id="reverse-profit-loss-colors"', script)
        self.assertIn('".profit-loss-color-setting"', display_group)
        self.assertIn(
            'localStorage.setItem(PROFIT_LOSS_COLOR_STORAGE_KEY, String(app.reverseProfitLossColors))',
            script,
        )
        self.assertIn(
            'applyProfitLossColorPreference(app.reverseProfitLossColors);',
            script,
        )
        reversed_palette = css.split(
            ":root[data-reverse-profit-loss-colors]", 1,
        )[1].split("}", 1)[0]
        self.assertIn("--profit-red:#08784a", reversed_palette)
        self.assertIn("--loss-green:#c9362e", reversed_palette)
        dark_reversed_palette = dark_css.split(
            ':root[data-theme="dark"][data-reverse-profit-loss-colors]', 1,
        )[1].split("}", 1)[0]
        self.assertIn("--profit-red:#49bd86", dark_reversed_palette)
        self.assertIn("--loss-green:#ef756c", dark_reversed_palette)

    def test_history_profit_and_loss_surfaces_follow_the_shared_palette(self):
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        for rule in (
            ".history-list-outcome.profit,.history-list-money.profit { color:var(--profit-red); }",
            ".history-list-outcome.missed,.history-list-money.missed { color:var(--loss-green); }",
            ".settled-history-card.profit .pending-pick { background:var(--profit-solid); }",
            ".settled-history-card.missed .pending-pick { background:var(--loss-solid); }",
        ):
            self.assertIn(rule, css)

    def test_outcomes_and_movements_follow_the_shared_palette(self):
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")
        dark_css = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")

        for rule in (
            ".status.won { color:var(--profit-red); background:var(--profit-tint); }",
            ".status.lost { color:var(--loss-green); background:var(--loss-tint); }",
            ".odds-button.quick-choice.odds-rise:not(.chosen) { color:var(--profit-red);",
            ".odds-button.quick-choice.odds-fall:not(.chosen) { color:var(--loss-green);",
            ".world-form-win { background:var(--profit-tint); color:var(--profit-red); }",
            ".world-form-loss { background:var(--loss-tint); color:var(--loss-green); }",
            ".team-form-summary em.win { background:var(--profit-tint); color:var(--profit-red); }",
            ".team-form-summary em.loss { background:var(--loss-tint); color:var(--loss-green); }",
            ".home-manager-record .record-win strong { color:var(--profit-red); }",
            ".home-manager-record .record-loss strong { color:var(--loss-green); }",
            ".contract-manager-record .record-win { background:var(--profit-tint); }",
            ".contract-manager-record .record-loss { background:var(--loss-tint); }",
            ".legacy-hof-value-summary>div.up strong,",
            ".legacy-hof-value-summary>div.down strong,",
            ".transfer-history-profit.up { color:var(--profit-red); }",
            ".transfer-history-profit.down { color:var(--loss-green); }",
        ):
            self.assertIn(rule, css)

        self.assertIn(
            ':root[data-theme="dark"] #page-betting .odds-button.quick-choice.odds-rise:not(.chosen) {',
            dark_css,
        )
        self.assertIn("background:var(--profit-tint);", dark_css)
        self.assertIn("background:var(--loss-tint);", dark_css)

    def test_profit_line_can_switch_between_period_and_cumulative_values(self):
        script = (ROOT / "web" / "bet_analysis_page.js").read_text(encoding="utf-8")
        host_script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn('analysisLineMode: "period"', host_script)
        self.assertIn('data-analysis-line="period"', script)
        self.assertIn('data-analysis-line="cumulative"', script)
        self.assertIn('lineMode === "cumulative" ? row.cumulative_profit : row.profit', script)
        self.assertIn('class="analysis-profit-line ${lineClass}"', script)
        self.assertIn('app.analysisLineMode = lineButton.dataset.analysisLine;', host_script)

    def test_profit_chart_axis_always_uses_one_compact_unit(self):
        script = (ROOT / "web" / "bet_analysis_page.js").read_text(encoding="utf-8")
        host_script = read_frontend_source(ROOT / "web" / "app.js", mode="raw")

        self.assertIn(
            "const axisScale = Math.max(Math.abs(maximum), Math.abs(minimum));",
            script,
        )
        self.assertIn("formatChartAxisMoney(value, axisScale)", script)
        self.assertIn("formatChartAxisMoney,", script)
        # Verify the actual shared formatter, not the obsolete M-only ternary.
        formatter = host_script.split("const MONEY_ABBREVIATION_UNITS", 1)[1].split(
            "const formatPounds", 1,
        )[0]
        probe = r'''
const vm = require("vm");
const context = {
  toDisplayMoney: value => value,
  activeUiLocale: () => "en-GB",
  moneySymbol: () => "£",
  formatFullMoney: value => `£${value.toFixed(2)}`,
};
vm.runInNewContext(process.argv[1] + `
  results = [250000, -1500000, 2000].map(value => formatChartAxisMoney(value, 2000000));
  results.push(formatChartAxisMoney(1500, 2000));
  results.push(formatChartAxisMoney(2e30, 3e30));
`, context);
process.stdout.write(JSON.stringify(context.results));
'''
        result = subprocess.run(
            ["node", "-e", probe, "const MONEY_ABBREVIATION_UNITS" + formatter],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(json.loads(result.stdout), [
            "£0.3M", "-£1.5M", "£0.0M", "£1.5K", "£2.0Q",
        ])

    def test_history_header_toggles_show_text_after_their_icons(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn('<i data-lucide="chart-no-axes-combined"></i><span>收益分析</span>', html)
        self.assertIn('<i data-lucide="layout-list"></i><span>列表视图</span>', html)
        self.assertIn('${analysisMode ? "投注历史" : "收益分析"}</span>', script)
        self.assertIn('${listView ? "卡片视图" : "列表视图"}</span>', script)
        self.assertIn(
            ".history-view-toggle,.history-analysis-toggle { width:auto; padding:0 11px; font-size:13px;",
            css,
        )

    def test_profitable_analysis_links_to_the_donation_page(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn('id="history-support-author" href="https://fmodd.com/donate"', html)
        self.assertIn('target="_blank" rel="noopener noreferrer" hidden', html)
        self.assertIn("玩的开心？赞助作者", html)
        self.assertIn('analysisMode && Number(app.betAnalysis?.net_profit || 0) > 0', script)
        self.assertNotIn('$("#history-support-author").addEventListener', script)
        self.assertIn(".history-support-author {", css)
        self.assertIn(".history-support-author[hidden] { display:none; }", css)
        self.assertIn("font-size:13px", css.split(".history-support-author {", 1)[1].split("}", 1)[0])

    def test_shareholder_report_uses_profit_red_and_loss_green(self):
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn(
            ".portfolio-report-row .profit{color:var(--profit-red)}",
            css,
        )
        self.assertIn(
            ".portfolio-report-row .loss{color:var(--loss-green)}",
            css,
        )

    def test_settled_system_parlay_group_uses_leg_outcome_background(self):
        script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn(
            'result?.outcome === "won" ? "leg-profit"',
            script,
        )
        self.assertIn(
            'groupResults.some((result) => result?.outcome === "won") ? "leg-profit"',
            script,
        )
        self.assertIn("system-history-group ${groupClass}", script)
        self.assertIn(".settled-history-card .pending-pick.leg-profit { background:var(--profit-solid); }", css)
        self.assertIn(".settled-history-card .pending-pick.leg-missed { background:var(--loss-solid); }", css)
        self.assertIn(".system-history-options strong.leg-profit { text-decoration:underline;", css)

    def test_returned_ticket_only_grays_the_pick_block(self):
        css = (ROOT / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn(
            ".settled-history-card.returned { border-color:#d7dfdb; background:#fff; }",
            css,
        )
        self.assertIn(
            ".settled-history-card.returned .pending-picks { background:#69766f; }",
            css,
        )


if __name__ == "__main__":
    unittest.main()
