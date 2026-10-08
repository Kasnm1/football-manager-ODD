from __future__ import annotations

import tempfile
import threading
import unittest
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fm_odds_web import LocalOddsState
from tools.app_paths import active_save_id
from tools import training_ground as training_ground_module

from tools.club_reader import (
    adjust_player_training_condition, develop_player,
    develop_player_hidden_attribute, develop_player_position,
    plan_player_development,
)
from tools.attribute_growth_hook import (
    FM24_ORIGINAL_SIZE, FM26_ORIGINAL_SIZE, TABLE_ENTRY_SIZE, TABLE_OFFSET,
    _build_fm24_code, _build_fm26_code,
)
from tools.preferred_moves import FM24_VERIFIED_PREFERRED_MOVES
from tools.training_ground import (
    FACILITIES,
    TRAINING_FLOORS,
    INDOOR_FACILITY_LIMIT,
    NO_EQUIPMENT_TRAINING_MULTIPLIER,
    OUTDOOR_SET_PIECE_AREA_ID,
    OUTDOOR_TEAM_TRAINING_AREA_ID,
    OUTDOOR_2_SET_PIECE_AREA_ID,
    OUTDOOR_2_TEAM_TRAINING_AREA_ID,
    OUTDOOR_GOALKEEPER_TRAINING_AREA_ID,
    OUTDOOR_2_GOALKEEPER_TRAINING_AREA_ID,
    OUTDOOR_PASSING_TRAINING_AREA_ID,
    OUTDOOR_2_PASSING_TRAINING_AREA_ID,
    POSITION_TRAINING_AREA_ID,
    POSITION_TRAINING_MULTIPLIER,
    position_training_points_required,
    advance_training_focuses,
    assert_training_available,
    active_training_focuses,
    cancel_training_focus,
    complete_attribute_training_focus,
    complete_coaching_license_focus,
    complete_habit_training_focus,
    complete_position_training_focus,
    clear_training_ground,
    complete_training_focuses,
    estimated_training_days,
    estimated_training_days_to_target,
    estimated_training_days_to_20,
    find_training_submission,
    due_coaching_license_focuses,
    place_facility,
    purchase_facility,
    public_training_ground,
    rebind_training_focus_player,
    release_training_users,
    record_habit_training_focus,
    record_coaching_license_focus,
    record_training_focus,
    record_position_training_focus,
    rebase_attribute_training_focus,
    store_facility,
    training_target_attribute,
    training_points_required,
    training_points_required_for_direction,
    unlock_training_floor,
)


class TrainingGroundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "training_ground.json"
        self.path_patch = patch("tools.training_ground.training_ground_path", return_value=self.path)
        self.path_patch.start()

    def tearDown(self) -> None:
        self.path_patch.stop()
        self.temporary.cleanup()

    @staticmethod
    def _purchase_existing_hidden_facility(sku: str) -> dict:
        with patch.dict(FACILITIES[sku], {"available": True}):
            return purchase_facility(sku)

    def test_purchase_place_store_round_trip(self) -> None:
        facility = purchase_facility("treadmill")
        self.assertEqual(facility["status"], "stored")
        placed = place_facility(facility["id"], "indoor", 35, 45, 1)
        self.assertEqual((placed["status"], placed["rotation"]), ("placed", 1))
        stored = store_facility(facility["id"])
        self.assertEqual(stored["status"], "stored")
        self.assertEqual(
            {row["sku"] for row in public_training_ground()["facilities"]},
            {
                "treadmill", "set_piece_area", "team_training_area",
                "goalkeeper_training_area", "passing_training_area",
                "position_training_area",
            },
        )

    def test_new_training_floors_are_locked_until_purchased(self) -> None:
        floors = {row["scene"]: row for row in public_training_ground()["floors"]}
        self.assertEqual(floors["outdoor"]["name"], "1号室外")
        self.assertEqual(floors["outdoor_2"]["name"], "2号室外")
        for scene in ("indoor_3f", "indoor_4f", "indoor_5f"):
            self.assertFalse(floors[scene]["unlocked"])
            self.assertEqual(TRAINING_FLOORS[scene]["price"], 5_000_000.0)
        facility = purchase_facility("treadmill")
        with self.assertRaisesRegex(ValueError, "请先解锁"):
            place_facility(facility["id"], "indoor_3f", 50, 50)
        unlock_training_floor("indoor_3f")
        placed = place_facility(facility["id"], "indoor_3f", 50, 50)
        self.assertEqual(placed["scene"], "indoor_3f")

    def test_public_training_ground_reuses_one_storage_and_settings_snapshot(self) -> None:
        original_load = training_ground_module.load_training_ground
        with (
            patch.object(
                training_ground_module, "load_training_ground",
                wraps=original_load,
            ) as load_training,
            patch.object(
                training_ground_module, "load_settings", return_value={},
            ) as load_training_settings,
        ):
            payload = public_training_ground("2026-07-19")

        self.assertTrue(payload["floors"])
        load_training.assert_called_once_with()
        load_training_settings.assert_called_once_with()

    def test_coaching_office_moves_to_sixth_floor_with_legacy_alias(self) -> None:
        desk = purchase_facility("coaching_desk")
        placed = place_facility(desk["id"], "office_6f", 50, 50)
        self.assertEqual(placed["scene"], "office_6f")
        stored = store_facility(desk["id"])
        legacy = place_facility(stored["id"], "office_3f", 50, 50)
        self.assertEqual(legacy["scene"], "office_3f")

    def test_wallet_only_connection_keeps_account_training_facilities_visible(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src").joinpath(
            "fm_odds_web.py",
        ).read_text(encoding="utf-8")
        block = source.split("training_ground = (", 1)[1].split(
            "managed_teams = managed_team_rows", 1,
        )[0]

        self.assertIn(
            "public_training_ground(game_date) if account_ready else None",
            block,
        )
        self.assertIn("if has_verified_save:", block)
        self.assertNotIn("clear_training_ground()", block)

    def test_training_purchase_ignores_forged_free_request_flag(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {"game_date": "2030-01-01"}
        state._bind_current_save = MagicMock(return_value="save-a")
        payment = {
            "bank": 3_000_000.0, "wallet": 0.0, "total": 3_000_000.0,
            "transaction_id": "payment-1",
        }
        with (
            patch("fm_odds_web.free_services_enabled", return_value=False),
            patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
            patch("fm_odds_web.purchase_facility", return_value={"id": "facility-1"}),
            patch("fm_odds_web.public_training_ground", return_value={}),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            result = state.training_purchase({
                "sku": "treadmill", "free_purchase": True,
            })

        charge.assert_called_once_with(
            800_000.0, "training_facility_purchase", sku="treadmill",
        )
        self.assertEqual(result["payment"], payment)

    def test_training_floor_purchase_uses_five_million_price(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.output = {"game_date": "2030-01-01"}
        state._bind_current_save = MagicMock(return_value="save-a")
        payment = {
            "bank": 5_000_000.0, "wallet": 0.0, "total": 5_000_000.0,
            "transaction_id": "payment-floor-1",
        }
        with (
            patch("fm_odds_web.free_services_enabled", return_value=False),
            patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
            patch("fm_odds_web.unlock_training_floor", return_value={"scene": "indoor_3f", "unlocked": True}),
            patch("fm_odds_web.public_training_ground", return_value={}),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            result = state.training_floor_purchase({"scene": "indoor_3f"})

        charge.assert_called_once_with(
            5_000_000.0, "training_floor_purchase", scene="indoor_3f",
        )
        self.assertEqual(result["payment"], payment)

    def test_training_run_schedules_coaching_license_course(self) -> None:
        desk = purchase_facility("coaching_desk")
        place_facility(desk["id"], "office_3f", 50, 50)
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {"game_date": "2026-01-01"}
        state.club_profile = None
        state.club_profiles = {
            679: {
                "team": {"id": 679, "name": "曼城", "address": "0xteam", "team_type": "club"},
                "staff": [{
                    "id": 88, "name": "测试教练", "role": "教练", "job_type": 2,
                    "address": "0xstaff", "coaching_license": {
                        "code": 2, "name": "洲际 A 级", "next_code": 1,
                        "next_name": "洲际职业级", "maximum": False,
                    },
                }],
            },
        }
        state._bind_current_save = MagicMock(return_value="save-a")
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.status.return_value = {"installed": False}

        with patch("fm_odds_web.public_economy", return_value={}):
            result = state.training_run({
                "facility_id": desk["id"], "staff_id": 88, "player_id": 88,
                "team_id": 679, "operation": "coaching_license",
                "target_license_code": 1, "client_submission_id": "course-a",
            })

        self.assertFalse(result["duplicate"])
        self.assertEqual(result["focus"]["focus_type"], "coaching_license")
        self.assertEqual(result["focus"]["due_on"], "2026-06-30")
        self.assertEqual(result["staff"]["target_license_code"], 1)

    def test_training_run_schedules_player_manager_coaching_course(self) -> None:
        desk = purchase_facility("coaching_desk")
        place_facility(desk["id"], "office_3f", 50, 50)
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "game_date": "2026-01-01", "selected_manager_id": 77,
            "manager": {
                "id": 77, "name": "玩家经理", "manager_address": "0xmanager",
                "coaching_license": {
                    "code": 3, "name": "洲际 B 级", "next_code": 2,
                    "next_name": "洲际 A 级", "maximum": False,
                },
            },
            "managed_teams": [{
                "id": 679, "name": "曼城", "address": "0xteam",
                "manager_address": "0xmanager", "team_type": "club",
            }],
        }
        state.club_profile = None
        state.club_profiles = {}
        state._bind_current_save = MagicMock(return_value="save-a")
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.status.return_value = {"installed": False}

        with patch("fm_odds_web.public_economy", return_value={}):
            result = state.training_run({
                "facility_id": desk["id"], "staff_id": 77, "player_id": 77,
                "team_id": 679, "operation": "coaching_license",
                "subject_type": "player_manager", "target_license_code": 2,
                "client_submission_id": "manager-course-a",
            })

        self.assertFalse(result["duplicate"])
        self.assertEqual(result["focus"]["subject_type"], "player_manager")
        self.assertEqual(result["focus"]["staff_role"], "玩家经理")
        self.assertEqual(result["focus"]["due_on"], "2026-05-01")

    def test_training_run_schedules_position_familiarity_focus(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {"game_date": "2026-01-01"}
        state._bind_current_save = MagicMock(return_value="save-a")
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.status.return_value = {"installed": False}
        player = {
            "id": 10, "name": "A", "address": "0x1000",
            "position_ratings": {"MC": 16, "AMC": 8}, "ca": 100, "pa": 130,
        }
        profile = {
            "team": {"id": 1, "name": "测试队", "team_type": "club"},
            "players": [player],
        }
        state._live_player_effect_target = MagicMock(return_value=SimpleNamespace(
            profile=profile, player=player, address="0x1000",
        ))

        with (
            patch(
                "fm_odds_web.read_player_training_positions",
                return_value={"position-training-preview": 8},
            ),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            result = state.training_run({
                "facility_id": POSITION_TRAINING_AREA_ID,
                "player_id": 10, "team_id": 1, "operation": "position",
                "position_key": "AMC", "duration_mode": "until_14",
                "client_submission_id": "position-a",
            })

        self.assertFalse(result["duplicate"])
        self.assertEqual(result["focus"]["focus_type"], "position")
        self.assertEqual(result["focus"]["position_key"], "AMC")
        self.assertEqual(result["focus"]["target_position"], 14)
        self.assertEqual(
            result["focus"]["multiplier"], POSITION_TRAINING_MULTIPLIER,
        )
        self.assertFalse(result["player"]["ca_affected"])

    def test_training_run_schedules_maxed_position_as_no_change_focus(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {"game_date": "2026-01-01"}
        state._bind_current_save = MagicMock(return_value="save-a")
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.status.return_value = {"installed": False}
        player = {
            "id": 10, "name": "A", "address": "0x1000",
            "position_ratings": {"ST": 20}, "ca": 100, "pa": 130,
        }
        profile = {
            "team": {"id": 1, "name": "测试队", "team_type": "club"},
            "players": [player],
        }
        state._live_player_effect_target = MagicMock(return_value=SimpleNamespace(
            profile=profile, player=player, address="0x1000",
        ))

        with (
            patch(
                "fm_odds_web.read_player_training_positions",
                return_value={"position-training-preview": 20},
            ),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            result = state.training_run({
                "facility_id": POSITION_TRAINING_AREA_ID,
                "player_id": 10, "team_id": 1, "operation": "position",
                "position_key": "ST", "duration_mode": "until_20",
                "client_submission_id": "position-maxed-a",
            })

        self.assertFalse(result["duplicate"])
        self.assertTrue(result["focus"]["no_change"])
        self.assertEqual(result["focus"]["target_position"], 20)
        self.assertEqual(result["focus"]["required_points"], 0)
        self.assertFalse(result["player"]["ca_affected"])

    def test_training_run_schedules_hidden_attribute_without_ca_estimate(self) -> None:
        desk = purchase_facility("adaptation_pod")
        place_facility(desk["id"], "comprehensive_7f", 50, 50)
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {"game_date": "2026-01-01"}
        state._bind_current_save = MagicMock(return_value="save-a")
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        state.attribute_growth_hook.status.return_value = {"installed": False}
        player = {
            "id": 10, "name": "A", "address": "0xplayer",
            "hidden_attributes": {"适应性": 15, "多面性": 12},
            "ca": 100, "pa": 120,
        }
        profile = {
            "team": {"id": 1, "name": "测试队", "team_type": "club"},
            "players": [player],
        }
        state._live_player_effect_target = MagicMock(return_value=SimpleNamespace(
            profile=profile, player=player, address="0xplayer",
        ))

        with (
            patch("fm_odds_web.read_player_training_attributes", return_value={"training-preview": 15}),
            patch("fm_odds_web.estimate_player_training_ca") as estimate_ca,
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            result = state.training_run({
                "facility_id": desk["id"], "player_id": 10,
                "team_id": 1, "operation": "attribute",
                "attribute_key": "隐藏:适应性", "duration_mode": "until_10",
                "direction": -1,
            })

        estimate_ca.assert_not_called()
        self.assertEqual(result["focus"]["attribute_kind"], "hidden")
        self.assertEqual(result["focus"]["direction"], -1)
        self.assertEqual(result["focus"]["attribute_id"], None)
        self.assertFalse(result["player"]["ca_affected"])
        self.assertEqual((player["ca"], player["pa"]), (100, 120))

    def test_football_launcher_stop_ball_accepts_outfield_and_goalkeepers(self) -> None:
        launcher = purchase_facility("handling_net")
        place_facility(launcher["id"], "outdoor", 50, 50)
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {"game_date": "2026-01-01"}
        state._bind_current_save = MagicMock(return_value="save-a")
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        state.attribute_growth_hook.status.return_value = {"installed": False}
        players = {
            10: {
                "id": 10, "name": "外场球员", "address": "0x1000",
                "attributes": {"技术": {"停球": 12}}, "ca": 100, "pa": 140,
            },
            11: {
                "id": 11, "name": "门将", "address": "0x1100",
                "attributes": {"门将": {"手控球": 13, "停球": 14}},
                "ca": 100, "pa": 140,
            },
        }
        profile = {
            "team": {"id": 1, "name": "测试队", "team_type": "club"},
            "players": list(players.values()),
        }
        state._live_player_effect_target = MagicMock(side_effect=lambda player_id, _team_id, **_kwargs: SimpleNamespace(
            profile=profile, player=players[player_id], address=players[player_id]["address"],
        ))

        def estimate(player_id, _address, attribute_key, _target):
            current = 12 if player_id == 10 else 14
            self.assertEqual(attribute_key, "技术:停球")
            return {
                "attribute_before": current, "ca_cost": 1,
                "ca_before": 100, "pa": 140,
                "model": "test", "verified": True,
            }

        with (
            patch("fm_odds_web.read_player_training_attributes", return_value={}),
            patch("fm_odds_web.estimate_player_training_ca", side_effect=estimate),
            patch("fm_odds_web.public_economy", return_value={}),
        ):
            results = [
                state.training_run({
                    "facility_id": launcher["id"], "player_id": player_id,
                    "team_id": 1, "operation": "attribute",
                    "attribute_key": "技术:停球", "duration_mode": "until_increase",
                    "client_submission_id": f"launcher-stop-ball-{player_id}",
                })
                for player_id in players
            ]

        self.assertEqual(
            [result["focus"]["initial_attribute"] for result in results],
            [12, 14],
        )
        self.assertTrue(all(
            result["focus"]["attribute_key"] == "技术:停球" for result in results
        ))

    def test_training_catalog_uses_updated_prices_and_free_sessions(self) -> None:
        expected_prices = {
            "coaching_desk": 100_000.0,
            "leadership_desk": 800_000.0,
            "defence_learning_desk": 900_000.0,
            "habit_lab": 1_000_000.0,
            "strength_rack": 200_000.0,
            "treadmill": 800_000.0,
            "reaction_wall": 400_000.0,
            "tactics_table": 400_000.0,
            "jump_rig": 200_000.0,
            "hypoxic_pod": 400_000.0,
            "balance_platform": 200_000.0,
            "pressure_pod": 800_000.0,
            "shooting_goal": 500_000.0,
            "set_piece_area": 0.0,
            "team_training_area": 0.0,
            "goalkeeper_training_area": 0.0,
            "passing_training_area": 0.0,
            "position_training_area": 0.0,
            "dribble_course": 800_000.0,
            "tackle_gate": 1_200_000.0,
            "marking_track": 1_000_000.0,
            "high_ball_frame": 1_400_000.0,
            "handling_net": 1_000_000.0,
            "adaptation_pod": 8_000_000.0,
            "career_terminal": 12_000_000.0,
            "club_culture_screen": 6_000_000.0,
            "decisive_match_console": 15_000_000.0,
            "brainwave_chair": 10_000_000.0,
            "rules_learning_desk": 5_000_000.0,
        }
        self.assertEqual(set(expected_prices), set(FACILITIES))
        for sku, price in expected_prices.items():
            self.assertEqual(FACILITIES[sku]["price"], price)
        self.assertTrue(all(item["session_fee"] == 0.0 for item in FACILITIES.values()))
        self.assertEqual(FACILITIES["strength_rack"]["attributes"], ["身体:强壮"])

    def test_duplicate_purchase_and_overlap_are_allowed(self) -> None:
        first = purchase_facility("treadmill")
        place_facility(first["id"], "indoor", 40, 50)
        second = purchase_facility("treadmill")
        self.assertNotEqual(first["id"], second["id"])
        placed = place_facility(second["id"], "indoor", 40, 50)
        self.assertEqual((placed["x"], placed["y"]), (40.0, 50.0))

    def test_hidden_attribute_facilities_are_single_user_two_speed_devices(self) -> None:
        expected = {
            "adaptation_pod": ("适应模拟舱", ["隐藏:适应性", "隐藏:多面性"], 1),
            "career_terminal": ("生涯终端", ["隐藏:雄心", "隐藏:职业素养"], 1),
            "club_culture_screen": ("俱乐部文化屏", ["隐藏:忠诚", "隐藏:体育精神"], 1),
            "decisive_match_console": ("决胜主机", ["隐藏:抗压能力", "隐藏:大赛发挥"], 1),
            "brainwave_chair": ("脑波座椅", ["隐藏:情绪控制", "隐藏:稳定性"], 1),
            "rules_learning_desk": ("规则学习桌", ["隐藏:争议性", "隐藏:肮脏动作"], -1),
        }
        expected_scenes = {
            sku: "comprehensive_7f" for sku in expected
        }
        expected_scenes["rules_learning_desk"] = "office_6f"
        expected_sizes = {
            "club_culture_screen": [18.2, 16.8],
            "decisive_match_console": [19.2, 20],
            "brainwave_chair": [17.6, 20],
        }
        for sku, (name, attributes, direction) in expected.items():
            product = FACILITIES[sku]
            self.assertEqual(product["name"], name)
            self.assertEqual(product["attributes"], attributes)
            self.assertEqual(product["attribute_kind"], "hidden")
            self.assertEqual(product["direction"], direction)
            self.assertEqual(product["capacity"], 1)
            self.assertEqual(product["training_multiplier"], 2)
            self.assertEqual(product["scene"], expected_scenes[sku])
            if sku in expected_sizes:
                self.assertEqual(product["size"], expected_sizes[sku])
            self.assertTrue(((Path(__file__).resolve().parents[1] / "src") / "web" / "assets" / "training" / f"{sku}.webp").is_file())
        self.assertFalse(any(
            "受伤倾向" in attribute
            for sku in expected
            for attribute in FACILITIES[sku]["attributes"]
        ))

    def test_reverse_hidden_training_targets_progress_and_rebase(self) -> None:
        self.assertEqual(training_target_attribute("until_decrease", 15, -1), 14)
        self.assertEqual(training_target_attribute("until_10", 15, -1), 10)
        self.assertEqual(training_points_required_for_direction(15, -1), 14)
        with self.assertRaisesRegex(ValueError, "低于当前属性"):
            training_target_attribute("until_10", 8, -1)

        facility = purchase_facility("rules_learning_desk")
        with self.assertRaisesRegex(ValueError, "不能放置"):
            place_facility(facility["id"], "comprehensive_7f", 45, 50)
        place_facility(facility["id"], "office_6f", 45, 50)
        focus, _session = record_training_focus(
            facility["id"], 10, "A", "2026-07-20",
            "隐藏:争议性", None, "until_10", 15,
            attribute_kind="hidden", direction=-1,
        )
        self.assertEqual(focus["target_attribute"], 10)
        self.assertEqual(focus["required_points"], 14)
        self.assertEqual(focus["multiplier"], 2)
        self.assertEqual(focus["facility_name"], "规则学习桌")
        self.assertEqual(focus["floor"], "6F")
        self.assertEqual(focus["training_content"], "隐藏:争议性")
        advance_training_focuses("2026-07-21")
        active = active_training_focuses("2026-07-21")[0]
        self.assertEqual(active["progress_points"], 2)

        self.assertEqual(
            complete_training_focuses("2026-07-21", {focus["id"]: 15}), [],
        )
        advanced = complete_training_focuses(
            "2026-07-22", {focus["id"]: 14},
        )
        self.assertEqual(advanced, [])
        active = active_training_focuses("2026-07-22")[0]
        self.assertEqual(active["initial_attribute"], 14)
        self.assertEqual(active["target_attribute"], 10)
        rebased = rebase_attribute_training_focus(
            focus["id"], "2026-07-23", 16,
        )
        self.assertEqual(rebased["initial_attribute"], 16)
        self.assertEqual(rebased["target_attribute"], 10)

    def test_removed_placeholder_facilities_are_not_obtainable(self) -> None:
        floors = {row["scene"]: row for row in public_training_ground()["floors"]}
        self.assertTrue(floors["comprehensive_7f"]["unlocked"])
        for sku in (
            "passing_wall", "mind_room", "mentoring_room", "tactical_room",
            "video_analysis_room", "sports_science_room",
        ):
            self.assertNotIn(sku, FACILITIES)
            with self.assertRaisesRegex(ValueError, "训练器材不存在"):
                purchase_facility(sku)

    def test_training_catalog_keeps_outfield_coverage_without_placeholders(self) -> None:
        open_attributes = {
            attribute
            for product in FACILITIES.values()
            if product.get("available", True)
            for attribute in product.get("attributes", [])
        }
        open_visible_attributes = {
            attribute for attribute in open_attributes
            if not attribute.startswith("隐藏:")
        }
        self.assertTrue({
            "技术:抢断", "技术:盯人", "精神:勇敢", "精神:防守站位",
        } <= open_visible_attributes)
        self.assertNotIn("defence_zone", FACILITIES)
        self.assertNotIn("goalkeeper_rig", FACILITIES)

    def test_scale_is_persisted_without_collision_bounds(self) -> None:
        first = purchase_facility("treadmill")
        placed = place_facility(first["id"], "indoor", 35, 50, scale=1.65)
        self.assertEqual(placed["scale"], 1.65)
        second = purchase_facility("strength_rack")
        place_facility(second["id"], "indoor", 35, 50, scale=1.5)
        with self.assertRaisesRegex(ValueError, "50%至200%"):
            place_facility(second["id"], "indoor", 70, 50, scale=2.1)

    def test_visual_size_does_not_inflate_custom_footprint(self) -> None:
        first = purchase_facility("tactics_table")
        place_facility(first["id"], "indoor", 40, 50)
        second = purchase_facility("tactics_table")
        placed = place_facility(second["id"], "indoor", 50, 50)
        self.assertEqual(placed["status"], "placed")

    def test_facility_capacity_is_enforced_per_instance(self) -> None:
        strength = purchase_facility("strength_rack")
        place_facility(strength["id"], "indoor", 45, 50)
        for player_id in (10, 11):
            assert_training_available(strength["id"], player_id, "2026-07-19")
            record_training_focus(
                strength["id"], player_id, str(player_id), "2026-07-19",
                "身体:强壮", 0x33, "until_increase", 10,
            )
        with self.assertRaisesRegex(ValueError, "使用人数已满"):
            assert_training_available(strength["id"], 12, "2026-07-19")
        with self.assertRaisesRegex(ValueError, "不能收回"):
            store_facility(strength["id"])

    def test_configured_capacities_match_training_catalog(self) -> None:
        self.assertEqual(FACILITIES["coaching_desk"]["capacity"], 1)
        self.assertEqual(FACILITIES["strength_rack"]["capacity"], 2)
        self.assertEqual(FACILITIES["balance_platform"]["capacity"], 2)
        self.assertEqual(FACILITIES["pressure_pod"]["capacity"], 1)
        self.assertEqual(FACILITIES["leadership_desk"]["capacity"], 1)
        self.assertTrue(FACILITIES["set_piece_area"]["unlimited"])
        self.assertTrue(FACILITIES["team_training_area"]["unlimited"])

    def test_training_facilities_use_verified_attribute_names(self) -> None:
        self.assertEqual(
            FACILITIES["reaction_wall"]["attributes"],
            ["门将:反应", "精神:预判", "精神:集中"],
        )
        self.assertEqual(
            FACILITIES["tactics_table"]["attributes"],
            ["精神:视野", "精神:决断", "精神:无球跑动"],
        )
        self.assertEqual(FACILITIES["jump_rig"]["attributes"], ["身体:弹跳"])
        self.assertEqual(FACILITIES["hypoxic_pod"]["attributes"], ["身体:耐力", "身体:体质"])
        self.assertEqual(FACILITIES["balance_platform"]["attributes"], ["身体:平衡"])
        self.assertEqual(
            FACILITIES["pressure_pod"]["attributes"],
            ["精神:意志力", "精神:镇定"],
        )
        self.assertEqual(
            FACILITIES["set_piece_area"]["attributes"],
            ["定位球:任意球", "定位球:角球", "定位球:界外球"],
        )
        self.assertEqual(FACILITIES["leadership_desk"]["attributes"], ["精神:领导力"])
        self.assertEqual(
            FACILITIES["team_training_area"]["attributes"],
            ["精神:团队合作", "精神:工作投入", "精神:防守站位", "精神:才华", "精神:侵略性"],
        )
        self.assertEqual(
            FACILITIES["dribble_course"]["attributes"],
            ["技术:盘带", "技术:技术", "身体:灵活"],
        )

    def test_outdoor_catalog_adds_builtin_set_piece_area_without_selling_it(self) -> None:
        catalog = public_training_ground()["catalog"]
        purchasable = {
            item["sku"] for item in catalog
            if item["scene"] == "outdoor"
            and item.get("available", True)
            and not item.get("built_in")
        }
        self.assertEqual(
            purchasable,
            {
                "shooting_goal", "dribble_course", "tackle_gate",
                "marking_track", "high_ball_frame", "handling_net",
            },
        )
        area = next(row for row in public_training_ground()["facilities"] if row["sku"] == "set_piece_area")
        self.assertEqual(area["id"], OUTDOOR_SET_PIECE_AREA_ID)
        self.assertTrue(area["built_in"])
        team_area = next(row for row in public_training_ground()["facilities"] if row["sku"] == "team_training_area")
        self.assertEqual(team_area["id"], OUTDOOR_TEAM_TRAINING_AREA_ID)
        self.assertTrue(team_area["built_in"])
        with self.assertRaisesRegex(ValueError, "无需购买"):
            purchase_facility("set_piece_area")
        with self.assertRaisesRegex(ValueError, "无需购买"):
            purchase_facility("team_training_area")
        with self.assertRaisesRegex(ValueError, "无需购买"):
            purchase_facility("goalkeeper_training_area")
        with self.assertRaisesRegex(ValueError, "无需购买"):
            purchase_facility("passing_training_area")
    def test_new_specialist_equipment_has_unique_small_attribute_sets(self) -> None:
        specialist = {
            "tackle_gate", "marking_track", "high_ball_frame",
            "handling_net", "defence_learning_desk",
        }
        self.assertTrue(specialist <= set(FACILITIES))
        seen = set()
        for sku in specialist:
            product = FACILITIES[sku]
            self.assertNotIn("position_group", product)
            self.assertLessEqual(len(product["attributes"]), 2)
            self.assertTrue(product["attributes"])
            self.assertTrue(seen.isdisjoint(product["attributes"]))
            seen.update(product["attributes"])
        for sku in (
            "aerial_bar", "reaction_goal", "review_desk", "command_console",
            "duel_pad", "advance_runway", "defence_zone", "punching_pad",
            "goalkeeper_rig",
        ):
            self.assertNotIn(sku, FACILITIES)
            self.assertIn(sku, training_ground_module.REMOVED_FACILITY_SKUS)
            with self.assertRaisesRegex(ValueError, "训练器材不存在"):
                purchase_facility(sku)

    def test_new_outdoor_equipment_uses_requested_default_sizes(self) -> None:
        self.assertEqual(FACILITIES["high_ball_frame"]["size"], [15.6, 14.3])
        self.assertEqual(FACILITIES["tackle_gate"]["size"], [16.1, 14])
        self.assertEqual(FACILITIES["marking_track"]["size"], [16.5, 13.5])
        self.assertEqual(FACILITIES["handling_net"]["size"], [11.55, 11.55])
        self.assertNotIn("通过连续", FACILITIES["handling_net"]["description"])

    def test_outdoor_two_has_its_own_builtin_areas_and_accepts_outdoor_equipment(self) -> None:
        floors = {row["scene"]: row for row in public_training_ground()["floors"]}
        self.assertTrue(floors["outdoor"]["unlocked"])
        self.assertTrue(floors["outdoor_2"]["unlocked"])
        outdoor_two = {
            row["id"]: row for row in public_training_ground()["facilities"]
            if row["scene"] == "outdoor_2"
        }
        self.assertEqual(outdoor_two[OUTDOOR_2_SET_PIECE_AREA_ID]["sku"], "set_piece_area")
        self.assertEqual(outdoor_two[OUTDOOR_2_TEAM_TRAINING_AREA_ID]["sku"], "team_training_area")
        self.assertEqual(outdoor_two[OUTDOOR_2_GOALKEEPER_TRAINING_AREA_ID]["sku"], "goalkeeper_training_area")
        self.assertEqual(outdoor_two[OUTDOOR_2_PASSING_TRAINING_AREA_ID]["sku"], "passing_training_area")
        outdoor_one = {
            row["id"]: row for row in public_training_ground()["facilities"]
            if row["scene"] == "outdoor"
        }
        self.assertEqual(outdoor_one[OUTDOOR_GOALKEEPER_TRAINING_AREA_ID]["sku"], "goalkeeper_training_area")
        self.assertEqual(outdoor_one[OUTDOOR_PASSING_TRAINING_AREA_ID]["sku"], "passing_training_area")
        facility = purchase_facility("tackle_gate")
        placed = place_facility(facility["id"], "outdoor_2", 50, 50)
        self.assertEqual(placed["scene"], "outdoor_2")

    def test_outdoor_builtin_goalkeeper_and_passing_areas_own_their_attributes(self) -> None:
        self.assertEqual(
            FACILITIES["goalkeeper_training_area"]["attributes"],
            ["门将:大脚开球", "门将:手抛球", "门将:出击(倾向)"],
        )
        self.assertEqual(
            FACILITIES["goalkeeper_training_area"]["position_group"],
            "goalkeepers",
        )
        self.assertEqual(
            FACILITIES["passing_training_area"]["attributes"],
            ["技术:传球", "技术:传中"],
        )
        self.assertNotIn("kick_target", FACILITIES)
        self.assertNotIn("distribution_wall", FACILITIES)
        self.assertEqual(FACILITIES["handling_net"]["name"], "足球发射器")
        self.assertEqual(FACILITIES["handling_net"]["image_sku"], "football_launcher")
        self.assertEqual(
            FACILITIES["handling_net"]["attributes"],
            ["门将:手控球", "技术:停球"],
        )

    def test_builtin_set_piece_area_has_no_player_capacity_limit(self) -> None:
        for player_id in range(1, 26):
            facility, product = assert_training_available(
                OUTDOOR_SET_PIECE_AREA_ID, player_id, "2026-07-19",
            )
            self.assertEqual(facility["sku"], "set_piece_area")
            self.assertTrue(product["unlimited"])
            record_training_focus(
                OUTDOOR_SET_PIECE_AREA_ID, player_id, str(player_id),
                "2026-07-19", "定位球:任意球", 0x32,
                "until_increase", 10,
            )
        active = [
            row for row in public_training_ground("2026-07-19")["focuses"]
            if row["status"] == "active"
            and row["facility_id"] == OUTDOOR_SET_PIECE_AREA_ID
        ]
        self.assertEqual(len(active), 25)

    def test_position_training_focus_advances_and_stages_to_target(self) -> None:
        self.assertEqual(
            FACILITIES["position_training_area"]["training_multiplier"],
            POSITION_TRAINING_MULTIPLIER,
        )
        self.assertIn("四倍速", FACILITIES["position_training_area"]["description"])
        focus, _session = record_position_training_focus(
            POSITION_TRAINING_AREA_ID, 10, "A", "2026-07-19",
            "AMC", "until_14", 8, player_address="0x1000",
            player_team_id=1, player_team_name="测试队",
        )
        self.assertEqual(focus["required_points"], 14)
        self.assertEqual(focus["multiplier"], POSITION_TRAINING_MULTIPLIER)
        self.assertEqual(
            public_training_ground("2026-07-19")["focuses"][0]["estimated_days"],
            4,
        )
        self.assertEqual(
            advance_training_focuses("2026-07-26")[0]["id"], focus["id"],
        )

        staged = complete_position_training_focus(
            focus["id"], "2026-07-26", {"position_after": 9},
        )
        self.assertEqual(staged["status"], "active")
        self.assertEqual(staged["initial_position"], 9)
        self.assertEqual(staged["target_position"], 14)
        self.assertEqual(staged["progress_points"], 14)

    def test_position_training_until_20_persists_one_point_per_stage(self) -> None:
        focus, _session = record_position_training_focus(
            POSITION_TRAINING_AREA_ID, 10, "A", "2026-07-19",
            "ST", "until_20", 15,
        )

        settlement_dates = (
            "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01",
            "2026-12-01",
        )
        for before, game_date in zip(range(15, 20), settlement_dates):
            after = complete_position_training_focus(
                focus["id"], game_date,
                {"position_before": before, "position_after": before + 1},
            )
            if before < 19:
                self.assertEqual(after["status"], "active")
                self.assertEqual(after["initial_position"], before + 1)
            else:
                self.assertEqual(after["status"], "completed")

        self.assertEqual(active_training_focuses("2027-01-01"), [])

    def test_position_training_rejects_multi_point_native_settlement(self) -> None:
        focus, _session = record_position_training_focus(
            POSITION_TRAINING_AREA_ID, 10, "A", "2026-07-19",
            "ST", "until_20", 15,
        )

        with self.assertRaisesRegex(ValueError, "每次只能提升1点"):
            complete_position_training_focus(
                focus["id"], "2026-08-01",
                {"position_before": 15, "position_after": 17},
            )

        active = active_training_focuses("2026-08-01")
        self.assertEqual(active[0]["initial_position"], 15)
        self.assertEqual(active[0]["status"], "active")

    def test_position_training_15_to_20_uses_smooth_180_day_curve(self) -> None:
        self.assertEqual(position_training_points_required(18), 192)
        self.assertEqual(position_training_points_required(19), 272)
        self.assertEqual(
            training_ground_module.estimated_position_training_days_to_target(15, 20),
            180,
        )
        self.assertEqual(
            training_ground_module.estimated_position_training_days_to_target(
                15, 20, 720,
            ),
            0,
        )
        focus, _session = record_position_training_focus(
            POSITION_TRAINING_AREA_ID, 10, "A", "2026-07-19",
            "ST", "until_20", 19,
        )
        self.assertEqual(focus["required_points"], 272)
        public = public_training_ground("2026-07-19")["focuses"][0]
        self.assertEqual(public["estimated_days"], 68)

    def test_maxed_position_training_stays_active_without_changes(self) -> None:
        self.assertEqual(position_training_points_required(20), 0)
        self.assertEqual(
            training_ground_module.estimated_position_training_days_to_target(20, 20),
            0,
        )
        focus, _session = record_position_training_focus(
            POSITION_TRAINING_AREA_ID, 10, "A", "2026-07-19",
            "ST", "until_20", 20,
        )

        self.assertTrue(focus["no_change"])
        self.assertEqual(focus["target_position"], 20)
        self.assertEqual(focus["required_points"], 0)
        self.assertEqual(focus["progress_points"], 0)
        self.assertEqual(advance_training_focuses("2027-07-19"), [])
        public = public_training_ground("2027-07-19")["focuses"][0]
        self.assertEqual(public["status"], "active")
        self.assertEqual(public["estimated_days"], 0)

    def test_position_training_writer_changes_only_selected_position_byte(self) -> None:
        positions = bytearray([1] * 15)
        positions[10] = 8
        reader = MagicMock()
        reader.bytes.return_value = bytes(positions)
        layout = SimpleNamespace(
            player_positions_offset=0x300, module_name="fm.exe",
        )
        layout.module = MagicMock(
            return_value=SimpleNamespace(base_address=0x400000),
        )
        process = MagicMock()
        opened = MagicMock()
        opened.__enter__.return_value = process
        opened.__exit__.return_value = False
        with (
            patch(
                "tools.club_reader.select_process_layout",
                return_value=(1, "fm.exe", layout),
            ),
            patch("tools.club_reader.open_process", return_value=opened),
            patch("tools.club_reader.Reader", return_value=reader),
            patch(
                "tools.club_reader._validated_player_person",
                return_value=0x2000,
            ),
            patch(
                "tools.club_reader._apply_verified_memory_changes",
            ) as apply_changes,
        ):
            result = develop_player_position(10, "0x1000", "AMC", 8)

        change = apply_changes.call_args.args[2][0]
        self.assertEqual(change["address"], 0x1000 + 0x300 + 10)
        self.assertEqual(change["original"], b"\x08")
        self.assertEqual(change["updated"], b"\x09")
        self.assertEqual(result["position_after"], 9)
        self.assertEqual(result["position_ratings"]["AMC"], 9)

    def test_builtin_training_areas_use_slower_two_point_daily_growth(self) -> None:
        for facility_id, attribute_key, attribute_id in (
            (OUTDOOR_SET_PIECE_AREA_ID, "定位球:任意球", 0x32),
            (OUTDOOR_TEAM_TRAINING_AREA_ID, "精神:团队合作", 0x2B),
        ):
            focus, _session = record_training_focus(
                facility_id, attribute_id, str(attribute_id), "2026-07-19",
                attribute_key, attribute_id, "until_increase", 10,
            )
            self.assertEqual(focus["multiplier"], NO_EQUIPMENT_TRAINING_MULTIPLIER)
        self.assertEqual(advance_training_focuses("2026-07-22"), [])
        rows = {
            row["facility_id"]: row
            for row in public_training_ground("2026-07-22")["focuses"]
            if row["status"] == "active"
        }
        for facility_id in (OUTDOOR_SET_PIECE_AREA_ID, OUTDOOR_TEAM_TRAINING_AREA_ID):
            self.assertEqual(rows[facility_id]["progress_points"], 6)
            self.assertEqual(rows[facility_id]["estimated_days"], 4)
            self.assertEqual(rows[facility_id]["multiplier"], 2)

    def test_indoor_facilities_can_be_placed_on_independent_second_floor(self) -> None:
        first = purchase_facility("treadmill")
        place_facility(first["id"], "indoor", 40, 50)
        second = purchase_facility("treadmill")
        placed = place_facility(second["id"], "indoor_2f", 40, 50)
        self.assertEqual(placed["scene"], "indoor_2f")
        goal = purchase_facility("shooting_goal")
        with self.assertRaisesRegex(ValueError, "不能放置"):
            place_facility(goal["id"], "indoor_2f", 60, 50)

    def test_coaching_desk_is_isolated_to_third_floor_office(self) -> None:
        desk = purchase_facility("coaching_desk")
        with self.assertRaisesRegex(ValueError, "不能放置"):
            place_facility(desk["id"], "indoor", 50, 50)
        placed = place_facility(desk["id"], "office_3f", 50, 50)
        self.assertEqual(placed["scene"], "office_3f")
        treadmill = purchase_facility("treadmill")
        with self.assertRaisesRegex(ValueError, "不能放置"):
            place_facility(treadmill["id"], "office_3f", 50, 50)

    def test_leadership_desk_is_single_person_office_equipment(self) -> None:
        self.assertEqual(FACILITIES["leadership_desk"]["name"], "领导力训练桌")
        desk = purchase_facility("leadership_desk")
        with self.assertRaisesRegex(ValueError, "不能放置"):
            place_facility(desk["id"], "indoor", 50, 50)
        placed = place_facility(desk["id"], "office_3f", 50, 50)
        self.assertEqual(placed["scene"], "office_3f")
        self.assertEqual(FACILITIES["leadership_desk"]["capacity"], 1)

    def test_defence_learning_desk_is_single_person_office_equipment(self) -> None:
        product = FACILITIES["defence_learning_desk"]
        self.assertEqual(product["name"], "防守学习桌")
        self.assertEqual(product["scene"], "office_6f")
        self.assertEqual(product["capacity"], 1)
        self.assertEqual(product["attributes"], ["门将:指挥防守", "门将:拳击球(倾向)"])
        desk = purchase_facility("defence_learning_desk")
        with self.assertRaisesRegex(ValueError, "不能放置"):
            place_facility(desk["id"], "indoor", 50, 50)
        placed = place_facility(desk["id"], "office_6f", 50, 50)
        self.assertEqual(placed["scene"], "office_6f")

    def test_coaching_license_course_progresses_and_completes(self) -> None:
        desk = purchase_facility("coaching_desk")
        place_facility(desk["id"], "office_3f", 50, 50)
        focus, session = record_coaching_license_focus(
            desk["id"], 88, "测试教练", "教练", "0x1234",
            679, "曼城", "2026-01-01", 2, 1,
            client_submission_id="course-1",
        )
        self.assertEqual(focus["due_on"], "2026-06-30")
        self.assertEqual(session["operation"], "coaching_license")
        self.assertEqual(focus["facility_name"], "教练学习桌")
        self.assertEqual(focus["floor"], "3F")
        self.assertEqual(focus["training_content"], "教练证书进修")
        self.assertEqual(due_coaching_license_focuses("2026-06-29"), [])
        self.assertEqual(
            [row["id"] for row in due_coaching_license_focuses("2026-06-30")],
            [focus["id"]],
        )
        completed = complete_coaching_license_focus(
            focus["id"], "2026-06-30", {"before": 2, "after": 1},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["memory_result"]["after"], 1)

    def test_coach_cannot_use_two_learning_desks_at_once(self) -> None:
        first = purchase_facility("coaching_desk")
        second = purchase_facility("coaching_desk")
        place_facility(first["id"], "office_3f", 35, 50)
        place_facility(second["id"], "office_3f", 65, 50)
        record_coaching_license_focus(
            first["id"], 88, "测试教练", "教练", "0x1234",
            679, "曼城", "2026-01-01", 3, 2,
        )
        with self.assertRaisesRegex(ValueError, "已有进行中的考证课程"):
            record_coaching_license_focus(
                second["id"], 88, "测试教练", "教练", "0x1234",
                679, "曼城", "2026-01-02", 3, 2,
            )

    def test_each_indoor_floor_is_limited_to_fifteen_facilities(self) -> None:
        for _index in range(INDOOR_FACILITY_LIMIT):
            facility = purchase_facility("treadmill")
            place_facility(facility["id"], "indoor", 40, 50)
        overflow = purchase_facility("treadmill")
        with self.assertRaisesRegex(ValueError, "每层最多放置15个"):
            place_facility(overflow["id"], "indoor", 40, 50)
        place_facility(overflow["id"], "indoor_2f", 40, 50)

    def test_outdoor_has_no_facility_limit(self) -> None:
        for _index in range(INDOOR_FACILITY_LIMIT + 1):
            facility = purchase_facility("shooting_goal")
            place_facility(facility["id"], "outdoor", 50, 50)
        purchased = [
            row for row in public_training_ground()["facilities"]
            if row["sku"] == "shooting_goal"
        ]
        self.assertEqual(len(purchased), INDOOR_FACILITY_LIMIT + 1)

    def test_clear_training_ground_removes_facilities_people_and_records(self) -> None:
        facility = purchase_facility("treadmill")
        place_facility(facility["id"], "indoor", 35, 50)
        record_training_focus(
            facility["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )

        cleared = clear_training_ground()

        self.assertEqual(cleared["facilities"], 1)
        self.assertEqual(cleared["focuses"], 1)
        self.assertEqual(cleared["sessions"], 1)
        public = public_training_ground()
        self.assertEqual(
            [(row["id"], row["sku"]) for row in public["facilities"]],
            [
                (OUTDOOR_SET_PIECE_AREA_ID, "set_piece_area"),
                (OUTDOOR_TEAM_TRAINING_AREA_ID, "team_training_area"),
                (OUTDOOR_2_SET_PIECE_AREA_ID, "set_piece_area"),
                (OUTDOOR_2_TEAM_TRAINING_AREA_ID, "team_training_area"),
                (OUTDOOR_GOALKEEPER_TRAINING_AREA_ID, "goalkeeper_training_area"),
                (OUTDOOR_2_GOALKEEPER_TRAINING_AREA_ID, "goalkeeper_training_area"),
                (OUTDOOR_PASSING_TRAINING_AREA_ID, "passing_training_area"),
                (OUTDOOR_2_PASSING_TRAINING_AREA_ID, "passing_training_area"),
                (POSITION_TRAINING_AREA_ID, "position_training_area"),
            ],
        )
        self.assertEqual(public["focuses"], [])
        self.assertEqual(public["sessions"], [])

    def test_releasing_training_users_preserves_facilities_and_sessions(self) -> None:
        facility = purchase_facility("treadmill")
        place_facility(facility["id"], "indoor", 35, 50)
        focus, session = record_training_focus(
            facility["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )
        before = public_training_ground("2026-07-19")

        released = release_training_users(
            "2026-07-20", reason="manager_club_change",
        )
        after = public_training_ground("2026-07-20")

        self.assertEqual(released["released"], 1)
        self.assertEqual(after["facilities"], before["facilities"])
        self.assertEqual(after["sessions"], before["sessions"])
        cancelled = next(row for row in after["focuses"] if row["id"] == focus["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancel_reason"], "manager_club_change")
        self.assertEqual(after["sessions"][0]["id"], session["id"])
        assert_training_available(facility["id"], 11, "2026-07-20")

    def test_releasing_training_users_save_failure_keeps_original_state(self) -> None:
        facility = purchase_facility("treadmill")
        place_facility(facility["id"], "indoor", 35, 50)
        focus, _session = record_training_focus(
            facility["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )

        with (
            patch(
                "tools.training_ground.save_training_ground",
                side_effect=OSError("simulated save failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated save failure"),
        ):
            release_training_users("2026-07-20")

        after = public_training_ground("2026-07-20")
        self.assertTrue(any(row["id"] == facility["id"] for row in after["facilities"]))
        active = next(row for row in after["focuses"] if row["id"] == focus["id"])
        self.assertEqual(active["status"], "active")

    def test_training_point_requirements_and_estimates(self) -> None:
        expected = {
            1: 14, 10: 14, 11: 21, 12: 28, 13: 35, 14: 42,
            15: 63, 16: 126, 17: 300, 18: 540, 19: 900,
        }
        for attribute, points in expected.items():
            self.assertEqual(training_points_required(attribute), points)
            self.assertEqual(estimated_training_days(attribute), (points + 2) // 3)
        with self.assertRaisesRegex(ValueError, "1至19"):
            training_points_required(20)

    def test_training_focus_accumulates_three_points_per_game_day(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)
        focus, session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )
        self.assertIsNone(focus["expires_on"])
        self.assertEqual(focus["multiplier"], 3)
        self.assertEqual((focus["required_points"], focus["progress_points"]), (14, 0))
        self.assertEqual(session["operation"], "attribute_focus")
        self.assertEqual(advance_training_focuses("2026-07-23"), [])
        row = public_training_ground("2026-07-23")["focuses"][0]
        self.assertEqual((row["progress_points"], row["remaining_points"]), (12, 2))
        self.assertEqual(row["estimated_days"], 1)
        due = advance_training_focuses("2026-07-24")
        self.assertEqual(due[0]["id"], focus["id"])
        self.assertEqual(due[0]["progress_points"], 14)

    def test_training_submission_id_is_persisted_for_safe_retry(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)
        focus, session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10, client_submission_id="training-request-1",
        )

        found = find_training_submission("training-request-1")

        self.assertIsNotNone(found)
        found_focus, found_session = found
        self.assertEqual(found_focus["id"], focus["id"])
        self.assertEqual(found_session["id"], session["id"])
        self.assertIsNone(find_training_submission("missing-request"))

    def test_training_focus_persists_managed_team_and_player_identity(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)

        focus, session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10, player_address="0x1234",
            player_team_id=86, player_team_name="中国", player_team_type="national",
        )

        for row in (focus, session):
            self.assertEqual(row["player_address"], "0x1234")
            self.assertEqual(row["player_team_id"], 86)
            self.assertEqual(row["player_team_name"], "中国")
            self.assertEqual(row["player_team_type"], "national")

        rebound = rebind_training_focus_player(focus["id"], "0x5678")
        self.assertEqual(rebound["player_address"], "0x5678")
        public = public_training_ground("2026-07-19")
        self.assertEqual(public["focuses"][0]["player_address"], "0x5678")
        self.assertEqual(public["sessions"][0]["player_address"], "0x5678")

    def test_training_sync_uses_focus_team_when_player_is_in_both_rosters(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {"save_instance_id": "save-a"}
        state.club_profile = None
        state.club_profiles = {
            1: {
                "team": {"id": 1, "team_type": "club"},
                "players": [{"id": 10, "address": "0x1111", "attributes": {"身体": {"速度": 10}}}],
            },
            86: {
                "team": {"id": 86, "team_type": "national"},
                "players": [{"id": 10, "address": "0x8686", "attributes": {"身体": {"速度": 10}}}],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "focus-1", "status": "active", "focus_type": "attribute",
            "player_id": 10, "player_name": "A", "player_team_id": 86,
            "player_address": "0xold", "attribute_key": "身体:速度",
            "attribute_id": 0x35, "initial_attribute": 10,
            "duration_mode": "until_increase", "expires_on": None,
        }
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.advance_training_focuses", return_value=[]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"focus-1": 10}) as read,
        ):
            state._sync_training_focuses("2026-07-20")

        self.assertEqual(read.call_args.args[0][0]["player_address"], "0x8686")

    def test_training_sync_rebases_external_position_jump_instead_of_settling(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {
            "save_instance_id": "save-a",
            "managed_teams": [{"id": 1, "address": "0xteam", "team_type": "club"}],
        }
        state.club_profile = None
        state.club_profiles = {
            1: {
                "team": {"id": 1, "address": "0xteam", "team_type": "club"},
                "players": [{"id": 10, "name": "A", "address": "0xplayer"}],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "position-focus", "status": "active", "focus_type": "position",
            "player_id": 10, "player_name": "A", "player_team_id": 1,
            "player_address": "0xplayer", "position_key": "ST",
            "initial_position": 15, "target_position": 20,
            "required_points": 48, "progress_points": 48,
            "duration_mode": "until_20", "expires_on": None,
        }
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch(
                "fm_odds_web.read_player_training_positions",
                return_value={"position-focus": 17},
            ),
            patch("fm_odds_web.rebase_position_training_focus") as rebase,
            patch("fm_odds_web.complete_position_training_focus") as complete,
        ):
            state._sync_training_focuses("2026-08-01")

        rebase.assert_called_once_with("position-focus", "2026-08-01", 17)
        complete.assert_not_called()

    def test_training_sync_catches_up_until_20_with_five_single_point_writes(self) -> None:
        focus, _session = record_position_training_focus(
            POSITION_TRAINING_AREA_ID, 10, "A", "2026-07-19",
            "ST", "until_20", 15, player_address="0xplayer",
            player_team_id=1, player_team_name="测试队",
        )
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {
            "save_instance_id": "save-a",
            "managed_teams": [{"id": 1, "address": "0xteam", "team_type": "club"}],
        }
        state.club_profile = None
        state.club_profiles = {
            1: {
                "team": {"id": 1, "address": "0xteam", "team_type": "club"},
                "players": [{"id": 10, "name": "A", "address": "0xplayer"}],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}

        def develop(_player_id, _address, position_key, expected):
            after = expected + 1
            return {
                "player_id": 10, "position_key": position_key,
                "position_before": expected, "position_after": after,
                "attribute_before": expected, "attribute_after": after,
                "position_ratings": {"ST": after}, "positions": ["ST"],
                "primary_positions": ["ST"],
                "_original_raw": bytes([expected]),
                "_updated_raw": bytes([after]),
            }

        with (
            patch(
                "fm_odds_web.read_player_training_positions",
                return_value={focus["id"]: 15},
            ),
            patch("fm_odds_web.develop_player_position", side_effect=develop) as writer,
            patch("fm_odds_web.restore_player_position") as restore,
        ):
            result = state._sync_training_focuses("2027-01-15")

        self.assertEqual(
            [call.args[3] for call in writer.call_args_list],
            [15, 16, 17, 18, 19],
        )
        restore.assert_not_called()
        self.assertEqual(result["completed_position_focus_ids"], [focus["id"]])
        self.assertEqual(active_training_focuses("2027-01-15"), [])

    def test_due_coaching_course_upgrades_validated_staff_and_completes(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {"save_instance_id": "save-a"}
        state.club_profile = None
        staff = {
            "id": 88, "name": "测试教练", "role": "教练", "job_type": 2,
            "address": "0xlive", "coaching_license": {
                "code": 2, "name": "洲际 A 级", "next_code": 1,
                "next_name": "洲际职业级", "maximum": False,
            },
        }
        state.club_profiles = {
            679: {
                "team": {"id": 679, "name": "曼城", "address": "0xteam", "team_type": "club"},
                "players": [], "staff": [staff],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "coach-focus", "status": "active",
            "focus_type": "coaching_license", "staff_id": 88,
            "staff_name": "测试教练", "staff_address": "0xold",
            "staff_team_id": 679, "initial_license_code": 2,
            "target_license_code": 1, "due_on": "2026-07-20",
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], []]),
            patch("fm_odds_web.advance_training_focuses", return_value=[]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[focus]),
            patch("fm_odds_web.rebind_training_focus_staff") as rebind,
            patch(
                "fm_odds_web.upgrade_staff_coaching_license",
                return_value={"staff_id": 88, "before": 2, "after": 1,
                              "license": "洲际职业级"},
            ) as upgrade,
            patch("fm_odds_web.complete_coaching_license_focus") as complete,
        ):
            result = state._sync_training_focuses("2026-07-20")

        rebind.assert_called_once_with("coach-focus", "0xlive")
        upgrade.assert_called_once_with(88, "0xlive", 679, "0xteam", 2, 1)
        complete.assert_called_once()
        self.assertEqual(result["completed_coaching_license_focus_ids"], ["coach-focus"])
        self.assertEqual(staff["coaching_license"]["code"], 1)
        self.assertTrue(staff["coaching_license"]["maximum"])

    def test_due_player_manager_course_uses_manager_writer_and_completes(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {
            "save_instance_id": "save-a", "selected_manager_id": 77,
            "manager": {
                "id": 77, "name": "玩家经理", "manager_address": "0xlive",
                "coaching_license": {
                    "code": 2, "name": "洲际 A 级", "next_code": 1,
                    "next_name": "洲际职业级", "maximum": False,
                },
            },
            "managed_teams": [{
                "id": 679, "name": "曼城", "address": "0xteam",
                "manager_address": "0xlive", "team_type": "club",
            }],
        }
        state.club_profile = None
        state.club_profiles = {}
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "manager-focus", "status": "active",
            "focus_type": "coaching_license", "subject_type": "player_manager",
            "staff_id": 77, "staff_name": "玩家经理", "staff_address": "0xold",
            "staff_team_id": 679, "initial_license_code": 2,
            "target_license_code": 1, "due_on": "2026-07-20",
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], []]),
            patch("fm_odds_web.advance_training_focuses", return_value=[]),
            patch("fm_odds_web.due_coaching_license_focuses", return_value=[focus]),
            patch("fm_odds_web.rebind_training_focus_staff") as rebind,
            patch(
                "fm_odds_web.upgrade_human_manager_coaching_license",
                return_value={"manager_id": 77, "before": 2, "after": 1,
                              "license": "洲际职业级"},
            ) as upgrade,
            patch("fm_odds_web.complete_coaching_license_focus") as complete,
        ):
            result = state._sync_training_focuses("2026-07-20")

        rebind.assert_called_once_with("manager-focus", "0xlive")
        upgrade.assert_called_once_with(77, "0xlive", 679, "0xteam", 2, 1)
        complete.assert_called_once()
        self.assertEqual(
            result["completed_coaching_license_focus_ids"], ["manager-focus"],
        )
        self.assertEqual(state.output["manager"]["coaching_license"]["code"], 1)
        self.assertTrue(state.output["manager"]["coaching_license"]["maximum"])

    def test_training_sync_can_validate_saved_address_after_national_roster_changes(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {"save_instance_id": "save-a"}
        state.club_profile = None
        state.club_profiles = {}
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "focus-1", "status": "active", "focus_type": "attribute",
            "player_id": 10, "player_name": "A", "player_team_id": 86,
            "player_address": "0x8686", "attribute_key": "身体:速度",
            "attribute_id": 0x35, "initial_attribute": 10,
            "duration_mode": "until_increase", "expires_on": None,
        }
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.advance_training_focuses", return_value=[]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"focus-1": 10}) as read,
        ):
            result = state._sync_training_focuses("2026-07-20")

        self.assertEqual(read.call_args.args[0][0]["player_address"], "0x8686")
        self.assertEqual(result["pending_player_ids"], [])

    def test_hidden_training_settlement_uses_hidden_writer_and_rolls_back_persist_failure(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {"save_instance_id": "save-a"}
        state.club_profile = None
        player = {
            "id": 10, "name": "A", "address": "0x1111",
            "hidden_attributes": {"争论": 15}, "ca": 100, "pa": 120,
        }
        state.club_profiles = {
            1: {
                "team": {"id": 1, "team_type": "club"},
                "players": [player],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "hidden-focus", "status": "active", "focus_type": "attribute",
            "attribute_kind": "hidden", "direction": -1,
            "player_id": 10, "player_name": "A", "player_team_id": 1,
            "player_address": "0x1111", "attribute_key": "隐藏:争议性",
            "attribute_id": None, "initial_attribute": 15,
            "progress_points": 14, "required_points": 14,
            "duration_mode": "until_decrease", "expires_on": None,
        }
        native = {
            "player_id": 10, "attribute_key": "隐藏:争议性",
            "attribute_before": 15, "attribute_after": 14,
            "hidden_attributes": {"争论": 14}, "ca_affected": False,
            "_original_raw": b"\x0f", "_updated_raw": b"\x0e",
        }
        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], [focus]]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch("fm_odds_web.read_player_training_attributes", return_value={"hidden-focus": 15}),
            patch("fm_odds_web.develop_player_hidden_attribute", return_value=native) as develop_hidden,
            patch("fm_odds_web.develop_player") as develop_visible,
            patch(
                "fm_odds_web.complete_attribute_training_focus",
                side_effect=RuntimeError("persist failed"),
            ),
            patch("fm_odds_web.restore_player_hidden_attribute") as restore_hidden,
        ):
            result = state._sync_training_focuses("2026-07-20")

        develop_hidden.assert_called_once_with(
            10, "0x1111", "隐藏:争议性", 15, -1,
        )
        develop_visible.assert_not_called()
        restore_hidden.assert_called_once_with(
            10, "0x1111", "隐藏:争议性", b"\x0f", b"\x0e",
        )
        self.assertIn("persist failed", result["progress_error"])
        self.assertEqual(player["hidden_attributes"]["争论"], 15)
        self.assertEqual((player["ca"], player["pa"]), (100, 120))

    def test_training_sync_safely_validates_saved_address_after_team_change(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {"save_instance_id": "save-a"}
        state.club_profile = None
        state.club_profiles = {
            200: {
                "team": {"id": 200, "team_type": "club"},
                "players": [{"id": 20, "address": "0x2020"}],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "focus-1", "status": "active", "focus_type": "attribute",
            "player_id": 10, "player_name": "Old Player", "player_team_id": 100,
            "player_address": "0xold", "attribute_key": "身体:速度",
            "attribute_id": 0x35, "initial_attribute": 10,
            "progress_points": 14, "required_points": 14,
            "duration_mode": "until_increase", "expires_on": None,
        }
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch("fm_odds_web.read_player_training_attributes", return_value={}) as read,
        ):
            result = state._sync_training_focuses("2026-07-20")

        self.assertEqual(read.call_args.args[0][0]["player_address"], "0xold")
        self.assertEqual(result["pending_player_ids"], [10])

    def test_due_training_searches_every_managed_team_and_finishes(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {
            "save_instance_id": "save-a",
            "managed_teams": [
                {"id": 86, "address": "0xteam-86", "team_type": "national"},
                {"id": 1, "address": "0xteam-1", "team_type": "club"},
            ],
        }
        state.club_profile = None
        state.club_profiles = {}
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "focus-1", "status": "active", "focus_type": "attribute",
            "player_id": 10, "player_name": "Moved Player", "player_team_id": 86,
            "player_address": "0xstale", "attribute_key": "身体:速度",
            "attribute_id": 0x35, "initial_attribute": 10,
            "progress_points": 14, "required_points": 14,
            "duration_mode": "until_increase", "expires_on": None,
        }
        development = {
            "_original_raw": b"original", "_original_ca": 100, "_original_pa": 120,
            "attributes": {"身体": {"速度": 11}},
            "attribute_after": 11, "ca": 101, "pa": 120,
        }

        def roster(team_address):
            if team_address == "0xteam-1":
                return [{"id": 10, "name": "Moved Player", "address": "0xlive"}]
            return []

        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[[focus], [focus]]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch("fm_odds_web.read_team_roster", side_effect=roster) as scan,
            patch(
                "fm_odds_web.read_player_training_attributes",
                return_value={"focus-1": 10},
            ) as read,
            patch("fm_odds_web.rebind_training_focus_player") as rebind,
            patch("fm_odds_web.develop_player", return_value=development) as develop,
            patch(
                "fm_odds_web.complete_attribute_training_focus",
                return_value={"status": "completed"},
            ),
        ):
            result = state._sync_training_focuses("2026-07-24")

        self.assertEqual(
            [call.args[0] for call in scan.call_args_list],
            ["0xteam-86", "0xteam-1"],
        )
        self.assertEqual(read.call_args.args[0][0]["player_address"], "0xlive")
        rebind.assert_called_once_with("focus-1", "0xlive")
        self.assertEqual(develop.call_args.args[1], "0xlive")
        self.assertEqual(result["pending_player_ids"], [])
        self.assertEqual(result["completed_attribute_focus_ids"], ["focus-1"])

    def test_training_settlement_worker_finishes_without_frontend_page(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-a",
            "account_scope_id": "scope-a",
        }
        state.training_settlement_requested = (
            "save-a", "scope-a", "2026-07-24",
        )
        state.training_settlement_worker_active = True
        state.training_sync_status = None
        state.attribute_growth_hook = MagicMock()
        state._sync_training_focuses = MagicMock(return_value={
            "pending_player_ids": [],
            "completed_attribute_focus_ids": ["focus-1"],
        })

        state._training_settlement_worker()

        state._sync_training_focuses.assert_called_once_with("2026-07-24")
        self.assertEqual(
            state.training_sync_status["completed_attribute_focus_ids"],
            ["focus-1"],
        )
        self.assertFalse(state.training_settlement_worker_active)

    def test_training_settlement_worker_binds_scope_in_real_thread(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-a",
            "account_scope_id": "scope-a",
        }
        state.training_settlement_requested = (
            "save-a", "scope-a", "2026-07-24",
        )
        state.training_settlement_worker_active = True
        state.training_sync_status = None
        state.attribute_growth_hook = MagicMock()
        observed_scopes = []

        def settle(_game_date: str) -> dict[str, object]:
            observed_scopes.append(active_save_id())
            return {"completed_attribute_focus_ids": []}

        state._sync_training_focuses = settle
        worker = threading.Thread(target=state._training_settlement_worker)
        worker.start()
        worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertEqual(observed_scopes, ["scope-a"])
        self.assertFalse(state.training_settlement_worker_active)

    def test_training_settlement_worker_refreshes_roster_once_when_pending(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-a",
            "account_scope_id": "scope-a",
        }
        state.training_settlement_requested = (
            "save-a", "scope-a", "2026-07-24",
        )
        state.training_settlement_worker_active = True
        state.training_settlement_roster_retry_key = None
        state.training_sync_status = None
        state.attribute_growth_hook = MagicMock()
        state._sync_training_focuses = MagicMock(return_value={
            "pending_player_ids": [10], "pending_staff_ids": [],
        })
        state.refresh_club_async = MagicMock(return_value=True)

        state._training_settlement_worker()

        state.refresh_club_async.assert_called_once_with()
        self.assertEqual(
            state.training_settlement_roster_retry_key,
            ("save-a", "scope-a", "2026-07-24"),
        )

    def test_manual_training_settlement_returns_updated_training_ground(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-a", "account_scope_id": "scope-a",
            "game_date": "2026-07-24",
        }
        state.last_operation_performance = None
        state._bind_current_save = MagicMock(return_value="scope-a")
        state._sync_training_focuses = MagicMock(return_value={
            "pending_player_ids": [],
            "completed_attribute_focus_ids": ["focus-1"],
        })
        with patch(
            "fm_odds_web.public_training_ground",
            return_value={"focuses": []},
        ):
            result = state.training_settle({})

        state._sync_training_focuses.assert_called_once_with("2026-07-24")
        self.assertEqual(
            result["settlement"]["completed_attribute_focus_ids"],
            ["focus-1"],
        )
        self.assertEqual(
            result["training_ground"]["growth_hook"], result["settlement"],
        )
        self.assertEqual(active_save_id(), "scope-a")

    def test_club_change_releases_users_without_clearing_training_ground(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-a", "game_date": "2026-07-24",
            "selected_manager_id": 123, "manager": {"id": 123},
            "managed_team": {"id": 10, "team_type": "club"},
            "managed_teams": [{"id": 10, "team_type": "club"}],
            "manager_options": [], "account_scope_id": "scope-a",
            "account_scope_manager_id": 123,
        }
        state.attribute_growth_hook = MagicMock()
        state.training_sync_key = ("old",)
        state.training_sync_status = {"pending_player_ids": [10]}
        state.refreshing = True
        state.refresh_mode = "manager"
        state.refresh_reason = "manager_team_context_change"
        state._sync_youth_generation_plans = MagicMock()
        state._sync_owned_club_board_hook = MagicMock()
        refreshed = {
            "game_date": "2026-07-24",
            "selected_manager_id": 123, "manager": {"id": 123},
            "managed_team": {"id": 20, "team_type": "club"},
            "managed_teams": [{"id": 20, "team_type": "club"}],
            "manager_options": [], "manager_selection_required": False,
        }

        with (
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.release_training_users") as release,
        ):
            state._manager_context_worker(
                123, 123, "save-a", refreshed_context=refreshed,
                account_only=True,
                release_training_users_on_club_change=True,
            )

        release.assert_called_once_with(
            "2026-07-24", reason="manager_club_change",
        )
        state.attribute_growth_hook.sync.assert_called_once_with([])
        self.assertIsNone(state.training_sync_key)
        self.assertIsNone(state.training_sync_status)
        self.assertEqual(state.output["managed_team"]["id"], 20)

    def test_context_worker_rechecks_club_change_before_releasing_users(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.lock = threading.RLock()
        state.memory_lock = threading.Lock()
        state.output = {
            "save_instance_id": "save-a", "game_date": "2026-07-24",
            "selected_manager_id": 123, "manager": {"id": 123},
            "managed_team": {"id": 10, "team_type": "club"},
            "managed_teams": [
                {"id": 10, "team_type": "club"},
                {"id": 86, "team_type": "national"},
            ],
            "manager_options": [], "account_scope_id": "scope-a",
            "account_scope_manager_id": 123,
        }
        state.attribute_growth_hook = MagicMock()
        state.training_sync_key = ("keep",)
        state.training_sync_status = {"pending_player_ids": [10]}
        state.refreshing = True
        state.refresh_mode = "manager"
        state.refresh_reason = "manager_team_context_change"
        state._sync_youth_generation_plans = MagicMock()
        state._sync_owned_club_board_hook = MagicMock()
        refreshed = {
            "game_date": "2026-07-24",
            "selected_manager_id": 123, "manager": {"id": 123},
            "managed_team": {"id": 10, "team_type": "club"},
            "managed_teams": [
                {"id": 10, "team_type": "club"},
                {"id": 99, "team_type": "national"},
            ],
            "manager_options": [], "manager_selection_required": False,
        }

        with (
            patch("fm_odds_web.remember_active_save_id"),
            patch("fm_odds_web.release_training_users") as release,
        ):
            state._manager_context_worker(
                123, 123, "save-a", refreshed_context=refreshed,
                account_only=True,
                release_training_users_on_club_change=True,
            )

        release.assert_not_called()
        state.attribute_growth_hook.sync.assert_not_called()
        self.assertEqual(state.training_sync_key, ("keep",))
        self.assertEqual(state.training_sync_status, {"pending_player_ids": [10]})

    def test_due_training_event_scans_live_roster_and_bypasses_cached_result(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {
            "save_instance_id": "save-a",
            "managed_teams": [{"id": 86, "address": "0xteam", "team_type": "club"}],
        }
        state.club_profile = None
        state.club_profiles = {
            86: {
                "team": {"id": 86, "address": "0xteam", "team_type": "club"},
                "players": [{
                    "id": 10, "address": "0xstale",
                    "attributes": {"身体": {"速度": 10}},
                }],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focus = {
            "id": "focus-1", "status": "active", "focus_type": "attribute",
            "player_id": 10, "player_name": "A", "player_team_id": 86,
            "player_address": "0xstale", "attribute_key": "身体:速度",
            "attribute_id": 0x35, "initial_attribute": 10,
            "progress_points": 14, "required_points": 14,
            "duration_mode": "until_increase", "expires_on": None,
        }
        state.training_sync_key = (
            "save-a", "2026-07-24",
            (("focus-1", "attribute", 10, 14, 14, "", "0xstale", 10),),
        )
        state.training_sync_status = {
            "installed": False, "pending_player_ids": [10],
        }
        development = {
            "_original_raw": b"original", "_original_ca": 100, "_original_pa": 120,
            "attributes": {"身体": {"速度": 11}},
            "attribute_after": 11, "ca": 101, "pa": 120,
        }
        with (
            patch("fm_odds_web.active_training_focuses", return_value=[focus]),
            patch("fm_odds_web.advance_training_focuses", return_value=[focus]),
            patch(
                "fm_odds_web.read_team_roster",
                return_value=[{"id": 10, "name": "A", "address": "0xlive"}],
            ) as scan,
            patch(
                "fm_odds_web.read_player_training_attributes",
                return_value={"focus-1": 10},
            ) as read,
            patch("fm_odds_web.rebind_training_focus_player") as rebind,
            patch("fm_odds_web.develop_player", return_value=development) as develop,
            patch(
                "fm_odds_web.complete_attribute_training_focus",
                return_value={"status": "completed"},
            ),
        ):
            result = state._sync_training_focuses("2026-07-24")

        scan.assert_called_once_with("0xteam")
        self.assertEqual(read.call_args.args[0][0]["player_address"], "0xlive")
        rebind.assert_called_once_with("focus-1", "0xlive")
        self.assertEqual(develop.call_args.args[1], "0xlive")
        self.assertEqual(result["pending_player_ids"], [])
        self.assertEqual(result["completed_attribute_focus_ids"], ["focus-1"])

    def test_two_person_facility_settles_player_missing_from_cached_profile(self) -> None:
        state = LocalOddsState.__new__(LocalOddsState)
        state.output = {
            "save_instance_id": "save-a",
            "managed_teams": [{"id": 86, "address": "0xteam", "team_type": "club"}],
        }
        state.club_profile = None
        state.club_profiles = {
            86: {
                "team": {"id": 86, "address": "0xteam", "team_type": "club"},
                "players": [{
                    "id": 20, "address": "0xstale-2",
                    "attributes": {"身体": {"速度": 10}},
                }],
            },
        }
        state.attribute_growth_hook = MagicMock()
        state.attribute_growth_hook.sync.return_value = {"installed": False}
        focuses = [
            {
                "id": "focus-1", "status": "active", "focus_type": "attribute",
                "facility_id": "two-person-facility", "player_id": 10,
                "player_name": "First", "player_team_id": 86,
                "player_address": "0xstale-1", "attribute_key": "身体:速度",
                "attribute_id": 0x35, "initial_attribute": 10,
                "progress_points": 14, "required_points": 14,
                "duration_mode": "until_increase", "expires_on": None,
            },
            {
                "id": "focus-2", "status": "active", "focus_type": "attribute",
                "facility_id": "two-person-facility", "player_id": 20,
                "player_name": "Second", "player_team_id": 86,
                "player_address": "0xstale-2", "attribute_key": "身体:速度",
                "attribute_id": 0x35, "initial_attribute": 10,
                "progress_points": 14, "required_points": 14,
                "duration_mode": "until_increase", "expires_on": None,
            },
        ]

        def development(player_id, *_args, **_kwargs):
            return {
                "_original_raw": b"original",
                "_original_ca": 100,
                "_original_pa": 120,
                "attributes": {"身体": {"速度": 11}},
                "attribute_after": 11,
                "ca": 101,
                "pa": 120,
                "player_id": player_id,
            }

        with (
            patch("fm_odds_web.active_training_focuses", side_effect=[focuses, focuses]),
            patch("fm_odds_web.advance_training_focuses", return_value=focuses),
            patch(
                "fm_odds_web.read_team_roster",
                return_value=[
                    {"id": 10, "name": "First", "address": "0xlive-1"},
                    {"id": 20, "name": "Second", "address": "0xlive-2"},
                ],
            ) as scan,
            patch(
                "fm_odds_web.read_player_training_attributes",
                return_value={"focus-1": 10, "focus-2": 10},
            ),
            patch("fm_odds_web.rebind_training_focus_player"),
            patch("fm_odds_web.develop_player", side_effect=development) as develop,
            patch(
                "fm_odds_web.complete_attribute_training_focus",
                return_value={"status": "completed"},
            ),
        ):
            result = state._sync_training_focuses("2026-07-24")

        scan.assert_called_once_with("0xteam")
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in develop.call_args_list],
            [(10, "0xlive-1"), (20, "0xlive-2")],
        )
        self.assertEqual(
            result["completed_attribute_focus_ids"],
            ["focus-1", "focus-2"],
        )

    def test_until_next_point_natural_growth_completes(self) -> None:
        until = purchase_facility("reaction_wall")
        place_facility(until["id"], "indoor", 70, 55)
        until_focus, _session = record_training_focus(
            until["id"], 11, "B", "2026-07-20", "精神:预判", 0x20,
            "until_increase", 12,
        )
        self.assertIsNone(until_focus["expires_on"])
        self.assertEqual(until_focus["target_attribute"], 13)
        self.assertEqual(complete_training_focuses("2026-07-25", {until_focus["id"]: 12}), [])
        completed = complete_training_focuses("2026-07-26", {until_focus["id"]: 13})
        self.assertEqual(completed[0]["status"], "completed")
        self.assertFalse(any(
            row["id"] == until_focus["id"]
            for row in active_training_focuses("2026-07-26")
        ))

    def test_attribute_focus_rebases_after_external_drop(self) -> None:
        facility = purchase_facility("reaction_wall")
        place_facility(facility["id"], "indoor", 70, 55)
        focus, _session = record_training_focus(
            facility["id"], 11, "B", "2026-07-20", "精神:预判", 0x20,
            "until_increase", 12,
        )
        rebased = rebase_attribute_training_focus(focus["id"], "2026-07-25", 11)
        self.assertEqual(rebased["initial_attribute"], 11)
        self.assertEqual(rebased["target_attribute"], 12)
        self.assertEqual(rebased["rebased_from_attribute"], 12)

    def test_until_20_recalculates_each_stage_and_completes_at_20(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor_2f", 55, 55)
        focus, _session = record_training_focus(
            treadmill["id"], 12, "C", "2026-07-20", "身体:速度", 0x35,
            "until_20", 17,
        )
        self.assertEqual(focus["target_attribute"], 20)
        self.assertEqual(focus["required_points"], 300)
        self.assertEqual(estimated_training_days_to_20(17), 580)

        stage_18 = complete_attribute_training_focus(
            focus["id"], "2026-09-28", {"attribute_after": 18},
        )
        self.assertEqual(stage_18["status"], "active")
        self.assertEqual(stage_18["initial_attribute"], 18)
        self.assertEqual((stage_18["required_points"], stage_18["progress_points"]), (540, 0))
        public = public_training_ground("2026-09-28")["focuses"][0]
        self.assertEqual(public["estimated_total_days"], 480)

        stage_19 = complete_attribute_training_focus(
            focus["id"], "2027-01-26", {"attribute_after": 19},
        )
        self.assertEqual(stage_19["status"], "active")
        self.assertEqual(stage_19["required_points"], 900)

        completed = complete_attribute_training_focus(
            focus["id"], "2027-08-14", {"attribute_after": 20},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(active_training_focuses("2027-08-14"), [])

    def test_fixed_even_target_recalculates_stages_and_stops_at_target(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 55, 55)
        focus, _session = record_training_focus(
            treadmill["id"], 12, "C", "2026-07-20", "身体:速度", 0x35,
            "until_18", 15,
        )
        self.assertEqual(focus["target_attribute"], 18)
        self.assertEqual(training_target_attribute("until_14", 12), 14)
        self.assertEqual(training_target_attribute("until_16", 12), 16)
        self.assertEqual(training_target_attribute("until_18", 12), 18)
        self.assertEqual(estimated_training_days_to_target(15, 18), 163)

        stage_16 = complete_attribute_training_focus(
            focus["id"], "2026-08-10", {"attribute_after": 16},
        )
        self.assertEqual((stage_16["status"], stage_16["initial_attribute"]), ("active", 16))
        stage_17 = complete_attribute_training_focus(
            focus["id"], "2026-09-21", {"attribute_after": 17},
        )
        self.assertEqual((stage_17["status"], stage_17["initial_attribute"]), ("active", 17))
        completed = complete_attribute_training_focus(
            focus["id"], "2026-12-30", {"attribute_after": 18},
        )
        self.assertEqual(completed["status"], "completed")

    def test_fixed_target_must_be_above_current_attribute(self) -> None:
        with self.assertRaisesRegex(ValueError, "必须高于当前属性"):
            training_target_attribute("until_16", 16)

    def test_habit_training_waits_one_week_before_completion(self) -> None:
        machine = purchase_facility("habit_lab")
        place_facility(machine["id"], "indoor", 35, 50)
        focus, session = record_habit_training_focus(
            machine["id"], 10, "A", "2026-07-19", 13, "喜欢反越位", "learn",
        )

        self.assertEqual(focus["duration_days"], 7)
        self.assertEqual(focus["expires_on"], "2026-07-26")
        self.assertEqual(focus["focus_type"], "preferred_move")
        self.assertEqual(active_training_focuses("2026-07-25")[0]["id"], focus["id"])
        self.assertEqual(active_training_focuses("2026-07-26")[0]["id"], focus["id"])
        with self.assertRaisesRegex(ValueError, "使用人数已满"):
            assert_training_available(machine["id"], 11, "2026-07-26")

        completed = complete_habit_training_focus(
            focus["id"], "2026-07-26", {"bit": 13, "operation": "learn"},
        )

        self.assertEqual(completed["status"], "completed")
        self.assertEqual(active_training_focuses("2026-07-26"), [])
        self.assertEqual(public_training_ground("2026-07-26")["sessions"][0]["id"], session["id"])

    def test_public_focus_contains_usage_days(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)
        focus, _session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )
        advance_training_focuses("2026-07-24")
        public = public_training_ground("2026-07-24")
        row = next(item for item in public["focuses"] if item["id"] == focus["id"])
        self.assertEqual((row["days_used"], row["duration_days"]), (5, None))
        self.assertEqual(row["estimated_days"], 0)

    def test_cancel_training_focus_releases_user_and_keeps_history(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)
        focus, session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )
        advance_training_focuses("2026-07-21")

        cancelled = cancel_training_focus(focus["id"], "2026-07-24")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["cancelled_on"], "2026-07-24")
        self.assertEqual(active_training_focuses("2026-07-24"), [])
        public = public_training_ground("2026-07-24")
        self.assertEqual(public["focuses"][0]["status"], "cancelled")
        self.assertEqual(public["sessions"][0]["id"], session["id"])
        with self.assertRaisesRegex(ValueError, "已不在使用"):
            cancel_training_focus(focus["id"], "2026-07-24")

        resumed, _session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-24", "身体:速度", 0x35,
            "until_increase", 10,
        )
        self.assertEqual(resumed["progress_points"], 6)
        self.assertEqual(public_training_ground("2026-07-24")["focuses"][0]["estimated_days"], 3)

    def test_training_completion_uses_position_weighted_ca_cost(self) -> None:
        raw = bytearray([50] * 54)
        updated, ca, pa, audit = plan_player_development(
            bytes(raw), 120, 150, "身体:速度", 10, "training", 1, 4, 3,
        )
        self.assertNotEqual(updated, bytes(raw))
        self.assertEqual((ca, pa), (123, 150))
        self.assertEqual(audit["ca_cost"], 3)
        self.assertEqual((audit["attribute_before"], audit["attribute_after"]), (10, 11))
        with self.assertRaisesRegex(ValueError, "CA 将超过 PA"):
            plan_player_development(
                bytes(raw), 148, 150, "身体:速度", 10, "training", 1, 4, 3,
            )
        _updated, ca, pa, audit = plan_player_development(
            bytes(raw), 148, 150, "身体:速度", 10, "training", 1, 4, 3, True,
        )
        self.assertEqual((ca, pa), (151, 150))
        self.assertTrue(audit["ca_exceeds_pa"])
        self.assertTrue(audit["ca_over_pa_confirmed"])
        with self.assertRaisesRegex(ValueError, "CA 将超过200"):
            plan_player_development(
                bytes(raw), 199, 200, "身体:速度", 10, "training", 1, 4, 3, True,
            )

    def test_training_focus_persists_ca_over_pa_confirmation(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)
        focus, session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_20", 17, allow_ca_over_pa=True,
        )
        self.assertTrue(focus["allow_ca_over_pa"])
        self.assertTrue(session["allow_ca_over_pa"])
        self.assertTrue(public_training_ground()["focuses"][0]["allow_ca_over_pa"])

    def test_confirmed_over_pa_training_writes_and_reads_back_all_fields(self) -> None:
        address = 0x100000
        raw = bytearray([50] * 54)
        updated = bytearray(raw)
        updated[0x35 - 0x0F] = 55
        layout = SimpleNamespace(
            key="fm24-test", attribute_display_bias=4,
            player_attributes_offset=0x200, player_positions_offset=0x300,
            player_ca_offset=0x400, player_pa_offset=0x402,
            module=MagicMock(return_value=SimpleNamespace(base_address=0x500000)),
        )
        process = object()
        process_context = MagicMock()
        process_context.__enter__.return_value = process
        reader = MagicMock()
        reader.bytes.side_effect = [bytes(raw), bytes(15), bytes(updated)]
        reader.u16.side_effect = [148, 150, 151, 150]
        training_plan = {
            "model": "fm24-verified", "verified": True, "ca_cost": 3,
            "recommended_ca_before": 148.0, "recommended_ca_after": 151.0,
        }
        with (
            patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.club_reader.open_process", return_value=process_context),
            patch("tools.club_reader.Reader", return_value=reader),
            patch("tools.club_reader._validated_player_person"),
            patch(
                "tools.club_reader._visible_player_attributes",
                side_effect=[{"身体": {"速度": 10}}, {"身体": {"速度": 11}}],
            ),
            patch("tools.club_reader._training_ca_change", return_value=training_plan),
            patch("tools.club_reader._player_training_ca_snapshot", return_value={}),
            patch("tools.club_reader.write_process_memory") as write,
        ):
            result = develop_player(
                10, hex(address), "身体:速度", 10, "training", 1,
                allow_ca_over_pa=True,
            )

        self.assertEqual((result["attribute_after"], result["ca"], result["pa"]), (11, 151, 150))
        self.assertTrue(result["ca_exceeds_pa"])
        self.assertEqual(write.call_count, 3)
        write.assert_any_call(process, address + 0x200, bytes(updated))
        write.assert_any_call(process, address + 0x400, struct.pack("<H", 151))
        write.assert_any_call(process, address + 0x402, struct.pack("<H", 150))

    def test_hidden_training_writes_only_the_target_hidden_byte_without_ca(self) -> None:
        address = 0x100000
        person = 0x110000
        layout = SimpleNamespace(
            key="fm24-test", attribute_display_bias=4,
            person_hidden_attributes_offset=0x70,
            player_attributes_offset=0x200,
            module=MagicMock(return_value=SimpleNamespace(base_address=0x500000)),
        )
        process = object()
        process_context = MagicMock()
        process_context.__enter__.return_value = process

        for attribute_key, original, expected, target, expected_address in (
            ("隐藏:争议性", b"\x0f", 15, 14, person + 0x70 + 7),
            ("隐藏:肮脏动作", b"\x47", 15, 14, address + 0x200 + 0x38 - 0x0F),
        ):
            with self.subTest(attribute_key=attribute_key):
                reader = MagicMock()
                reader.layout = layout
                reader.bytes.side_effect = [original, bytes(54)]
                staged = MagicMock()
                with (
                    patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
                    patch("tools.club_reader.open_process", return_value=process_context),
                    patch("tools.club_reader.Reader", return_value=reader),
                    patch("tools.club_reader._validated_player_person", return_value=person),
                    patch("tools.club_reader._apply_verified_memory_changes", staged),
                    patch("tools.club_reader._player_hidden_attributes", return_value={"争论": target}),
                ):
                    result = develop_player_hidden_attribute(
                        10, hex(address), attribute_key, expected, -1,
                    )

                changes = staged.call_args.args[2]
                self.assertEqual(changes[0]["address"], expected_address)
                self.assertEqual(changes[0]["original"], original)
                self.assertEqual(changes[0]["updated"], b"\x0e" if attribute_key.endswith("争议性") else b"\x42")
                self.assertEqual(result["attribute_after"], target)
                self.assertFalse(result["ca_affected"])
                reader.u16.assert_not_called()

    def test_height_training_writes_only_the_verified_height_byte(self) -> None:
        address = 0x100000
        layout = SimpleNamespace(
            key="fm24-test", attribute_display_bias=4,
            player_height_offset=0x22E, player_attributes_offset=0x200,
            module=MagicMock(return_value=SimpleNamespace(base_address=0x500000)),
        )
        process_context = MagicMock()
        reader = MagicMock()
        reader.layout = layout
        reader.bytes.side_effect = [b"\xb4", bytes(54)]
        staged = MagicMock()
        with (
            patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.club_reader.open_process", return_value=process_context),
            patch("tools.club_reader.Reader", return_value=reader),
            patch("tools.club_reader._validated_player_person", return_value=0x110000),
            patch("tools.club_reader._apply_verified_memory_changes", staged),
            patch("tools.club_reader._player_hidden_attributes", return_value={}),
        ):
            result = develop_player_hidden_attribute(
                10, hex(address), "physical:height", 180, 1,
            )
        change = staged.call_args.args[2][0]
        self.assertEqual(change["address"], address + 0x22E)
        self.assertEqual((change["original"], change["updated"]), (b"\xb4", b"\xb5"))
        self.assertEqual(result["attribute_after"], 181)

    def test_sports_science_condition_is_staged_as_one_verified_transaction(self) -> None:
        address = 0x100000
        layout = SimpleNamespace(
            player_fatigue_offset=0x10, player_sharpness_offset=0x12,
            player_fitness_offset=0x14,
            module=MagicMock(return_value=SimpleNamespace(base_address=0x500000)),
        )
        process_context = MagicMock()
        reader = MagicMock()
        reader.u16.side_effect = [5000, 4000, 6000]
        staged = MagicMock()
        with (
            patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.club_reader.open_process", return_value=process_context),
            patch("tools.club_reader.Reader", return_value=reader),
            patch("tools.club_reader._validated_player_person"),
            patch("tools.club_reader._apply_verified_memory_changes", staged),
        ):
            result = adjust_player_training_condition(10, hex(address), 1.2)
        self.assertEqual(result["after"], {
            "fatigue": 4400, "sharpness": 4600, "fitness": 6360,
        })
        changes = staged.call_args.args[2]
        self.assertEqual([row["address"] for row in changes], [
            address + 0x10, address + 0x12, address + 0x14,
        ])

    def test_sports_science_condition_clamps_all_fields_to_raw_domain(self) -> None:
        address = 0x100000
        layout = SimpleNamespace(
            player_fatigue_offset=0x10, player_sharpness_offset=0x12,
            player_fitness_offset=0x14,
            module=MagicMock(return_value=SimpleNamespace(base_address=0x500000)),
        )
        process_context = MagicMock()
        reader = MagicMock()
        reader.u16.side_effect = [12000, 14000, 15000]
        with (
            patch("tools.club_reader.select_process_layout", return_value=(1, "fm.exe", layout)),
            patch("tools.club_reader.open_process", return_value=process_context),
            patch("tools.club_reader.Reader", return_value=reader),
            patch("tools.club_reader._validated_player_person"),
            patch("tools.club_reader._apply_verified_memory_changes"),
        ):
            result = adjust_player_training_condition(10, hex(address), 1.2)
        self.assertEqual(result["after"], {
            "fatigue": 10000, "sharpness": 10000, "fitness": 10000,
        })

    def test_training_to_20_caps_high_raw_value_instead_of_overflowing(self) -> None:
        raw = bytearray([50] * 54)
        raw[0x35 - 0x0F] = 88

        updated, ca, pa, audit = plan_player_development(
            bytes(raw), 120, 150, "身体:速度", 17, "training", 3, 0, 3,
        )

        self.assertEqual(updated[0x35 - 0x0F], 100)
        self.assertEqual((audit["attribute_before"], audit["attribute_after"]), (17, 20))
        self.assertEqual((ca, pa), (123, 150))

    def test_completed_focus_does_not_persist_rollback_bytes(self) -> None:
        treadmill = purchase_facility("treadmill")
        place_facility(treadmill["id"], "indoor", 35, 50)
        focus, _session = record_training_focus(
            treadmill["id"], 10, "A", "2026-07-19", "身体:速度", 0x35,
            "until_increase", 10,
        )
        completed = complete_attribute_training_focus(
            focus["id"], "2026-07-24",
            {"attribute_after": 11, "ca": 121, "_original_raw": b"private"},
        )
        self.assertEqual(completed["memory_result"], {"attribute_after": 11, "ca": 121})

    def test_growth_hook_code_contains_targets_for_both_versions(self) -> None:
        fm24_original = bytes.fromhex("48 8B 4D F0 48 8B 41 18")
        fm26_original = bytes.fromhex("0F 57 C0 0F 2E F0")
        target = 0x123456789ABC
        fm24 = _build_fm24_code(
            0x10000000, fm24_original, 0x10001000, [(target, 0x35)],
        )
        fm26 = _build_fm26_code(
            0x20000000, fm26_original, 0x20001000, [(target, 0x35)],
        )
        self.assertEqual(len(fm24_original), FM24_ORIGINAL_SIZE)
        self.assertEqual(len(fm26_original), FM26_ORIGINAL_SIZE)
        self.assertIn(target.to_bytes(8, "little"), fm24)
        self.assertIn(target.to_bytes(8, "little"), fm26)

    def test_growth_hook_uses_bounded_dynamic_target_table(self) -> None:
        targets = [(0x123456780000 + index * 0x1000, 0x35) for index in range(64)]
        fm24 = _build_fm24_code(
            0x10000000, bytes.fromhex("48 8B 4D F0 48 8B 41 18"),
            0x10001000, targets,
        )
        fm26 = _build_fm26_code(
            0x20000000, bytes.fromhex("0F 57 C0 0F 2E F0"),
            0x20001000, targets,
        )
        expected_size = TABLE_OFFSET + 8 + len(targets) * TABLE_ENTRY_SIZE
        for code in (fm24, fm26):
            self.assertEqual(len(code), expected_size)
            self.assertEqual(int.from_bytes(code[TABLE_OFFSET:TABLE_OFFSET + 4], "little"), 64)
            self.assertIn(targets[-1][0].to_bytes(8, "little"), code)
            self.assertIn(bytes.fromhex("8D 0C 49"), code[:TABLE_OFFSET])

    def test_training_descriptions_match_equipment_speed(self) -> None:
        for item in FACILITIES.values():
            if item.get("attributes") and item.get("available", True):
                multiplier = int(item.get("training_multiplier") or 3)
                action = (
                    "改变"
                    if item.get("attribute_kind") == "hidden"
                    and item.get("scene") == "comprehensive_7f"
                    else ("降低" if int(item.get("direction") or 1) < 0 else "提升")
                )
                numeral = {2: "二", 3: "三", 4: "四"}.get(
                    multiplier, str(multiplier),
                )
                expected = f"{numeral}倍速{action}"
                self.assertIn(expected, item["description"])

    def test_right_dock_has_capacity_people_and_inline_submission(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        page = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="training-capacity-badge"', script)
        self.assertIn('class="training-available-title"', script)
        self.assertIn('class="training-member-list"', script)
        self.assertIn('class="training-available-attributes"', script)
        self.assertIn('class="training-cancel-use"', script)
        self.assertIn('data-lucide="x"', script)
        self.assertIn('request("/api/training/cancel"', script)
        self.assertIn('"三倍速"', script)
        self.assertIn('"训练点已满，正在结算"', script)
        self.assertNotIn("训练点已满，等待结算", script)
        self.assertIn('data-training-repeat=', script)
        self.assertIn('<span>再来一次</span>', script)
        self.assertIn('app.trainingRepeatTarget=attributeKey;', script)
        self.assertIn('function reloadTrainingSettlementPlayers()', script)
        self.assertIn('request("/api/training/settle"', script)
        self.assertIn('await ensureTrainingRosterLoaded(true, forceRoster);', script)
        self.assertIn('payload.team_id = Number(selectedPlayer?.training_team_id || 0);', script)
        self.assertIn('const needsFormulaSnapshots = !requiresStaff && product.attribute_kind !== "hidden";', script)
        self.assertIn('? ensurePlayerDevelopmentRosterLoaded()', script)
        self.assertIn('durationSelect.innerHTML = `<option value="${escapeHtml(active.duration_mode || "until_increase")}">${durationLabel}</option>`;', script)
        self.assertIn('player.training_team_type === "national"', script)
        self.assertIn('const settlingFocusIds = (data.focuses || [])', script)
        self.assertIn('Date.now() - app.trainingSettlementReloadAt > 30000', script)
        self.assertIn('settleTrainingProgress({forceRoster:true,notify:true})', script)
        self.assertIn('id="training-settle-button"', page)
        self.assertIn('.training-settle-button', styles)
        self.assertIn('habit ? `<option value="week">一周</option>`', script)
        self.assertIn('const TRAINING_FIXED_TARGETS = [14,16,18,20];', script)
        self.assertIn('mode:`until_${target}`,target,label:uiText("training.until_value", {target})', script)
        self.assertIn('label:direction < 0 ? "降低下一点" : "直到下一点"', script)
        self.assertNotIn('不影响 CA', script)
        self.assertIn('return `<option value="${mode}">${label}（${detail}）</option>`;', script)
        self.assertIn('id="training-inline-direction"', script)
        self.assertIn('<option value="1"', script)
        self.assertIn('<option value="-1"', script)
        self.assertIn('payload.direction = Number(form.querySelector("#training-inline-direction").value)', script)
        for attribute_key in (
            "精神:侵略性", "精神:意志力", "精神:才华", "身体:体质",
            "门将:出击(倾向)", "门将:拳击球(倾向)",
        ):
            self.assertIn(f'  "{attribute_key}",', script)
        self.assertIn('function trainingAttributeAffectsCa(attributeKey, product = {}, caCosts = {})', script)
        self.assertIn('!TRAINING_ZERO_CA_ATTRIBUTE_KEYS.has(String(attributeKey || ""))', script)
        self.assertIn('const caAffected = trainingAttributeAffectsCa(attributeKey, product, caCosts);', script)
        self.assertIn('const detail = !caAffected', script)
        self.assertIn('"training.estimated_days"', script)
        self.assertIn('"training.estimated_over_pa"', script)
        self.assertIn('"training.estimated_ca_only"', script)
        self.assertIn('"training.confirm_continue_ca"', script)
        self.assertIn('if (trainingAttributeAffectsCa(target, product, caCosts))', script)
        self.assertIn('payload.confirm_ca_over_pa = true;', script)
        self.assertNotIn('nextOption.disabled = caSpace < nextCaRequired;', script)
        self.assertNotIn('<option value="two_weeks">', script)
        self.assertNotIn('<option value="month"', script)
        self.assertIn('习惯训练已开始，一周后生效', script)
        self.assertIn('targetSelect.innerHTML = options || `<option value="">没有可训练属性</option>`;', script)
        self.assertNotIn('} · 专项训练中</option>`;', script)
        self.assertIn('.training-cancel-use { width:30px; height:30px;', styles)
        self.assertIn('background:#c9362e; color:#fff;', styles)
        self.assertIn('.training-repeat-use { min-height:32px;', styles)
        self.assertIn('function trainingAttributeWords(product)', script)
        self.assertIn('product.scene === "outdoor" ? "outdoor-equipment" : "indoor-equipment"', script)
        self.assertIn('.training-facility.indoor-equipment img { inset:12.5%; width:75%; height:75%; }', styles)
        self.assertIn('"outdoor-equipment"', script)
        self.assertIn('.training-facility.outdoor-equipment img { inset:10%; width:80%; height:80%; }', styles)
        self.assertIn('submitInlineTraining(form)', script)
        self.assertIn('data-training-player-toggle', script)
        self.assertIn('data-training-player-option=', script)
        self.assertIn('class="training-player-picker-card"', script)
        self.assertIn('data-training-player-picker-layer', script)
        self.assertIn('id="training-player-search"', script)
        self.assertIn('data-training-player-position=', script)
        self.assertIn('app.trainingPlayerSearch=event.target.value || "";', script)
        self.assertIn('const attributeFormFields = `${targetField}${directionField}${membersSection}${playerPicker}`;', script)
        self.assertNotIn("roomAdvisor", script)
        self.assertIn('class="training-target-attribute"', script)
        self.assertIn('app.trainingTargetKey=event.target.value||null;', script)
        self.assertIn('data-position-training-set-position', script)
        self.assertIn('function positionTrainingSetSlotPosition(position)', script)
        self.assertIn('先选位置，再拖到球场', script)
        self.assertIn('key:"defence",label:', script)
        self.assertIn('"training.position_group.defenders"', script)
        self.assertIn('positions:["GK","SW","DL","DC","DR","WBL","WBR"]', script)
        self.assertIn('key:"midfield",label:', script)
        self.assertIn('"training.position_group.midfielders"', script)
        self.assertIn('positions:["DM","ML","MC","MR"]', script)
        self.assertIn('key:"attack",label:', script)
        self.assertIn('"training.position_group.forwards"', script)
        self.assertIn('positions:["AML","AMC","AMR","ST"]', script)
        self.assertIn('class="position-training-position-groups"', script)
        self.assertIn('data-position-training-tray-slot', script)
        self.assertIn('application/x-fmodd-position-slot', script)
        self.assertIn('data-position-training-edit-start', script)
        self.assertIn('data-position-training-edit-finish', script)
        self.assertIn('data-position-training-unplace', script)
        self.assertIn('function positionTrainingUnplaceSlot(slotId)', script)
        self.assertIn('开始编辑', script)
        self.assertIn('完成编辑', script)
        self.assertIn('customWorkflowVersion:2', script)
        self.assertIn('customFormations:state.customFormations', script)
        self.assertIn('data-position-training-custom-new', script)
        self.assertIn('data-position-training-custom-select', script)
        self.assertIn('data-position-training-custom-name', script)
        self.assertIn('data-position-training-save-name', script)
        self.assertIn('data-position-training-delete', script)
        self.assertIn('function positionTrainingDeleteCustom()', script)
        self.assertIn('自定义阵型已删除', script)
        self.assertIn('.position-training-custom-name button.danger', styles)
        self.assertIn('state.editing = true;', script)
        self.assertIn('.position-training-custom-library', styles)
        self.assertNotIn('平均熟练度 ${positionTrainingStars', script)
        self.assertNotIn('已安排 <b>${assignedRatings.length}</b>/11', script)
        self.assertIn('.position-training-workspace', styles)
        self.assertIn('position-training-workspace ${state.preset==="custom"?"custom":"preset"}', script)
        self.assertIn('.position-training-workspace.preset{grid-template-columns:minmax(0,1fr)}', styles)
        self.assertIn('.position-training-workspace.custom .position-training-pitch{width:min(94%,820px);margin:0 auto}', styles)
        self.assertIn('.position-training-slot>button.position-training-unplace{', styles)
        self.assertIn('clip-path:none', styles)
        self.assertIn('<button data-training-scene="position_training">位置训练</button>', page)
        self.assertNotIn('<button data-training-scene="position_training"><i ', page)
        self.assertIn('.position-rating-empty', styles)
        self.assertIn('.position-training-position-palette', styles)
        self.assertIn('.position-training-position-group{', styles)
        self.assertIn('min-width:32px;min-height:26px', styles)
        self.assertIn('Array.from({length:5}', script)
        self.assertIn('(rating-index*4)/4*100', script)
        self.assertIn('class="position-rating-star"', script)
        self.assertIn('background:linear-gradient(90deg,#f2d56f var(--fill),#52635b var(--fill))', styles)
        self.assertIn('.position-training-slot .position-slot-role{margin:0 -8px 4px', styles)
        self.assertIn('overflow-wrap:anywhere', styles)
        self.assertIn('class="position-player-peaks"', script)
        self.assertIn('.position-player-peaks{', styles)
        self.assertIn('const best = positionTrainingTopRatings(player)[0] || null;', script)
        self.assertIn('ratings.push({position:selectedPosition,rating:current});', script)
        self.assertIn('data-position-training-auto-add', script)
        self.assertIn('function positionTrainingAutoAssign()', script)
        self.assertIn('until_20:"练至满级"', script)
        self.assertIn('durationMode:"until_20"', script)
        self.assertIn('function positionTrainingDaysToMax', script)
        self.assertIn('if (baseline >= 20) return 20;', script)
        self.assertIn('focus.no_change && current >= 20', script)
        self.assertIn(
            'trainingManagedPlayers().filter((player) => !activeTrainingFocus(player.id))',
            script,
        )
        self.assertNotIn('.filter((item) => item.rating < 20)', script)
        self.assertIn('满级约 ${positionTrainingDaysToMax', script)
        self.assertNotIn('until_14:"练至 14"', script)
        self.assertNotIn('until_16:"练至 16"', script)
        self.assertNotIn('until_18:"练至 18"', script)
        self.assertNotIn('目标 ${target}/20', script)
        self.assertNotIn(' · 训练中', script)
        self.assertIn('.position-training-roster>footer button{width:max-content;max-width:100%;min-width:0', styles)
        self.assertIn('.position-training-roster{grid-template-rows:auto auto auto minmax(120px,1fr) auto auto}', styles)
        self.assertIn('id="show-hidden-attributes"', page)
        self.assertIn('localStorage.setItem("fm-odds-show-hidden-attributes"', script)
        self.assertIn('app.showHiddenAttributes?` · CA ${Number(player.ca||0)}/PA ${Number(player.pa||0)}`:""', script)
        self.assertIn('const playerPositionFilter = String(app.trainingPlayerPositionFilter || "all");', script)
        self.assertIn('const idleCount = sortedPlayers.filter((player) => !activeTrainingFocus(player.id)).length;', script)
        self.assertIn('trainingPlayerPositionFilter: app.trainingPlayerPositionFilter,', script)
        self.assertIn('class="training-active-detail"', script)
        self.assertIn('class="training-location-tag"', script)
        self.assertIn('id="training-inline-player"', script)
        self.assertNotIn('<select id="training-inline-player"', script)
        self.assertIn('interactiveSurfaceBusy($("#training-dock-content"))', script)
        self.assertIn('.training-player-option {', styles)
        self.assertIn('.training-player-picker-layer {', styles)
        self.assertIn('.training-player-picker-filters button.active', styles)
        self.assertIn('payload.client_submission_id = app.trainingSubmissionRetry.id;', script)
        self.assertIn('timeoutMs:60000', script)
        self.assertIn('path === "/api/training/run"\n      ? 60000', script)
        self.assertIn('"正在安排..."', script)
        self.assertIn('.training-person.occupied svg { fill:currentColor;', styles)
        self.assertIn('.training-person.open svg { fill:none;', styles)

    def test_training_equipment_capacity_player_details_and_dock_hash(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        markup = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('renderCategory("available", "可供使用", available)', script)
        self.assertIn('renderCategory("unavailable", "不可使用", unavailable)', script)
        self.assertIn('partial: trainingIsUnlimited(product) ? used > 0 : capacity > 1 && used > 0 && used < capacity', script)
        self.assertIn('function trainingIsFull(product, used)', script)
        self.assertIn('unlimited ? "不限人数"', script)
        self.assertIn('!item.built_in && item.available !== false', script)
        self.assertNotIn('training.built_in_note', script)
        self.assertNotIn('training-built-in-note', script)
        self.assertIn('trainingEstimatedDays(player, attributeKey, mode, multiplier, direction)', script)
        self.assertIn('trainingMultiplier: item.training_multiplier', script)
        self.assertIn('visiblePlaced.map((facility)', script)
        self.assertIn(
            'const partialDifference = Number(right[1].some((row) => row.partial))',
            script,
        )
        self.assertIn('const totalBySku = new Map();', script)
        self.assertNotIn('facility._partialUse', script)
        self.assertIn('class="training-target-attribute"', script)
        self.assertIn('.training-player-option small.training-target-attribute', styles)
        self.assertNotIn('facilityAttrKeys.map((key)', script)
        self.assertIn('overflow-wrap:anywhere', styles)
        self.assertIn('grid-auto-rows:max-content; align-content:start;', styles)
        self.assertIn('height:max-content;', styles)
        self.assertNotIn(
            '.training-player-option small.training-idle-attrs { font-size:11px',
            styles,
        )
        self.assertIn(
            'const attributeRange = currentAttribute > 0 && targetAttribute > 0',
            script,
        )
        self.assertIn('progressPoints: focus.progress_points', script)
        self.assertIn('attributes: player.attributes', script)
        self.assertIn('function renderTraining({skipStage = false} = {})', script)
        self.assertIn('if (app._lastStageHash !== stageHash)', script)
        self.assertIn('if (app._lastDockHash !== dockHash)', script)
        self.assertIn('if (app._lastSelHash !== selHash)', script)
        self.assertIn('function trainingDataIndex()', script)
        self.assertIn('if (app.trainingIndexSource === data && app.trainingIndex)', script)
        self.assertIn('activeFocusesByFacility.get(facilityId) || []', script)
        self.assertIn('facilityOrdinalById.set(facility.id,index)', script)
        helper_block = script.split('function trainingDataIndex()', 1)[1].split(
            'function trainingIsUnlimited', 1,
        )[0]
        self.assertNotIn('.filter((item) => item.status === "active"', helper_block)
        self.assertIn('submissionRetry: app.trainingSubmissionRetry', script)
        self.assertIn('staffLoading: app.trainingStaffLoading', script)
        self.assertIn('growth_hook:response.training_ground.growth_hook ?? growthHook', script)
        self.assertIn('await ensureTrainingRosterLoaded(true);', script)
        self.assertIn('const abilityLine = app.showHiddenAttributes', script)
        self.assertNotIn('const abilityLine = app.showHiddenAttributes && habit', script)
        self.assertIn('const selectedTargetSummary = selectedPlayer && selectedTargetKey', script)
        self.assertIn('targetKey: app.trainingTargetKey,', script)
        self.assertIn('showHiddenAttributes: app.showHiddenAttributes,', script)
        self.assertIn('gameDate: String(app.gameClock?.date', script)
        self.assertIn('function syncTrainingStatus()', script)
        self.assertIn('root.setAttribute("aria-busy", "true")', script)
        self.assertIn('正在结算训练进度', script)
        self.assertIn('id="training-sync-status"', markup)
        self.assertIn('.training-toolbar button:focus-visible', styles)
        reduced_motion = styles.split('@media (prefers-reduced-motion: reduce)', 1)[1]
        self.assertIn('.training-sync-status > i { animation:none; }', reduced_motion)

    def test_training_mutation_keeps_state_supplements_for_every_facility(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        prefix = "function updateTrainingResponse(response) {"
        function_body = script.split(prefix, 1)[1].split(
            "\n}\nfunction firstTrainingPosition", 1,
        )[0]
        function_source = f"{prefix}{function_body}\n}}"
        harness = f"""
const app = {{
  trainingMutationRevision:0,
  state:{{training_ground:{{
    player_manager:{{id:77}},
    growth_hook:{{installed:false}},
    preferred_moves:[{{bit:1,name:"测试习惯"}}],
    preferred_moves_error:"kept",
  }}}},
}};
function trainingData() {{ return app.state.training_ground; }}
function applyEconomySnapshot() {{}}
function renderTraining() {{}}
{function_source}
updateTrainingResponse({{training_ground:{{catalog:[{{sku:"treadmill"}}]}}}});
const state = app.state.training_ground;
if (state.player_manager.id !== 77) throw new Error("player manager lost");
if (state.growth_hook.installed !== false) throw new Error("growth hook lost");
if (state.preferred_moves[0].bit !== 1) throw new Error("preferred moves lost");
if (state.preferred_moves_error !== "kept") throw new Error("preferred move error lost");
updateTrainingResponse({{training_ground:{{growth_hook:{{installed:true}}}}}});
if (app.state.training_ground.growth_hook.installed !== true) throw new Error("fresh growth hook ignored");
"""
        result = subprocess.run(
            ["node", "-"], input=harness, text=True, encoding="utf-8",
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_training_use_panel_executes_for_builtin_and_single_user_facilities(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        prefix = "function trainingUsePanel(facility) {"
        function_body = script.split(prefix, 1)[1].split(
            "\n}\n\nfunction selectedGameIsFm26", 1,
        )[0]
        function_source = f"{prefix}{function_body}\n}}"
        harness = f"""
const app = {{
  trainingPlayerId:0, trainingActionBusy:false,
  trainingPlayerGroupState:{{}}, trainingPlayerPickerOpen:false,
  trainingRosterLoading:false, showHiddenAttributes:false,
}};
const products = {{
  set_piece_area:{{
    name:"定位球训练区", built_in:true, unlimited:true,
    capacity:null, training_multiplier:2,
    attributes:["定位球:任意球"], icon:"flag-triangle-right",
  }},
  leadership_desk:{{
    name:"领导力训练桌", built_in:false, capacity:1,
    attributes:["精神:领导力"],
  }},
  habit_lab:{{
    name:"习惯塑形机", built_in:false, capacity:1,
    attributes:[],
  }},
}};
function trainingProduct(sku) {{ return products[sku] || {{}}; }}
let focuses = [];
function trainingFacilityFocuses() {{ return focuses; }}
function trainingIsUnlimited(product) {{ return product?.unlimited === true; }}
function trainingManagedPlayers() {{ return []; }}
function trainingFacilityVisual() {{ return "<span></span>"; }}
    function trainingFacilityLabel(facility) {{ return trainingProduct(facility.sku).name; }}
    function localizedPreferredMove(focus) {{ return focus.preferred_move_name || ""; }}
    function localizedTrainingAttribute(value) {{ return value || ""; }}
    function trainingCapacityIcons() {{ return ""; }}
function trainingMemberRows() {{ return ""; }}
function escapeHtml(value) {{ return String(value ?? ""); }}
function uiText(key, parameters = {{}}) {{
  if (key === "training.people_training") return `${{parameters.used}}/${{parameters.capacity}} 人使用中`;
  if (key === "training.player_active_summary") return `球员${{parameters.name}}使用中，训练${{parameters.target}}，还需${{parameters.days}}天`;
  if (key === "training.multiplier") return `以 ${{parameters.value}} 倍速训练`;
  return key;
}}
function uiPlural(key, count) {{
  if (key === "training.people_studying") return `${{count}} 人训练中`;
  return String(count);
}}
{function_source}
const builtin = trainingUsePanel({{id:"area",sku:"set_piece_area"}});
const leadership = trainingUsePanel({{id:"desk",sku:"leadership_desk"}});
if (!builtin.includes("当前无人训练 · 不限人数")) throw new Error("builtin usage label missing");
if (builtin.includes("以 2 倍速训练")) throw new Error("builtin note should be hidden");
if (!leadership.includes("0/1 人使用中")) throw new Error("single-user usage label missing");
focuses = [{{
  focus_type:"preferred_move", player_id:10, player_name:"梅西",
  preferred_move_name:"尝试倒钩", days_used:2,
}}];
const activeHabit = trainingUsePanel({{id:"habit",sku:"habit_lab"}});
if (!activeHabit.includes("球员梅西使用中，训练尝试倒钩，还需5天")) throw new Error("active player summary missing");
"""
        result = subprocess.run(
            ["node", "-"], input=harness, text=True, encoding="utf-8",
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_goalkeeper_training_selector_accepts_any_player_with_gk_ability(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        prefix = "function trainingPlayerMatchesPositionGroup(player, positionGroup) {"
        function_body = script.split(prefix, 1)[1].split(
            "\n}\n\nfunction trainingUsePanel", 1,
        )[0]
        function_source = f"{prefix}{function_body}\n}}"
        harness = f"""
function playerPositionGroup(player) {{ return player.group || "defenders"; }}
{function_source}
const secondaryGoalkeeper = {{position_ratings:{{ST:20,GK:8}},primary_positions:["ST"],positions:["ST","GK"]}};
const outfieldPlayer = {{position_ratings:{{ST:20}},primary_positions:["ST"],positions:["ST"]}};
const legacyGoalkeeper = {{primary_positions:["GK"],positions:["GK"]}};
if (!trainingPlayerMatchesPositionGroup(secondaryGoalkeeper,"goalkeepers")) throw new Error("secondary goalkeeper missing");
if (trainingPlayerMatchesPositionGroup(outfieldPlayer,"goalkeepers")) throw new Error("outfield player included");
if (!trainingPlayerMatchesPositionGroup(legacyGoalkeeper,"goalkeepers")) throw new Error("legacy goalkeeper missing");
"""
        result = subprocess.run(
            ["node", "-"], input=harness, text=True, encoding="utf-8",
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_training_attribute_targets_keep_stop_ball_available_to_every_player(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        prefix = "function playerAttributeValue(player, key) {"
        helper_body = script.split(prefix, 1)[1].split(
            "function trainingPointsRequired", 1,
        )[0]
        helper_source = f"{prefix}{helper_body}"
        harness = f"""
{helper_source}
const product = {{attributes:["门将:手控球","技术:停球"]}};
const outfield = {{attributes:{{"技术":{{"停球":12}}}}}};
const goalkeeper = {{attributes:{{"门将":{{"手控球":13,"停球":14}}}}}};
const outfieldTargets = trainingAttributeKeysForPlayer(product,outfield);
const goalkeeperTargets = trainingAttributeKeysForPlayer(product,goalkeeper);
if (JSON.stringify(outfieldTargets) !== JSON.stringify(["技术:停球"])) throw new Error("outfield target mismatch");
if (JSON.stringify(goalkeeperTargets) !== JSON.stringify(product.attributes)) throw new Error("goalkeeper target mismatch");
if (playerAttributeValue(goalkeeper,"技术:停球") !== 14) throw new Error("goalkeeper stop-ball alias missing");
"""
        result = subprocess.run(
            ["node", "-"], input=harness, text=True, encoding="utf-8",
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_new_indoor_training_equipment_has_webp_assets(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        for sku in ("balance_platform", "pressure_pod", "leadership_desk", "defence_learning_desk"):
            asset = root / "web" / "assets" / "training" / f"{sku}.webp"
            self.assertTrue(asset.is_file())
            self.assertGreater(asset.stat().st_size, 10_000)

    def test_training_ground_is_nested_under_team_facilities(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        facilities = index.split('id="facilities-subnav"', 1)[1].split("</div>", 1)[0]
        self.assertIn(
            '<button data-page="training"><i data-lucide="dumbbell"></i>'
            '<span>训练场</span></button>', facilities,
        )

    def test_training_equipment_tabs_use_requested_text_without_icons(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        for marker in (
            'data-training-dock="available"', 'data-training-dock="shop"',
            'data-training-dock="storage"',
        ):
            self.assertIn(marker, index)
            self.assertIn(marker, script)
        for key in ("training.available_equipment", "training.equipment_shop", "training.equipment_storage"):
            self.assertIn(key, script)
        self.assertNotIn('data-training-dock="shop"><i ', index)

    def test_training_dock_keeps_localised_copy_readable(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn("grid-template-columns:minmax(620px,1fr) clamp(330px,30vw,420px)", styles)
        self.assertIn("grid-template-rows:minmax(48px,auto) minmax(180px,1fr) auto", styles)
        dock_copy_rule = styles.split(".training-dock-item small {", 1)[1].split("}", 1)[0]
        self.assertIn("display:block", dock_copy_rule)
        self.assertIn("overflow-wrap:anywhere", dock_copy_rule)
        self.assertNotIn("line-clamp", dock_copy_rule)

    def test_coaching_page_is_named_coaching_management(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('<span>执教管理</span>', index)
        self.assertIn('<strong>执教管理</strong>', index)
        self.assertIn('<h1 id="club-title">执教管理</h1>', index)

    def test_training_ground_has_independent_second_floor_scene(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-training-scene="indoor_2f">室内 2F</button>', index)
        self.assertIn(".training-stage.indoor_2f", styles)
        self.assertIn('item.scene === app.trainingScene', script)

    def test_training_ground_has_placeable_comprehensive_seventh_floor(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn(
            'data-training-scene="comprehensive_7f">实验室 7F</button>', index,
        )
        self.assertIn(".training-stage.comprehensive_7f", styles)
        self.assertIn(
            ".training-stage.comprehensive_7f { background-image:url('/assets/training/comprehensive_7f.webp'); }",
            styles,
        )
        self.assertTrue(
            (root / "web" / "assets" / "training" / "comprehensive_7f.webp").is_file(),
        )

        self.assertNotIn(".training-facility.training-room", styles)
        self.assertNotIn("training-room-icon", script)
        for sku in (
            "mind_room", "mentoring_room", "tactical_room",
            "video_analysis_room", "sports_science_room",
        ):
            self.assertTrue((root / "web" / "assets" / "training" / f"{sku}.webp").is_file())
        self.assertNotIn("trainingRoomAdvisorCandidates", script)
        self.assertNotIn("payload.advisor_kind", script)
        self.assertNotIn('id="training-inline-advisor"', script)
    def test_training_floor_backgrounds_keep_office_separate_from_training_halls(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")

        self.assertIn(
            ".training-stage.indoor_3f,.training-stage.indoor_4f,.training-stage.indoor_5f { background-image:url('/assets/training/indoor_2f.webp'); }",
            styles,
        )
        self.assertIn(
            ".training-stage.office_3f,.training-stage.office_6f { background-image:url('/assets/training/office_3f.webp'); }",
            styles,
        )
        self.assertTrue(
            (root / "web" / "assets" / "training" / "office_3f.webp").is_file(),
        )

    def test_training_ground_has_third_floor_coaching_office(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        index = (root / "web" / "index.html").read_text(encoding="utf-8")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-training-scene="office_3f">办公室 3F</button>', index)
        self.assertIn(".training-stage.office_3f", styles)
        self.assertIn('facility.sku === "coaching_desk"', script)
        self.assertIn('operation:"coaching_license"', script)
        self.assertIn('subject_type:String(staff.training_subject_type || "staff")', script)
        self.assertIn('training_subject_type:"player_manager"', script)
        self.assertIn("managedCoachingRostersAreCurrent()", script)
        self.assertIn('profile.profile_stage === "complete"', script)
        self.assertIn("ensureTrainingCoachingRosterLoaded()", script)
        self.assertIn("player_manager:response.training_ground.player_manager ?? playerManager", script)
        self.assertIn('"training.sync_roster"', script)
        self.assertIn('教练 / 玩家经理', script)
        self.assertIn('已最高等级', script)
        self.assertIn('id="training-inline-staff"', script)
        self.assertIn('开始考证', script)
        self.assertTrue((root / "web" / "assets" / "training" / "office_3f.webp").is_file())
        self.assertTrue((root / "web" / "assets" / "training" / "coaching_desk.webp").is_file())

    def test_training_dock_content_cannot_resize_desktop_stage(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        styles = (root / "web" / "app.css").read_text(encoding="utf-8")
        self.assertIn(
            ".training-layout { height:calc(100vh - 188px); min-height:592px;",
            styles,
        )
        self.assertIn(
            ".training-dock { min-width:0; min-height:0;", styles,
        )
        self.assertIn("overflow:hidden; background:#f8faf9;", styles)
        self.assertIn(
            ".training-layout{height:auto;min-height:0;grid-template-columns:1fr}",
            styles,
        )

    def test_removed_training_dialog_has_no_stale_event_bindings(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn('$("#training-player").addEventListener', script)
        self.assertNotIn('$("#training-operation-toggle").addEventListener', script)
        self.assertNotIn('$("#training-form").addEventListener', script)

    def test_relationship_room_detail_accepts_dom_string_instance_id(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('const lookupId = String(instanceId ?? "");', script)
        self.assertIn('String(row.id ?? "") === lookupId', script)

    def test_relationship_room_staff_sort_has_chief_classifier(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function isStaffChief(staff)", script)
        self.assertIn("[38, 44, 50, 62].includes(jobType)", script)

    def test_relationship_training_people_include_youth_squads(self) -> None:
        profile = {
            "players": [{"id": 1, "name": "Senior"}],
            "staff": [{"id": 10, "name": "Coach"}],
            "squads": [
                {"team_id": 100, "type_code": 0, "label": "一线队", "players": [{"id": 1, "name": "Senior"}]},
                {
                    "team_id": 101,
                    "type_code": 1,
                    "label": "U21",
                    "players": [{
                        "id": 2,
                        "name": "Youth",
                        "squad_team_id": 0,
                        "squad_type_code": None,
                        "squad_label": "",
                    }],
                },
            ],
        }
        players = LocalOddsState._relationship_training_people(profile, "player")
        self.assertEqual({row["id"] for row in players}, {1, 2})
        youth = next(row for row in players if row["id"] == 2)
        self.assertEqual(youth["squad_team_id"], 101)
        self.assertEqual(youth["squad_type_code"], 1)
        self.assertEqual(youth["squad_label"], "U21")
        self.assertEqual(
            LocalOddsState._relationship_training_people(profile, "staff"),
            profile["staff"],
        )

    def test_training_mutation_keeps_newer_equipment_over_stale_state_response(self) -> None:
        root = (Path(__file__).resolve().parents[1] / "src")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("trainingMutationRevision", script)
        self.assertIn("trainingRevisionAtStart !== Number(app.trainingMutationRevision || 0)", script)
        self.assertIn("nextState.training_ground = app.state.training_ground", script)

    def test_fm24_verified_move_sample(self) -> None:
        self.assertEqual(FM24_VERIFIED_PREFERRED_MOVES[13], "喜欢反越位")
        self.assertEqual(FM24_VERIFIED_PREFERRED_MOVES[41], "鼓动观众情绪")
        self.assertEqual(FM24_VERIFIED_PREFERRED_MOVES[42], "第一时间射门")


def test_training_compact_view_keeps_equipment_images_and_excludes_position_training() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    markup = (root / "web" / "index.html").read_text(encoding="utf-8")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")
    styles = (root / "web" / "app.css").read_text(encoding="utf-8")

    page = markup.split('id="page-training"', 1)[1].split('</section>', 1)[0]
    header = page.split('</header>', 1)[0]
    assert '<label class="facility-view-toggle"><input type="checkbox" data-facility-view-page="training"' in header
    assert header.rindex('class="facility-view-toggle"') > header.index('id="training-settle-button"')
    assert 'if (mode === "text") app.trainingEditing = false;' in script
    assert 'app._lastSelHash = null;' in script
    assert 'if (page === "training" && app.trainingScene === "position_training") return "visual";' in script
    assert 'const visual = `<img src="${trainingSprite(facility.sku, product)}" alt="">`;' in script
    assert 'trainingFacilityVisual(product, sku)' in script
    assert 'product?.built_in\n    ? `<span class="training-area-icon">' in script
    assert 'training-text-facility-icon' not in script
    assert 'const selHash = JSON.stringify({\n      viewMode,' in script
    for dock in ('available', 'shop', 'storage'):
        assert f'data-training-dock="{dock}"' in script or f'data-training-dock="{dock}"' in markup
    for action in ('data-training-use=', 'data-training-buy=', 'data-training-place=', 'data-training-sell='):
        assert action in script
    assert '.training-page.facility-text-view .training-stage-items' in styles
    assert '.training-page.facility-text-view .training-facility.indoor-equipment img' in styles
    assert '.training-page.facility-text-view .training-dock-item{grid-template-columns:76px minmax(0,1fr) auto' in styles
    assert '.training-page.facility-text-view .training-dock-item img{width:72px;height:72px;max-width:100%' in styles
    assert '.training-page.facility-text-view .training-stage-panel{min-width:0;overflow:hidden' in styles
    assert '.training-page.facility-text-view .training-toolbar #training-scene-tabs{width:100%;min-width:0;overflow-x:auto}' in styles
    assert '.training-page.facility-text-view .training-dock{position:relative;z-index:3;min-width:0;overflow:hidden}' in styles
    assert '.training-page.facility-text-view .training-selection{min-width:0;overflow:hidden}' in styles
    assert '.training-page.facility-text-view .training-selection>div:first-child strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' in styles
    assert '.training-page.facility-text-view .position-training-pitch' not in styles


def test_training_stage_equipment_click_opens_matching_available_detail() -> None:
    root = (Path(__file__).resolve().parents[1] / "src")
    script = (root / "web" / "app.js").read_text(encoding="utf-8")

    click_handler = script.split('stage.addEventListener("click",(event)=>{', 1)[1].split("\n  });", 1)[0]
    assert 'event.target.closest("[data-training-facility]")' in click_handler
    assert 'app.trainingSelectedId=node.dataset.trainingFacility;' in click_handler
    assert 'openTrainingDetail(node.dataset.trainingFacility)' in click_handler
    assert "if (app.trainingEditing)" not in click_handler
    assert 'app.trainingIgnoreFacilityClickUntil = Date.now() + 180;' in script
    assert 'app.trainingDock = "available";' in script
    assert 'app.trainingDetailId = facility.id;' in script


if __name__ == "__main__":
    unittest.main()
