from __future__ import annotations

import struct
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, patch
from types import SimpleNamespace

from fm_odds_web import (
    archive_schedule_refund_mail,
    captured_same_day_result_keys,
    detected_pending_schedule_changes,
    missing_result_timeout_due,
    result_payload,
)
from tools.betting_account import (
    manual_refund_options,
    manual_refund_pending_bets,
    pending_due_result_keys,
    pending_result_fixture_hints,
    refund_pending_schedule_changes,
    reopen_bets,
    settled_missing_half_score_keys,
    settle_pending_bets,
)
from tools.initial_data_audit import (
    _read_result_event_records,
    parse_completed_result_from_raw,
    parse_season_completed_result,
    reconcile_fm24_result_event_summary,
    reconcile_result_event_summary,
)
from tools.preview_cup_odds import (
    collapse_adjacent_duplicate_fixtures,
    enrich_halftime_results,
    fixture_is_open,
    merge_result_history,
    result_details_consistent,
)


def _result(home_goals: int, away_goals: int, **details):
    return {
        "date": "2028-06-06",
        "competition": {"id": 100},
        "home_team": {"id": 10, "name": "Home"},
        "away_team": {"id": 20, "name": "Away"},
        "home_goals": home_goals,
        "away_goals": away_goals,
        "settlement_verified": True,
        "settlement_evidence": "manual_verified_result_override",
        **details,
    }


class ResultSettlementRegressionTests(unittest.TestCase):
    def test_result_payload_preserves_settlement_authority(self):
        display = {
            "date": "2028-06-06", "competition_id": 100,
            "home": {"id": 10}, "away": {"id": 20},
            "home_goals": 2, "away_goals": 1,
            "kickoff_minutes": 1215, "kickoff_time": "20:15",
            "settlement_verified": True,
            "settlement_evidence": "independent_native_result_consensus",
            "settlement_sources": [
                {"result_source": "fixture_result_archive"},
                {"result_source": "persistent_season_record"},
            ],
        }

        payload = result_payload({"settlement_results": [display]})[0]

        self.assertTrue(payload["settlement_verified"])
        self.assertEqual(
            payload["settlement_evidence"],
            "independent_native_result_consensus",
        )
        self.assertEqual(payload["kickoff_minutes"], 1215)
        self.assertEqual(payload["kickoff_time"], "20:15")

    def test_result_payload_excludes_hall_of_fame_goal_events(self):
        display = {
            "date": "2028-06-06", "competition_id": 100,
            "home": {"id": 10}, "away": {"id": 20},
            "home_goals": 2, "away_goals": 1,
            "settlement_verified": True,
            "goal_events": [{"time": {"minute": 88}, "scorer": {"id": 9}}],
        }

        payload = result_payload({"settlement_results": [display]})[0]

        self.assertNotIn("goal_events", payload)

    def test_native_result_records_recover_kickoff_slot(self):
        # FM's slot 58 decodes to 20:15: ((58 + 23) * 15) % 1440.
        date_code = (2028 << 16) | (58 << 9) | 158
        layout = SimpleNamespace(
            result_event_record_format="fm24",
            result_home_goals_offset=0x50,
            result_away_goals_offset=0x51,
            result_home_outcome_offset=0x52,
            result_away_outcome_offset=0x53,
        )
        teams = {
            0x10: {"id": 10, "name": "Home", "short_name": "Home"},
            0x20: {"id": 20, "name": "Away", "short_name": "Away"},
        }
        competition = {"id": 100, "name": "League"}

        archive_raw = bytearray(0x80)
        struct.pack_into("<QQQQQ", archive_raw, 0x08, 0x10, 0x20, 0, 0x30, 0)
        struct.pack_into("<I", archive_raw, 0x4C, date_code)
        archive_raw[0x50:0x54] = bytes((2, 1, 1, 10))
        archive_reader = SimpleNamespace(
            layout=layout,
            team=lambda address: teams.get(address),
            competition=lambda address: competition if address == 0x30 else None,
        )

        season_raw = bytearray(0xC0)
        struct.pack_into("<I", season_raw, 0x10, date_code)
        struct.pack_into("<QQ", season_raw, 0x18, 0x10, 0x20)
        struct.pack_into("<Q", season_raw, 0x38, 0x30)
        season_raw[0x28:0x2A] = bytes((2, 1))
        season_raw[0x33:0x35] = bytes((1, 10))
        season_reader = SimpleNamespace(
            layout=layout,
            bytes=lambda address, size: bytes(season_raw),
            team=lambda address: teams.get(address),
            competition=lambda address: competition if address == 0x30 else None,
        )

        archive = parse_completed_result_from_raw(
            archive_reader, 0x1000, bytes(archive_raw),
        )
        persistent = parse_season_completed_result(season_reader, 0x2000)

        for result in (archive, persistent):
            self.assertIsNotNone(result)
            self.assertEqual(result["date"], "2028-06-06")
            self.assertEqual(result["kickoff_minutes"], 1215)
            self.assertEqual(result["kickoff_time"], "20:15")

    @staticmethod
    def _goal_event(minute: int, *, away: bool = False, conditional: bool = False):
        return {
            "scorer": 1,
            "assist": 0xFFFFFFFF,
            "clock": minute * 256,
            "flags": (minute << 16) | int(away) | (0x300 if conditional else 0),
            "minute_code": minute,
        }

    def test_captured_same_day_result_is_released_only_after_active_match_ends(self):
        key = ("2028-06-06", 100, 10, 20)
        captured = {("save-1", *key)}

        self.assertEqual(
            captured_same_day_result_keys(
                {key}, key[0], "save-1",
                {"known": True, "active": False}, True, captured,
            ),
            {key},
        )
        self.assertEqual(
            captured_same_day_result_keys(
                {key}, key[0], "save-1",
                {"known": True, "active": True}, True, captured,
            ),
            set(),
        )
        self.assertEqual(
            captured_same_day_result_keys(
                {key}, key[0], "save-2",
                {"known": True, "active": False}, True, captured,
            ),
            set(),
        )

    def test_conditional_excess_event_is_removed_when_unambiguous(self):
        records = [
            self._goal_event(7, away=True),
            self._goal_event(24, away=True),
            self._goal_event(47, away=True),
            self._goal_event(68, away=True, conditional=True),
        ]

        summary = reconcile_result_event_summary(records, 0, 3)

        self.assertEqual(summary, {
            "half_home_goals": 0,
            "half_away_goals": 2,
            "first_scoring_team": "away",
        })

    def test_counted_conditional_event_is_retained_when_score_matches(self):
        summary = reconcile_result_event_summary(
            [self._goal_event(30, conditional=True)], 1, 0,
        )

        self.assertEqual(summary["half_home_goals"], 1)
        self.assertEqual(summary["first_scoring_team"], "home")

    def test_fm26_reconciles_extra_time_goal_against_regular_time_score(self):
        records = [
            self._goal_event(26),
            self._goal_event(68, away=True),
            self._goal_event(93, away=True),
        ]

        summary = reconcile_result_event_summary(records, 1, 1)

        self.assertEqual(summary, {
            "half_home_goals": 1,
            "half_away_goals": 0,
            "first_scoring_team": "home",
        })

    def test_fm26_retains_regular_stoppage_time_goal_when_score_matches(self):
        records = [
            self._goal_event(26),
            self._goal_event(93, away=True),
        ]

        summary = reconcile_result_event_summary(records, 1, 1)

        self.assertEqual(summary, {
            "half_home_goals": 1,
            "half_away_goals": 0,
            "first_scoring_team": "home",
        })

    def test_ambiguous_conditional_events_are_rejected(self):
        records = [
            self._goal_event(20, conditional=True),
            self._goal_event(70, conditional=True),
        ]

        self.assertIsNone(reconcile_result_event_summary(records, 1, 0))

    def test_fm24_reconciles_disallowed_goal_without_dropping_penalty_goal(self):
        records = [
            {**self._goal_event(12), "flags": 0x200, "minute_code": 12},
            {**self._goal_event(30), "flags": 0x300, "minute_code": 30},
            {**self._goal_event(44), "flags": 0, "minute_code": 44},
        ]

        summary = reconcile_fm24_result_event_summary(records, 2, 0)

        self.assertEqual(summary, {
            "half_home_goals": 2,
            "half_away_goals": 0,
            "first_scoring_team": "home",
        })

    def test_fm24_reader_accepts_extra_conditional_event_record(self):
        event_data = b"".join((
            struct.pack("<IHH", 1, 0, 12),
            struct.pack("<IHH", 2, 0x300, 30),
            struct.pack("<IHH", 3, 0, 44),
        ))

        class Reader:
            layout = SimpleNamespace(
                result_event_root_offset=0x70,
                result_event_record_size=8,
                result_event_record_format="fm24",
            )

            @staticmethod
            def ptr(_address):
                return 0x2000

            @staticmethod
            def bytes(address, _size):
                if address == 0x2000:
                    return struct.pack(
                        "<QQQ", 0x30000,
                        0x30000 + len(event_data), 0x30000 + len(event_data),
                    )
                return event_data if address == 0x30000 else None

        records = _read_result_event_records(Reader(), 0x1000, expected_goals=2)

        self.assertEqual(len(records), 3)

    def test_fm24_ignores_extra_time_and_shootout_records_for_90_minutes(self):
        records = [
            {**self._goal_event(30), "flags": 0, "minute_code": 30},
            {**self._goal_event(40, away=True), "flags": 1, "minute_code": 40},
            {**self._goal_event(106, away=True), "flags": 1, "minute_code": 106},
            {**self._goal_event(120), "flags": 0x400, "minute_code": 120},
            {**self._goal_event(120, away=True), "flags": 0x501, "minute_code": 120},
        ]

        summary = reconcile_fm24_result_event_summary(records, 1, 1)

        self.assertEqual(summary, {
            "half_home_goals": 1,
            "half_away_goals": 1,
            "first_scoring_team": "home",
        })

    def test_same_day_fixture_closes_after_kickoff(self):
        fixture = SimpleNamespace(
            match_date=date(2028, 12, 17), kickoff_minutes=840,
        )

        self.assertTrue(fixture_is_open(fixture, date(2028, 12, 17), 839))
        self.assertTrue(fixture_is_open(fixture, date(2028, 12, 17), 840))
        self.assertFalse(fixture_is_open(fixture, date(2028, 12, 17), 841))
        self.assertFalse(fixture_is_open(fixture, date(2028, 12, 18), 0))

    def test_rescheduled_fixture_clones_keep_the_newer_date(self):
        competition = {"id": 200}
        home = {"id": 30}
        away = {"id": 40}
        old = SimpleNamespace(
            address=0x1000, match_date=date(2027, 4, 26), home_team=0x3000,
            away_team=0x4000, round_or_slot=5,
        )
        current = SimpleNamespace(
            address=0x2000, match_date=date(2027, 4, 27), home_team=0x3000,
            away_team=0x4000, round_or_slot=5,
        )

        rows = collapse_adjacent_duplicate_fixtures([
            (current, competition, home, away),
            (old, competition, home, away),
        ])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].match_date, date(2027, 4, 27))

    def test_verified_schedule_change_detects_postponement_and_disappearance(self):
        postponed = {
            "fixture_date": "2028-06-08", "competition_id": 200,
            "home": {"id": 30}, "away": {"id": 40},
            "kickoff_minutes": 900,
        }
        disappeared = {
            "fixture_date": "2028-06-09", "competition_id": 201,
            "home": {"id": 50}, "away": {"id": 60},
            "kickoff_minutes": 600,
        }
        previous = {
            "full_verified_at": "2028-06-06T10:00:00",
            "save_instance_id": "save-123", "odds_scope": "all", "odds_days": 14,
            "hidden_competition_ids": [], "matches": [postponed, disappeared],
        }
        current = {
            **previous,
            "game_date": "2028-06-07", "game_time": "12:00",
            "fixture_scan_mode": "heap",
            "matches": [
                {**postponed, "fixture_date": "2028-06-10", "kickoff_minutes": 930},
                {
                    "fixture_date": "2028-06-11", "competition_id": 202,
                    "home": {"id": 70}, "away": {"id": 80},
                    "kickoff_minutes": 700,
                },
            ],
        }

        changes = detected_pending_schedule_changes(previous, current, [])

        self.assertEqual(changes[("2028-06-08", 200, 30, 40)]["reason"], "fixture_postponed")
        self.assertEqual(changes[("2028-06-08", 200, 30, 40)]["new_date"], "2028-06-10")
        self.assertEqual(changes[("2028-06-09", 201, 50, 60)]["reason"], "fixture_disappeared")

    def test_started_same_day_fixture_is_not_treated_as_disappeared(self):
        fixture = {
            "fixture_date": "2028-06-08", "competition_id": 200,
            "home": {"id": 30}, "away": {"id": 40},
            "kickoff_minutes": 600,
        }
        previous = {
            "full_verified_at": "2028-06-08T09:00:00",
            "save_instance_id": "save-123", "odds_scope": "all", "odds_days": 14,
            "hidden_competition_ids": [], "matches": [fixture],
        }
        current = {
            **previous,
            "game_date": "2028-06-08", "game_time": "10:30",
            "fixture_scan_mode": "heap",
            "matches": [{
                "fixture_date": "2028-06-09", "competition_id": 201,
                "home": {"id": 50}, "away": {"id": 60},
                "kickoff_minutes": 600,
            }],
        }

        self.assertEqual(detected_pending_schedule_changes(previous, current, []), {})

    def test_schedule_change_detection_prefers_result_and_catches_same_day_delay(self):
        fixture = {
            "fixture_date": "2028-06-08", "competition_id": 200,
            "home": {"id": 30}, "away": {"id": 40},
            "kickoff_minutes": 600,
        }
        previous = {
            "full_verified_at": "2028-06-07T09:00:00",
            "save_instance_id": "save-123", "odds_scope": "all", "odds_days": 14,
            "hidden_competition_ids": [], "matches": [fixture],
        }
        delayed = {
            **previous, "game_date": "2028-06-07", "game_time": "12:00",
            "fixture_scan_mode": "heap",
            "matches": [{**fixture, "kickoff_minutes": 720}],
        }

        changes = detected_pending_schedule_changes(previous, delayed, [])
        self.assertEqual(changes[("2028-06-08", 200, 30, 40)]["reason"], "fixture_postponed")

        result = {
            "date": "2028-06-08", "competition": {"id": 200},
            "home_team": {"id": 30}, "away_team": {"id": 40},
            "home_goals": 1, "away_goals": 0,
        }
        self.assertEqual(
            detected_pending_schedule_changes(previous, delayed, [result]), {},
        )

    def test_schedule_change_refunds_entire_parlay_atomically(self):
        changed_leg = {
            "fixture_date": "2028-06-08", "competition_id": 200,
            "home_id": 30, "away_id": 40, "home": "Home", "away": "Away",
        }
        record = {
            "bet_id": "schedule-refund", "status": "pending", "type": "parlay",
            "stake": 100.0,
            "legs": [changed_leg, {
                "fixture_date": "2028-06-09", "competition_id": 201,
                "home_id": 50, "away_id": 60, "home": "Other", "away": "Team",
            }],
        }
        wallet = {"balance": 900.0, "transactions": []}
        changes = {("2028-06-08", 200, 30, 40): {
            "reason": "fixture_postponed", "original_date": "2028-06-08",
            "new_date": "2028-06-10",
        }}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = refund_pending_schedule_changes(changes)

        self.assertEqual(result["refunded"], 1)
        self.assertEqual(result["returned"], 100.0)
        self.assertEqual(record["status"], "schedule_refund")
        self.assertEqual(record["payout"], 100.0)
        self.assertEqual(wallet["balance"], 1000.0)
        self.assertEqual(wallet["transactions"][-1]["type"], "bet_schedule_refund")
        self.assertTrue(all(item["outcome"] == "void" for item in record["settlement"]))
        commit.assert_called_once_with([record], wallet, "refund_pending_schedule_changes")

    def test_disappeared_fixture_waits_for_three_day_leg_void(self):
        changes = {("2028-06-08", 200, 30, 40): {
            "reason": "fixture_disappeared", "original_date": "2028-06-08",
            "new_date": None,
        }}
        with (
            patch("tools.betting_account.load_bets") as load_bets,
            patch("tools.betting_account.load_wallet") as load_wallet,
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = refund_pending_schedule_changes(changes)

        self.assertEqual(result, {"refunded": 0, "returned": 0.0, "records": []})
        load_bets.assert_not_called()
        load_wallet.assert_not_called()
        commit.assert_not_called()

    def test_schedule_refund_archives_mail_notification(self):
        refunded = [{
            "bet_id": "schedule-refund", "type": "single", "legs": 1,
            "home": "Home", "away": "Away", "game_date": "2028-06-08",
            "payout": 100.0,
            "refund_changes": [{
                "reason": "fixture_postponed", "new_date": "2028-06-10",
            }],
        }]
        with (
            patch("fm_odds_web.load_mail", return_value=[]),
            patch("fm_odds_web.save_mail") as save,
        ):
            added = archive_schedule_refund_mail(refunded)

        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["type"], "bet_schedule_refund")
        self.assertIn("比赛延期", added[0]["title"])
        self.assertIn("全额退回", added[0]["message"])
        save.assert_called_once()

    def test_schedule_refund_has_distinct_history_labels(self):
        script = Path("web/app.js").read_text(encoding="utf-8")

        self.assertIn('schedule_refund:"赛程异常退款"', script)
        self.assertIn('record.schedule_refund_reason === "fixture_postponed"', script)
        self.assertIn('"比赛延期 · 已自动退款"', script)
        self.assertIn('"赛程消失 · 已自动退款"', script)

    def test_different_native_rounds_are_not_collapsed(self):
        competition = {"id": 200}
        home = {"id": 30}
        away = {"id": 40}
        first = SimpleNamespace(
            address=0x1000, match_date=date(2027, 4, 26), home_team=0x3000,
            away_team=0x4000, round_or_slot=5,
        )
        second = SimpleNamespace(
            address=0x2000, match_date=date(2027, 4, 27), home_team=0x3000,
            away_team=0x4000, round_or_slot=6,
        )

        rows = collapse_adjacent_duplicate_fixtures([
            (first, competition, home, away),
            (second, competition, home, away),
        ])

        self.assertEqual(len(rows), 2)

    def test_two_legged_cup_tie_keeps_reversed_home_and_away_fixtures(self):
        competition = {"id": 200}
        first = SimpleNamespace(
            address=0x1000, match_date=date(2027, 4, 26), home_team=0x3000,
            away_team=0x4000, round_or_slot=5,
        )
        second = SimpleNamespace(
            address=0x2000, match_date=date(2027, 4, 27), home_team=0x4000,
            away_team=0x3000, round_or_slot=5,
        )

        rows = collapse_adjacent_duplicate_fixtures([
            (first, competition, {"id": 30}, {"id": 40}),
            (second, competition, {"id": 40}, {"id": 30}),
        ])

        self.assertEqual(len(rows), 2)

    def test_changed_final_score_is_preserved_as_a_conflict(self):
        stale = _result(
            6, 0,
            half_home_goals=5,
            half_away_goals=0,
            first_scoring_team="home",
        )
        corrected = _result(1, 2)

        merged = merge_result_history([stale, corrected], None)

        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["result_conflict"])
        self.assertEqual(
            {
                (item["home_goals"], item["away_goals"])
                for item in merged[0]["result_conflict_candidates"]
            },
            {(6, 0), (1, 2)},
        )
        self.assertNotIn("half_home_goals", merged[0])
        self.assertNotIn("half_away_goals", merged[0])
        self.assertNotIn("first_scoring_team", merged[0])

    def test_result_history_keeps_known_kickoff_when_new_capture_omits_it(self):
        known = _result(2, 1, kickoff_minutes=1215, kickoff_time="20:15")
        repeated = _result(2, 1)

        merged = merge_result_history([known, repeated], None)

        self.assertEqual(merged[0]["kickoff_minutes"], 1215)
        self.assertEqual(merged[0]["kickoff_time"], "20:15")

    def test_result_history_deep_copies_known_goal_events_when_repeat_omits_them(self):
        events = [{"minute": 88, "player_team_id": 10}]
        known = _result(2, 1, goal_events=events)
        repeated = _result(2, 1)

        merged = merge_result_history([known, repeated], None)

        self.assertEqual(merged[0]["goal_events"], events)
        self.assertIsNot(merged[0]["goal_events"], events)

    def test_result_without_date_is_rejected_before_model_comparisons(self):
        invalid = {**_result(1, 0), "date": None}

        self.assertEqual(merge_result_history([invalid], None), [])

    def test_impossible_halftime_score_is_discarded(self):
        impossible = _result(
            1, 2,
            half_home_goals=5,
            half_away_goals=0,
            first_scoring_team="home",
        )

        self.assertFalse(result_details_consistent(impossible))
        merged = merge_result_history([impossible], None)
        self.assertNotIn("half_home_goals", merged[0])
        self.assertNotIn("half_away_goals", merged[0])
        self.assertEqual(merged[0]["first_scoring_team"], "home")

    def test_valid_halftime_score_survives_without_first_scorer(self):
        partial = _result(
            2, 1,
            half_home_goals=1,
            half_away_goals=0,
        )

        self.assertFalse(result_details_consistent(partial))
        merged = merge_result_history([partial], None)

        self.assertEqual(merged[0]["half_home_goals"], 1)
        self.assertEqual(merged[0]["half_away_goals"], 0)
        self.assertNotIn("first_scoring_team", merged[0])

    def test_invalid_first_scorer_does_not_discard_valid_halftime_score(self):
        partial = _result(
            2, 1,
            half_home_goals=1,
            half_away_goals=0,
            first_scoring_team="none",
        )

        merged = merge_result_history([partial], None)

        self.assertEqual(merged[0]["half_home_goals"], 1)
        self.assertEqual(merged[0]["half_away_goals"], 0)
        self.assertNotIn("first_scoring_team", merged[0])

    def test_missing_next_day_result_stays_pending(self):
        record = {
            "bet_id": "pending-fm24",
            "status": "pending",
            "type": "single",
            "game_date": "2027-02-01",
            "fixture_date": "2027-02-02",
            "competition_id": 200,
            "home_id": 30,
            "away_id": 40,
            "home": "Korea",
            "away": "Jordan",
            "market": "1X2",
            "selection_code": "draw",
            "stake": 100.0,
            "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account.migrate_legacy_bets", return_value=0),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([], "2027-02-03")

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertNotIn("settlement", record)
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_not_called()

    def test_later_halftime_result_enriches_history_without_recalculation(self):
        record = {
            "bet_id": "settled-full-time",
            "status": "won",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "market": "1X2",
            "selection_code": "home",
            "stake": 100.0,
            "odds": 2.0,
            "payout": 200.0,
            "settlement": [{
                "outcome": "won", "score": "2-1", "half_score": None,
                "result_date": "2028-06-06",
            }],
        }
        wallet = {
            "balance": 1100.0,
            "transactions": [{"type": "bet_settlement", "amount": 200.0}],
        }
        result = _result(2, 1, half_home_goals=1, half_away_goals=0)

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([result], "2028-06-07")

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["details_updated"], 1)
        self.assertEqual(record["settlement"][0]["half_score"], "1-0")
        self.assertEqual(record["payout"], 200.0)
        self.assertEqual(wallet["balance"], 1100.0)
        self.assertEqual(len(wallet["transactions"]), 1)
        commit.assert_called_once()

    def test_recent_settled_leg_missing_halftime_is_scheduled_for_backfill(self):
        expected = ("2028-06-06", 100, 10, 20)
        records = [{
            "bet_id": "settled-full-time",
            "status": "lost",
            "type": "single",
            "fixture_date": expected[0],
            "competition_id": expected[1],
            "home_id": expected[2],
            "away_id": expected[3],
            "settlement": [{
                "outcome": "lost", "score": "5-1", "half_score": None,
                "result_date": expected[0],
            }],
        }, {
            "bet_id": "already-complete",
            "status": "won",
            "type": "single",
            "fixture_date": "2028-06-07",
            "competition_id": 200,
            "home_id": 30,
            "away_id": 40,
            "settlement": [{"score": "2-0", "half_score": "1-0"}],
        }, {
            "bet_id": "manual-refund",
            "status": "manual_refund",
            "type": "single",
            "fixture_date": "2028-06-07",
            "competition_id": 300,
            "home_id": 50,
            "away_id": 60,
            "settlement": [{"score": "手动退款", "half_score": None}],
        }]

        with patch("tools.betting_account.load_bets", return_value=records):
            keys = settled_missing_half_score_keys("2028-06-08")

        self.assertEqual(keys, {expected})

    def test_old_settled_leg_is_not_kept_in_backfill_window(self):
        record = {
            "bet_id": "old-settlement",
            "status": "lost",
            "type": "single",
            "fixture_date": "2028-06-01",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "settlement": [{"score": "1-0", "half_score": None}],
        }

        with patch("tools.betting_account.load_bets", return_value=[record]):
            keys = settled_missing_half_score_keys("2028-06-08")

        self.assertEqual(keys, set())

    def test_pending_fixture_hint_uses_retained_source_snapshot_once(self):
        key = ("2028-06-06", 100, 10, 20)
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text(
                '{"matches":[{"fixture_date":"2028-06-06",'
                '"competition_id":100,"home":{"id":10},"away":{"id":20},'
                '"fixture_address":"0x1234"}]}',
                encoding="utf-8",
            )
            record = {
                "status": "pending", "type": "single",
                "fixture_date": key[0], "competition_id": key[1],
                "home_id": key[2], "away_id": key[3],
                "source_snapshot": str(snapshot),
            }
            with patch("tools.betting_account.load_bets", return_value=[record]):
                hints = pending_result_fixture_hints({key})

        self.assertEqual(hints, {key: 0x1234})

    def test_halftime_backfill_key_recovers_settled_fixture_hint(self):
        key = ("2028-06-06", 100, 10, 20)
        record = {
            "bet_id": "settled-full-time",
            "status": "won",
            "type": "single",
            "fixture_date": key[0],
            "competition_id": key[1],
            "home_id": key[2],
            "away_id": key[3],
            "fixture_address": "0x5678",
            "settlement": [{"score": "2-1", "half_score": None}],
        }

        with patch("tools.betting_account.load_bets", return_value=[record]):
            self.assertEqual(
                pending_result_fixture_hints({key}), {key: 0x5678},
            )
            self.assertEqual(pending_result_fixture_hints(), {})

    def test_duplicate_public_identity_with_two_fixture_addresses_is_ambiguous(self):
        key = ("2028-06-06", 100, 10, 20)
        records = [
            {
                "status": "pending", "type": "single",
                "fixture_date": key[0], "competition_id": key[1],
                "home_id": key[2], "away_id": key[3],
                "fixture_address": address,
            }
            for address in ("0x1111", "0x2222")
        ]

        with patch("tools.betting_account.load_bets", return_value=records):
            self.assertEqual(pending_result_fixture_hints({key}), {})

    def test_direct_and_snapshot_fixture_addresses_must_not_disagree(self):
        key = ("2028-06-06", 100, 10, 20)
        with TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.json"
            snapshot.write_text(
                '{"matches":[{"fixture_date":"2028-06-06",'
                '"competition_id":100,"home":{"id":10},"away":{"id":20},'
                '"fixture_address":"0x2222"}]}',
                encoding="utf-8",
            )
            record = {
                "status": "pending", "type": "single",
                "fixture_date": key[0], "competition_id": key[1],
                "home_id": key[2], "away_id": key[3],
                "fixture_address": "0x1111", "source_snapshot": str(snapshot),
            }

            with patch("tools.betting_account.load_bets", return_value=[record]):
                self.assertEqual(pending_result_fixture_hints({key}), {})

    def test_real_result_reopens_false_fixture_cancellation(self):
        record = {
            "bet_id": "false-cancellation",
            "status": "void",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "1X2",
            "selection_code": "home",
            "stake": 100.0,
            "odds": 2.0,
            "payout": 100.0,
            "settlement": [{
                "outcome": "void",
                "score": "比赛取消/改期",
                "result_date": "2028-06-06",
                "reason": "fixture_cancelled",
            }],
        }
        wallet = {"balance": 1000.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(2, 1)], "2028-06-07")

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")
        self.assertEqual(record["payout"], 200.0)
        self.assertEqual(wallet["balance"], 1100.0)
        self.assertEqual(wallet["transactions"][0]["type"], "premature_cancellation_reversal")
        commit.assert_called_once()

    def test_false_fixture_cancellation_reopens_before_result_is_available(self):
        record = {
            "bet_id": "false-cancellation-pending",
            "status": "void",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "stake": 100.0,
            "odds": 2.0,
            "payout": 100.0,
            "settlement": [{
                "outcome": "void",
                "score": "比赛取消/改期",
                "result_date": "2028-06-06",
                "reason": "fixture_cancelled",
            }],
        }
        wallet = {"balance": 1000.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([], "2028-06-07")

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_called_once()

    def test_legacy_halftime_refund_is_reopened_and_kept_pending(self):
        record = {
            "bet_id": "legacy-halftime-refund",
            "status": "void",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "market": "HTFT",
            "selection_code": "draw_home",
            "stake": 100.0,
            "odds": 4.0,
            "payout": 100.0,
            "settlement": [{
                "outcome": "void",
                "score": "2-1",
                "result_date": "2028-06-06",
                "reason": "half_time_result_timeout",
            }],
        }
        wallet = {"balance": 1000.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(2, 1)], "2028-07-10")

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["returned"], 0.0)
        self.assertEqual(record["status"], "pending")
        self.assertNotIn("settlement", record)
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_called_once()

    def test_legacy_no_result_refund_is_reopened_then_voided_after_timeout(self):
        record = {
            "bet_id": "legacy-no-result-refund",
            "status": "no_result",
            "type": "single",
            "fixture_date": "2027-02-02",
            "competition_id": 200,
            "home_id": 30,
            "away_id": 40,
            "market": "1X2",
            "selection_code": "draw",
            "stake": 100.0,
            "odds": 2.0,
            "payout": 100.0,
            "no_result_reason": "missing result",
            "settlement": [{
                "outcome": "no_result",
                "score": "no result",
                "result_date": "2027-02-02",
            }],
        }
        wallet = {"balance": 1000.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [], "2027-03-01", void_missing_after_timeout=True,
            )

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "void")
        self.assertEqual(
            record["settlement"][0]["reason"],
            "missing_result_after_three_days",
        )
        self.assertEqual(wallet["balance"], 1000.0)
        commit.assert_called_once()

    def test_legacy_winning_parlay_with_cancelled_leg_is_reopened(self):
        first_leg = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "market": "1X2",
            "selection_code": "home", "odds": 2.0,
        }
        second_leg = {
            "fixture_date": "2028-06-07", "competition_id": 101,
            "home_id": 30, "away_id": 40, "market": "1X2",
            "selection_code": "away", "odds": 2.0,
        }
        record = {
            "bet_id": "legacy-winning-cancelled-parlay",
            "status": "won",
            "type": "parlay",
            "stake": 100.0,
            "payout": 200.0,
            "legs": [first_leg, second_leg],
            "settlement": [
                {
                    "outcome": "void", "reason": "fixture_cancelled",
                    "score": "cancelled", "result_date": "2028-06-06",
                },
                {
                    "outcome": "won", "score": "0-1",
                    "result_date": "2028-06-07",
                },
            ],
        }
        wallet = {"balance": 1100.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([], "2028-06-08")

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_called_once()

    def test_missing_result_voids_after_three_game_days(self):
        record = {
            "bet_id": "refund-after-three-checks",
            "status": "pending",
            "type": "single",
            "fixture_date": "2027-02-02",
            "competition_id": 200,
            "home_id": 30,
            "away_id": 40,
            "home": "Korea",
            "away": "Jordan",
            "market": "1X2",
            "selection_code": "draw",
            "stake": 100.0,
            "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            before = settle_pending_bets(
                [], "2027-02-04", void_missing_after_timeout=True,
            )
            after = settle_pending_bets(
                [], "2027-02-05", void_missing_after_timeout=True,
            )

        self.assertEqual(before["settled"], 0)
        self.assertEqual(after["settled"], 1)
        self.assertEqual(after["returned"], 100.0)
        self.assertEqual(record["status"], "void")
        self.assertEqual(
            record["settlement"][0]["reason"],
            "missing_result_after_three_days",
        )
        self.assertEqual(record["settlement"][0]["return_multiplier"], 1.0)
        self.assertEqual(wallet["balance"], 1000.0)
        commit.assert_called_once_with([record], wallet, "settle_pending_bets")

    def test_parlay_missing_leg_voids_and_preserves_winning_leg_after_three_days(self):
        record = {
            "bet_id": "parlay-with-phantom", "status": "pending", "type": "parlay",
            "stake": 100.0,
            "legs": [{
                "fixture_date": "2028-06-06", "competition_id": 100,
                "home_id": 10, "away_id": 20, "home": "Home", "away": "Away",
                "market": "1X2", "selection_code": "home", "odds": 2.0,
            }, {
                "fixture_date": "2028-06-06", "competition_id": 200,
                "home_id": 30, "away_id": 40, "home": "Fake", "away": "Fixture",
                "market": "1X2", "selection_code": "home", "odds": 3.0,
            }],
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [_result(2, 1)], "2028-06-09",
                void_missing_after_timeout=True,
            )

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(settlement["returned"], 200.0)
        self.assertEqual(record["status"], "won")
        self.assertEqual(record["settlement"][0]["outcome"], "won")
        self.assertEqual(record["settlement"][1]["outcome"], "void")
        self.assertEqual(wallet["balance"], 1100.0)
        commit.assert_called_once()

    def test_system_parlay_settles_when_phantom_group_voids_after_three_days(self):
        first = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "home": "Home", "away": "Away",
            "market": "1X2", "odds": 3.0,
        }
        phantom = {
            "fixture_date": "2028-06-06", "competition_id": 200,
            "home_id": 30, "away_id": 40, "home": "Fake", "away": "Fixture",
            "market": "1X2", "odds": 4.0,
        }
        first_group = [
            {**first, "selection_code": "draw"},
            {**first, "selection_code": "away"},
        ]
        phantom_group = [
            {**phantom, "selection_code": "home"},
            {**phantom, "selection_code": "away"},
        ]
        record = {
            "bet_id": "system-with-phantom", "status": "pending",
            "type": "system_parlay", "stake": 100.0, "unit_stake": 25.0,
            "combination_count": 4, "pass_code": "2X1",
            "legs": [*first_group, *phantom_group],
            "groups": [
                {"selections": first_group}, {"selections": phantom_group},
            ],
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account"),
        ):
            settlement = settle_pending_bets(
                [_result(2, 1)], "2028-06-09",
                void_missing_after_timeout=True,
            )

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(settlement["returned"], 0.0)
        self.assertEqual(record["status"], "lost")
        self.assertTrue(all(
            item["outcome"] == "void" for item in record["settlement"][2:]
        ))

    def test_missing_result_timeout_uses_three_game_days(self):
        key = ("2028-06-06", 100, 10, 20)
        self.assertFalse(missing_result_timeout_due({key}, "2028-06-08"))
        self.assertTrue(missing_result_timeout_due({key}, "2028-06-09"))

    def test_missing_same_day_result_stays_pending(self):
        record = {
            "bet_id": "same-day-pending",
            "status": "pending",
            "type": "single",
            "fixture_date": "2027-02-02",
            "competition_id": 200,
            "home_id": 30,
            "away_id": 40,
            "stake": 100.0,
            "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = settle_pending_bets([], "2027-02-02")

        self.assertEqual(result["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertNotIn("no_result_check_count", record)
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_not_called()

    def test_untrusted_same_day_result_does_not_refund_or_settle_pending_bet(self):
        record = {
            "bet_id": "precreated-result",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "AH",
            "selection_code": "home",
            "line": 0.0,
            "stake": 100.0,
            "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(0, 0)], "2028-06-06")

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["returned"], 0.0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(wallet["balance"], 900.0)
        self.assertEqual(wallet["transactions"], [])
        commit.assert_not_called()

    def test_trusted_same_day_result_waits_until_earliest_finish(self):
        key = ("2028-06-06", 100, 10, 20)
        record = {
            "bet_id": "trusted-but-too-early", "status": "pending",
            "type": "single", "fixture_date": key[0],
            "competition_id": key[1], "home_id": key[2], "away_id": key[3],
            "kickoff_minutes": 1200, "market": "1X2",
            "selection_code": "home", "stake": 100.0, "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [_result(2, 1)], key[0], game_minutes=1304,
                trusted_same_day_keys={key},
            )

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_not_called()

    def test_trusted_same_day_result_settles_at_earliest_finish(self):
        key = ("2028-06-06", 100, 10, 20)
        record = {
            "bet_id": "trusted-and-due", "status": "pending",
            "type": "single", "fixture_date": key[0],
            "competition_id": key[1], "home_id": key[2], "away_id": key[3],
            "kickoff_minutes": 1200, "market": "1X2",
            "selection_code": "home", "stake": 100.0, "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [_result(2, 1)], key[0], game_minutes=1305,
                trusted_same_day_keys={key},
            )

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")
        commit.assert_called_once()

    def test_parlay_persists_due_leg_result_without_final_settlement(self):
        first = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "home": "Home", "away": "Away",
            "market": "1X2", "selection_code": "home", "odds": 2.0,
        }
        last = {
            "fixture_date": "2028-06-08", "competition_id": 200,
            "home_id": 30, "away_id": 40, "home": "Later Home",
            "away": "Later Away", "market": "1X2",
            "selection_code": "away", "odds": 3.0,
        }
        record = {
            "bet_id": "partial-parlay", "status": "pending", "type": "parlay",
            "stake": 100.0, "odds": 6.0, "legs": [first, last],
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(2, 1)], "2028-06-07")
            repeated = settle_pending_bets([], "2028-06-07")

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(repeated["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["partial_settlement"][0]["outcome"], "won")
        self.assertIsNone(record["partial_settlement"][1])
        self.assertNotIn("settlement", record)
        self.assertEqual(wallet["balance"], 900.0)
        self.assertEqual(wallet["transactions"], [])
        commit.assert_called_once()

    def test_final_parlay_settlement_replaces_partial_leg_projection(self):
        first = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "home": "Home", "away": "Away",
            "market": "1X2", "selection_code": "home", "odds": 2.0,
        }
        last = {
            "fixture_date": "2028-06-08", "competition_id": 200,
            "home_id": 30, "away_id": 40, "home": "Later Home",
            "away": "Later Away", "market": "1X2",
            "selection_code": "away", "odds": 3.0,
        }
        record = {
            "bet_id": "completed-parlay", "status": "pending", "type": "parlay",
            "stake": 100.0, "odds": 6.0, "legs": [first, last],
            "partial_settlement": [
                {"outcome": "won", "score": "2-1", "return_multiplier": 2.0},
                None,
            ],
        }
        last_result = {
            "date": "2028-06-08", "competition": {"id": 200},
            "home_team": {"id": 30, "name": "Later Home"},
            "away_team": {"id": 40, "name": "Later Away"},
            "home_goals": 0, "away_goals": 1,
            "settlement_verified": True,
            "settlement_evidence": "manual_verified_result_override",
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [_result(2, 1), last_result], "2028-06-09",
            )

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")
        self.assertNotIn("partial_settlement", record)
        self.assertEqual([item["outcome"] for item in record["settlement"]], ["won", "won"])
        self.assertEqual(wallet["balance"], 1500.0)
        self.assertEqual(wallet["transactions"][-1]["type"], "bet_settlement")
        commit.assert_called_once()

    def test_due_result_keys_use_absolute_time_across_midnight(self):
        record = {
            "bet_id": "late-kickoff", "status": "pending", "type": "single",
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "kickoff_minutes": 1410,
        }
        key = ("2028-06-06", 100, 10, 20)

        with patch("tools.betting_account.load_bets", return_value=[record]):
            self.assertEqual(pending_due_result_keys("2028-06-07", 74), set())
            self.assertEqual(pending_due_result_keys("2028-06-07", 75), {key})

    def test_winning_settlement_credits_payout_and_wallet_transaction_together(self):
        record = {
            "bet_id": "winning-payout",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "1X2",
            "selection_code": "home",
            "stake": 100.0,
            "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(2, 1)], "2028-06-07")

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(settlement["returned"], 200.0)
        self.assertEqual(record["status"], "won")
        self.assertEqual(record["payout"], 200.0)
        self.assertEqual(wallet["balance"], 1100.0)
        self.assertEqual(wallet["transactions"][-1]["type"], "bet_settlement")
        self.assertEqual(wallet["transactions"][-1]["amount"], 200.0)
        commit.assert_called_once_with([record], wallet, "settle_pending_bets")

    def test_selected_manual_refunds_apply_fee_before_kickoff_and_full_after_three_days(self):
        early = {
            "bet_id": "early-refund",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-07",
            "kickoff_minutes": 900,
            "stake": 100.0,
        }
        started = {
            "bet_id": "started-refund",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-04",
            "kickoff_minutes": 600,
            "stake": 200.0,
        }
        untouched = {
            "bet_id": "untouched",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-09",
            "kickoff_minutes": 600,
            "stake": 50.0,
        }
        records = [early, started, untouched]
        wallet = {"balance": 650.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=records),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = manual_refund_pending_bets(
                {"early-refund", "started-refund"}, "2028-06-07", 800,
            )

        self.assertEqual(result["refunded"], 2)
        self.assertEqual(result["returned"], 299.0)
        self.assertEqual(result["fee"], 1.0)
        self.assertEqual(wallet["balance"], 949.0)
        self.assertEqual(early["status"], "manual_refund")
        self.assertEqual(early["payout"], 99.0)
        self.assertEqual(early["manual_refund_type"], "early_cashout")
        self.assertEqual(started["status"], "manual_refund")
        self.assertEqual(started["payout"], 200.0)
        self.assertEqual(started["manual_refund_type"], "post_match_three_day_full")
        self.assertEqual(untouched["status"], "pending")
        self.assertEqual(wallet["transactions"][-1]["type"], "manual_bet_refund")
        commit.assert_called_once_with(records, wallet, "manual_refund_pending_bets")

    def test_post_match_manual_refund_is_locked_until_three_days_after_match_end(self):
        record = {
            "bet_id": "three-day-lock", "status": "pending", "type": "single",
            "fixture_date": "2028-06-04", "kickoff_minutes": 600,
            "stake": 100.0,
        }

        with patch("tools.betting_account.load_bets", return_value=[record]):
            before = manual_refund_options("2028-06-07", 704)
            after = manual_refund_options("2028-06-07", 705)

        self.assertEqual(before["options"], [])
        self.assertEqual(len(before["all_options"]), 1)
        self.assertFalse(before["all_options"][0]["available"])
        self.assertEqual(
            before["all_options"][0]["available_at"], "2028-06-07T11:45",
        )
        self.assertEqual(len(after["options"]), 1)
        self.assertEqual(after["options"][0]["refund"], 100.0)

    def test_rescheduled_date_controls_manual_refund_clock(self):
        record = {
            "bet_id": "rescheduled-refund", "status": "pending", "type": "single",
            "fixture_date": "2028-06-04", "kickoff_minutes": 600,
            "settlement_fixture_date": "2028-06-10",
            "settlement_kickoff_minutes": 900,
            "stake": 100.0,
        }

        with patch("tools.betting_account.load_bets", return_value=[record]):
            quote = manual_refund_options("2028-06-08", 800)["all_options"][0]

        self.assertFalse(quote["started"])
        self.assertTrue(quote["available"])
        self.assertEqual(quote["fee"], 1.0)
        self.assertEqual(quote["refund"], 99.0)

    def test_rescheduled_kickoff_controls_manual_refund_clock(self):
        record = {
            "bet_id": "rescheduled-kickoff", "status": "pending", "type": "single",
            "fixture_date": "2028-06-04", "kickoff_minutes": 600,
            "settlement_fixture_date": "2028-06-10",
            "settlement_kickoff_minutes": 900,
            "stake": 100.0,
        }

        with patch("tools.betting_account.load_bets", return_value=[record]):
            before = manual_refund_options("2028-06-10", 800)["all_options"][0]
            after = manual_refund_options("2028-06-10", 900)["all_options"][0]

        self.assertFalse(before["started"])
        self.assertTrue(before["available"])
        self.assertEqual(before["refund"], 99.0)
        self.assertTrue(after["started"])
        self.assertFalse(after["available"])
        self.assertEqual(after["available_at"], "2028-06-13T16:45")

    def test_current_fixture_restores_rescheduled_kickoff_for_legacy_bet(self):
        record = {
            "bet_id": "legacy-rescheduled-kickoff", "status": "pending",
            "type": "single", "fixture_date": "2028-06-04",
            "kickoff_minutes": 600, "competition_id": 10,
            "home_id": 1, "away_id": 2,
            "settlement_fixture_date": "2028-06-10",
            "settlement_home_id": 2, "settlement_away_id": 1,
            "stake": 100.0,
        }
        fixture_kickoffs = {("2028-06-10", 10, 2, 1): 900}

        with patch("tools.betting_account.load_bets", return_value=[record]):
            quote = manual_refund_options(
                "2028-06-10", 800, None, fixture_kickoffs,
            )["all_options"][0]

        self.assertFalse(quote["started"])
        self.assertTrue(quote["available"])
        self.assertEqual(quote["refund"], 99.0)

    def test_championship_refund_uses_published_settlement_date(self):
        record = {
            "bet_id": "stale-championship",
            "status": "pending",
            "type": "championship",
            "season_key": "77:2028",
            "season_start": "2028-01-01",
            "season_end": "2028-12-31",
            "settlement_date": "",
            "stake": 100.0,
        }
        settlement_dates = {"77:2028": "2028-06-01"}
        wallet = {"balance": 900.0, "transactions": []}

        with patch("tools.betting_account.load_bets", return_value=[record]):
            locked = manual_refund_options(
                "2028-06-03", 1439, settlement_dates,
            )
            available = manual_refund_options(
                "2028-06-04", 0, settlement_dates,
            )

        self.assertEqual(locked["options"], [])
        self.assertEqual(len(available["options"]), 1)
        self.assertEqual(available["options"][0]["refund"], 100.0)

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = manual_refund_pending_bets(
                {"stale-championship"}, "2028-06-04", 0,
                settlement_dates,
            )

        self.assertEqual(result["returned"], 100.0)
        self.assertEqual(record["status"], "manual_refund")
        self.assertEqual(record["manual_refund_type"], "post_match_three_day_full")
        self.assertEqual(record["settlement_date"], "2028-06-01")
        self.assertEqual(record["settlement"][0]["result_date"], "2028-06-01")
        commit.assert_called_once()

    def test_post_kickoff_manual_refund_submission_is_rejected_before_three_days(self):
        record = {
            "bet_id": "locked-refund", "status": "pending", "type": "single",
            "fixture_date": "2028-06-07", "kickoff_minutes": 600,
            "stake": 100.0,
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet") as load_wallet,
            patch("tools.betting_account._commit_account") as commit,
        ):
            with self.assertRaisesRegex(ValueError, "结束未满3天"):
                manual_refund_pending_bets({"locked-refund"}, "2028-06-07", 800)

        self.assertEqual(record["status"], "pending")
        load_wallet.assert_not_called()
        commit.assert_not_called()

    def test_parlay_refund_waits_three_days_after_its_last_leg(self):
        first = {
            "fixture_date": "2028-06-01", "kickoff_minutes": 600,
        }
        last = {
            "fixture_date": "2028-06-05", "kickoff_minutes": 600,
        }
        record = {
            "bet_id": "last-leg-wait", "status": "pending", "type": "parlay",
            "stake": 100.0, "legs": [first, last],
        }

        with patch("tools.betting_account.load_bets", return_value=[record]):
            too_early = manual_refund_options("2028-06-07", 1200)
            unlocked = manual_refund_options("2028-06-08", 705)

        self.assertEqual(too_early["options"], [])
        self.assertEqual(len(unlocked["options"]), 1)

    def test_manual_refund_rejects_changed_selection_atomically(self):
        pending = {
            "bet_id": "still-pending", "status": "pending",
            "fixture_date": "2028-06-08", "stake": 100.0,
        }
        settled = {
            "bet_id": "already-settled", "status": "won",
            "fixture_date": "2028-06-07", "stake": 100.0, "payout": 200.0,
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[pending, settled]),
            patch("tools.betting_account.load_wallet") as load_wallet,
            patch("tools.betting_account._commit_account") as commit,
        ):
            with self.assertRaisesRegex(ValueError, "已结算或不存在"):
                manual_refund_pending_bets(
                    {"still-pending", "already-settled"}, "2028-06-07", 700,
                )

        self.assertEqual(pending["status"], "pending")
        load_wallet.assert_not_called()
        commit.assert_not_called()

    def test_missing_halftime_result_stays_pending_before_three_days(self):
        record = {
            "bet_id": "pending-halftime",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "HTFT",
            "selection_code": "draw_home",
            "stake": 100.0,
            "odds": 4.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(2, 1)], "2028-06-08")

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_not_called()

    def test_missing_halftime_result_remains_pending_after_third_day(self):
        record = {
            "bet_id": "refund-halftime",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "HTFT",
            "selection_code": "draw_home",
            "stake": 100.0,
            "odds": 4.0,
        }
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(2, 1)], "2028-06-09")

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["returned"], 0.0)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(wallet["balance"], 900.0)
        commit.assert_not_called()

    def test_voided_advance_bet_is_reopened_and_paid_from_shootout_winner(self):
        record = {
            "bet_id": "recovered-advance",
            "status": "void",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "ADVANCE",
            "selection_code": "10",
            "stake": 100.0,
            "odds": 1.86,
            "payout": 100.0,
            "settlement": [{
                "outcome": "void",
                "score": "1-1",
                "result_date": "2028-06-06",
                "reason": "missing_result_details",
            }],
        }
        wallet = {"balance": 1000.0, "transactions": []}
        result = _result(
            1, 1, winner_side="home", decided_by="penalties",
            advanced_team_id=10,
        )

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([result], "2028-06-10")

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(settlement["returned"], 186.0)
        self.assertEqual(record["status"], "won")
        self.assertEqual(record["settlement"][0]["outcome"], "won")
        self.assertEqual(wallet["balance"], 1086.0)
        commit.assert_called_once()

    def test_recovered_halftime_result_reopens_auto_refund_and_recalculates(self):
        record = {
            "bet_id": "recovered-halftime",
            "status": "void",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "HT_1X2",
            "selection_code": "away",
            "stake": 100.0,
            "odds": 2.0,
            "payout": 100.0,
            "settlement": [{
                "outcome": "void",
                "score": "2-4",
                "half_score": None,
                "result_date": "2028-06-06",
                "reason": "missing_result_details",
            }],
        }
        wallet = {"balance": 1000.0, "transactions": []}
        result = _result(
            2, 4, half_home_goals=1, half_away_goals=3,
            first_scoring_team="away",
        )

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([result], "2028-06-09")

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(settlement["returned"], 200.0)
        self.assertEqual(record["status"], "won")
        self.assertEqual(record["settlement"][0]["half_score"], "1-3")
        self.assertEqual(record["reopen_reason"], "recovered_result_details_reversal")
        self.assertEqual(wallet["balance"], 1100.0)
        commit.assert_called_once()

    def test_system_parlay_same_fixture_options_are_not_duplicate_legs(self):
        first_home = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "home": "Home", "away": "Away",
            "market": "1X2", "selection_code": "home", "odds": 2.0,
        }
        first_draw = {
            **first_home, "selection_code": "draw", "odds": 3.0,
        }
        second_away = {
            "fixture_date": "2028-06-07", "competition_id": 101,
            "home_id": 30, "away_id": 40, "home": "Second Home", "away": "Second Away",
            "market": "1X2", "selection_code": "away", "odds": 4.0,
        }
        record = {
            "bet_id": "system-same-fixture-options",
            "status": "pending",
            "type": "system_parlay",
            "stake": 20.0,
            "unit_stake": 10.0,
            "combination_count": 2,
            "pass_code": "2X1",
            "legs": [first_home, first_draw, second_away],
            "groups": [
                {"selections": [first_home, first_draw]},
                {"selections": [second_away]},
            ],
        }
        wallet = {"balance": 980.0, "transactions": []}
        second_result = {
            "date": "2028-06-07",
            "competition": {"id": 101},
            "home_team": {"id": 30},
            "away_team": {"id": 40},
            "home_goals": 0,
            "away_goals": 1,
            "settlement_verified": True,
            "settlement_evidence": "manual_verified_result_override",
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [_result(2, 1), second_result], "2028-06-08",
            )

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(settlement["returned"], 80.0)
        self.assertEqual(record["status"], "won")
        self.assertEqual(
            [item["outcome"] for item in record["settlement"]],
            ["won", "lost", "won"],
        )
        self.assertEqual(record["settlement_summary"]["winning_combinations"], 1)
        self.assertEqual(wallet["balance"], 1060.0)
        commit.assert_called_once()

    def test_legacy_same_group_duplicate_voids_are_reopened_and_recalculated(self):
        first = {
            "fixture_date": "2028-06-06", "competition_id": 100,
            "home_id": 10, "away_id": 20, "home": "First Home", "away": "First Away",
            "market": "1X2", "selection_code": "away", "odds": 2.5,
        }
        away = {
            "fixture_date": "2028-06-07", "competition_id": 101,
            "home_id": 30, "away_id": 40, "home": "Second Home", "away": "Second Away",
            "market": "1X2", "selection_code": "away", "odds": 3.0,
        }
        draw = {**away, "selection_code": "draw", "odds": 4.0}
        home = {**away, "selection_code": "home", "odds": 2.08}
        record = {
            "bet_id": "legacy-same-group-duplicate",
            "status": "lost", "type": "system_parlay",
            "stake": 30.0, "unit_stake": 10.0,
            "combination_count": 3, "pass_code": "2X1",
            "legs": [first, away, draw, home],
            "groups": [
                {"selections": [first]},
                {"selections": [away, draw, home]},
            ],
            "payout": 0.0,
            "settlement": [
                {"outcome": "lost", "score": "1-0", "result_date": "2028-06-06"},
                {"outcome": "lost", "score": "2-0", "result_date": "2028-06-07"},
                {
                    "outcome": "void", "score": "重复赛程",
                    "result_date": "2028-06-07", "reason": "duplicate_fixture_leg",
                    "settled_odds": 1.0,
                },
                {
                    "outcome": "void", "score": "重复赛程",
                    "result_date": "2028-06-07", "reason": "duplicate_fixture_leg",
                    "settled_odds": 1.0,
                },
            ],
        }
        wallet = {"balance": 970.0, "transactions": []}
        second_result = {
            "date": "2028-06-07", "competition": {"id": 101},
            "home_team": {"id": 30}, "away_team": {"id": 40},
            "home_goals": 2, "away_goals": 0,
            "settlement_verified": True,
            "settlement_evidence": "manual_verified_result_override",
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [_result(1, 0), second_result], "2028-06-08",
            )

        self.assertEqual(settlement["reopened"], 1)
        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(
            [item["outcome"] for item in record["settlement"]],
            ["lost", "lost", "lost", "won"],
        )
        self.assertNotIn("settled_odds", record["settlement"][3])
        self.assertEqual(
            record["reopen_reason"], "legacy_same_group_duplicate_reversal",
        )
        commit.assert_called_once()

    def test_persistent_scoreline_is_not_parsed_as_fixture_result(self):
        persistent = _result(
            1,
            2,
            result_address="0x12345678",
            result_source="persistent_season_record",
        )
        required = {("2028-06-06", 100, 10, 20)}

        with patch("tools.preview_cup_odds.parse_result_event_summary") as parser:
            enriched = enrich_halftime_results(object(), [persistent], required)

        parser.assert_not_called()
        self.assertNotIn("half_home_goals", enriched[0])
        self.assertNotIn("half_away_goals", enriched[0])

    def test_corrected_score_conflict_does_not_change_existing_settlement(self):
        record = {
            "bet_id": "aggregate-score",
            "status": "won",
            "type": "single",
            "stake": 100.0,
            "payout": 200.0,
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "SCORE",
            "selection_code": "exact",
            "line": "3-2",
            "odds": 2.0,
            "settlement": [{
                "outcome": "won",
                "score": "3-2",
                "result_date": "2028-06-06",
                "return_multiplier": 2.0,
            }],
        }
        wallet = {"balance": 1100.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(1, 0)], "2028-06-07")

        self.assertEqual(settlement["reopened"], 0)
        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["conflicts"], 1)
        self.assertEqual(record["status"], "won")
        self.assertEqual(record["settlement"][0]["score"], "3-2")
        self.assertEqual(
            record["settlement_conflict"]["reason"],
            "conflicting_final_score",
        )
        self.assertEqual(wallet["balance"], 1100.0)
        self.assertEqual(wallet["transactions"], [])
        commit.assert_called_once()

    def test_large_corrected_score_conflict_never_reverses_wallet(self):
        previous_payout_minor = 53_065_485_868_298_530_000_000_000_000
        available_minor = 571_924_403_456
        record = {
            "bet_id": "spent-corrected-payout",
            "status": "won",
            "type": "single",
            "stake": 100.0,
            "payout": previous_payout_minor / 100,
            "payout_minor": previous_payout_minor,
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "home": "Home",
            "away": "Away",
            "market": "SCORE",
            "selection_code": "exact",
            "line": "3-2",
            "odds": 2.0,
            "settlement": [{
                "outcome": "won",
                "score": "3-2",
                "result_date": "2028-06-06",
                "return_multiplier": 2.0,
            }],
        }
        wallet = {
            "balance": available_minor / 100,
            "balance_minor": available_minor,
            "transactions": [],
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([_result(1, 0)], "2028-06-07")

        self.assertEqual(settlement["reopened"], 0)
        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["conflicts"], 1)
        self.assertEqual(record["status"], "won")
        self.assertEqual(wallet["balance_minor"], available_minor)
        self.assertEqual(wallet["transactions"], [])
        commit.assert_called_once()

    def test_conflicting_live_scores_keep_pending_bet_unsettled(self):
        record = {
            "bet_id": "conflicting-live-scores",
            "status": "pending",
            "type": "single",
            "fixture_date": "2028-06-06",
            "competition_id": 100,
            "home_id": 10,
            "away_id": 20,
            "market": "1X2",
            "selection_code": "home",
            "stake": 100.0,
            "odds": 2.0,
        }
        wallet = {"balance": 900.0, "transactions": []}
        first = _result(2, 0, result_source="fixture_result_archive")
        second = _result(0, 2, result_source="persistent_season_record")

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets(
                [first, second], "2028-06-07",
            )

        self.assertEqual(settlement["settled"], 0)
        self.assertEqual(settlement["conflicts"], 1)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(
            record["settlement_conflict"]["reason"],
            "conflicting_result_sources",
        )
        self.assertEqual(wallet["balance"], 900.0)
        self.assertEqual(wallet["transactions"], [])
        commit.assert_called_once()

    def test_exact_result_identity_does_not_require_same_fixture_address(self):
        record = {
            "bet_id": "wrong-native-fixture", "status": "pending",
            "type": "single", "fixture_date": "2028-06-06",
            "competition_id": 100, "home_id": 10, "away_id": 20,
            "fixture_address": "0x1111", "market": "1X2",
            "selection_code": "home", "stake": 100.0, "odds": 2.0,
        }
        result = _result(
            2, 1,
            settlement_evidence="bet_fixture_result_pointer",
            fixture_address="0x2222",
        )
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([result], "2028-06-07")

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")
        self.assertEqual(wallet["balance"], 1100.0)
        commit.assert_called_once()

    def test_single_exact_result_source_settles_after_fixture_address_expires(self):
        record = {
            "bet_id": "expired-native-fixture", "status": "pending",
            "type": "single", "fixture_date": "2028-06-06",
            "competition_id": 100, "home_id": 10, "away_id": 20,
            "fixture_address": "0x1111", "market": "1X2",
            "selection_code": "home", "stake": 100.0, "odds": 2.0,
        }
        result = _result(2, 1, result_source="fixture_result_archive")
        result.pop("settlement_verified", None)
        result.pop("settlement_evidence", None)
        wallet = {"balance": 900.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            settlement = settle_pending_bets([result], "2028-06-07")

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")
        self.assertEqual(wallet["balance"], 1100.0)
        commit.assert_called_once()

    def test_manual_reopen_reversal_cannot_make_wallet_negative(self):
        record = {
            "bet_id": "spent-manual-payout",
            "status": "won",
            "payout": 200.0,
            "settlement": [{"outcome": "won", "score": "2-0"}],
        }
        wallet = {"balance": 50.0, "transactions": []}

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account") as commit,
        ):
            result = reopen_bets({record["bet_id"]}, "manual_correction")

        reversal = wallet["transactions"][0]
        self.assertEqual(result["reversed"], 50.0)
        self.assertEqual(result["unrecovered"], 150.0)
        self.assertEqual(result["balance"], 0.0)
        self.assertEqual(reversal["amount"], -50.0)
        self.assertEqual(reversal["requested_reversal"], 200.0)
        self.assertEqual(reversal["unrecovered_reversal"], 150.0)
        commit.assert_called_once()

    def test_fixture_result_pointer_can_supply_halftime_details(self):
        archive = _result(
            1,
            2,
            result_address="0x12345678",
            result_source="fixture_result_pointer",
        )
        required = {("2028-06-06", 100, 10, 20)}
        summary = {
            "half_home_goals": 0,
            "half_away_goals": 1,
            "first_scoring_team": "away",
        }

        with (
            patch(
                "tools.preview_cup_odds.parse_completed_result",
                return_value=_result(1, 2),
            ) as result_parser,
            patch(
                "tools.preview_cup_odds.parse_result_event_summary",
                return_value=summary,
            ) as parser,
        ):
            enriched = enrich_halftime_results(object(), [archive], required)

        parser.assert_called_once_with(
            ANY,
            0x12345678,
            expected_home_goals=1,
            expected_away_goals=2,
        )
        self.assertEqual(result_parser.call_count, 2)
        self.assertEqual(enriched[0]["half_home_goals"], 0)
        self.assertEqual(enriched[0]["half_away_goals"], 1)

    def test_two_legged_aggregate_snapshot_uses_corrected_score_for_halftime(self):
        corrected = _result(
            1,
            0,
            aggregate_home_goals=3,
            aggregate_away_goals=2,
            result_address="0x12345678",
            result_source="fixture_result_pointer",
        )
        aggregate = _result(3, 2)
        required = {("2028-06-06", 100, 10, 20)}
        summary = {
            "half_home_goals": 1,
            "half_away_goals": 0,
            "first_scoring_team": "home",
        }

        with (
            patch(
                "tools.preview_cup_odds.parse_completed_result",
                return_value=aggregate,
            ),
            patch(
                "tools.preview_cup_odds.parse_result_event_summary",
                return_value=summary,
            ) as parser,
        ):
            enriched = enrich_halftime_results(object(), [corrected], required)

        parser.assert_called_once_with(
            ANY,
            0x12345678,
            expected_home_goals=1,
            expected_away_goals=0,
        )
        self.assertEqual(enriched[0]["half_home_goals"], 1)
        self.assertEqual(enriched[0]["half_away_goals"], 0)

    def test_reused_fixture_result_slot_does_not_supply_halftime_details(self):
        archive = _result(
            6,
            0,
            result_address="0x12345678",
            result_source="fixture_result_archive",
        )
        required = {("2028-06-06", 100, 10, 20)}
        replacement = {
            **_result(2, 0),
            "home_team": {"id": 30, "name": "Other Home"},
            "away_team": {"id": 40, "name": "Other Away"},
        }

        with (
            patch(
                "tools.preview_cup_odds.parse_completed_result",
                return_value=replacement,
            ),
            patch("tools.preview_cup_odds.parse_result_event_summary") as parser,
        ):
            enriched = enrich_halftime_results(object(), [archive], required)

        parser.assert_not_called()
        self.assertNotIn("half_home_goals", enriched[0])
        self.assertNotIn("half_away_goals", enriched[0])

    def test_fixture_result_slot_reused_during_detail_read_is_rejected(self):
        archive = _result(
            3,
            5,
            result_address="0x12345678",
            result_source="fixture_result_archive",
        )
        required = {("2028-06-06", 100, 10, 20)}
        replacement = {
            **_result(1, 0),
            "home_team": {"id": 30, "name": "Other Home"},
            "away_team": {"id": 40, "name": "Other Away"},
        }
        summary = {
            "half_home_goals": 2,
            "half_away_goals": 0,
            "first_scoring_team": "home",
        }

        with (
            patch(
                "tools.preview_cup_odds.parse_completed_result",
                side_effect=[_result(3, 5), replacement],
            ),
            patch(
                "tools.preview_cup_odds.parse_result_event_summary",
                return_value=summary,
            ),
        ):
            enriched = enrich_halftime_results(object(), [archive], required)

        self.assertNotIn("half_home_goals", enriched[0])
        self.assertNotIn("half_away_goals", enriched[0])

    def test_unique_adjacent_date_result_settles_legacy_fixture_legs(self):
        first = {
            "fixture_date": "2027-04-26", "competition_id": 200,
            "home_id": 30, "away_id": 40, "home": "Totten",
            "away": "Solihull Moors", "market": "1X2",
            "selection_code": "home", "stake": 100.0, "odds": 2.0,
        }
        second = {**first, "fixture_date": "2027-04-27"}
        record = {
            "bet_id": "duplicate-fixture-parlay", "status": "pending",
            "type": "parlay", "stake": 100.0, "legs": [first, second],
        }
        wallet = {"balance": 0.0, "transactions": []}
        result = {
            "date": "2027-04-27", "competition": {"id": 200},
            "home_team": {"id": 30}, "away_team": {"id": 40},
            "home_goals": 2, "away_goals": 0,
            "settlement_verified": True,
            "settlement_evidence": "manual_verified_result_override",
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account"),
        ):
            settlement = settle_pending_bets([result], "2027-05-09")

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")
        self.assertEqual(settlement["returned"], 200.0)
        self.assertEqual(len(record["settlement"]), 2)

    def test_unique_adjacent_date_result_settles_legacy_single(self):
        record = {
            "bet_id": "date-drift", "status": "pending", "type": "single",
            "fixture_date": "2027-04-26", "competition_id": 200,
            "home_id": 30, "away_id": 40, "home": "Totten",
            "away": "Solihull Moors", "market": "1X2",
            "selection_code": "home", "stake": 100.0, "odds": 2.0,
        }
        wallet = {"balance": 0.0, "transactions": []}
        result = {
            "date": "2027-04-27", "competition": {"id": 200},
            "home_team": {"id": 30}, "away_team": {"id": 40},
            "home_goals": 2, "away_goals": 0,
            "settlement_verified": True,
            "settlement_evidence": "manual_verified_result_override",
        }

        with (
            patch("tools.betting_account.load_bets", return_value=[record]),
            patch("tools.betting_account.load_wallet", return_value=wallet),
            patch("tools.betting_account._commit_account"),
        ):
            settlement = settle_pending_bets([result], "2027-05-09")

        self.assertEqual(settlement["settled"], 1)
        self.assertEqual(record["status"], "won")


if __name__ == "__main__":
    unittest.main()
