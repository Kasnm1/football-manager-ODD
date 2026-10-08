import threading
import base64
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fm_odds_web
import pytest


def state():
    value = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    value.lock = threading.RLock()
    value.memory_lock = threading.RLock()
    value.data_version = 4
    value.output = {
        "account_scope_id": "account-1", "save_instance_id": "career-1",
    }
    value.club_profiles = {10: {
        "team": {"id": 10, "name": "主队", "team_type": "club"},
        "players": [{"id": 1, "name": "A", "address": "0x1000"}],
        "staff": [], "squads": [],
    }}
    return value


def relationship_training_state():
    value = state()
    value.output["game_date"] = "2026-08-20"
    value.club_profiles[10]["players"][0].update({
        "ca": 120, "pa": 160, "age": 21, "attributes": {},
    })
    return value


def relationship_room_catalog(room_id, room_sku):
    return {
        "rooms": [{"sku": room_sku, "instances": [{"id": room_id}]}],
    }


def test_relationship_room_catalog_uses_ready_connection_scope_while_identity_is_deferred():
    value = relationship_training_state()
    value.cache_verified = False
    value.output["save_identity_discovery_deferred"] = True
    value.connection_scope_id = "connection-account"
    value.wallet_ready = True
    with (
        patch("fm_odds_web.set_active_save_id") as activate,
        patch("fm_odds_web.public_training_ground", return_value={"floors": []}),
        patch("fm_odds_web.relationship_training_catalog", return_value={"rooms": []}),
    ):
        result = value.relationship_training_rooms_state()
    activate.assert_called_once_with("connection-account")
    assert result == {"rooms": [], "floor": None}


def test_relationship_room_unlock_charges_real_combined_funds():
    value = relationship_training_state()
    payment = {
        "bank": 4_000_000.0, "wallet": 1_000_000.0,
        "total": 5_000_000.0, "transaction_id": "payment-1",
    }
    economy = {"bank_balance": 6_000_000.0, "casino_balance": 2_000_000.0}
    rooms = {"rooms": [{"sku": "mind_room", "instances": [{"id": "mind-1"}]}]}
    with (
        patch.object(value, "_bind_request_scope", return_value=True),
        patch("fm_odds_web.assert_training_floor_unlocked"),
        patch("fm_odds_web.free_services_enabled", return_value=False),
        patch("fm_odds_web.local_purchase_price", return_value=5_000_000.0),
        patch("fm_odds_web.charge_combined_funds", return_value=payment) as charge,
        patch("fm_odds_web.unlock_relationship_training_room", return_value={"id": "mind-1"}),
        patch.object(value, "relationship_training_rooms_state", return_value=rooms),
        patch("fm_odds_web.public_economy", return_value=economy),
    ):
        result = value.relationship_training_room_unlock({"room_sku": "mind_room"})

    charge.assert_called_once_with(
        5_000_000.0, "relationship_training_room_unlock", room_sku="mind_room",
    )
    assert result["payment"] == payment
    assert result["economy"] == economy


def operation_context(*, writable=False):
    operation = SimpleNamespace(
        reader=object(), process=object(), writable=writable,
    )
    context = Mock()
    context.__enter__ = Mock(return_value=operation)
    context.__exit__ = Mock(return_value=False)
    return context


def test_relationship_routes_are_registered():
    registered = {(method, path) for method, path, _ in fm_odds_web.API_ROUTES.describe()}
    assert ("GET", "/api/relations/scopes") in registered
    for path in ("people", "network", "pair", "timeline", "entities"):
        assert ("POST", f"/api/relations/{path}") in registered
    assert ("POST", "/api/rel3d/screenshot") in registered
    if not fm_odds_web.FROZEN:
        assert ("POST", "/api/relations/adjust-bidirectional") not in registered
        assert ("POST", "/api/training/relationship-rooms/settle") in registered


def test_relationship_timeline_uses_current_account_scope():
    value = state()
    with patch("fm_odds_web.relationship_timeline", return_value={"events": []}) as timeline:
        assert value.relations_timeline({"page": 2}) == {"events": []}
    timeline.assert_called_once_with({"page": 2}, "account-1")


def test_relationship_timeline_enriches_current_ca_pa_and_player_manager_role():
    value = state()
    value.club_profiles[10]["players"][0].update({"ca": 123, "pa": 167})
    value.output.update({
        "selected_manager_id": 99,
        "manager": {"id": 99, "name": "玩家", "ca": 144, "pa": 171},
    })
    archived = {"events": [{
        "person_a": {
            "id": 1, "name": "旧球员名", "kind": "player", "ca": 0, "pa": 0,
            "event_role": "activity_target", "event_role_label": "活动对象",
        },
        "person_b": {
            "id": 99, "name": "玩家", "kind": "staff",
            "event_role": "manager", "event_role_label": "玩家经理",
        },
    }]}
    with patch("fm_odds_web.relationship_timeline", return_value=archived):
        result = value.relations_timeline({})

    player = result["events"][0]["person_a"]
    manager = result["events"][0]["person_b"]
    assert (player["name"], player["ca"], player["pa"]) == ("A", 123, 167)
    assert player["event_role_label"] == "活动对象"
    assert manager["kind"] == "staff"
    assert manager["role"] == "玩家经理"
    assert manager["is_player_manager"] is True


def test_relationship_timeline_uses_real_player_and_staff_categories():
    value = state()
    value.club_profiles[10]["players"][0].update({
        "primary_positions": ["AML"], "ca": 123, "pa": 167,
    })
    value.club_profiles[10]["staff"] = [{
        "id": 3, "name": "队医", "job_type": 38, "role": "队医",
        "ca": 142, "pa": 155,
    }]
    archived = {"events": [{
        "person_a": {"id": 1, "name": "旧球员", "kind": "player"},
        "person_b": {"id": 3, "name": "旧职员", "kind": "staff"},
    }]}
    with patch("fm_odds_web.relationship_timeline", return_value=archived):
        result = value.relations_timeline({})

    player = result["events"][0]["person_a"]
    staff = result["events"][0]["person_b"]
    assert (player["timeline_category_key"], player["timeline_category_label"]) == ("forwards", "前场")
    assert (staff["timeline_category_key"], staff["timeline_category_label"]) == ("医疗团队", "医疗团队")
    assert player["team_name"] == staff["team_name"] == "主队"
    assert player["team_type"] == staff["team_type"] == "club"


def test_rel3d_screenshot_validates_png_and_returns_real_path(tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (b"\x00" * 12)
    result = fm_odds_web.save_rel3d_screenshot({
        "dir": str(tmp_path),
        "data": "data:image/png;base64," + base64.b64encode(raw).decode("ascii"),
    })
    assert result["ok"] is True
    assert result["size"] == len(raw)
    assert Path(result["path"]).read_bytes() == raw


def test_rel3d_screenshot_rejects_non_png_payload():
    encoded = base64.b64encode(b"not a png payload long enough").decode("ascii")
    with pytest.raises(ValueError, match="PNG"):
        fm_odds_web.save_rel3d_screenshot({
            "data": "data:image/png;base64," + encoded,
        })


def test_scopes_do_not_load_acquired_club_detail():
    value = state()
    value.world_club_detail = Mock(side_effect=AssertionError("must stay lazy"))
    with patch("fm_odds_web.account_acquired_clubs", return_value=[{
        "team_id": 20, "team_name": "集团队", "available": True,
    }]):
        result = value.relations_scopes()
    assert result["managed"][0]["team_id"] == 10
    assert result["acquired"][0]["team_id"] == 20
    value.world_club_detail.assert_not_called()


def test_scopes_include_profiles_published_during_club_refresh():
    value = state()
    value.club_refreshing = True
    value.club_loading_profiles = {11: {
        "team": {"id": 11, "name": "分批发布队伍", "team_type": "club"},
        "players": [], "staff": [], "squads": [],
    }}
    with patch("fm_odds_web.account_acquired_clubs", return_value=[]):
        result = value.relations_scopes()

    assert [row["team_id"] for row in result["managed"]] == [10, 11]


def test_people_reject_stale_data_version():
    value = state()
    try:
        value.relations_people({"team_id": 10, "data_version": 3})
    except ValueError as error:
        assert "已刷新" in str(error)
    else:
        raise AssertionError("stale request was accepted")


def test_network_borrows_resident_reader_for_selected_page_only():
    value = state()
    operation = SimpleNamespace(reader=object())
    context = Mock()
    context.__enter__ = Mock(return_value=operation)
    context.__exit__ = Mock(return_value=False)
    enriched = {"people": [{"id": 1}]}
    with (
        patch("fm_odds_web.borrow_game_operation", return_value=context),
        patch("fm_odds_web.enrich_relationship_page", return_value=enriched) as enrich,
    ):
        result = value.relations_network({
            "team_id": 10, "data_version": 4, "page": 1, "page_size": 24,
        })
    assert result["data_version"] == 4
    enrich.assert_called_once()
    assert enrich.call_args.args[1]["total"] == 1


def test_relationship_room_preview_checks_all_pairs_but_marks_only_effect_pairs():
    value = relationship_training_state()
    value.club_profiles[10]["players"].append({
        "id": 2, "name": "B", "address": "0x2000",
    })
    value.club_profiles[10]["staff"].append({
        "id": 7, "name": "分析员", "address": "0x7000",
        "job_type": 60, "abilities": {"数据分析": 14},
    })
    positive = {
        "a_to_b": {"relation_type": 1, "level": 30, "reason": 6},
        "b_to_a": {"relation_type": 1, "level": 20, "reason": 6},
    }
    negative = {
        "a_to_b": {"relation_type": 0, "signed_score": -10, "reason": 12},
        "b_to_a": {"relation_type": 1, "level": 0, "reason": 0},
    }
    with (
        patch.object(value, "_bind_current_save"),
        patch("fm_odds_web.relationship_training_catalog", return_value=
              relationship_room_catalog("video-1", "video_analysis_room")),
        patch("fm_odds_web.borrow_game_operation", return_value=operation_context()),
        patch("fm_odds_web.resolve_public_person_address", side_effect=[
            0x7000, 0x1000, 0x2000,
        ]),
        patch("fm_odds_web.read_relationship_pair", side_effect=[
            positive, positive, negative,
        ]),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {}, "intimacy_progress": {},
        }),
    ):
        result = value.relationship_training_room_preview({
            "room_id": "video-1",
            "participants": [
                {"role": "analyst", "kind": "staff", "id": 7, "team_id": 10},
                {"role": "player", "kind": "player", "id": 1, "team_id": 10},
                {"role": "player", "kind": "player", "id": 2, "team_id": 10},
            ],
        })
    assert len(result["pairs"]) == 3
    assert [pair["effect_pair"] for pair in result["pairs"]] == [True, True, False]
    assert result["pairs"][2]["negative"] is True
    assert result["negative"] is True


def test_relationship_room_preview_returns_actual_attribute_growth_and_eta():
    value = relationship_training_state()
    value.club_profiles[10]["staff"].append({
        "id": 7, "name": "教练", "address": "0x7000", "job_type": 2,
        "abilities": {"人员管理": 18, "激励": 16},
        "coaching_license": {"code": 7}, "current_reputation": 5000,
    })
    room = {
        **fm_odds_web.RELATIONSHIP_TRAINING_ROOMS["tactical_room"],
        "sku": "tactical_room", "instances": [{"id": "tactical-1"}],
    }
    relation = {
        "a_to_b": {"relation_type": 1, "level": 40, "signed_score": 40},
        "b_to_a": {"relation_type": 1, "level": 60, "signed_score": 60},
    }
    with (
        patch.object(value, "_bind_current_save"),
        patch("fm_odds_web.relationship_training_catalog", return_value={"rooms": [room]}),
        patch("fm_odds_web.borrow_game_operation", return_value=operation_context()),
        patch("fm_odds_web.resolve_public_person_address", side_effect=[0x7000, 0x1000]),
        patch("fm_odds_web.read_relationship_pair", return_value=relation),
        patch("fm_odds_web.read_player_training_attributes", return_value={
            "1:hidden:职业素养": 10,
        }),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {"tactical_room:player:1:hidden:职业素养": 5.0},
            "intimacy_progress": {},
        }),
    ):
        result = value.relationship_training_room_preview({
            "room_id": "tactical-1", "attributes": ["hidden:职业素养"],
            "participants": [
                {"role": "coach", "kind": "staff", "id": 7, "team_id": 10},
                {"role": "player", "kind": "player", "id": 1, "team_id": 10},
            ],
        })
    effect = result["effects"][0]
    assert effect["current"] == 10
    assert effect["target"] == 11
    assert effect["stored_points"] == 5.0
    assert effect["daily_points"] > effect["base_daily_points"]
    assert effect["required_points"] > effect["stored_points"]
    assert effect["estimated_days"] >= 1
    assert result["pairs"][0]["a_to_b_effect"]["daily"] > 0
    assert result["pairs"][0]["b_to_a_effect"]["daily"] > 0


def test_relationship_training_settlement_commits_hidden_progress_atomically():
    value = relationship_training_state()
    participant = {
        "kind": "player", "id": 1, "name": "A", "role": "player_a",
        "team_id": 10,
    }
    session = {
        "id": "focus-1", "room_sku": "mind_room",
        "participants": [participant], "attributes": ["hidden:适应性"],
    }
    plan = {
        "elapsed_days": 1, "focus_coefficient": 1.0,
        "attribute_points": 3.0, "beneficiaries": [participant],
        "attributes": [{"key": "hidden:适应性", "kind": "hidden"}],
        "pairs": [],
    }
    committed = {
        "completed": {"attribute_changes": [{"change": 3}]},
        "archive": {"created": True},
    }
    with (
        patch("fm_odds_web.FROZEN", False),
        patch.object(value, "_bind_current_save"),
        patch.object(value, "relationship_training_rooms_state", return_value={"rooms": []}),
        patch("fm_odds_web.assert_training_floor_unlocked"),
        patch("fm_odds_web.release_relationship_training_room", return_value=session),
        patch("fm_odds_web.relationship_training_effect_plan", return_value=plan),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {}, "intimacy_progress": {},
        }),
        patch("fm_odds_web.borrow_game_operation", return_value=operation_context()),
        patch("fm_odds_web.resolve_public_person_address", return_value=0x1000),
        patch("fm_odds_web.read_person_public_profile", return_value={"uid": 1}),
        patch("fm_odds_web.read_player_training_attributes", return_value={
            "1:hidden:适应性": 10,
        }),
        patch("fm_odds_web.training_points_required_for_direction", return_value=1),
        patch("fm_odds_web.develop_player_hidden_attribute", side_effect=[
            {"_original_raw": index, "_updated_raw": index + 1}
            for index in range(3)
        ]) as develop,
        patch("fm_odds_web.commit_relationship_training_settlement", return_value=committed) as commit,
        patch("fm_odds_web.invalidate_club_profile_cache"),
    ):
        result = value.relationship_training_room_settle({"room_id": "room-1"})
    assert develop.call_count == 3
    assert commit.call_args.kwargs["attribute_progress"]["mind_room:player:1:hidden:适应性"] == 0
    assert result["archive"] == {"created": True}


def test_video_analysis_team_capability_multiplies_attribute_progress():
    value = relationship_training_state()
    value.club_profiles[10]["staff"].append({
        "id": 7, "name": "分析师", "address": "0x7000", "job_type": 60,
        "abilities": {
            "数据分析": 20, "判断球员能力": 20,
            "判断球员潜力": 20, "战术知识": 20,
        },
    })
    player = {
        "kind": "player", "id": 1, "name": "A", "role": "player",
        "team_id": 10,
    }
    analyst = {
        "kind": "staff", "id": 7, "name": "分析师", "role": "analyst",
        "team_id": 10,
    }
    session = {
        "id": "video-focus", "room_sku": "video_analysis_room",
        "participants": [player, analyst], "attributes": ["精神:决断"],
    }
    plan = {
        "elapsed_days": 1, "focus_coefficient": 1.0,
        "attribute_points": 1.0, "beneficiaries": [player],
        "attributes": [{"key": "精神:决断", "kind": "visible"}],
        "pairs": [],
    }
    committed = {"completed": {}, "archive": {}}
    with (
        patch("fm_odds_web.FROZEN", False),
        patch.object(value, "_bind_current_save"),
        patch.object(value, "relationship_training_rooms_state", return_value={"rooms": []}),
        patch("fm_odds_web.assert_training_floor_unlocked"),
        patch("fm_odds_web.release_relationship_training_room", return_value=session),
        patch("fm_odds_web.relationship_training_effect_plan", return_value=plan),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {}, "intimacy_progress": {}, "height_baselines": {},
        }),
        patch("fm_odds_web.borrow_game_operation", return_value=operation_context()),
        patch("fm_odds_web.resolve_public_person_address", side_effect=[0x1000, 0x7000]),
        patch("fm_odds_web.read_person_public_profile", side_effect=[
            {"uid": 1}, {"uid": 7},
        ]),
        patch("fm_odds_web.read_player_training_attributes", return_value={
            "1:精神:决断": 10,
        }),
        patch("fm_odds_web.training_points_required_for_direction", return_value=100),
        patch("fm_odds_web.commit_relationship_training_settlement", return_value=committed) as commit,
        patch("fm_odds_web.invalidate_club_profile_cache"),
    ):
        value.relationship_training_room_settle({"room_id": "room-1"})
    assert commit.call_args.kwargs["attribute_progress"][
        "video_analysis_room:player:1:精神:决断"
    ] == pytest.approx(1.5)


def test_sports_condition_write_rolls_back_when_account_commit_fails():
    value = relationship_training_state()
    value.club_profiles[10]["players"][0].update({
        "availability": {"injury_count": 0, "injuries": []},
        "fatigue": 3000, "sharpness": 70,
    })
    value.club_profiles[10]["staff"].append({
        "id": 8, "name": "运动科学家", "address": "0x8000", "job_type": 40,
        "abilities": {"运动科学": 15},
    })
    player = {
        "kind": "player", "id": 1, "name": "A", "role": "player",
        "team_id": 10,
    }
    scientist = {
        "kind": "staff", "id": 8, "name": "运动科学家", "role": "scientist",
        "team_id": 10,
    }
    session = {
        "id": "sports-focus", "room_sku": "sports_science_room",
        "participants": [player, scientist], "attributes": ["精神:勇敢"],
    }
    plan = {
        "elapsed_days": 0, "focus_coefficient": 1.0,
        "attribute_points": 0.0, "beneficiaries": [player],
        "attributes": [{"key": "精神:勇敢", "kind": "visible"}],
        "pairs": [],
    }
    condition = {
        "before": {"fatigue": 3000, "sharpness": 7000, "fitness": 8000},
        "after": {"fatigue": 2375, "sharpness": 7625, "fitness": 8375},
        "_original": b"original", "_updated": b"updated",
    }
    with (
        patch("fm_odds_web.FROZEN", False),
        patch.object(value, "_bind_current_save"),
        patch("fm_odds_web.assert_training_floor_unlocked"),
        patch("fm_odds_web.release_relationship_training_room", return_value=session),
        patch("fm_odds_web.relationship_training_effect_plan", return_value=plan),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {}, "intimacy_progress": {}, "height_baselines": {},
        }),
        patch("fm_odds_web.borrow_game_operation", return_value=operation_context()),
        patch("fm_odds_web.resolve_public_person_address", side_effect=[0x1000, 0x8000]),
        patch("fm_odds_web.read_person_public_profile", side_effect=[
            {"uid": 1}, {"uid": 8},
        ]),
        patch("fm_odds_web.read_player_training_attributes", return_value={
            "1:精神:勇敢": 10,
        }),
        patch("fm_odds_web.adjust_player_training_condition", return_value=condition),
        patch("fm_odds_web.commit_relationship_training_settlement", side_effect=RuntimeError("commit failed")),
        patch("fm_odds_web.restore_player_training_condition") as restore_condition,
        patch("fm_odds_web.restore_relationship_training_room") as restore_room,
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            value.relationship_training_room_settle({"room_id": "room-1"})
    restore_condition.assert_called_once_with(1, "0x1000", b"original", b"updated")
    restore_room.assert_called_once_with("room-1")


def test_relationship_training_commit_failure_rolls_back_native_write_and_room():
    value = relationship_training_state()
    participant = {
        "kind": "player", "id": 1, "name": "A", "role": "player_a",
        "team_id": 10,
    }
    session = {
        "id": "focus-1", "room_sku": "mind_room",
        "participants": [participant], "attributes": ["hidden:适应性"],
    }
    plan = {
        "elapsed_days": 1, "focus_coefficient": 1.0,
        "attribute_points": 1.0, "beneficiaries": [participant],
        "attributes": [{"key": "hidden:适应性", "kind": "hidden"}],
        "pairs": [],
    }
    write_result = {"_original_raw": 10, "_updated_raw": 11}
    with (
        patch("fm_odds_web.FROZEN", False),
        patch.object(value, "_bind_current_save"),
        patch("fm_odds_web.assert_training_floor_unlocked"),
        patch("fm_odds_web.release_relationship_training_room", return_value=session),
        patch("fm_odds_web.relationship_training_effect_plan", return_value=plan),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {}, "intimacy_progress": {},
        }),
        patch("fm_odds_web.borrow_game_operation", return_value=operation_context()),
        patch("fm_odds_web.resolve_public_person_address", return_value=0x1000),
        patch("fm_odds_web.read_person_public_profile", return_value={"uid": 1}),
        patch("fm_odds_web.read_player_training_attributes", return_value={
            "1:hidden:适应性": 10,
        }),
        patch("fm_odds_web.training_points_required_for_direction", return_value=1),
        patch("fm_odds_web.develop_player_hidden_attribute", return_value=write_result),
        patch("fm_odds_web.commit_relationship_training_settlement", side_effect=RuntimeError("commit failed")),
        patch("fm_odds_web.restore_player_hidden_attribute") as restore_attribute,
        patch("fm_odds_web.restore_relationship_training_room") as restore_room,
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            value.relationship_training_room_settle({"room_id": "room-1"})
    restore_attribute.assert_called_once_with(
        1, "0x1000", "hidden:适应性", 10, 11,
    )
    restore_room.assert_called_once_with("room-1")


def test_relationship_training_commit_failure_rolls_back_relationship_write():
    value = relationship_training_state()
    value.club_profiles[10]["players"].append({
        "id": 2, "name": "B", "address": "0x2000",
        "ca": 110, "pa": 150, "age": 20, "attributes": {},
    })
    left = {
        "kind": "player", "id": 1, "name": "A", "role": "player_a",
        "team_id": 10,
    }
    right = {
        "kind": "player", "id": 2, "name": "B", "role": "player_b",
        "team_id": 10,
    }
    session = {
        "id": "focus-relationship", "room_sku": "mind_room",
        "participants": [left, right], "attributes": [],
    }
    plan = {
        "elapsed_days": 1, "focus_coefficient": 1.0,
        "attribute_points": 0.0, "beneficiaries": [], "attributes": [],
        "pairs": [{
            "person_a": left, "person_b": right, "base_increment": 1.0,
            "reason_a2b": 5, "reason_b2a": 5,
        }],
    }
    write_result = {
        "a_to_b": {"before": 10, "after": 11, "applied": 1, "undo": {"a": 1}},
        "b_to_a": {"before": 20, "after": 21, "applied": 1, "undo": {"b": 1}},
    }
    context = operation_context(writable=True)
    with (
        patch("fm_odds_web.FROZEN", False),
        patch.object(value, "_bind_current_save"),
        patch("fm_odds_web.assert_training_floor_unlocked"),
        patch("fm_odds_web.release_relationship_training_room", return_value=session),
        patch("fm_odds_web.relationship_training_effect_plan", return_value=plan),
        patch("fm_odds_web.relationship_training_progress", return_value={
            "attribute_progress": {}, "intimacy_progress": {},
        }),
        patch("fm_odds_web.borrow_game_operation", return_value=context),
        patch("fm_odds_web.resolve_public_person_address", side_effect=[0x1000, 0x2000]),
        patch("fm_odds_web.read_person_public_profile", side_effect=[
            {"uid": 1}, {"uid": 2},
        ]),
        patch("fm_odds_web.read_relationship_pair", return_value={
            "a_to_b": {"level": 10, "relation_type": 1},
            "b_to_a": {"level": 20, "relation_type": 1},
        }),
        patch("fm_odds_web.read_player_training_attributes", return_value={}),
        patch("fm_odds_web.write_bidirectional_intimacy", return_value=write_result) as write,
        patch("fm_odds_web.commit_relationship_training_settlement", side_effect=RuntimeError("commit failed")),
        patch("fm_odds_web.rollback_bidirectional_intimacy") as rollback,
        patch("fm_odds_web.restore_relationship_training_room") as restore_room,
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            value.relationship_training_room_settle({"room_id": "room-1"})

    write.assert_called_once()
    rollback.assert_called_once_with(write_result)
    restore_room.assert_called_once_with("room-1")
