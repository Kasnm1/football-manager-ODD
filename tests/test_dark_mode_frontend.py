from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = (Path(__file__).resolve().parents[1] / "src")


class DarkModeFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        cls.script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        cls.theme = (ROOT / "web" / "dark-theme.css").read_text(encoding="utf-8")

    def test_saved_theme_is_applied_before_stylesheets(self) -> None:
        head = self.html.split("<head>", 1)[1].split("</head>", 1)[0]

        self.assertIn('localStorage.getItem("fmodd-color-theme")', head)
        self.assertLess(head.index("fmodd-color-theme"), head.index('href="/app.css"'))
        self.assertLess(head.index('href="/app.css"'), head.index('href="/dark-theme.css"'))
        self.assertIn('meta id="theme-color" name="theme-color"', head)

    def test_display_settings_expose_a_persistent_night_mode_toggle(self) -> None:
        display_group = self.script.split('key:"display"', 1)[1].split(
            'key:"cheats"', 1,
        )[0]
        handler = self.script.split(
            '$("#dark-mode").addEventListener', 1,
        )[1].split('$("#money-currency")', 1)[0]

        self.assertIn('id="dark-mode"', self.html)
        self.assertIn('class="setting-toggle dark-mode-setting"', self.html)
        self.assertIn('".dark-mode-setting"', display_group)
        self.assertIn('localStorage.setItem(THEME_STORAGE_KEY, theme)', handler)
        self.assertIn('applyColorTheme(theme)', handler)
        self.assertIn('$("#dark-mode").checked = app.darkMode;', self.script)

    def test_theme_switch_updates_document_and_native_color_scheme(self) -> None:
        helper = self.script.split("function applyColorTheme", 1)[1].split(
            "function storedFavoriteMarkets", 1,
        )[0]

        self.assertIn('dataset.theme = "dark"', helper)
        self.assertIn('style.colorScheme = dark ? "dark" : "light"', helper)
        self.assertIn('meta[name="theme-color"]', helper)
        self.assertIn('applyColorTheme(app.darkMode ? "dark" : "light");', self.script)

    def test_dark_styles_cover_major_surface_families(self) -> None:
        for selector in (
            ':root[data-theme="dark"]',
            ".home-wallpaper",
            ".settings-tabs",
            ".competition-rail",
            ".history-content",
            ".league-page",
            ".feature-card",
            ".world-club-card",
            ".activity-page",
            ".hospital-bed-card",
            ".training-page",
            ".canteen-page",
            ".club-legacy-workspace",
            ".counseling-wheel-dialog",
        ):
            self.assertIn(selector, self.theme)

    def test_betting_team_names_are_white_in_night_mode(self) -> None:
        self.assertIn(
            ':root[data-theme="dark"] #page-betting .team-name-card { color:#fff; }',
            self.theme,
        )
        self.assertIn(
            ':root[data-theme="dark"] #page-betting .team-name-card:hover { color:#bdebd6; }',
            self.theme,
        )

    def test_explicit_light_fills_have_night_theme_overrides(self) -> None:
        audited_overrides = self.theme.split(
            "/* Explicit light fills in app.css must not leak through the night theme. */",
            1,
        )[1]

        for selector in (
            ".market-note",
            ".panel-heading-actions .refresh-button",
            ".competition-parent.expanded",
            ".owned-club-row-identity",
            ".owned-club-row-form",
            ".owned-club-row-toggle",
            ".bank-statement-pagination button",
            ".league-refresh-button",
            ".relations-tabs button",
            ".activity-nationality-pager>button",
        ):
            self.assertIn(selector, audited_overrides)

        self.assertIn("background:var(--night-surface-1)", audited_overrides)
        self.assertIn("background:var(--night-surface-3)", audited_overrides)

    def test_betting_market_interaction_states_stay_dark(self) -> None:
        market_overrides = self.theme.split(
            "/* Betting controls need page-scoped specificity",
            1,
        )[1]

        for selector in (
            "#page-betting :is(.odds-button,.market-choice,.slider-add)",
            ":hover:not(:disabled)",
            ").chosen",
            ".odds-button.quick-choice.odds-rise:not(.chosen)",
            ".odds-button.quick-choice.odds-fall:not(.chosen)",
            ".odds-button.quick-choice.odds-flat.live-flat:not(.chosen)",
            ":is(.market-choice,.slider-add).result-correct:disabled",
            "#page-betting .bet-market-panel>header",
            ".bet-market-panel>header h3",
            ".bet-market-panel>header .favorite-market:hover",
        ):
            self.assertIn(selector, market_overrides)

        self.assertIsNone(re.search(r"background\s*:\s*#fff", market_overrides))

    def test_purchase_confirmation_breakdown_stays_dark(self) -> None:
        purchase_overrides = self.theme.split(
            "/* Purchase confirmation: keep the payment breakdown inside the dark dialog. */",
            1,
        )[1]

        for selector in (
            ".purchase-confirm-message",
            ".purchase-confirm-product",
            ".purchase-confirm-breakdown",
            ".purchase-confirm-breakdown>span",
            ".purchase-confirm-breakdown .total",
        ):
            self.assertIn(selector, purchase_overrides)

        self.assertIsNone(re.search(r"background\s*:\s*#fff", purchase_overrides))

    def test_theme_does_not_introduce_too_small_user_text(self) -> None:
        sizes = [int(value) for value in re.findall(r"font-size:\s*(\d+)px", self.theme)]
        self.assertTrue(all(size >= 13 for size in sizes), sizes)


if __name__ == "__main__":
    unittest.main()
