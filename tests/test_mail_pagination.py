from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

import fm_odds_web


class MailRetentionTests(unittest.TestCase):
    def test_profit_mail_expires_after_five_game_days_but_formal_mail_stays(self):
        records = [
            {"id": "profit-old", "type": "bet_profit", "game_date": "2030-01-05"},
            {"id": "profit-new", "type": "bet_profit", "game_date": "2030-01-06"},
            {"id": "integrity-old", "type": "match_integrity", "game_date": "2030-01-01"},
        ]
        with patch.object(fm_odds_web, "load_mail", return_value=records), patch.object(fm_odds_web, "save_mail") as save:
            kept = fm_odds_web.prune_expired_profit_mail("2030-01-10")
        self.assertEqual([item["id"] for item in kept], ["profit-new", "integrity-old"])
        save.assert_called_once()

    def test_acknowledged_integrity_notice_marks_archived_mail_read(self):
        records = [{
            "id": "mail-1", "source_id": "match_integrity:penalty-1",
            "type": "match_integrity", "read": False,
            "notice": {"id": "penalty-1", "acknowledged": False},
        }]
        notice = {
            "id": "penalty-1", "acknowledged": True,
            "acknowledged_at": "2030-01-10T12:00:00",
        }
        with patch.object(fm_odds_web, "load_mail", return_value=records), patch.object(
            fm_odds_web, "save_mail"
        ) as save:
            mail = fm_odds_web.archive_notice_mail("match_integrity", notice)
        self.assertTrue(mail["read"])
        self.assertEqual(mail["read_at"], "2030-01-10T12:00:00")
        self.assertTrue(mail["notice"]["acknowledged"])
        save.assert_called_once_with(records)

    def test_repaired_integrity_body_updates_existing_archived_mail(self):
        records = [{
            "id": "mail-1", "source_id": "match_integrity:penalty-1",
            "type": "match_integrity", "read": True,
            "title": "旧标题", "notice": {"id": "penalty-1", "body": "投注金额：£0.00"},
        }]
        notice = {
            "id": "penalty-1", "title": "足协处罚", "game_date": "2030-01-10",
            "body": "尊敬的玩家姓名：\n投注金额：£100.00",
        }
        with patch.object(fm_odds_web, "load_mail", return_value=records), patch.object(
            fm_odds_web, "save_mail"
        ) as save:
            mail = fm_odds_web.archive_notice_mail("match_integrity", notice)
        self.assertEqual(mail["notice"]["body"], notice["body"])
        self.assertEqual(mail["title"], "足协处罚")
        save.assert_called_once_with(records)


class MailPaginationTests(unittest.TestCase):
    def test_mail_page_returns_newest_first_and_clamps_page(self):
        state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
        state.lock = threading.RLock()
        state.output = {"game_date": "2030-01-10"}
        state._bind_current_save = lambda: None
        records = [{"id": str(index), "game_date": "2030-01-09"} for index in range(25)]
        with patch.object(fm_odds_web, "prune_expired_profit_mail", return_value=records):
            result = state.mail_page(99, 10)
        self.assertEqual(result["page"], 3)
        self.assertEqual(result["pages"], 3)
        self.assertEqual([item["id"] for item in result["records"]], ["4", "3", "2", "1", "0"])


if __name__ == "__main__":
    unittest.main()
