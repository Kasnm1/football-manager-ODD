from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import app_paths
from tools.save_context import SaveContextRegistry, UnsupportedSaveContextVersion


class SaveContextRegistryV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.legacy_path = self.root / "save_registry.json"
        self.registry_path = self.root / "context_registry.v2.json"
        self.registry = SaveContextRegistry(
            self.registry_path, self.root, self.legacy_path,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_legacy(self, payload: dict) -> None:
        self.legacy_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8",
        )

    def test_same_stable_identity_returns_same_career_uuid(self):
        first = self.registry.resolve_career(namespace="fm26", stable_key="123")
        second = self.registry.resolve_career(namespace="fm26", stable_key="123")

        self.assertEqual(first, second)
        self.assertTrue(str(first).startswith("career-"))

    def test_last_active_scope_round_trips_without_changing_storage_aliases(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="123")
        account = self.registry.resolve_account(career, 99)
        storage_scope = self.registry.resolve_storage_scope(account)

        self.assertIsNone(self.registry.last_active_scope())
        self.assertEqual(self.registry.remember_active_scope(account), account)
        self.assertEqual(self.registry.last_active_scope(), account)
        self.assertEqual(self.registry.resolve_storage_scope(account), storage_scope)

    def test_last_active_career_container_is_not_restored_as_an_account(self):
        career = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-a",
        )

        self.registry.remember_active_scope(career)

        self.assertIsNone(self.registry.last_active_scope())

    def test_legacy_storage_scope_restores_to_registered_account_id(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="save-a")
        account = self.registry.resolve_account(career, 99)
        storage_scope = self.registry.resolve_storage_scope(account)

        self.registry.remember_active_scope(storage_scope)

        self.assertEqual(self.registry.last_active_scope(), account)

    def test_forget_storage_scope_removes_only_matching_account(self):
        first_career = self.registry.resolve_career(namespace="fm26", stable_key="first")
        second_career = self.registry.resolve_career(namespace="fm26", stable_key="second")
        first_account = self.registry.resolve_account(first_career, 11)
        second_account = self.registry.resolve_account(second_career, 22)
        first_storage = self.registry.resolve_storage_scope(first_account)
        second_storage = self.registry.resolve_storage_scope(second_account)

        removed = self.registry.forget_storage_scopes({str(second_storage)})
        remaining = {row["scope_id"] for row in self.registry.list_accounts()}

        self.assertEqual(removed, 1)
        self.assertIn(first_account, remaining)
        self.assertNotIn(second_account, remaining)
        self.assertEqual(self.registry.resolve_storage_scope(first_account), first_storage)

    def test_same_manager_in_two_stable_saves_never_merges_careers(self):
        self.write_legacy({"manager_aliases": {"human-99": "old-save"}})

        first = self.registry.resolve_career(
            namespace="fm26", stable_key="save-a", manager_id=99,
        )
        second = self.registry.resolve_career(
            namespace="fm26", stable_key="save-b", manager_id=99,
        )

        self.assertNotEqual(first, second)
        self.assertEqual(self.registry.resolve_storage_scope(first), "save-a")
        self.assertEqual(self.registry.resolve_storage_scope(second), "save-b")

    def test_create_merged_account_rebinds_current_career_and_marks_sources(self):
        first = self.registry.resolve_career(namespace="fm24", stable_key="save-a")
        second = self.registry.resolve_career(namespace="fm24", stable_key="save-b")
        current = self.registry.resolve_career(namespace="fm24", stable_key="save-current")
        first_account = self.registry.resolve_account(first, 99)
        second_account = self.registry.resolve_account(second, 99)
        current_account = self.registry.resolve_account(current, 99)

        merge = self.registry.create_merged_account(
            current, 99, [first_account, second_account, current_account],
            manager_name="Miku", team_name="新城",
        )

        self.assertNotEqual(merge["account_id"], current_account)
        self.assertEqual(
            self.registry.resolve_account(current, 99), merge["account_id"],
        )
        rows = {row["scope_id"]: row for row in self.registry.list_accounts()}
        self.assertEqual(rows[first_account]["merged_into"], merge["account_id"])
        self.assertEqual(rows[second_account]["merged_into"], merge["account_id"])
        self.assertEqual(rows[current_account]["merged_into"], merge["account_id"])

    def test_conflicting_legacy_aliases_do_not_merge_two_stable_saves(self):
        self.write_legacy({
            "savegame_aliases": {
                "save-a": "shared-old-scope",
                "save-b": "shared-old-scope",
            },
        })

        first = self.registry.resolve_career(namespace="fm26", stable_key="save-a")
        second = self.registry.resolve_career(namespace="fm26", stable_key="save-b")

        self.assertNotEqual(first, second)
        self.assertEqual(
            self.registry.resolve_storage_scope(first), "shared-old-scope",
        )
        self.assertEqual(self.registry.resolve_storage_scope(second), "save-b")

    def test_existing_stable_alias_and_manager_account_are_adopted_without_move(self):
        self.write_legacy({
            "savegame_aliases": {"9437665": "9161565"},
            "account_scopes": {
                "9161565|manager-200": "9161565.manager-200",
            },
        })
        old_account = self.root / "saves" / "9161565.manager-200"
        old_account.mkdir(parents=True)

        career = self.registry.resolve_career(
            namespace="fm26", stable_key="9437665",
        )
        account = self.registry.resolve_account(career, 200)

        self.assertEqual(self.registry.resolve_storage_scope(career), "9161565")
        self.assertEqual(
            self.registry.resolve_storage_scope(account),
            "9161565.manager-200",
        )
        self.assertTrue(old_account.is_dir())

    def test_legacy_selected_manager_claims_base_directory_even_if_resolved_later(self):
        self.write_legacy({"selected_managers": {"old-save": 10}})
        (self.root / "saves" / "old-save").mkdir(parents=True)
        career = self.registry.resolve_career(
            namespace="fm26", stable_key="old-save",
        )

        other = self.registry.resolve_account(career, 20)
        selected = self.registry.resolve_account(career, 10)

        self.assertEqual(
            self.registry.resolve_storage_scope(other), "old-save.manager-20",
        )
        self.assertEqual(self.registry.resolve_storage_scope(selected), "old-save")

    def test_missing_identity_can_hold_previously_confirmed_career(self):
        career = self.registry.resolve_career(namespace="fm24", stable_key="save-name-a")

        retained = self.registry.resolve_career(
            namespace="fm24", preferred_career_id=career,
        )

        self.assertEqual(retained, career)

    def test_stable_identity_promotes_active_provisional_manager_account(self):
        provisional = self.registry.resolve_career(
            namespace="fm24", manager_id=2002088087,
        )
        account = self.registry.resolve_account(provisional, 2002088087)
        storage_scope = self.registry.resolve_storage_scope(account)

        upgraded = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-bb0114",
            manager_id=2002088087, preferred_career_id=provisional,
        )
        upgraded_account = self.registry.resolve_account(upgraded, 2002088087)

        self.assertEqual(upgraded, provisional)
        self.assertEqual(upgraded_account, account)
        self.assertEqual(self.registry.resolve_storage_scope(account), storage_scope)

    def test_existing_stable_career_adopts_active_provisional_manager_account(self):
        provisional = self.registry.resolve_career(
            namespace="fm24", manager_id=2002088087,
        )
        account = self.registry.resolve_account(provisional, 2002088087)
        storage_scope = self.registry.resolve_storage_scope(account)
        stable = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-bb0114",
        )

        resolved = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-bb0114",
            manager_id=2002088087, preferred_career_id=provisional,
        )
        resolved_account = self.registry.resolve_account(resolved, 2002088087)

        self.assertEqual(resolved, stable)
        self.assertEqual(resolved_account, account)
        self.assertEqual(self.registry.resolve_storage_scope(account), storage_scope)
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertNotIn(
            "2002088087", payload["careers"][provisional]["managers"],
        )

    def test_missing_identity_reuses_unique_existing_manager_account(self):
        for namespace in ("fm24", "fm26"):
            with self.subTest(namespace=namespace):
                career = self.registry.resolve_career(
                    namespace=namespace, stable_key="network-session-a",
                    manager_id=2002071005,
                )
                account = self.registry.resolve_account(career, 2002071005)

                retained = self.registry.resolve_career(
                    namespace=namespace, manager_id=2002071005,
                )
                retained_account = self.registry.resolve_account(retained, 2002071005)

                self.assertEqual(retained, career)
                self.assertEqual(retained_account, account)

    def test_missing_identity_does_not_merge_ambiguous_manager_careers(self):
        first = self.registry.resolve_career(
            namespace="fm24", stable_key="save-a", manager_id=99,
        )
        second = self.registry.resolve_career(
            namespace="fm24", stable_key="save-b", manager_id=99,
        )
        self.registry.resolve_account(first, 99)
        self.registry.resolve_account(second, 99)

        unresolved = self.registry.resolve_career(namespace="fm24", manager_id=99)

        self.assertNotIn(unresolved, {first, second})

    def test_first_v2_write_backs_up_legacy_registry(self):
        self.write_legacy({"save_names": {"123": "My Save"}})

        self.registry.resolve_career(namespace="fm26", stable_key="123")

        backup = self.root / "save_registry.v1.backup.json"
        self.assertTrue(backup.is_file())
        self.assertEqual(
            json.loads(backup.read_text(encoding="utf-8"))["save_names"]["123"],
            "My Save",
        )

    def test_corrupt_v2_registry_recovers_from_last_valid_backup(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="123")
        self.registry.remember_name(career, "有效名称")
        self.registry_path.write_text("{broken", encoding="utf-8")

        recovered = self.registry.resolve_career(namespace="fm26", stable_key="123")

        self.assertEqual(recovered, career)

    def test_future_registry_version_fails_closed_without_overwrite(self):
        future = {
            "schema_version": 3,
            "updated_at": None,
            "careers": {},
            "evidence_index": {},
            "storage_index": {},
            "account_index": {},
            "conflicts": [],
        }
        self.registry_path.write_text(json.dumps(future), encoding="utf-8")
        before = self.registry_path.read_bytes()

        with self.assertRaises(UnsupportedSaveContextVersion):
            self.registry.resolve_career(namespace="fm26", stable_key="123")

        self.assertEqual(self.registry_path.read_bytes(), before)

    def test_non_numeric_registry_version_recovers_from_valid_backup(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="123")
        self.registry.remember_name(career, "有效名称")
        self.registry_path.write_text(
            json.dumps({"schema_version": "broken"}), encoding="utf-8",
        )

        recovered = self.registry.resolve_career(
            namespace="fm26", stable_key="123",
        )

        self.assertEqual(recovered, career)

    def test_account_uuid_is_stable_for_manager(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="123")

        first = self.registry.resolve_account(career, 77)
        second = self.registry.resolve_account(career, 77)

        self.assertEqual(first, second)
        self.assertTrue(str(first).startswith("account-"))

    def test_account_name_freezes_manager_team_id_and_first_recognition_date(self):
        with patch("tools.save_context._now", return_value="2026-08-10T09:30:00"):
            career = self.registry.resolve_career(
                namespace="fm26", stable_key="save-a",
            )
            account = self.registry.resolve_account(
                career, 2002071005,
                manager_name="  Kasumi  ", team_name=" Manchester   United ",
            )

        with patch("tools.save_context._now", return_value="2027-01-02T10:00:00"):
            same_account = self.registry.resolve_account(
                career, 2002071005,
                manager_name="Kasumi", team_name="Real Madrid",
            )

        rows = {row["scope_id"]: row for row in self.registry.list_accounts()}
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        self.assertEqual(same_account, account)
        self.assertEqual(
            rows[account]["save_name"],
            "Kasumi-Manchester United-2002071005-2026-08-10",
        )
        self.assertEqual(
            payload["account_index"][account]["recognized_on"], "2026-08-10",
        )

    def test_new_stable_identity_does_not_merge_into_preferred_career(self):
        first = self.registry.resolve_career(namespace="fm26", stable_key="save-a")

        second = self.registry.resolve_career(
            namespace="fm26", stable_key="save-b", preferred_career_id=first,
        )

        self.assertNotEqual(first, second)

    def test_verified_equivalent_stable_key_migrates_existing_career(self):
        legacy = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-old",
        )

        upgraded = self.registry.resolve_career(
            namespace="fm24",
            stable_key="savegame-id-4088782",
            equivalent_stable_key="network-session-old",
        )

        self.assertEqual(upgraded, legacy)
        self.assertEqual(
            self.registry.lookup_career(
                namespace="fm24", stable_key="savegame-id-4088782",
            ),
            legacy,
        )

    def test_equivalent_key_never_overrides_existing_direct_owner(self):
        legacy = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-old",
        )
        direct = self.registry.resolve_career(
            namespace="fm24", stable_key="savegame-id-4088782",
        )

        resolved = self.registry.resolve_career(
            namespace="fm24",
            stable_key="savegame-id-4088782",
            equivalent_stable_key="network-session-old",
        )

        self.assertNotEqual(direct, legacy)
        self.assertEqual(resolved, direct)

    def test_legacy_key_can_migrate_to_only_one_direct_savegame_id(self):
        legacy = self.registry.resolve_career(
            namespace="fm24", stable_key="network-session-old",
        )
        first = self.registry.resolve_career(
            namespace="fm24",
            stable_key="savegame-id-100",
            equivalent_stable_key="network-session-old",
        )

        second = self.registry.resolve_career(
            namespace="fm24",
            stable_key="savegame-id-200",
            equivalent_stable_key="network-session-old",
        )

        self.assertEqual(first, legacy)
        self.assertNotEqual(second, legacy)

    def test_provisional_telemetry_career_accepts_only_its_first_stable_identity(self):
        provisional = self.registry.resolve_career(
            namespace="fm26", telemetry_key="session-a",
        )
        upgraded = self.registry.resolve_career(
            namespace="fm26", stable_key="save-a", telemetry_key="session-a",
        )
        different_save = self.registry.resolve_career(
            namespace="fm26", stable_key="save-b", telemetry_key="session-a",
        )

        self.assertEqual(upgraded, provisional)
        self.assertNotEqual(different_save, provisional)

    def test_fm24_and_fm26_evidence_are_isolated(self):
        fm24 = self.registry.resolve_career(namespace="fm24", stable_key="same")
        fm26 = self.registry.resolve_career(namespace="fm26", stable_key="same")

        self.assertNotEqual(fm24, fm26)
        self.assertEqual(self.registry.resolve_storage_scope(fm24), "fm24-same")
        self.assertEqual(self.registry.resolve_storage_scope(fm26), "same")

    def test_preferred_account_cannot_be_reused_for_another_manager(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="123")
        first = self.registry.resolve_account(career, 10)

        second = self.registry.resolve_account(career, 20, preferred_scope=first)

        self.assertNotEqual(first, second)
        accounts = {row["scope_id"]: row for row in self.registry.list_accounts()}
        self.assertEqual(accounts[first]["manager_id"], 10)
        self.assertEqual(accounts[second]["manager_id"], 20)

    def test_legacy_save_name_is_adopted(self):
        self.write_legacy({"save_names": {"123": "旧档案"}})

        career = self.registry.resolve_career(namespace="fm26", stable_key="123")

        self.assertEqual(self.registry.career_name(career), "旧档案")

    def test_unchanged_name_and_manager_do_not_rewrite_registry(self):
        career = self.registry.resolve_career(namespace="fm26", stable_key="123")
        self.registry.remember_name(career, "固定名称")
        self.registry.remember_selected_manager(career, 10)

        with patch.object(self.registry, "_write") as write:
            self.registry.remember_name(career, "固定名称")
            self.registry.remember_selected_manager(career, 10)

        write.assert_not_called()


class AppPathsSaveContextV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.legacy_path = self.root / "save_registry.json"
        self.context_path = self.root / "context_registry.v2.json"
        self.registry = SaveContextRegistry(
            self.context_path, self.root, self.legacy_path,
        )
        self.patches = (
            patch.object(app_paths, "DATA_ROOT", self.root),
            patch.object(app_paths, "SAVE_REGISTRY_PATH", self.legacy_path),
            patch.object(app_paths, "CONTEXT_REGISTRY_PATH", self.context_path),
            patch.object(app_paths, "SAVE_CONTEXTS", self.registry),
        )
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def test_uuid_paths_resolve_to_existing_legacy_storage_without_duplicates(self):
        self.legacy_path.write_text(json.dumps({
            "savegame_aliases": {"new-key": "old-save"},
            "save_names": {"old-save": "旧存档"},
            "account_scopes": {
                "old-save|manager-88": "old-save.manager-88",
            },
        }, ensure_ascii=False), encoding="utf-8")
        legacy_directory = self.root / "saves" / "old-save.manager-88"
        legacy_directory.mkdir(parents=True)
        career = app_paths.resolve_save_identity(
            None, None, stable_id="new-key", namespace="fm26",
        )
        account = app_paths.resolve_account_scope(career, 88)

        self.assertEqual(app_paths.save_data_root(career), self.root / "saves" / "old-save")
        self.assertEqual(app_paths.save_data_root(account), legacy_directory)
        rows = app_paths.saved_account_scopes()
        claimed = [row for row in rows if row.get("storage_scope") == "old-save.manager-88"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["scope_id"], account)
        self.assertEqual(claimed[0]["save_name"], "旧存档")

    def test_career_container_is_not_listed_as_a_manager_account(self):
        career = app_paths.resolve_save_identity(
            None, None, stable_id="network-session-a", namespace="fm24",
        )
        saves_root = self.root / "saves"
        saves_root.mkdir()
        career_storage = self.registry.resolve_storage_scope(career)
        manager_storage = f"{career_storage}.manager-2002178992"
        (saves_root / f"{career_storage}.fmodd").write_bytes(b"career facts")
        (saves_root / f"{manager_storage}.fmodd").write_bytes(b"manager account")

        account = app_paths.resolve_account_scope(career, 2002178992)
        rows = app_paths.saved_account_scopes()

        self.assertEqual(self.registry.resolve_storage_scope(account), manager_storage)
        self.assertEqual([row["scope_id"] for row in rows], [account])

    def test_legacy_base_fmodd_is_claimed_when_it_is_the_only_account_file(self):
        career = app_paths.resolve_save_identity(
            None, None, stable_id="legacy-save", namespace="fm24",
        )
        saves_root = self.root / "saves"
        saves_root.mkdir()
        career_storage = self.registry.resolve_storage_scope(career)
        (saves_root / f"{career_storage}.fmodd").write_bytes(b"legacy account")

        account = app_paths.resolve_account_scope(career, 88)

        self.assertEqual(self.registry.resolve_storage_scope(account), career_storage)

    def test_account_scope_exposes_identity_display_name_without_renaming_storage(self):
        with patch("tools.save_context._now", return_value="2026-08-10T12:00:00"):
            career = app_paths.resolve_save_identity(
                None, None, stable_id="stable-save", namespace="fm26",
            )
            account = app_paths.resolve_account_scope(
                career, 77,
                manager_name="张三", team_name="上海海港",
            )

        row = next(
            item for item in app_paths.saved_account_scopes()
            if item["scope_id"] == account
        )
        self.assertEqual(
            app_paths.account_scope_display_name(account),
            "张三-上海海港-77-2026-08-10",
        )
        self.assertEqual(row["save_name"], "张三-上海海港-77-2026-08-10")
        self.assertTrue(row["identity_display_name"])
        self.assertEqual(
            app_paths.SAVE_CONTEXTS.resolve_storage_scope(account),
            "stable-save.manager-77",
        )

    def test_saved_accounts_are_sorted_from_newest_to_oldest(self):
        with patch("tools.save_context._now", return_value="2026-08-10T12:00:00"):
            old_career = app_paths.resolve_save_identity(
                None, None, stable_id="old-save", namespace="fm26",
            )
            old_account = app_paths.resolve_account_scope(old_career, 11)
        with patch("tools.save_context._now", return_value="2026-08-12T12:00:00"):
            new_career = app_paths.resolve_save_identity(
                None, None, stable_id="new-save", namespace="fm26",
            )
            new_account = app_paths.resolve_account_scope(new_career, 22)

        rows = app_paths.saved_account_scopes()

        self.assertLess(
            next(index for index, row in enumerate(rows) if row["scope_id"] == new_account),
            next(index for index, row in enumerate(rows) if row["scope_id"] == old_account),
        )

    def test_legacy_saved_accounts_use_storage_modified_time_for_sorting(self):
        self.legacy_path.write_text(json.dumps({
            "save_names": {"old-save": "旧存档", "new-save": "新存档"},
            "account_scopes": {
                "old-save|manager-11": "old-save.manager-11",
                "new-save|manager-22": "new-save.manager-22",
            },
        }, ensure_ascii=False), encoding="utf-8")
        saves_root = self.root / "saves"
        saves_root.mkdir()
        old_path = saves_root / "old-save.manager-11.fmodd"
        new_path = saves_root / "new-save.manager-22.fmodd"
        old_path.write_bytes(b"old")
        new_path.write_bytes(b"new")
        os.utime(old_path, (100, 100))
        os.utime(new_path, (200, 200))

        rows = app_paths.saved_account_scopes()

        self.assertLess(
            next(index for index, row in enumerate(rows) if row["scope_id"] == "new-save.manager-22"),
            next(index for index, row in enumerate(rows) if row["scope_id"] == "old-save.manager-11"),
        )

    def test_upgrade_keeps_wallet_and_bank_in_existing_legacy_account(self):
        from tools.betting_account import available_balance
        from tools.club_economy import public_economy

        self.legacy_path.write_text(json.dumps({
            "savegame_aliases": {"new-key": "old-save"},
            "account_scopes": {
                "old-save|manager-88": "old-save.manager-88",
            },
        }), encoding="utf-8")
        legacy_directory = self.root / "saves" / "old-save.manager-88"
        bets_directory = legacy_directory / "bets"
        economy_directory = legacy_directory / "economy"
        bets_directory.mkdir(parents=True)
        economy_directory.mkdir(parents=True)
        (bets_directory / "fm26_wallet.json").write_text(json.dumps({
            "schema_version": 1,
            "initial_balance": 10000.0,
            "balance": 4321.5,
            "credit_effective_bet_count": 0,
            "transactions": [],
        }), encoding="utf-8")
        (economy_directory / "club_economy.json").write_text(json.dumps({
            "schema_version": 3,
            "general_balance": 8765.25,
            "wallet_mode": "bank_v2",
        }), encoding="utf-8")

        career = app_paths.resolve_save_identity(
            None, None, stable_id="new-key", namespace="fm26",
        )
        account = app_paths.resolve_account_scope(career, 88)
        app_paths.set_active_save_id(account)
        try:
            economy = public_economy()
            self.assertEqual(available_balance(), 4321.5)
            self.assertEqual(economy["casino_balance"], 4321.5)
            self.assertEqual(economy["bank_balance"], 8765.25)
        finally:
            app_paths.set_active_save_id(None)

    def test_empty_identities_do_not_match_or_create_unknown_account(self):
        self.assertFalse(app_paths.save_identity_matches(None, None))
        self.assertIsNone(app_paths.resolve_account_scope(None, 10))

    def test_old_and_v2_snapshot_identities_are_equivalent(self):
        career = app_paths.resolve_save_identity(
            None, None, stable_id="old-save", namespace="fm26",
        )

        self.assertTrue(app_paths.save_identity_matches(career, "old-save"))

    def test_legacy_snapshot_requires_fixture_completeness_rebuild(self):
        from fm_odds_web import MODEL_VERSION, startup_cache_validation

        career = app_paths.resolve_save_identity(
            None, None, stable_id="old-save", namespace="fm26",
        )
        valid, reason = startup_cache_validation(
            {
                "matches": [],
                "save_instance_id": "old-save",
                "game_date": "2029-01-02",
                "model_version": MODEL_VERSION,
                "odds_days": 14,
                "odds_scope": "known",
            },
            career,
            {"date": "2029-01-02"},
            {"odds_days": 14, "odds_scope": "known"},
        )

        self.assertFalse(valid)
        self.assertEqual(reason, "fixture_scan_unverified")


class ManualAccountScopeTests(unittest.TestCase):
    def test_assign_account_scope_applies_identity_display_name(self):
        from fm_odds_web import LocalOddsState

        output = {
            "save_instance_id": "career-a",
            "selected_manager_id": 77,
            "manager": {"id": 77, "name": "张三", "team_name": "上海海港"},
            "managed_team": {"id": 42, "name": "上海海港"},
            "manager_options": [{"id": 77, "name": "张三"}],
        }
        with (
            patch("fm_odds_web.resolve_account_scope", return_value="account-a") as resolve,
            patch(
                "fm_odds_web.account_scope_display_name",
                return_value="张三-上海海港-77-2026-08-10",
            ),
        ):
            scope = LocalOddsState._assign_account_scope(output)

        self.assertEqual(scope, "account-a")
        self.assertEqual(output["save_name"], "张三-上海海港-77-2026-08-10")
        resolve.assert_called_once_with(
            "career-a", 77, preferred_scope=None,
            manager_name="张三", team_name="上海海港",
        )

    def test_manual_selection_survives_refresh_of_same_career(self):
        from fm_odds_web import LocalOddsState

        previous = {
            "save_instance_id": "career-a",
            "selected_manager_id": 10,
            "manual_account_scope_id": "account-old",
            "manual_account_context_id": "career-a",
            "account_scope_id": "account-old",
        }
        output = {"save_instance_id": "career-a", "selected_manager_id": 10}
        with patch("fm_odds_web.saved_account_scopes", return_value=[{
            "scope_id": "account-old", "manager_id": 10,
        }]):
            scope = LocalOddsState._assign_account_scope(output, previous)

        self.assertEqual(scope, "account-old")
        self.assertEqual(output["manual_account_context_id"], "career-a")

    def test_manual_selection_does_not_leak_into_another_career(self):
        from fm_odds_web import LocalOddsState

        previous = {
            "save_instance_id": "career-a",
            "selected_manager_id": 10,
            "manual_account_scope_id": "account-old",
            "manual_account_context_id": "career-a",
            "account_scope_id": "account-old",
            "account_scope_manager_id": 10,
        }
        output = {"save_instance_id": "career-b", "selected_manager_id": 10}
        with (
            patch("fm_odds_web.saved_account_scopes", return_value=[{
                "scope_id": "account-old", "manager_id": 10,
            }]),
            patch("fm_odds_web.resolve_account_scope", return_value="account-new"),
        ):
            scope = LocalOddsState._assign_account_scope(output, previous)

        self.assertEqual(scope, "account-new")
        self.assertNotIn("manual_account_scope_id", output)
        self.assertNotIn("manual_account_context_id", output)


if __name__ == "__main__":
    unittest.main()
