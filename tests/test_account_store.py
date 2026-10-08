from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import account_store, app_paths, storage_management


class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        account_store._CONTAINER_CACHE.clear()
        account_store._SPLIT_RECOVERY_CHECKED.clear()

    def test_repeated_document_reads_decode_container_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            account_store._write_container(save_path, {
                "documents": {
                    "wallet": {"balance": 1000.0},
                    "economy": {"general_balance": 500.0},
                },
            })
            account_store._CONTAINER_CACHE.clear()

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
                patch.object(
                    account_store.gzip, "decompress",
                    wraps=account_store.gzip.decompress,
                ) as decompress,
            ):
                wallet = account_store.load_document("wallet", None)
                economy = account_store.load_document("economy", None)
                wallet["balance"] = 0.0
                reloaded = account_store.load_document("wallet", None)

            self.assertEqual(decompress.call_count, 1)
            self.assertEqual(economy["general_balance"], 500.0)
            self.assertEqual(reloaded["balance"], 1000.0)

    def test_merge_account_containers_unions_bets_and_wallet_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.fmodd" for name in ("a", "b", "target")}
            for name, payload in {
                "a": {
                    "documents": {
                        "bets": [{"bet_id": "bet-1", "status": "pending"}],
                        "wallet": {"transactions": [{"id": "tx-1", "amount_minor": 100}]},
                        "economy": {"general_balance_minor": 9000, "inventory": [{"id": "item-1"}], "transactions": [{"id": "bank-1"}]},
                    },
                    "updated_at": "2026-01-01T00:00:00",
                },
                "b": {
                    "documents": {
                        "bets": [{"bet_id": "bet-1", "status": "won"}, {"bet_id": "bet-2"}],
                        "wallet": {"transactions": [{"id": "tx-1", "amount_minor": 100}, {"id": "tx-2", "amount_minor": 50}]},
                        "economy": {"general_balance_minor": 0, "inventory": [], "transactions": []},
                    },
                    "updated_at": "2026-01-02T00:00:00",
                },
            }.items():
                account_store._write_container(paths[name], payload)
            with (
                patch.object(account_store, "account_save_path", side_effect=lambda scope=None: paths[str(scope)]),
                patch.object(account_store, "save_data_root", return_value=root / "legacy"),
            ):
                result = account_store.merge_account_containers(["a", "b"], "target")
                merged = account_store.load_container("target")

            self.assertEqual(result["source_count"], 2)
            self.assertEqual(
                {row["bet_id"] for row in merged["documents"]["bets"]},
                {"bet-1", "bet-2"},
            )
            self.assertEqual(
                {row["id"] for row in merged["documents"]["wallet"]["transactions"]},
                {"tx-1", "tx-2"},
            )
            self.assertEqual(merged["documents"]["economy"]["general_balance_minor"], 9000)
            self.assertEqual(merged["documents"]["economy"]["inventory"], [{"id": "item-1"}])

    def test_hot_document_and_container_reads_do_not_take_storage_mutex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            account_store._write_container(save_path, {
                "documents": {"wallet": {"balance": 1000.0}},
            })

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
                patch.object(account_store, "storage_lock") as storage_lock,
            ):
                wallet = account_store.load_document("wallet", None)
                payload = account_store.load_container()

            storage_lock.assert_not_called()
            self.assertEqual(wallet["balance"], 1000.0)
            self.assertEqual(
                payload["documents"]["wallet"]["balance"], 1000.0,
            )

    def test_irrelevant_split_account_is_checked_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            split_path = legacy / "bets" / ".account.fmodd"
            account_store._write_container(save_path, {
                "documents": {"wallet": {"balance": 1000.0}},
            })
            account_store._write_container(split_path, {"documents": {}})

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
                patch.object(
                    account_store, "storage_lock",
                    wraps=account_store.storage_lock,
                ) as storage_lock,
            ):
                first = account_store.load_document("wallet", None)
                second = account_store.load_document("wallet", None)

            self.assertEqual(first, second)
            storage_lock.assert_called_once_with(save_path)

    def test_cached_read_modify_write_does_not_decode_valid_container_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            account_store._write_container(save_path, {
                "documents": {"counter": {"value": 1}},
            })

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
                patch.object(
                    account_store.gzip, "decompress",
                    wraps=account_store.gzip.decompress,
                ) as decompress,
            ):
                account_store.update_document(
                    "counter", {"value": 0},
                    lambda document: document.update(value=2),
                )

            self.assertEqual(decompress.call_count, 0)
            self.assertEqual(
                account_store._read_container(save_path)["documents"]["counter"],
                {"value": 2},
            )
            self.assertFalse(
                save_path.with_suffix(save_path.suffix + ".backup").exists(),
            )

    def test_uncached_existing_container_is_validated_before_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-a.fmodd"
            account_store._write_container(path, {
                "documents": {"wallet": {"balance": 1000.0}},
            })
            account_store._CONTAINER_CACHE.clear()

            with patch.object(
                account_store, "_decode_container",
                wraps=account_store._decode_container,
            ) as decode:
                account_store._write_container(path, {
                    "documents": {"wallet": {"balance": 900.0}},
                })

            decode.assert_called_once()
            self.assertFalse(path.with_suffix(path.suffix + ".backup").exists())

    def test_document_update_serializes_the_complete_read_modify_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            errors: list[Exception] = []
            barrier = threading.Barrier(2)

            def worker(marker: str) -> None:
                try:
                    barrier.wait(timeout=2)

                    def mutate(document: dict) -> str:
                        values = list(document.get("values") or [])
                        time.sleep(0.03)
                        values.append(marker)
                        document["values"] = values
                        return marker

                    result = account_store.update_document(
                        "acquired_clubs", {"values": []}, mutate,
                    )
                    self.assertEqual(result, marker)
                except Exception as error:  # pragma: no cover - assertion relay
                    errors.append(error)

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
            ):
                threads = [
                    threading.Thread(target=worker, args=(marker,))
                    for marker in ("first", "second")
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=3)
                stored = account_store.load_document("acquired_clubs", {})

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(sorted(stored["values"]), ["first", "second"])

    def test_external_container_replacement_invalidates_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            account_store._write_container(save_path, {
                "documents": {"wallet": {"balance": 1000.0}},
            })

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
                patch.object(
                    account_store, "storage_lock",
                    wraps=account_store.storage_lock,
                ) as storage_lock,
            ):
                self.assertEqual(
                    account_store.load_document("wallet", None)["balance"],
                    1000.0,
                )
                replacement = json.dumps({
                    "schema_version": 1,
                    "documents": {
                        "wallet": {"balance": 2250.0},
                        "mail": [{"id": "external-update"}],
                    },
                }).encode("utf-8")
                save_path.write_bytes(account_store.gzip.compress(replacement))

                wallet = account_store.load_document("wallet", None)

            self.assertEqual(wallet["balance"], 2250.0)
            storage_lock.assert_called_once_with(save_path)

    def test_corrupt_container_recovers_existing_legacy_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-a.fmodd"
            account_store._write_container(path, {
                "documents": {"wallet": {"balance": 1000.0}},
            })
            backup = path.with_suffix(path.suffix + ".backup")
            backup.write_bytes(path.read_bytes())
            account_store._write_container(path, {
                "documents": {"wallet": {"balance": 900.0}},
            })
            path.write_bytes(b"broken")
            account_store._CONTAINER_CACHE.clear()

            recovered = account_store._read_container(path)

            self.assertEqual(recovered["documents"]["wallet"]["balance"], 1000.0)
            self.assertEqual(
                account_store._read_container(path)["scope_id"], "account-a",
            )

    def test_future_container_version_never_falls_back_to_older_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-a.fmodd"
            account_store._write_container(path, {
                "documents": {"wallet": {"balance": 1000.0}},
            })
            backup = path.with_suffix(path.suffix + ".backup")
            backup.write_bytes(path.read_bytes())
            future = json.dumps({
                "schema_version": account_store.SCHEMA_VERSION + 1,
                "scope_id": "account-a",
                "documents": {"wallet": {"balance": 5000.0}},
            }).encode("utf-8")
            path.write_bytes(account_store.gzip.compress(future))
            account_store._CONTAINER_CACHE.clear()

            with self.assertRaises(account_store.UnsupportedAccountStoreVersion):
                account_store._read_container(path)

    def test_container_scope_must_match_its_storage_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-a.fmodd"
            encoded = json.dumps({
                "schema_version": account_store.SCHEMA_VERSION,
                "scope_id": "account-b",
                "documents": {},
            }).encode("utf-8")
            path.write_bytes(account_store.gzip.compress(encoded))
            account_store._CONTAINER_CACHE.clear()

            with self.assertRaisesRegex(RuntimeError, "作用域不匹配"):
                account_store._read_container(path)

    def test_legacy_manager_qualified_scope_is_read_from_career_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fm24-network-session.mana.fmodd"
            encoded = json.dumps({
                "schema_version": account_store.SCHEMA_VERSION,
                "scope_id": "fm24-network-session.mana.manager-2002202852",
                "documents": {"wallet": {"balance": 1250.0}},
            }).encode("utf-8")
            path.write_bytes(account_store.gzip.compress(encoded))
            account_store._CONTAINER_CACHE.clear()

            payload = account_store._read_container(path)

            self.assertEqual(payload["scope_id"], path.stem)
            self.assertEqual(payload["documents"]["wallet"]["balance"], 1250.0)

    def test_newer_container_schema_is_rejected_without_rewriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            original = account_store.gzip.compress(json.dumps({
                "schema_version": account_store.SCHEMA_VERSION + 1,
                "documents": {"wallet": {"balance": 2250.0}},
            }).encode("utf-8"), mtime=0)
            save_path.write_bytes(original)

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
            ):
                with self.assertRaisesRegex(RuntimeError, "由更新版本创建"):
                    account_store.load_document("wallet", None)

            self.assertEqual(save_path.read_bytes(), original)

    def test_invalid_container_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_path = root / "account.fmodd"
            legacy = root / "legacy"
            save_path.write_bytes(account_store.gzip.compress(json.dumps({
                "schema_version": "not-a-version",
                "documents": {},
            }).encode("utf-8"), mtime=0))

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
            ):
                with self.assertRaisesRegex(RuntimeError, "损坏且没有可用备份"):
                    account_store.load_container()

    def test_multi_document_save_uses_canonical_account_for_wallet_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "saves" / "account-1"
            save_path = root / "saves" / "account-1.fmodd"
            wallet_path = legacy / "bets" / "fm26_wallet.json"

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
            ):
                account_store.save_documents(
                    {
                        "bets": [{"bet_id": "bet-1", "status": "pending"}],
                        "wallet": {"balance": 900.0, "transactions": []},
                    },
                    legacy_path=wallet_path,
                )
                wallet = account_store.load_document(
                    "wallet", None, legacy_path=wallet_path,
                )

            self.assertEqual(wallet["balance"], 900.0)
            self.assertTrue(save_path.is_file())
            self.assertFalse((legacy / "bets" / ".account.fmodd").exists())

    def test_split_betting_account_is_merged_back_with_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "saves" / "account-1"
            save_path = root / "saves" / "account-1.fmodd"
            split_path = legacy / "bets" / ".account.fmodd"
            save_path.parent.mkdir(parents=True)
            account_store._write_container(save_path, {
                "documents": {
                    "wallet": {
                        "balance": 1000.0,
                        "transactions": [{
                            "id": "opening", "at": "2028-01-01T00:00:00",
                            "type": "opening_balance", "amount": 1000.0,
                        }],
                    },
                },
            })
            account_store._write_container(split_path, {
                "documents": {
                    "bets": [{"bet_id": "bet-1", "status": "pending"}],
                    "wallet": {
                        "balance": 900.0,
                        "transactions": [
                            {
                                "id": "opening", "at": "2028-01-01T00:00:00",
                                "type": "opening_balance", "amount": 1000.0,
                            },
                            {
                                "id": "placed", "at": "2028-01-02T00:00:00",
                                "type": "bet_placed", "amount": -100.0,
                            },
                        ],
                    },
                },
            })

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
            ):
                wallet = account_store.load_document(
                    "wallet", None,
                    legacy_path=legacy / "bets" / "fm26_wallet.json",
                )
                bets = account_store.load_document(
                    "bets", [],
                    legacy_path=legacy / "bets" / "fm26_bets.jsonl",
                )

            self.assertEqual(wallet["balance"], 900.0)
            self.assertEqual(bets, [{"bet_id": "bet-1", "status": "pending"}])
            self.assertFalse(split_path.exists())
            self.assertTrue((legacy / "bets" / ".account.recovered.fmodd").is_file())

    def test_legacy_account_migrates_to_one_compressed_save_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "saves" / "707432"
            (legacy / "bets").mkdir(parents=True)
            (legacy / "economy").mkdir(parents=True)
            (legacy / "training").mkdir(parents=True)
            (legacy / "model").mkdir(parents=True)
            wallet = {"schema_version": 1, "balance": 321.5, "transactions": []}
            economy = {"schema_version": 3, "general_balance": 654.25, "inventory": [{"id": "item-1"}]}
            training = {"schema_version": 3, "facilities": [{"id": "facility-1"}]}
            (legacy / "bets" / "fm26_wallet.json").write_text(json.dumps(wallet), encoding="utf-8")
            (legacy / "bets" / "fm26_bets.jsonl").write_text(
                json.dumps({"bet_id": "bet-1"}) + "\n", encoding="utf-8",
            )
            (legacy / "economy" / "club_economy.json").write_text(json.dumps(economy), encoding="utf-8")
            (legacy / "training" / "training_ground.json").write_text(json.dumps(training), encoding="utf-8")
            cache_file = legacy / "model" / "forecast_ledger.json"
            cache_file.write_bytes(b"x" * 100_000)
            save_path = root / "saves" / "707432.fmodd"
            cache_root = root / "cache" / "707432"

            with (
                patch.object(account_store, "account_save_path", return_value=save_path),
                patch.object(account_store, "save_data_root", return_value=legacy),
                patch.object(account_store, "cache_data_root", return_value=cache_root),
            ):
                migrated_wallet = account_store.load_document("wallet", None)
                payload = account_store.load_container()

            self.assertEqual(migrated_wallet["balance"], 321.5)
            self.assertEqual(payload["documents"]["economy"], economy)
            self.assertEqual(payload["documents"]["training_ground"], training)
            self.assertEqual(payload["documents"]["bets"], [{"bet_id": "bet-1"}])
            self.assertTrue(save_path.is_file())
            self.assertLess(save_path.stat().st_size, 10_000)
            self.assertTrue((cache_root / "model" / "forecast_ledger.json").is_file())
            self.assertFalse((legacy / "bets" / "fm26_wallet.json").exists())
            self.assertFalse(legacy.exists())

    def test_clear_cache_preserves_single_file_saves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saves = root / "saves"
            cache = root / "cache" / "707432" / "model"
            saves.mkdir(parents=True)
            cache.mkdir(parents=True)
            save_path = saves / "707432.fmodd"
            save_path.write_bytes(b"durable-account")
            (cache / "forecast.json").write_bytes(b"cache" * 100)
            before = hashlib.sha256(save_path.read_bytes()).hexdigest()

            with (
                patch.object(storage_management, "_validated_data_root", return_value=root),
                patch.object(storage_management, "ensure_data_directories"),
            ):
                result = storage_management.clear_cache_files()

            after = hashlib.sha256(save_path.read_bytes()).hexdigest()
            self.assertEqual(after, before)
            self.assertTrue(save_path.is_file())
            self.assertFalse((root / "cache").exists())
            self.assertEqual(result["cleared_files"], 1)

    def test_clear_cache_preserves_result_history_and_pending_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scope_root = root / "cache" / "scope-a"
            result_history = scope_root / "results" / "fm26_result_history.json"
            pending_snapshot = scope_root / "odds" / "live" / "pending.json"
            disposable = scope_root / "model" / "forecast.json"
            for path, content in (
                (result_history, b"result-history"),
                (pending_snapshot, b"pending-snapshot"),
                (disposable, b"disposable"),
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            with (
                patch.object(storage_management, "_validated_data_root", return_value=root),
                patch.object(storage_management, "ensure_data_directories"),
            ):
                result = storage_management.clear_cache_files(
                    protected_paths={pending_snapshot},
                )

            self.assertEqual(result_history.read_bytes(), b"result-history")
            self.assertEqual(pending_snapshot.read_bytes(), b"pending-snapshot")
            self.assertFalse(disposable.exists())
            self.assertEqual(result["cleared_files"], 1)

    def test_unbound_account_store_rejects_unidentified_container(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unidentified = root / "saves" / "unidentified.fmodd"
            legacy = root / "saves" / "unidentified"
            with (
                patch.object(account_store, "account_save_path", return_value=unidentified),
                patch.object(account_store, "save_data_root", return_value=legacy),
            ):
                with self.assertRaisesRegex(RuntimeError, "拒绝访问 unidentified"):
                    account_store.load_document("wallet", None)

            self.assertFalse(unidentified.exists())

    def test_unbound_cache_and_career_scopes_are_rejected(self) -> None:
        previous = app_paths.active_save_id()
        app_paths.set_active_save_id(None)
        try:
            for accessor in (
                app_paths.cache_data_root,
                lambda: app_paths.career_data_root(None),
            ):
                with self.subTest(accessor=accessor):
                    with self.assertRaisesRegex(RuntimeError, "拒绝使用 unidentified"):
                        accessor()
        finally:
            app_paths.set_active_save_id(previous)

    def test_delete_other_save_files_preserves_current_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saves = root / "saves"
            saves.mkdir(parents=True)
            current = saves / "current.fmodd"
            other = saves / "other.fmodd"
            other_backup = saves / "other.fmodd.backup"
            unrelated = saves / "notes.txt"
            current.write_bytes(b"current")
            other.write_bytes(b"other")
            other_backup.write_bytes(b"other-backup")
            unrelated.write_text("keep", encoding="utf-8")

            with (
                patch.object(storage_management, "_validated_data_root", return_value=root),
                patch.object(storage_management, "account_save_path", return_value=current),
                patch.object(storage_management, "forget_saved_storage_scopes", return_value=1) as forget,
                patch.object(storage_management, "ensure_data_directories"),
            ):
                result = storage_management.delete_other_save_files("current-account")

            self.assertTrue(current.is_file())
            self.assertFalse(other.exists())
            self.assertFalse(other_backup.exists())
            self.assertTrue(unrelated.is_file())
            self.assertEqual(result["deleted_files"], 2)
            forget.assert_called_once_with({"other"})

    def test_delete_current_save_file_removes_only_current_and_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saves = root / "saves"
            saves.mkdir(parents=True)
            current = saves / "current.fmodd"
            current_backup = saves / "current.fmodd.backup"
            other = saves / "other.fmodd"
            current.write_bytes(b"current")
            current_backup.write_bytes(b"current-backup")
            other.write_bytes(b"other")

            with (
                patch.object(storage_management, "_validated_data_root", return_value=root),
                patch.object(storage_management, "account_save_path", return_value=current),
                patch.object(storage_management, "ensure_data_directories"),
            ):
                result = storage_management.delete_current_save_file("current-account")

            self.assertFalse(current.exists())
            self.assertFalse(current_backup.exists())
            self.assertTrue(other.is_file())
            self.assertEqual(result["deleted_files"], 2)


if __name__ == "__main__":
    unittest.main()
