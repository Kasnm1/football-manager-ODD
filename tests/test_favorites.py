from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fm_odds_web


class FavoritesTests(unittest.TestCase):
    def test_defaults_are_added_once_and_collections_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(fm_odds_web, "save_data_root", return_value=root):
                initial = fm_odds_web.load_favorites([11, 12])
                self.assertEqual(initial["team_ids"], [11, 12])
                self.assertTrue(initial["managed_defaults_initialized"])

                fm_odds_web.save_favorite_competition_ids([101, 102])
                fm_odds_web.save_favorite_market_keys(["correct_score", "handicap"])
                fm_odds_web.save_favorite_team_ids([])
                saved = fm_odds_web.load_favorites([11, 12])

                self.assertEqual(saved["team_ids"], [])
                self.assertEqual(saved["competition_ids"], [101, 102])
                self.assertEqual(saved["market_keys"], ["correct_score", "handicap"])
                self.assertTrue(saved["market_keys_initialized"])
                self.assertTrue(saved["managed_defaults_initialized"])

    def test_saving_competitions_preserves_existing_teams(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(fm_odds_web, "save_data_root", return_value=root):
                fm_odds_web.save_favorite_team_ids([7, 8])
                fm_odds_web.save_favorite_competition_ids([99])
                fm_odds_web.save_favorite_market_keys([" total_goals ", "", "total_goals"])
                saved = fm_odds_web.load_favorites()

                self.assertEqual(saved["team_ids"], [7, 8])
                self.assertEqual(saved["competition_ids"], [99])
                self.assertEqual(saved["market_keys"], ["total_goals"])

    def test_hiding_competition_removes_favorite_and_preserves_other_favorites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(fm_odds_web, "save_data_root", return_value=root):
                fm_odds_web.save_favorite_team_ids([7])
                fm_odds_web.save_favorite_competition_ids([98, 99])
                fm_odds_web.save_favorite_market_keys(["asian_handicap"])

                hidden, favorites = fm_odds_web.save_hidden_competitions([
                    {"id": 99, "name": " 本国杯赛 "},
                    {"id": 99, "name": "重复名称"},
                    {"id": -1, "name": "无效"},
                ])
                saved = fm_odds_web.load_favorites()

                self.assertEqual(hidden, [{"id": 99, "name": "本国杯赛"}])
                self.assertEqual(favorites, [98])
                self.assertEqual(saved["hidden_competitions"], hidden)
                self.assertEqual(saved["competition_ids"], [98])
                self.assertEqual(saved["team_ids"], [7])
                self.assertEqual(saved["market_keys"], ["asian_handicap"])
                self.assertEqual(fm_odds_web.load_hidden_competition_ids(), [99])

    def test_restoring_hidden_competition_keeps_it_unfavorited(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(fm_odds_web, "save_data_root", return_value=root):
                fm_odds_web.save_favorite_competition_ids([99])
                fm_odds_web.save_hidden_competitions([{"id": 99, "name": "杯赛"}])
                self.assertEqual(
                    fm_odds_web.save_favorite_competition_ids([99]), [],
                )

                hidden, favorites = fm_odds_web.save_hidden_competitions([])

                self.assertEqual(hidden, [])
                self.assertEqual(favorites, [])

    def test_saving_markets_preserves_existing_teams_and_competitions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(fm_odds_web, "save_data_root", return_value=root):
                fm_odds_web.save_favorite_team_ids([7])
                fm_odds_web.save_favorite_competition_ids([99])
                fm_odds_web.save_favorite_market_keys(["asian_handicap"])
                saved = fm_odds_web.load_favorites()

                self.assertEqual(saved["team_ids"], [7])
                self.assertEqual(saved["competition_ids"], [99])
                self.assertEqual(saved["market_keys"], ["asian_handicap"])

    def test_account_ready_state_restores_favorites_before_save_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(fm_odds_web, "save_data_root", return_value=root):
                fm_odds_web.save_favorite_team_ids([7, 8])
                fm_odds_web.save_favorite_competition_ids([99])
                fm_odds_web.save_favorite_market_keys(["asian_handicap"])

                restored = fm_odds_web._public_favorites(
                    account_ready=True, default_team_ids=None,
                )

                self.assertEqual(restored["team_ids"], [7, 8])
                self.assertEqual(restored["competition_ids"], [99])
                self.assertEqual(restored["market_keys"], ["asian_handicap"])
                self.assertTrue(restored["market_keys_initialized"])
                self.assertTrue(restored["managed_defaults_initialized"])


if __name__ == "__main__":
    unittest.main()
