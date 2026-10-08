import struct
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fm_odds_web import LocalOddsState, result_payload
from tools.betting_account import _leg_result_key, evaluate_leg, result_details_available
from tools.game_layout import FM24_LAYOUT, FM26_LAYOUT, FM26_XGP_TEMPLATE
from tools.initial_data_audit import fm26_result_decision
from tools.live_market import live_market_quote
from tools.preview_cup_odds import (
    MAX_KNOCKOUT_STAGE_TEAM_SLOTS,
    annotate_knockout_fixture_rows, annotate_result_fixture_stages,
    competition_final_metadata,
    competition_match_role, ensure_match_market_catalog,
    fixture_knockout_context, is_competition_final_round,
    knockout_market_prices, normalize_two_legged_fixture_result,
    result_knockout_identity,
)


class KnockoutResultTests(unittest.TestCase):
    def test_result_inherits_native_fixture_stage_identity(self):
        reader = SimpleNamespace(
            competition=lambda _address: {"address": "0x9000"},
        )
        fixture = SimpleNamespace(
            match_date=date(2028, 5, 1), competition_season=0x8000,
            home_team=0x1000, away_team=0x2000,
            stage_index=1, group_index=0,
        )
        result = {
            "date": "2028-05-01",
            "competition": {"id": 7, "address": "0x9000"},
            "home_team": {"id": 1, "address": "0x1000"},
            "away_team": {"id": 2, "address": "0x2000"},
            "home_goals": 2, "away_goals": 1,
        }

        annotated = annotate_result_fixture_stages(reader, [result], [fixture])

        self.assertEqual(annotated[0]["competition_stage_index"], 1)
        self.assertEqual(annotated[0]["competition_group_index"], 0)
        self.assertNotIn("competition_stage_index", result)

    def test_fm26_steam_and_xgp_share_the_cup_stage_contract(self):
        self.assertEqual(FM26_LAYOUT.competition_actual_offset, 0xB0)
        self.assertEqual(FM26_LAYOUT.actual_competition_stages_offset, 0x1C0)
        self.assertEqual(FM26_LAYOUT.competition_stage_index_offset, 0x4E)
        self.assertEqual(FM26_LAYOUT.cup_stage_teams_offset, 0x98)
        self.assertEqual(FM26_LAYOUT.cup_stage_round_ties_offset, 0xA0)
        self.assertEqual(FM26_XGP_TEMPLATE.competition_actual_offset, 0xB0)
        self.assertEqual(FM26_XGP_TEMPLATE.actual_competition_stages_offset, 0x1C0)
        self.assertEqual(FM26_XGP_TEMPLATE.competition_stage_index_offset, 0x4E)
        self.assertEqual(FM26_XGP_TEMPLATE.cup_stage_teams_offset, 0x98)
        self.assertEqual(FM26_XGP_TEMPLATE.cup_stage_round_ties_offset, 0xA0)

    def test_fm24_and_fm26_expose_the_same_stage_contract(self):
        for layout in (FM24_LAYOUT, FM26_LAYOUT):
            self.assertEqual(layout.competition_actual_offset, 0xB0)
            self.assertEqual(layout.actual_competition_stages_offset, 0x1C0)
            self.assertIsNotNone(layout.competition_stage_type_offset)
            self.assertIsNotNone(layout.competition_stage_index_offset)
            self.assertIsNotNone(layout.cup_stage_teams_offset)
            self.assertIsNotNone(layout.cup_stage_round_ties_offset)

    def test_penalty_shootout_winner(self):
        result = fm26_result_decision(1, 1, 0xFF, 0xFF, 6, 5, 3, 10)
        self.assertEqual(result["winner_side"], "home")
        self.assertEqual(result["decided_by"], "penalties")
        self.assertEqual(result["penalty_shootout_home_goals"], 6)
        self.assertNotIn("after_extra_time_home_goals", result)

    def test_extra_time_winner(self):
        result = fm26_result_decision(1, 1, 3, 1, 0xFF, 0xFF, 2, 10)
        self.assertEqual(result["winner_side"], "home")
        self.assertEqual(result["decided_by"], "extra_time")
        self.assertEqual(result["after_extra_time_home_goals"], 3)

    def test_regular_time_winner_uses_settlement_method_vocabulary(self):
        result = fm26_result_decision(2, 1, 0xFF, 0xFF, 0xFF, 0xFF, 1, 10)
        self.assertEqual(result["winner_side"], "home")
        self.assertEqual(result["decided_by"], "regular")

    def test_regular_draw_has_no_winner(self):
        result = fm26_result_decision(1, 1, 0xFF, 0xFF, 0xFF, 0xFF, 9, 9)
        self.assertNotIn("winner_side", result)
        self.assertNotIn("decided_by", result)

    def test_two_legged_round_tie_marks_both_legs_and_settles_on_return(self):
        first = SimpleNamespace(
            address=0x100, match_date=date(2028, 4, 5), kickoff_minutes=600,
        )
        second = SimpleNamespace(
            address=0x200, match_date=date(2028, 4, 11), kickoff_minutes=900,
        )
        rows = [
            (first, {"id": 10}, {"id": 676}, {"id": 915}),
            (second, {"id": 10}, {"id": 915}, {"id": 676}),
        ]
        contexts = {
            0x100: {"tie_address": 0xABC, "orientation": "first"},
            0x200: {"tie_address": 0xABC, "orientation": "second"},
        }
        with patch(
            "tools.preview_cup_odds.fixture_knockout_context",
            side_effect=lambda _reader, fixture, **_kwargs: contexts[fixture.address],
        ):
            output = annotate_knockout_fixture_rows(object(), rows)
        self.assertEqual((output[0x100]["leg_index"], output[0x100]["leg_count"]), (1, 2))
        self.assertEqual((output[0x200]["leg_index"], output[0x200]["leg_count"]), (2, 2))
        self.assertEqual(output[0x100]["settlement_fixture"]["date"], "2028-04-11")
        self.assertEqual(output[0x100]["settlement_fixture"]["kickoff_minutes"], 900)
        self.assertEqual(output[0x100]["settlement_fixture"]["kickoff_time"], "15:00")

    def test_fm26_return_leg_uses_native_second_game_score_not_aggregate(self):
        reader = SimpleNamespace(
            layout=SimpleNamespace(result_event_record_format="fm26"),
        )
        fixture = SimpleNamespace(address=0x200)
        aggregate_result = {
            "home_goals": 3,
            "away_goals": 2,
            # This stale value remains numerically valid after the score is
            # corrected, so normalization must still force a fresh event read.
            "half_home_goals": 0,
            "half_away_goals": 0,
            "first_scoring_team": "away",
        }
        context = {
            "tie_address": 0xABC,
            "orientation": "second",
            "first_home_goals": 2,
            "first_away_goals": 0,
            "second_home_goals": 0,
            "second_away_goals": 1,
        }

        with patch(
            "tools.preview_cup_odds.fixture_knockout_context",
            return_value=context,
        ):
            normalized = normalize_two_legged_fixture_result(
                reader, fixture, aggregate_result,
            )

        self.assertEqual(
            (normalized["home_goals"], normalized["away_goals"]),
            (1, 0),
        )
        self.assertEqual(
            (normalized["aggregate_home_goals"], normalized["aggregate_away_goals"]),
            (3, 2),
        )
        self.assertEqual(normalized["leg_index"], 2)
        self.assertNotIn("half_home_goals", normalized)
        self.assertNotIn("half_away_goals", normalized)
        self.assertNotIn("first_scoring_team", normalized)

    def test_two_leg_score_is_not_inferred_before_return_leg_is_complete(self):
        reader = SimpleNamespace(
            layout=SimpleNamespace(result_event_record_format="fm26"),
        )
        result = {"home_goals": 2, "away_goals": 0}
        context = {
            "tie_address": 0xABC,
            "orientation": "first",
            "first_home_goals": 2,
            "first_away_goals": 0,
            "second_home_goals": None,
            "second_away_goals": None,
        }

        with patch(
            "tools.preview_cup_odds.fixture_knockout_context",
            return_value=context,
        ):
            normalized = normalize_two_legged_fixture_result(
                reader, SimpleNamespace(address=0x100), result,
            )

        self.assertIs(normalized, result)

    def test_large_custom_cup_stage_resolves_and_reuses_stage_reads(self):
        class FakeReader:
            def __init__(self, team_count):
                self.layout = SimpleNamespace(
                    key="fm26", competition_actual_offset=0x10,
                    actual_competition_stages_offset=0x20,
                    competition_stage_type_offset=0x30,
                    competition_stage_index_offset=0x34,
                    cup_stage_teams_offset=0x40,
                    cup_stage_round_ties_offset=0x48,
                )
                self.team_count = team_count
                self.bulk_team_reads = 0
                entries = bytearray(team_count * 0x10)
                if team_count <= MAX_KNOCKOUT_STAGE_TEAM_SLOTS:
                    struct.pack_into("<Q", entries, 500 * 0x10, 0xA000)
                    struct.pack_into("<Q", entries, 557 * 0x10, 0xB000)
                self.entries = bytes(entries)
                tie = bytearray(b"\xff" * 0x20)
                struct.pack_into("<H", tie, 0, 500)
                struct.pack_into("<H", tie, 8, 557)
                self.tie = bytes(tie)

            @staticmethod
            def _vector(begin, count):
                end = begin + count * 8
                return struct.pack("<QQQ", begin, end, end)

            def competition(self, _address):
                return {"address": "0x1000"}

            def ptr(self, address):
                return {
                    0x1010: 0x2000,
                    0x4040: 0x5000,
                    0x5000: 0x6000,
                    0x4048: 0x7000,
                }.get(address)

            def ptr_array(self, address, _count):
                return {
                    0x3000: [0x4000],
                    0x7100: [0x8000],
                    0x8100: [0x9000],
                }.get(address, [])

            def u8(self, address):
                return 0 if address == 0x4034 else None

            def u32(self, address):
                return self.team_count if address == 0x5008 else None

            def bytes(self, address, size):
                if address == 0x2020 and size == 0x18:
                    return self._vector(0x3000, 1)
                if address == 0x4030 and size == 4:
                    return b" puc"
                if address == 0x6000 and size == self.team_count * 0x10:
                    self.bulk_team_reads += 1
                    return self.entries
                if address == 0x7000 and size == 0x18:
                    return self._vector(0x7100, 1)
                if address == 0x8000 and size == 0x18:
                    return self._vector(0x8100, 1)
                if address == 0x9000 and size == 0x20:
                    return self.tie
                return None

        fixture = SimpleNamespace(
            competition_season=0xC000, stage_index=0, group_index=0,
            home_team=0xA000, away_team=0xB000,
        )
        reader = FakeReader(558)
        cache = {}
        first = fixture_knockout_context(reader, fixture, stage_cache=cache)
        second = fixture_knockout_context(reader, fixture, stage_cache=cache)
        self.assertEqual(first["tie_address"], 0x9000)
        self.assertEqual(second["tie_address"], 0x9000)
        self.assertEqual(reader.bulk_team_reads, 1)

        oversized = FakeReader(MAX_KNOCKOUT_STAGE_TEAM_SLOTS + 1)
        self.assertIsNone(
            fixture_knockout_context(oversized, fixture, stage_cache={}),
        )
        self.assertEqual(oversized.bulk_team_reads, 0)

    def test_second_leg_can_be_priced_before_first_leg_is_played(self):
        match = {
            "home": {"id": 915, "name": "Bayern"},
            "away": {"id": 676, "name": "Liverpool"},
            "xg": {"home": 1.55, "away": 1.30},
        }
        context = {
            "orientation": "second", "leg_index": 2, "leg_count": 2,
            "first_home_goals": None, "first_away_goals": None,
            "settlement_fixture": {
                "date": "2028-04-11", "home_id": 915, "away_id": 676,
                "kickoff_minutes": 900, "kickoff_time": "15:00",
            },
        }
        prices = knockout_market_prices(match, context)
        self.assertEqual(set(prices["advance"]), {"915", "676"})
        self.assertEqual(prices["settlement_kickoff_minutes"], 900)
        self.assertEqual(prices["settlement_kickoff_time"], "15:00")
        self.assertGreater(prices["extra_time"]["yes"], 1.0)
        self.assertGreater(prices["penalties"]["yes"], 1.0)

    def test_backend_reprices_all_knockout_markets(self):
        match = {
            "fixture_date": "2030-04-05", "competition_id": 10,
            "kickoff_minutes": 600, "competition_name": "测试杯赛",
            "competition_match_role": "knockout",
            "home": {"id": 676, "name": "主队", "profile": {}},
            "away": {"id": 915, "name": "客队", "profile": {}},
            "xg": {"home": 1.55, "away": 1.30},
            "casino_1x2": {"home": 2.1, "draw": 3.4, "away": 3.2},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        match["knockout"] = knockout_market_prices(match, {
            "orientation": "first", "leg_index": 1, "leg_count": 2,
            "first_leg_xg": {"home": 1.55, "away": 1.30},
            "second_leg_xg": {"home": 1.30, "away": 1.55},
            "settlement_fixture": {
                "date": "2030-04-11", "home_id": 915, "away_id": 676,
                "kickoff_minutes": 900, "kickoff_time": "15:00",
            },
        })
        output = {"matches": [match], "managed_teams": [{"id": 676}]}
        clock = {"date": "2030-04-01", "minutes": 600}
        base = {
            "fixture_date": "2030-04-05", "competition_id": 10,
            "home_id": 676, "away_id": 915, "pricing_mode": "casino",
        }
        cases = (
            ("ADVANCE", "676", "主队晋级"),
            ("EXTRA_TIME", "yes", "进入加时"),
            ("PENALTIES", "no", "不进入点球大战"),
            ("ADVANCE_METHOD", "676_extra_time", "主队 · 加时晋级"),
        )
        for market, code, label in cases:
            with self.subTest(market=market):
                leg = LocalOddsState._validated_leg(
                    output, {**base, "market": market, "selection_code": code}, clock,
                )
                self.assertEqual(leg["selection"], label)
                self.assertGreaterEqual(leg["odds"], 1.01)
                self.assertEqual(leg["settlement_fixture_date"], "2030-04-11")
                self.assertEqual(leg["settlement_home_id"], 915)
                self.assertEqual(leg["settlement_away_id"], 676)
                self.assertEqual(leg["settlement_leg_count"], 2)

    def test_live_draw_pressure_reprices_extra_time_and_backend_validation(self):
        match = {
            "fixture_date": "2030-05-28", "competition_id": 10,
            "kickoff_minutes": 900, "competition_name": "测试杯赛",
            "competition_match_role": "final",
            "home": {"id": 10, "name": "主队", "profile": {}},
            "away": {"id": 20, "name": "客队", "profile": {}},
            "xg": {"home": 1.4, "away": 1.2},
            "casino_1x2": {"home": 2.2, "draw": 3.2, "away": 3.4},
            "early_casino_1x2": {"home": 2.2, "draw": 3.2, "away": 3.4},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        match["knockout"] = knockout_market_prices(match, {
            "orientation": "first", "leg_index": 1, "leg_count": 1,
            "settlement_fixture": {
                "date": "2030-05-28", "home_id": 10, "away_id": 20,
                "kickoff_minutes": 900, "kickoff_time": "15:00",
            },
        })
        opening_extra_time = match["knockout"]["extra_time"]["yes"]
        clock = {"date": "2030-05-28", "minutes": 885}

        with (
            patch("tools.live_market._market_profile", return_value=(("draw",), 1.0)),
            patch("tools.live_market.market_trajectory", return_value="growth"),
        ):
            quote = live_market_quote(match, clock)
            leg = LocalOddsState._validated_leg(
                {"matches": [match], "managed_teams": []},
                {
                    "fixture_date": "2030-05-28", "competition_id": 10,
                    "home_id": 10, "away_id": 20, "market": "EXTRA_TIME",
                    "selection_code": "yes", "pricing_mode": "casino",
                },
                clock,
            )

        self.assertLess(quote["odds"]["draw"], match["early_casino_1x2"]["draw"])
        self.assertLess(quote["knockout"]["extra_time"]["yes"], opening_extra_time)
        self.assertEqual(leg["odds"], quote["knockout"]["extra_time"]["yes"])
        self.assertNotEqual(
            quote["knockout"]["advance_method"],
            match["knockout"]["advance_method"],
        )

    def test_frontend_applies_live_knockout_quotes_to_match_and_bet_slip(self):
        source = ((Path(__file__).resolve().parents[1] / "src") / "web" / "app.js").read_text(
            encoding="utf-8",
        )
        live_apply = source.split("function applyLiveClockQuotes(clock)", 1)[1].split(
            "function syncSelectionOddsFromMatches", 1,
        )[0]
        self.assertIn("if (quote.knockout) match.knockout = quote.knockout;", live_apply)
        self.assertIn('"ADVANCE_METHOD"', live_apply)
        self.assertIn("selectionFor(match, selection.market", live_apply)

    def test_backend_rejects_invalid_knockout_market_options(self):
        match = {
            "fixture_date": "2030-05-28", "competition_id": 10,
            "kickoff_minutes": 900, "competition_name": "测试杯赛",
            "competition_match_role": "final",
            "home": {"id": 10, "name": "主队", "profile": {}},
            "away": {"id": 20, "name": "客队", "profile": {}},
            "xg": {"home": 1.4, "away": 1.2},
            "casino_1x2": {"home": 2.2, "draw": 3.2, "away": 3.4},
        }
        self.assertTrue(ensure_match_market_catalog(match))
        match["knockout"] = knockout_market_prices(match, {
            "orientation": "first", "leg_index": 1, "leg_count": 1,
            "settlement_fixture": {
                "date": "2030-05-28", "home_id": 10, "away_id": 20,
                "kickoff_minutes": 900, "kickoff_time": "15:00",
            },
        })
        output = {"matches": [match], "managed_teams": []}
        clock = {"date": "2030-05-20", "minutes": 600}
        base = {
            "fixture_date": "2030-05-28", "competition_id": 10,
            "home_id": 10, "away_id": 20, "pricing_mode": "casino",
        }
        invalid = (
            ("ADVANCE", "999", "晋级盘口选项无效"),
            ("EXTRA_TIME", "maybe", "淘汰赛盘口选项无效"),
            ("ADVANCE_METHOD", "10_overtime", "晋级方式选项无效"),
        )
        for market, code, message in invalid:
            with self.subTest(market=market):
                with self.assertRaisesRegex(ValueError, message):
                    LocalOddsState._validated_leg(
                        output, {**base, "market": market, "selection_code": code}, clock,
                    )

    def test_terminal_single_tie_exposes_final_settlement_fixture(self):
        self.assertTrue(is_competition_final_round(2, 3, 4, 5, 1))
        self.assertFalse(is_competition_final_round(1, 3, 4, 5, 1))
        self.assertFalse(is_competition_final_round(2, 3, 3, 5, 1))
        self.assertFalse(is_competition_final_round(2, 3, 4, 5, 2))
        match = {
            "fixture_date": "2028-05-28",
            "home": {"id": 1}, "away": {"id": 2},
        }
        context = {
            "is_competition_final": True,
            "stage_index": 2, "round_index": 4, "tie_address": 0xABC,
            "settlement_fixture": {"date": "2028-05-28", "home_id": 1, "away_id": 2},
        }
        self.assertEqual(competition_final_metadata(match, context), {
            "date": "2028-05-28", "home_id": 1, "away_id": 2,
            "kickoff_minutes": None,
            "stage_index": 2, "round_index": 4, "tie_address": 0xABC,
        })
        self.assertIsNone(competition_final_metadata(match, {**context, "is_competition_final": False}))

    def test_terminal_round_roles_distinguish_third_place_and_final(self):
        self.assertEqual(
            competition_match_role(1, 2, 4, 6, 1, 1),
            "third_place",
        )
        self.assertEqual(
            competition_match_role(1, 2, 5, 6, 1, 1),
            "final",
        )
        self.assertEqual(
            competition_match_role(1, 2, 3, 6, 2, 1),
            "knockout",
        )

    def test_knockout_bet_uses_deciding_fixture_and_round_tie_decision(self):
        leg = {
            "fixture_date": "2028-04-05", "competition_id": 10,
            "home_id": 676, "away_id": 915,
            "settlement_fixture_date": "2028-04-11",
            "settlement_home_id": 915, "settlement_away_id": 676,
            "market": "ADVANCE_METHOD", "selection_code": "676_penalties", "odds": 8.0,
        }
        self.assertEqual(_leg_result_key(leg, {}), ("2028-04-11", 10, 915, 676))
        result = {
            "date": "2028-04-11", "home_goals": 1, "away_goals": 1,
            "advanced_team_id": 676, "decided_by": "penalties",
        }
        self.assertTrue(result_details_available(leg, result))
        self.assertEqual(evaluate_leg(leg, result)["outcome"], "won")

    def test_cup_final_result_supplies_winner_id_without_round_tie_fallback(self):
        result = {
            "winner_side": "home", "decided_by": "regular",
            "home_goals": 2, "away_goals": 1, "date": "2028-07-19",
        }
        display = {"home": {"id": 10, "name": "Home"}, "away": {"id": 20, "name": "Away"}}
        display.update(result_knockout_identity("cup", result, display))

        self.assertEqual(display["advanced_team_id"], 10)
        leg = {"market": "ADVANCE_METHOD", "selection_code": "10_regular", "odds": 2.0}
        settled_result = {**result, **display}
        self.assertTrue(result_details_available(leg, settled_result))
        self.assertEqual(evaluate_leg(leg, settled_result)["outcome"], "won")

    def test_national_shootout_result_supplies_advancement_winner(self):
        result = {
            "winner_side": "home", "decided_by": "penalties",
            "home_goals": 1, "away_goals": 1, "date": "2028-07-19",
        }
        display = {
            "home": {"id": 51, "name": "突尼斯"},
            "away": {"id": 6, "name": "安哥拉"},
        }

        identity = result_knockout_identity("national", result, display)

        self.assertEqual(identity["advanced_side"], "home")
        self.assertEqual(identity["advanced_team_id"], 51)
        leg = {"market": "ADVANCE", "selection_code": "51", "odds": 1.86}
        self.assertEqual(evaluate_leg(leg, {**result, **display, **identity})["outcome"], "won")

    def test_national_group_win_is_not_mislabeled_as_advancement(self):
        result = {
            "winner_side": "home", "decided_by": "regular",
            "home_goals": 2, "away_goals": 0, "date": "2028-07-19",
        }
        display = {"home": {"id": 51}, "away": {"id": 6}}

        self.assertEqual(result_knockout_identity("national", result, display), {})

    def test_single_leg_regular_winner_uses_bet_time_knockout_context(self):
        leg = {
            "market": "ADVANCE", "selection_code": "51", "odds": 1.86,
            "settlement_leg_count": 1,
            "settlement_home_id": 51, "settlement_away_id": 6,
        }
        result = {
            "home_goals": 2, "away_goals": 0,
            "winner_side": "home", "decided_by": "regular",
            "home_team": {"id": 51}, "away_team": {"id": 6},
            "date": "2028-07-19",
        }

        self.assertTrue(result_details_available(leg, result))
        self.assertEqual(evaluate_leg(leg, result)["outcome"], "won")

    def test_two_leg_regular_match_winner_is_not_assumed_to_advance(self):
        leg = {
            "market": "ADVANCE", "selection_code": "51", "odds": 1.86,
            "settlement_leg_count": 2,
            "settlement_home_id": 51, "settlement_away_id": 6,
        }
        result = {
            "home_goals": 2, "away_goals": 0,
            "winner_side": "home", "decided_by": "regular",
            "home_team": {"id": 51}, "away_team": {"id": 6},
            "date": "2028-07-19",
        }

        self.assertFalse(result_details_available(leg, result))

    def test_legacy_regular_time_result_still_settles(self):
        result = {
            "date": "2028-07-19", "home_goals": 2, "away_goals": 1,
            "advanced_team_id": 10, "decided_by": "regular_time",
        }
        leg = {"market": "ADVANCE_METHOD", "selection_code": "10_regular", "odds": 2.0}

        self.assertTrue(result_details_available(leg, result))
        self.assertEqual(evaluate_leg(leg, result)["outcome"], "won")

    def test_knockout_details_survive_public_and_light_result_merges(self):
        display = {
            "date": "2028-07-19", "competition_id": 10,
            "competition_name": "测试杯赛", "competition_kind": "cup",
            "home": {"id": 10, "name": "主队"},
            "away": {"id": 20, "name": "客队"},
            "home_goals": 1, "away_goals": 1,
            "penalty_shootout_home_goals": 5,
            "penalty_shootout_away_goals": 4,
            "winner_side": "home", "decided_by": "penalties",
            "advanced_team_id": 10,
        }
        payload = result_payload({"season_results": [display]})[0]
        self.assertEqual(payload["penalty_shootout_home_goals"], 5)
        self.assertEqual(payload["decided_by"], "penalties")
        self.assertEqual(payload["advanced_team_id"], 10)

        updated, added = LocalOddsState._merge_light_results(
            LocalOddsState.__new__(LocalOddsState),
            {"matches": [], "season_results": [], "save_instance_id": ""},
            [payload],
        )
        self.assertEqual(added, 1)
        self.assertEqual(updated["season_results"][0]["penalty_shootout_away_goals"], 4)
        self.assertEqual(updated["season_results"][0]["decided_by"], "penalties")

    def test_frontend_opens_team_form_and_labels_knockout_results(self):
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-team-card="${Number(match.home.id)}"', script)
        self.assertEqual(script.count('class="team-name-card"'), 2)
        self.assertEqual(script.count('title="查看战绩"'), 2)
        self.assertIn("async function openTeamFormCard(teamId, teamName)", script)
        self.assertIn(".slice(0, 10)", script)
        self.assertIn("function resultDecisionMark(result, side)", script)
        self.assertIn('method === "penalties" ? "P" : "ET"', script)
        self.assertIn('class="result-method-badge ${method}"', script)
        self.assertIn(".team-form-row.win .team-form-club.selected-team", styles)
        self.assertIn(".team-form-row.draw .team-form-club.selected-team", styles)
        self.assertIn(".team-form-row.loss .team-form-club.selected-team", styles)
        self.assertIn(".team-form-dialog .dialog-head p { color:#000;", styles)
        self.assertIn(".team-form-row>time,.team-form-row>.competition { overflow:hidden; color:#000;", styles)
        self.assertIn('id="team-form-dialog"', markup)

        outcome_function = script.split("function teamResultOutcome(result, teamId)", 1)[1].split(
            "async function openTeamFormCard", 1,
        )[0]
        self.assertNotIn("resultWinner", outcome_function)
        self.assertIn('goalsFor < goalsAgainst ? "loss" : "draw"', outcome_function)


if __name__ == "__main__":
    unittest.main()
