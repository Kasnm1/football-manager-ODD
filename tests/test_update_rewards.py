from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import account_store, betting_account, update_rewards


class VersionUpdateRewardTests(unittest.TestCase):
    def setUp(self) -> None:
        account_store._CONTAINER_CACHE.clear()

    def test_account_reward_is_queued_then_claimed_atomically_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "saves" / "old-account.fmodd"
            legacy_root = root / "saves" / "old-account"
            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy_root),
            ):
                first = betting_account.queue_version_update_reward(
                    "old-account", "V2.2.0b",
                )
                legacy = account_store.load_container("old-account")
                for row in legacy["documents"]["mail"]:
                    if row.get("type") == "version_update_reward":
                        row["id"] = "version_update_reward:V2.2.0b"
                        row["source_id"] = "version_update_reward:V2.2.0b"
                        row.pop("reward_version", None)
                account_store.save_documents(
                    legacy["documents"], "old-account",
                )
                second = betting_account.queue_version_update_reward(
                    "old-account", "V2.2.0c",
                )
                third = betting_account.queue_version_update_reward(
                    "old-account", "V2.2.0d",
                )
                before_claim = account_store.load_container("old-account")
                claimed = betting_account.claim_version_update_reward(
                    "old-account", "version_update_reward:V2.2.0b",
                )
                repeated = betting_account.claim_version_update_reward(
                    "old-account", "version_update_reward:V2.2.0b",
                )
                container = account_store.load_container("old-account")

            self.assertEqual(before_claim["documents"]["wallet"]["balance"], 10_000.0)
            self.assertFalse(before_claim["documents"]["mail"][0]["claimed"])
            wallet = container["documents"]["wallet"]
            reward_transactions = [
                row for row in wallet["transactions"]
                if row.get("type") == "version_update_reward"
            ]
            reward_mail = [
                row for row in container["documents"]["mail"]
                if row.get("type") == "version_update_reward"
            ]
            self.assertTrue(first["queued"])
            self.assertFalse(second["queued"])
            self.assertFalse(third["queued"])
            self.assertTrue(claimed["credited"])
            self.assertFalse(repeated["credited"])
            self.assertEqual(first["reward_version"], "2.2.0")
            self.assertEqual(wallet["balance"], 50_010_000.0)
            self.assertEqual(len(reward_transactions), 1)
            self.assertEqual(len(reward_mail), 1)
            self.assertEqual(reward_mail[0]["title"], "更新奖励")
            self.assertTrue(reward_mail[0]["read"])
            self.assertTrue(reward_mail[0]["claimed"])
            self.assertFalse(reward_mail[0]["claimable"])

    def test_legacy_auto_credited_reward_cannot_be_claimed_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "saves" / "old-account.fmodd"
            legacy_root = root / "saves" / "old-account"
            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy_root),
            ):
                betting_account.queue_version_update_reward(
                    "old-account", "V2.2.0d",
                )
                container = account_store.load_container("old-account")
                wallet = container["documents"]["wallet"]
                betting_account._adjust_wallet(
                    wallet, 100_000_000.0, "version_update_reward",
                    id="version_update_reward:2.2.0",
                    source_id="version_update_reward:2.2.0",
                    app_version="V2.2.0d", reward_version="2.2.0",
                )
                account_store.save_documents(container["documents"], "old-account")
                result = betting_account.claim_version_update_reward(
                    "old-account", "version_update_reward:2.2.0",
                )
                final = account_store.load_container("old-account")

            self.assertFalse(result["credited"])
            self.assertEqual(final["documents"]["wallet"]["balance"], 100_010_000.0)
            self.assertTrue(final["documents"]["mail"][0]["claimed"])

    def test_letter_suffixed_builds_share_one_reward_version(self) -> None:
        for version in (
            "2.2.0", "V2.2.0b", "2.2.0c", "V2.2.0d", "2.2.0bcd",
        ):
            with self.subTest(version=version):
                self.assertEqual(
                    betting_account.version_update_reward_key(version), "2.2.0",
                )

    def test_same_version_family_does_not_reward_accounts_created_later(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "update_rewards.json"
            existing = {"old-account"}
            calls: list[tuple[str, str]] = []

            def account_path(scope_id: str) -> Path:
                path = root / "saves" / f"{scope_id}.fmodd"
                if scope_id in existing:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.touch(exist_ok=True)
                return path

            def queue(scope_id: str, version: str) -> dict[str, object]:
                calls.append((scope_id, version))
                return {"scope_id": scope_id, "app_version": version, "queued": True}

            with (
                patch.object(update_rewards, "STATE_PATH", state_path),
                patch.object(
                    update_rewards, "saved_account_scopes",
                    side_effect=lambda: [{"scope_id": scope} for scope in sorted(existing)],
                ),
                patch.object(update_rewards, "account_save_path", side_effect=account_path),
                patch.object(
                    update_rewards, "save_data_root",
                    side_effect=lambda scope: root / "saves" / scope,
                ),
                patch.object(update_rewards, "queue_version_update_reward", side_effect=queue),
            ):
                update_rewards.process_version_update_rewards("V2.2.0b")
                existing.add("new-account")
                update_rewards.process_version_update_rewards("V2.2.0c")
                update_rewards.process_version_update_rewards("V2.2.0d")
                update_rewards.process_version_update_rewards("V2.2.1")

            self.assertEqual(calls, [
                ("old-account", "V2.2.0b"),
                ("new-account", "V2.2.1"),
                ("old-account", "V2.2.1"),
            ])

    def test_failed_account_remains_pending_for_same_version_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            account_path = root / "saves" / "old-account.fmodd"
            account_path.parent.mkdir(parents=True)
            account_path.touch()
            attempts = 0

            def queue(scope_id: str, version: str) -> dict[str, object]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("temporary failure")
                return {"scope_id": scope_id, "app_version": version, "queued": True}

            with (
                patch.object(update_rewards, "STATE_PATH", root / "update_rewards.json"),
                patch.object(
                    update_rewards, "saved_account_scopes",
                    return_value=[{"scope_id": "old-account"}],
                ),
                patch.object(update_rewards, "account_save_path", return_value=account_path),
                patch.object(
                    update_rewards, "save_data_root",
                    return_value=root / "saves" / "old-account",
                ),
                patch.object(update_rewards, "queue_version_update_reward", side_effect=queue),
            ):
                first = update_rewards.process_version_update_rewards("V2.1.7")
                second = update_rewards.process_version_update_rewards("V2.1.7")

            self.assertEqual(len(first["failures"]), 1)
            self.assertEqual(second["queued"], 1)
            self.assertEqual(attempts, 2)

    def test_deleted_pending_account_is_not_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "update_rewards.json"
            account_path = root / "saves" / "old-account.fmodd"
            account_path.parent.mkdir(parents=True)
            account_path.touch()

            with (
                patch.object(update_rewards, "STATE_PATH", state_path),
                patch.object(
                    update_rewards, "saved_account_scopes",
                    return_value=[{"scope_id": "old-account"}],
                ),
                patch.object(update_rewards, "account_save_path", return_value=account_path),
                patch.object(
                    update_rewards, "save_data_root",
                    return_value=root / "saves" / "old-account",
                ),
                patch.object(
                    update_rewards, "queue_version_update_reward",
                    side_effect=RuntimeError("temporary failure"),
                ) as queue,
            ):
                first = update_rewards.process_version_update_rewards("V2.1.7")
                account_path.unlink()
                second = update_rewards.process_version_update_rewards("V2.1.7")

            self.assertEqual(len(first["failures"]), 1)
            self.assertEqual(second["failures"], [])
            self.assertEqual(queue.call_count, 1)
            self.assertFalse(account_path.exists())


if __name__ == "__main__":
    unittest.main()
