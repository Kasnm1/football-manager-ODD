import unittest
from unittest.mock import patch
from datetime import date

from tools import club_sponsorship as sponsorship


class SponsorshipTests(unittest.TestCase):
    def setUp(self):
        self.state = sponsorship._empty_state()

    def _patch_store(self):
        def update(_name, default, mutator, _scope=None):
            current = self.state
            result = mutator(current)
            self.state = current
            return result
        return patch.object(sponsorship, "load_document", return_value=self.state), patch.object(
            sponsorship, "update_document", side_effect=update,
        )

    def test_league_quote_is_one_quarter_of_complete_mean(self):
        self.assertEqual(sponsorship.league_quote_annual([1_000, 600, -400]), 100)
        with self.assertRaises(ValueError):
            sponsorship.league_quote_annual([])

    def test_offer_delay_and_three_offer_cap(self):
        with self._patch_store()[0] as load, self._patch_store()[1]:
            first = sponsorship.start_search(
                "save", team_id=10, team_name="测试俱乐部", competition_id=20,
                competition_name="测试联赛", annual_value=250, game_date="2028-01-01",
                candidates=[{"id": 1, "name": "球员甲"}],
            )
            offer = first["offer"]
            self.assertGreater(offer["available_after_game_date"], "2028-01-01")
            with self.assertRaisesRegex(ValueError, "下一份意向"):
                sponsorship.continue_search("save", team_id=10, game_date="2028-01-02")
            second = sponsorship.continue_search(
                "save", team_id=10, game_date=offer["available_after_game_date"],
            )
            third = sponsorship.continue_search(
                "save", team_id=10, game_date=second["offer"]["available_after_game_date"],
            )
            self.assertEqual(third["campaign"]["offers_total"], 3)
            with self.assertRaisesRegex(ValueError, "三份报价"):
                sponsorship.continue_search("save", team_id=10, game_date="2030-01-01")
            loaded = load.return_value
            self.assertEqual(len(loaded["campaigns"]["10"]["offers"]), 3)
            self.assertEqual({row["annual_value"] for row in loaded["campaigns"]["10"]["offers"]}, {250})

    def test_leap_day_contract_end_and_idempotent_acceptance(self):
        with self._patch_store()[1]:
            first = sponsorship.start_search(
                "save", team_id=11, team_name="闰日俱乐部", competition_id=20,
                competition_name="测试联赛", annual_value=400, game_date="2028-02-29",
            )
            offer = first["offer"]
            accepted = sponsorship.accept_offer(
                "save", team_id=11, offer_id=offer["offer_id"],
                game_date=offer["available_after_game_date"],
            )
            self.assertEqual(sponsorship._add_years(date(2028, 2, 29), 1), date(2029, 2, 28))
            again = sponsorship.accept_offer(
                "save", team_id=11, offer_id=offer["offer_id"],
                game_date=offer["available_after_game_date"],
            )
            self.assertTrue(again["idempotent"])
            self.assertEqual(again["payment"]["payment_id"], accepted["payment"]["payment_id"])

    def test_rollback_restores_offer_selection(self):
        with self._patch_store()[1]:
            first = sponsorship.start_search(
                "save", team_id=12, team_name="回滚俱乐部", competition_id=20,
                competition_name="测试联赛", annual_value=400, game_date="2028-01-01",
            )
            offer = first["offer"]
            sponsorship.accept_offer(
                "save", team_id=12, offer_id=offer["offer_id"],
                game_date=offer["available_after_game_date"],
            )
            result = sponsorship.rollback_acceptance(
                "save", team_id=12, offer_id=offer["offer_id"],
            )
            self.assertTrue(result["rolled_back"])
            self.assertNotIn("12", self.state["contracts"])
            self.assertEqual(self.state["campaigns"]["12"]["status"], "searching")


if __name__ == "__main__":
    unittest.main()
