from __future__ import annotations

import json
import math
import threading
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tools.account_store import load_document, save_document
from tools.app_paths import save_data_root
from tools.app_settings import load_settings, local_purchase_price
from tools.training_archive import archive_training_focus


_LOCK = threading.RLock()


def _archive_completed_focus(focus: dict[str, Any]) -> None:
    """Archive when an account scope exists; completion must remain usable in previews/tests."""
    try:
        archive_training_focus(focus)
    except RuntimeError as error:
        if "unidentified.fmodd" not in str(error).casefold():
            raise
TRAINING_MULTIPLIER = 3
NO_EQUIPMENT_TRAINING_MULTIPLIER = 2
POSITION_TRAINING_MULTIPLIER = 4
INDOOR_FACILITY_LIMIT = 15
TRAINING_FLOOR_UNLOCK_PRICE = 5_000_000.0
TRAINING_FLOORS: dict[str, dict[str, Any]] = {
    "indoor": {"floor": "1F", "name": "室内 1F", "price": 0.0, "unlocked_by_default": True},
    "indoor_2f": {"floor": "2F", "name": "室内 2F", "price": 0.0, "unlocked_by_default": True},
    "indoor_3f": {"floor": "3F", "name": "室内 3F", "price": TRAINING_FLOOR_UNLOCK_PRICE},
    "indoor_4f": {"floor": "4F", "name": "室内 4F", "price": TRAINING_FLOOR_UNLOCK_PRICE},
    "indoor_5f": {"floor": "5F", "name": "室内 5F", "price": TRAINING_FLOOR_UNLOCK_PRICE},
    "office_6f": {"floor": "6F", "name": "办公室 6F", "price": 0.0, "unlocked_by_default": True},
    "comprehensive_7f": {
        "floor": "7F", "name": "实验室 7F", "price": 0.0,
        "unlocked_by_default": True,
    },
    "relationship_8f": {
        "floor": "8F", "name": "关系综训楼 8F",
        "price": TRAINING_FLOOR_UNLOCK_PRICE,
    },
    "outdoor": {"floor": "", "name": "1号室外", "price": 0.0, "unlocked_by_default": True},
    "outdoor_2": {"floor": "", "name": "2号室外", "price": 0.0, "unlocked_by_default": True},
    "position_training": {
        "floor": "", "name": "位置训练", "price": 0.0,
        "unlocked_by_default": True,
    },
}
OUTDOOR_SET_PIECE_AREA_ID = "outdoor-set-piece-area"
OUTDOOR_TEAM_TRAINING_AREA_ID = "outdoor-team-training-area"
OUTDOOR_2_SET_PIECE_AREA_ID = "outdoor-2-set-piece-area"
OUTDOOR_2_TEAM_TRAINING_AREA_ID = "outdoor-2-team-training-area"
OUTDOOR_GOALKEEPER_TRAINING_AREA_ID = "outdoor-goalkeeper-training-area"
OUTDOOR_2_GOALKEEPER_TRAINING_AREA_ID = "outdoor-2-goalkeeper-training-area"
OUTDOOR_PASSING_TRAINING_AREA_ID = "outdoor-passing-training-area"
OUTDOOR_2_PASSING_TRAINING_AREA_ID = "outdoor-2-passing-training-area"
POSITION_TRAINING_AREA_ID = "position-training-area"
TRAINING_POSITION_NAMES = (
    "GK", "SW", "DL", "DC", "DR", "DM", "ML", "MC", "MR",
    "AML", "AMC", "AMR", "ST", "WBL", "WBR",
)
TRAINING_DURATIONS: dict[str, int | None] = {
    "until_increase": None,
    "until_14": 14,
    "until_16": 16,
    "until_18": 18,
    "until_20": 20,
    "until_decrease": None,
    "until_10": 10,
    "until_5": 5,
    "until_1": 1,
}
TRAINING_POINT_REQUIREMENTS = {
    11: 21, 12: 28, 13: 35, 14: 42, 15: 63,
    16: 126, 17: 300, 18: 540, 19: 900,
}
# Position training uses a smooth exponential-style curve from 15→20 totalling
# 180 game days at four-times speed, without a disproportionate final step.
POSITION_TRAINING_POINT_REQUIREMENTS = {
    **TRAINING_POINT_REQUIREMENTS,
    15: 48, 16: 80, 17: 128, 18: 192, 19: 272,
}
COACHING_LICENSE_COURSE_DAYS = {
    0: 30, 7: 45, 6: 60, 5: 75, 4: 90, 3: 120, 2: 180,
}
COACHING_LICENSE_NEXT = {0: 7, 7: 6, 6: 5, 5: 4, 4: 3, 3: 2, 2: 1}

FACILITIES: dict[str, dict[str, Any]] = {
    "coaching_desk": {
        "name": "教练学习桌", "scene": "office_6f", "price": 100_000.0,
        "session_fee": 0.0, "icon": "graduation-cap", "size": [22, 22],
        "footprint": [12, 10], "capacity": 1,
        "description": "安排俱乐部教练进修，结业后证书提升一级。",
        "attributes": [], "staff_training": True,
    },
    "leadership_desk": {
        "name": "领导力训练桌", "scene": "office_6f", "price": 800_000.0,
        "session_fee": 0.0, "icon": "badge-captain", "size": [22, 22],
        "footprint": [12, 10], "capacity": 1,
        "description": "三倍速提升领导力。",
        "attributes": ["精神:领导力"],
    },
    "defence_learning_desk": {
        "name": "防守学习桌", "scene": "office_6f", "price": 900_000.0,
        "session_fee": 0.0, "icon": "shield-check", "size": [22, 22],
        "footprint": [12, 10], "capacity": 1,
        "description": "三倍速提升指挥防守与拳击球倾向。",
        "attributes": ["门将:指挥防守", "门将:拳击球(倾向)"],
    },
    "habit_lab": {
        "name": "习惯塑形机", "scene": "indoor", "price": 1_000_000.0,
        "session_fee": 0.0, "icon": "brain-circuit", "size": [18, 23],
        "footprint": [9, 8],
        "capacity": 1,
        "description": "学习或遗忘一项球员习惯。", "attributes": [],
    },
    "strength_rack": {
        "name": "综合力量架", "scene": "indoor", "price": 200_000.0,
        "session_fee": 0.0, "icon": "dumbbell", "size": [19, 18],
        "footprint": [13.5, 10],
        "capacity": 2,
        "description": "三倍速提升强壮。",
        "attributes": ["身体:强壮"],
    },
    "treadmill": {
        "name": "专业跑步机", "scene": "indoor", "price": 800_000.0,
        "session_fee": 0.0, "icon": "activity", "size": [17, 19],
        "footprint": [12, 13],
        "capacity": 1,
        "description": "三倍速提升速度和爆发力。",
        "attributes": ["身体:速度", "身体:爆发力"],
    },
    "reaction_wall": {
        "name": "掉棍反应机", "scene": "indoor", "price": 400_000.0,
        "session_fee": 0.0, "icon": "scan-eye", "size": [25, 25],
        "footprint": [17, 11],
        "capacity": 2,
        "description": "三倍速提升反应、预判和集中。",
        "attributes": ["门将:反应", "精神:预判", "精神:集中"],
    },
    "tactics_table": {
        "name": "战术投影台", "scene": "indoor", "price": 400_000.0,
        "session_fee": 0.0, "icon": "presentation", "size": [24, 25],
        "footprint": [9, 8],
        "capacity": 2,
        "description": "三倍速提升视野、决断和无球跑动。",
        "attributes": ["精神:视野", "精神:决断", "精神:无球跑动"],
    },
    "jump_rig": {
        "name": "垂直弹跳机", "scene": "indoor", "price": 200_000.0,
        "session_fee": 0.0, "icon": "move-up", "size": [22, 27],
        "footprint": [12, 9],
        "capacity": 1,
        "description": "三倍速提升弹跳能力。",
        "attributes": ["身体:弹跳"],
    },
    "hypoxic_pod": {
        "name": "低氧适应舱", "scene": "indoor", "price": 400_000.0,
        "session_fee": 0.0, "icon": "wind", "size": [23, 24],
        "footprint": [13, 11],
        "capacity": 1,
        "description": "三倍速提升耐力和体质。",
        "attributes": ["身体:耐力", "身体:体质"],
    },
    "balance_platform": {
        "name": "动态平衡台", "scene": "indoor", "price": 200_000.0,
        "session_fee": 0.0, "icon": "scale", "size": [23, 21],
        "footprint": [14, 11],
        "capacity": 2,
        "description": "三倍速提升平衡。",
        "attributes": ["身体:平衡"],
    },
    "pressure_pod": {
        "name": "决胜压力舱", "scene": "indoor", "price": 800_000.0,
        "session_fee": 0.0, "icon": "brain", "size": [18, 24],
        "footprint": [10, 9],
        "capacity": 1,
        "description": "三倍速提升意志力和镇定。",
        "attributes": ["精神:意志力", "精神:镇定"],
    },
    "shooting_goal": {
        "name": "射门训练门", "scene": "outdoor", "price": 500_000.0,
        "session_fee": 0.0, "icon": "crosshair", "size": [15, 13],
        "footprint": [12, 7],
        "capacity": 2,
        "description": "三倍速提升射门、远射、头球和罚点球。",
        "attributes": ["技术:射门", "技术:远射", "技术:头球", "定位球:罚点球"],
    },
    "set_piece_area": {
        "name": "定位球训练区", "scene": "outdoor", "price": 0.0,
        "session_fee": 0.0, "icon": "flag-triangle-right",
        "capacity": None, "unlimited": True, "built_in": True,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速提升任意球、角球和界外球。",
        "attributes": ["定位球:任意球", "定位球:角球", "定位球:界外球"],
    },
    "team_training_area": {
        "name": "球队合练区", "scene": "outdoor", "price": 0.0,
        "session_fee": 0.0, "icon": "users-round",
        "capacity": None, "unlimited": True, "built_in": True,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速提升团队合作、工作投入、防守站位、才华和侵略性。",
        "attributes": [
            "精神:团队合作", "精神:工作投入", "精神:防守站位", "精神:才华",
            "精神:侵略性",
        ],
    },
    "goalkeeper_training_area": {
        "name": "门将训练区", "scene": "outdoor", "price": 0.0,
        "session_fee": 0.0, "icon": "goal",
        "capacity": None, "unlimited": True, "built_in": True,
        "position_group": "goalkeepers",
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速提升大脚开球、手抛球和出击（倾向）。",
        "attributes": ["门将:大脚开球", "门将:手抛球", "门将:出击(倾向)"],
    },
    "passing_training_area": {
        "name": "传球训练区", "scene": "outdoor", "price": 0.0,
        "session_fee": 0.0, "icon": "arrow-left-right",
        "capacity": None, "unlimited": True, "built_in": True,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速提升传球和传中。",
        "attributes": ["技术:传球", "技术:传中"],
    },
    "position_training_area": {
        "name": "阵型位置训练区", "scene": "position_training", "price": 0.0,
        "session_fee": 0.0, "icon": "route", "capacity": None,
        "unlimited": True, "built_in": True,
        "training_multiplier": POSITION_TRAINING_MULTIPLIER,
        "description": "根据阵型安排球员，四倍速提升目标位置熟练度。",
        "attributes": [],
    },
    "dribble_course": {
        "name": "盘带障碍区", "scene": "outdoor", "price": 800_000.0,
        "session_fee": 0.0, "icon": "route", "size": [17, 14],
        "footprint": [14, 9],
        "capacity": 2,
        "description": "三倍速提升盘带、技术和灵活性。",
        "attributes": ["技术:盘带", "技术:技术", "身体:灵活"],
    },
    "tackle_gate": {
        "name": "截球闸门", "scene": "outdoor", "price": 1_200_000.0,
        "session_fee": 0.0, "icon": "shield", "image_sku": "tackle_gate",
        "size": [16.1, 14], "footprint": [14, 10], "capacity": 2,
        "description": "三倍速提升抢断与勇敢。",
        "attributes": ["技术:抢断", "精神:勇敢"],
    },
    "marking_track": {
        "name": "跟防跑道", "scene": "outdoor", "price": 1_000_000.0,
        "session_fee": 0.0, "icon": "scan-line", "image_sku": "marking_track",
        "size": [16.5, 13.5], "footprint": [14, 9], "capacity": 2,
        "description": "三倍速提升盯人与防守站位。",
        "attributes": ["技术:盯人", "精神:防守站位"],
    },
    "high_ball_frame": {
        "name": "高球落点架", "scene": "outdoor", "price": 1_400_000.0,
        "session_fee": 0.0, "icon": "hand", "image_sku": "high_ball_frame",
        "size": [15.6, 14.3], "footprint": [15, 10], "capacity": 2,
        "description": "三倍速提升拦截传中与制空范围。",
        "attributes": ["门将:拦截传中", "门将:制空范围"],
    },
    "handling_net": {
        "name": "足球发射器", "scene": "outdoor", "price": 1_000_000.0,
        "session_fee": 0.0, "icon": "circle-dot", "image_sku": "football_launcher",
        "size": [11.55, 11.55], "footprint": [13, 10], "capacity": 2,
        "description": "三倍速提升手控球与停球。",
        "attributes": ["门将:手控球", "技术:停球"],
    },
    "adaptation_pod": {
        "name": "适应模拟舱", "scene": "comprehensive_7f", "price": 8_000_000.0,
        "session_fee": 0.0, "icon": "orbit", "size": [22, 27],
        "footprint": [14, 12], "capacity": 1,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速改变适应性和多面性属性。",
        "attributes": ["隐藏:适应性", "隐藏:多面性"],
        "attribute_kind": "hidden", "direction": 1,
    },
    "career_terminal": {
        "name": "生涯终端", "scene": "comprehensive_7f", "price": 12_000_000.0,
        "session_fee": 0.0, "icon": "monitor-up", "size": [22, 25],
        "footprint": [14, 11], "capacity": 1,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速改变雄心和职业素养属性。",
        "attributes": ["隐藏:雄心", "隐藏:职业素养"],
        "attribute_kind": "hidden", "direction": 1,
    },
    "club_culture_screen": {
        "name": "俱乐部文化屏", "scene": "comprehensive_7f", "price": 6_000_000.0,
        "session_fee": 0.0, "icon": "badge", "size": [18.2, 16.8],
        "footprint": [18, 10], "capacity": 1,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速改变忠诚和体育精神属性。",
        "attributes": ["隐藏:忠诚", "隐藏:体育精神"],
        "attribute_kind": "hidden", "direction": 1,
    },
    "decisive_match_console": {
        "name": "决胜主机", "scene": "comprehensive_7f", "price": 15_000_000.0,
        "session_fee": 0.0, "icon": "gamepad-2", "size": [19.2, 20],
        "footprint": [16, 11], "capacity": 1,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速改变抗压能力和大赛发挥属性。",
        "attributes": ["隐藏:抗压能力", "隐藏:大赛发挥"],
        "attribute_kind": "hidden", "direction": 1,
    },
    "brainwave_chair": {
        "name": "脑波座椅", "scene": "comprehensive_7f", "price": 10_000_000.0,
        "session_fee": 0.0, "icon": "armchair", "size": [17.6, 20],
        "footprint": [15, 12], "capacity": 1,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速改变情绪控制和稳定性属性。",
        "attributes": ["隐藏:情绪控制", "隐藏:稳定性"],
        "attribute_kind": "hidden", "direction": 1,
    },
    "rules_learning_desk": {
        "name": "规则学习桌", "scene": "office_6f", "price": 5_000_000.0,
        "session_fee": 0.0, "icon": "book-open-check", "size": [24, 22],
        "footprint": [16, 11], "capacity": 1,
        "training_multiplier": NO_EQUIPMENT_TRAINING_MULTIPLIER,
        "description": "二倍速降低争议性和肮脏动作。",
        "attributes": ["隐藏:争议性", "隐藏:肮脏动作"],
        "attribute_kind": "hidden", "direction": -1,
    },
}

# Keep removed identifiers for one-time migration of old account documents;
# they are intentionally absent from FACILITIES and cannot be purchased or
# placed in a new session.
REMOVED_FACILITY_SKUS = frozenset({
    "passing_wall", "mind_room", "mentoring_room", "tactical_room",
    "video_analysis_room", "sports_science_room",
    "kick_target", "distribution_wall", "aerial_bar", "reaction_goal",
    "review_desk", "command_console", "duel_pad", "advance_runway",
    "defence_zone", "punching_pad", "goalkeeper_rig",
})

COMPREHENSIVE_ROOM_SKUS = frozenset({
    "adaptation_pod", "career_terminal", "club_culture_screen",
    "decisive_match_console", "brainwave_chair",
})


_BUILT_IN_FACILITY_SKUS = {
    OUTDOOR_SET_PIECE_AREA_ID: "set_piece_area",
    OUTDOOR_TEAM_TRAINING_AREA_ID: "team_training_area",
    OUTDOOR_2_SET_PIECE_AREA_ID: "set_piece_area",
    OUTDOOR_2_TEAM_TRAINING_AREA_ID: "team_training_area",
    OUTDOOR_GOALKEEPER_TRAINING_AREA_ID: "goalkeeper_training_area",
    OUTDOOR_2_GOALKEEPER_TRAINING_AREA_ID: "goalkeeper_training_area",
    OUTDOOR_PASSING_TRAINING_AREA_ID: "passing_training_area",
    OUTDOOR_2_PASSING_TRAINING_AREA_ID: "passing_training_area",
    POSITION_TRAINING_AREA_ID: "position_training_area",
}

_BUILT_IN_FACILITY_SCENES = {
    OUTDOOR_2_SET_PIECE_AREA_ID: "outdoor_2",
    OUTDOOR_2_TEAM_TRAINING_AREA_ID: "outdoor_2",
    OUTDOOR_2_GOALKEEPER_TRAINING_AREA_ID: "outdoor_2",
    OUTDOOR_2_PASSING_TRAINING_AREA_ID: "outdoor_2",
}


def _built_in_facility(facility_id: str) -> dict[str, Any]:
    sku = _BUILT_IN_FACILITY_SKUS[facility_id]
    scene = _BUILT_IN_FACILITY_SCENES.get(
        facility_id, str(FACILITIES[sku]["scene"]),
    )
    return {
        "id": facility_id,
        "sku": sku,
        "scene": scene,
        "status": "placed",
        "x": 50.0,
        "y": 50.0,
        "rotation": 0,
        "scale": 1.0,
        "built_in": True,
    }


def _built_in_facilities() -> list[dict[str, Any]]:
    return [
        _built_in_facility(facility_id)
        for facility_id in _BUILT_IN_FACILITY_SKUS
    ]


def _placed_facility(
    payload: dict[str, Any], facility_id: str,
) -> dict[str, Any] | None:
    if facility_id in _BUILT_IN_FACILITY_SKUS:
        return _built_in_facility(facility_id)
    return next((
        row for row in payload["facilities"]
        if row.get("id") == facility_id and row.get("status") == "placed"
    ), None)


def _facility_has_capacity(
    payload: dict[str, Any], facility_id: str, product: dict[str, Any],
) -> bool:
    if product.get("unlimited"):
        return True
    capacity = int(product.get("capacity") or 1)
    current_users = sum(
        row.get("status") == "active" and row.get("facility_id") == facility_id
        for row in payload["focuses"]
    )
    return current_users < capacity


def _placed_facility_product(
    payload: dict[str, Any], facility_id: str, *, sku: str | None = None,
    missing_message: str = "请先把训练器材放置到场地",
) -> tuple[dict[str, Any], dict[str, Any]]:
    facility = _placed_facility(payload, facility_id)
    if not facility or (sku is not None and facility.get("sku") != sku):
        raise ValueError(missing_message)
    return facility, FACILITIES[facility["sku"]]


def _training_archive_location(
    facility: dict[str, Any], product: dict[str, Any],
) -> dict[str, Any]:
    scene = str(facility.get("scene") or product.get("scene") or "")
    floor = "3F" if scene == "office_3f" else str(
        (TRAINING_FLOORS.get(scene) or {}).get("floor") or ""
    )
    return {
        "facility_sku": str(facility.get("sku") or ""),
        "facility_name": str(product.get("name") or facility.get("sku") or "训练设施"),
        "scene": scene,
        "floor": floor,
    }


def _assert_player_available(payload: dict[str, Any], player_id: int) -> None:
    if any(
        row.get("status") == "active"
        and (
            int(row.get("player_id") or 0) == int(player_id)
            or (
                row.get("advisor_kind") == "player"
                and int(row.get("advisor_id") or 0) == int(player_id)
            )
        )
        for row in payload["focuses"]
    ):
        raise ValueError("该球员已有进行中的专项训练")


def _assert_facility_capacity(
    payload: dict[str, Any], facility: dict[str, Any], product: dict[str, Any],
    *, message: str = "该器材当前使用人数已满",
) -> None:
    if not _facility_has_capacity(payload, str(facility["id"]), product):
        raise ValueError(message)


def _append_training_records(
    payload: dict[str, Any], focus: dict[str, Any], session: dict[str, Any],
    client_submission_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if client_submission_id:
        submission_id = str(client_submission_id)
        focus["client_submission_id"] = submission_id
        session["client_submission_id"] = submission_id
    payload["focuses"].append(focus)
    payload["focuses"] = payload["focuses"][-500:]
    payload["sessions"].append(session)
    payload["sessions"] = payload["sessions"][-500:]
    save_training_ground(payload)
    return dict(focus), dict(session)


def _active_focus(
    payload: dict[str, Any], focus_id: str, *, focus_type: str | None = None,
    missing_message: str,
) -> dict[str, Any]:
    focus = next((
        row for row in payload["focuses"]
        if row.get("id") == focus_id
        and row.get("status") == "active"
        and (focus_type is None or row.get("focus_type") == focus_type)
    ), None)
    if not focus:
        raise ValueError(missing_message)
    return focus


def _complete_focus(
    focus: dict[str, Any], game_date: str, result: dict[str, Any],
) -> None:
    focus["status"] = "completed"
    focus["completed_on"] = game_date
    focus["completed_at"] = _now()
    focus["memory_result"] = dict(result)


def training_ground_path() -> Path:
    return save_data_root() / "training" / "training_ground.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": 12, "facilities": [], "sessions": [],
        "focuses": [], "history": [], "unlocked_floors": [],
    }


def training_points_required(current_attribute: int) -> int:
    current = int(current_attribute)
    if not 1 <= current < 20:
        raise ValueError("所选属性必须处于1至19")
    if current <= 10:
        return 14
    return TRAINING_POINT_REQUIREMENTS[current]


def training_points_required_for_direction(
    current_attribute: int, direction: int = 1,
) -> int:
    current = int(current_attribute)
    resolved_direction = -1 if int(direction) < 0 else 1
    effective = current if resolved_direction > 0 else 21 - current
    return training_points_required(effective)


def position_training_points_required(current_attribute: int) -> int:
    current = int(current_attribute)
    if current == 20:
        return 0
    if not 1 <= current < 20:
        raise ValueError("所选位置熟练度必须处于1至19")
    return POSITION_TRAINING_POINT_REQUIREMENTS[current] if current > 10 else 14


def estimated_position_training_days_to_target(
    current_position: int, target_position: int, progress_points: int = 0,
    multiplier: int = POSITION_TRAINING_MULTIPLIER, direction: int = 1,
) -> int:
    del direction
    current = int(current_position)
    target = int(target_position)
    if current == target == 20:
        return 0
    if not 1 <= current < target <= 20:
        raise ValueError("位置训练目标必须高于当前熟练度且不超过20")
    total_required = position_training_points_required(current) + sum(
        position_training_points_required(position)
        for position in range(current + 1, target)
    )
    remaining = max(0, total_required - max(0, int(progress_points)))
    return int(math.ceil(remaining / max(1, int(multiplier))))


def estimated_training_days(
    current_attribute: int, progress_points: int = 0,
    multiplier: int = TRAINING_MULTIPLIER,
) -> int:
    required = training_points_required(current_attribute)
    remaining = max(0, required - max(0, int(progress_points)))
    return int(math.ceil(remaining / max(1, int(multiplier))))


def training_target_attribute(
    duration_mode: str, current_attribute: int, direction: int = 1,
) -> int:
    current = int(current_attribute)
    resolved_direction = -1 if int(direction) < 0 else 1
    valid_modes = (
        {"until_decrease", "until_10", "until_5", "until_1"}
        if resolved_direction < 0 else
        {"until_increase", "until_14", "until_16", "until_18", "until_20"}
    )
    if duration_mode not in valid_modes:
        raise ValueError("请选择有效的训练时长")
    configured = TRAINING_DURATIONS[duration_mode]
    target = current + resolved_direction if configured is None else int(configured)
    valid_target = (
        current < target <= 20 if resolved_direction > 0
        else 1 <= target < current
    )
    if not valid_target:
        raise ValueError(
            "所选训练目标必须高于当前属性"
            if resolved_direction > 0 else "所选训练目标必须低于当前属性"
        )
    return target


def estimated_training_days_to_target(
    current_attribute: int, target_attribute: int, progress_points: int = 0,
    multiplier: int = TRAINING_MULTIPLIER, direction: int = 1,
) -> int:
    current = int(current_attribute)
    target = int(target_attribute)
    resolved_direction = -1 if int(direction) < 0 else 1
    if resolved_direction > 0 and not 1 <= current < target <= 20:
        raise ValueError("训练目标必须高于当前属性且不超过20")
    if resolved_direction < 0 and not 1 <= target < current <= 20:
        raise ValueError("训练目标必须低于当前属性且不低于1")
    remaining = max(
        0, training_points_required_for_direction(current, resolved_direction)
        - max(0, int(progress_points)),
    )
    remaining += sum(
        training_points_required_for_direction(attribute, resolved_direction)
        for attribute in range(
            current + resolved_direction, target, resolved_direction,
        )
    )
    return int(math.ceil(remaining / max(1, int(multiplier))))


def estimated_training_days_to_20(
    current_attribute: int, progress_points: int = 0,
    multiplier: int = TRAINING_MULTIPLIER,
) -> int:
    return estimated_training_days_to_target(
        current_attribute, 20, progress_points, multiplier,
    )


def _focus_training_multiplier(focus: dict[str, Any]) -> int:
    facility_id = str(focus.get("facility_id") or "")
    sku = _BUILT_IN_FACILITY_SKUS.get(facility_id)
    if sku:
        return max(1, int(
            FACILITIES[sku].get("training_multiplier") or TRAINING_MULTIPLIER,
        ))
    return max(1, int(focus.get("multiplier") or TRAINING_MULTIPLIER))


def _normalize_attribute_focus(
    focus: dict[str, Any], *, migrated: bool,
) -> None:
    initial = focus.get("initial_attribute")
    focus.setdefault("attribute_kind", "visible")
    direction = -1 if int(focus.get("direction") or 1) < 0 else 1
    focus["direction"] = direction
    try:
        initial_value = int(initial)
        required = training_points_required_for_direction(initial_value, direction)
    except (TypeError, ValueError):
        initial_value = 0
        required = 0
    default_mode = "until_decrease" if direction < 0 else "until_increase"
    duration_mode = str(focus.get("duration_mode") or default_mode)
    focus["duration_mode"] = duration_mode
    focus["duration_days"] = None
    focus["expires_on"] = None
    focus["allow_ca_over_pa"] = bool(focus.get("allow_ca_over_pa", False))
    focus.setdefault("started_attribute", initial_value or None)
    try:
        target_attribute = training_target_attribute(
            duration_mode, initial_value, direction,
        )
    except ValueError:
        duration_mode = default_mode
        focus["duration_mode"] = duration_mode
        target_attribute = initial_value + direction if required else None
    focus["target_attribute"] = target_attribute
    focus["multiplier"] = _focus_training_multiplier(focus)
    focus["required_points"] = required
    focus["progress_points"] = min(
        required, max(0, int(focus.get("progress_points") or 0)),
    ) if required else 0
    # Existing Hook-based tasks start from zero on first V4 synchronization.
    # This avoids an upgrade immediately applying an unreviewed attribute write.
    if migrated:
        focus["last_progress_on"] = None
    else:
        focus.setdefault("last_progress_on", focus.get("started_on"))


def load_training_ground() -> dict[str, Any]:
    payload = load_document(
        "training_ground", None, legacy_path=training_ground_path(),
    )
    if not isinstance(payload, dict):
        payload = _new_state()
    previous_schema = int(payload.get("schema_version") or 0)
    payload.setdefault("facilities", [])
    payload.setdefault("sessions", [])
    payload.setdefault("focuses", [])
    payload.setdefault("history", [])
    payload.setdefault("unlocked_floors", [])
    payload["schema_version"] = 12
    original_facilities = list(payload["facilities"])
    removed_facility_ids = {
        str(row.get("id") or "") for row in original_facilities
        if str(row.get("sku") or "") not in FACILITIES
        and str(row.get("id") or "")
    }
    payload["facilities"] = [
        row for row in original_facilities if row.get("sku") in FACILITIES
    ]
    migrated_removed = False
    if removed_facility_ids:
        for focus in payload["focuses"]:
            if (
                focus.get("status") == "active"
                and str(focus.get("facility_id") or "") in removed_facility_ids
            ):
                focus["status"] = "cancelled"
                focus["cancelled_at"] = _now()
                focus["cancelled_reason"] = "facility_removed"
                migrated_removed = True
        if migrated_removed:
            payload["history"].append({
                "at": _now(), "type": "facility_retirement",
                "facility_ids": sorted(removed_facility_ids),
            })
            payload["history"] = payload["history"][-500:]
    payload["unlocked_floors"] = [
        str(scene) for scene in payload["unlocked_floors"]
        if str(scene) in TRAINING_FLOORS
        and not TRAINING_FLOORS[str(scene)].get("unlocked_by_default")
    ]
    # Existing 3F office placements remain usable after the office moves to 6F.
    if any(row.get("scene") == "office_3f" for row in payload["facilities"]):
        payload["unlocked_floors"].append("office_6f")
    payload["unlocked_floors"] = list(dict.fromkeys(payload["unlocked_floors"]))
    for facility in payload["facilities"]:
        facility.setdefault("scale", 1.0)
        if (
            facility.get("sku") == "rules_learning_desk"
            and facility.get("scene") == "comprehensive_7f"
        ):
            facility["scene"] = "office_6f"
    for focus in payload["focuses"]:
        focus.setdefault("focus_type", "attribute")
        focus.setdefault("initial_attribute", None)
        focus.setdefault("target_attribute", None)
        focus.setdefault("player_address", None)
        focus.setdefault("player_team_id", None)
        focus.setdefault("player_team_name", None)
        focus.setdefault("player_team_type", None)
        if focus.get("focus_type") == "coaching_license":
            focus.setdefault("staff_id", focus.get("player_id"))
            focus.setdefault("staff_name", focus.get("player_name"))
            focus.setdefault("staff_address", focus.get("player_address"))
            focus.setdefault("staff_team_id", focus.get("player_team_id"))
            focus.setdefault("staff_team_name", focus.get("player_team_name"))
            focus.setdefault("staff_role", None)
        if focus.get("focus_type") == "attribute":
            _normalize_attribute_focus(focus, migrated=previous_schema < 4)
        elif focus.get("focus_type") == "position":
            initial = max(1, int(focus.get("initial_position") or 1))
            focus["initial_position"] = initial
            focus.setdefault("started_position", initial)
            focus.setdefault("target_position", min(20, initial + 1))
            focus.setdefault("duration_mode", "until_increase")
            focus["multiplier"] = _focus_training_multiplier(focus)
            focus.setdefault(
                "required_points", position_training_points_required(initial),
            )
            focus["progress_points"] = max(
                0, int(focus.get("progress_points") or 0),
            )
            focus.setdefault("last_progress_on", focus.get("started_on"))
        focus.setdefault("advisor_id", None)
        focus.setdefault("advisor_name", None)
        focus.setdefault("advisor_kind", None)
        focus.setdefault("advisor_team_id", None)
        focus.setdefault("advisor_role", None)
        focus.setdefault("advisor_capability", None)
    if removed_facility_ids:
        # Persist the migration immediately so an orphaned active task cannot
        # block the same player again after the next state refresh.
        try:
            save_training_ground(payload)
        except RuntimeError as error:
            if "unidentified.fmodd" not in str(error).casefold():
                raise
    return payload


def save_training_ground(payload: dict[str, Any]) -> None:
    save_document("training_ground", payload, legacy_path=training_ground_path())


def _training_floor_unlocked(payload: dict[str, Any], scene: str) -> bool:
    floor = TRAINING_FLOORS.get(str(scene))
    if not floor:
        return False
    return bool(
        floor.get("unlocked_by_default")
        or str(scene) in set(payload.get("unlocked_floors") or [])
    )


def _training_floor_status(
    payload: dict[str, Any], settings: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "scene": scene,
            **details,
            "price": local_purchase_price(details["price"], settings),
            "unlocked": _training_floor_unlocked(payload, scene),
        }
        for scene, details in TRAINING_FLOORS.items()
    ]


def training_floor_status(game_date: str = "") -> list[dict[str, Any]]:
    with _LOCK:
        return _training_floor_status(load_training_ground(), load_settings())


def assert_training_floor_unlocked(scene: str) -> dict[str, Any]:
    scene = str(scene or "")
    option = TRAINING_FLOORS.get(scene)
    if not option:
        raise ValueError("训练场楼层无效")
    with _LOCK:
        payload = load_training_ground()
        if not _training_floor_unlocked(payload, scene):
            raise ValueError(f"请先解锁{option['name']}")
    return dict(option)


def unlock_training_floor(scene: str) -> dict[str, Any]:
    scene = str(scene or "")
    option = TRAINING_FLOORS.get(scene)
    if not option:
        raise ValueError("训练场楼层无效")
    if option.get("unlocked_by_default"):
        raise ValueError(f"{option['name']}无需购买")
    with _LOCK:
        payload = load_training_ground()
        unlocked = payload.setdefault("unlocked_floors", [])
        if scene in unlocked:
            raise ValueError(f"{option['name']}已经解锁")
        unlocked.append(scene)
        payload["history"].append({"at": _now(), "type": "floor_unlock", "scene": scene})
        payload["history"] = payload["history"][-500:]
        save_training_ground(payload)
        return {"scene": scene, "unlocked": True}


def clear_training_ground() -> dict[str, int]:
    with _LOCK:
        payload = load_training_ground()
        cleared = {
            "facilities": len(payload["facilities"]),
            "sessions": len(payload["sessions"]),
            "focuses": len(payload["focuses"]),
            "history": len(payload["history"]),
        }
        save_training_ground(_new_state())
        return cleared


def release_training_users(
    game_date: str = "", *, reason: str = "manager_club_change",
) -> dict[str, int]:
    """Release active users while preserving facilities and audit records."""
    with _LOCK:
        payload = load_training_ground()
        released = 0
        released_at = _now()
        for focus in payload["focuses"]:
            if focus.get("status") != "active":
                continue
            focus["status"] = "cancelled"
            focus["cancelled_at"] = released_at
            focus["cancelled_on"] = game_date or None
            focus["cancel_reason"] = reason
            released += 1
        if released:
            payload["history"].append({
                "at": released_at,
                "type": "release_training_users",
                "reason": reason,
                "count": released,
                "game_date": game_date or None,
            })
            payload["history"] = payload["history"][-500:]
            save_training_ground(payload)
        return {
            "released": released,
            "facilities": len(payload["facilities"]),
            "sessions": len(payload["sessions"]),
            "history": len(payload["history"]),
        }


def _expire_focuses(payload: dict[str, Any], game_date: str) -> bool:
    if not game_date:
        return False
    today = date.fromisoformat(game_date)
    changed = False
    for focus in payload["focuses"]:
        if focus.get("status") != "active" or not focus.get("expires_on"):
            continue
        if focus.get("focus_type") in {"preferred_move", "coaching_license"}:
            continue
        if today >= date.fromisoformat(str(focus["expires_on"])):
            focus["status"] = "expired"
            focus["expired_at"] = _now()
            changed = True
    return changed


def advance_training_focuses(game_date: str) -> list[dict[str, Any]]:
    if not game_date:
        return []
    today = date.fromisoformat(game_date)
    due: list[dict[str, Any]] = []
    with _LOCK:
        payload = load_training_ground()
        changed = False
        for focus in payload["focuses"]:
            if (
                focus.get("status") != "active"
                or focus.get("focus_type") not in {"attribute", "position"}
            ):
                continue
            if focus.get("focus_type") == "attribute":
                _normalize_attribute_focus(focus, migrated=False)
            required = int(focus.get("required_points") or 0)
            if not required:
                continue
            last_text = focus.get("last_progress_on")
            if not last_text:
                focus["last_progress_on"] = game_date
                changed = True
            else:
                try:
                    last = date.fromisoformat(str(last_text))
                except ValueError:
                    last = today
                    focus["last_progress_on"] = game_date
                    changed = True
                if today > last:
                    elapsed = (today - last).days
                    multiplier = _focus_training_multiplier(focus)
                    earned_points = (
                        int(focus.get("progress_points") or 0)
                        + elapsed * multiplier
                    )
                    # Keep overflow for position training.  A long date jump
                    # may cross several one-point stages; the synchronizer
                    # consumes that carry-over through one native write per
                    # stage instead of silently discarding trained time.
                    focus["progress_points"] = (
                        max(0, earned_points)
                        if focus.get("focus_type") == "position"
                        else min(required, max(0, earned_points))
                    )
                    focus["last_progress_on"] = game_date
                    changed = True
            if int(focus.get("progress_points") or 0) >= required:
                due.append(dict(focus))
        if changed:
            save_training_ground(payload)
    return due


def complete_training_focuses(
    game_date: str, current_attributes: dict[str, int],
) -> list[dict[str, Any]]:
    if not game_date or not current_attributes:
        return []
    completed: list[dict[str, Any]] = []
    with _LOCK:
        payload = load_training_ground()
        changed = _expire_focuses(payload, game_date)
        for focus in payload["focuses"]:
            if (
                focus.get("status") != "active"
                or focus.get("focus_type") != "attribute"
            ):
                continue
            focus_id = str(focus.get("id") or "")
            current = current_attributes.get(focus_id)
            direction = -1 if int(focus.get("direction") or 1) < 0 else 1
            next_attribute = int(focus.get("initial_attribute") or 0) + direction
            satisfied = (
                int(current) >= next_attribute if direction > 0
                else int(current) <= next_attribute
            ) if current is not None else False
            if not satisfied:
                continue
            settled = _settle_attribute_focus(
                focus, game_date,
                {"attribute_after": int(current), "already_satisfied": True},
            )
            if settled.get("status") == "completed":
                completed.append(dict(settled))
            changed = True
        if changed:
            save_training_ground(payload)
    for focus in completed:
        archive_training_focus(focus)
    return completed


def _settle_attribute_focus(
    focus: dict[str, Any], game_date: str, result: dict[str, Any],
) -> dict[str, Any]:
    attribute_after = int(result.get("attribute_after") or 0)
    focus["attribute_after"] = attribute_after
    focus["memory_result"] = {
        key: value for key, value in result.items()
        if not str(key).startswith("_")
    }
    target_attribute = int(focus.get("target_attribute") or attribute_after)
    direction = -1 if int(focus.get("direction") or 1) < 0 else 1
    single_stage = focus.get("duration_mode") in {"until_increase", "until_decrease"}
    target_pending = (
        attribute_after < target_attribute if direction > 0
        else attribute_after > target_attribute
    )
    if not single_stage and target_pending:
        focus["initial_attribute"] = attribute_after
        focus["required_points"] = training_points_required_for_direction(
            attribute_after, direction,
        )
        focus["progress_points"] = 0
        focus["last_progress_on"] = game_date
        focus["stage_completed_on"] = game_date
        focus["stage_count"] = int(focus.get("stage_count") or 0) + 1
        return focus
    focus["status"] = "completed"
    focus["completed_on"] = game_date
    focus["completed_at"] = _now()
    return focus


def complete_attribute_training_focus(
    focus_id: str, game_date: str, result: dict[str, Any],
) -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="attribute",
            missing_message="专项训练任务已失效",
        )
        _settle_attribute_focus(focus, game_date, result)
        save_training_ground(payload)
        completed = dict(focus)
    if completed.get("status") == "completed":
        _archive_completed_focus(completed)
    return completed


def _settle_position_focus(
    focus: dict[str, Any], game_date: str, result: dict[str, Any],
) -> dict[str, Any]:
    # Native position training is deliberately a one-point transaction.  Keep
    # this invariant at the persistence boundary as well, so a buggy caller
    # cannot turn a multi-point jump into one completed training stage.
    position_before = result.get("position_before")
    position_after = int(result.get("position_after") or 0)
    if position_before is not None:
        try:
            expected_after = max(1, int(position_before)) + 1
        except (TypeError, ValueError) as error:
            raise ValueError("位置训练写入结果无效") from error
        if position_after != expected_after:
            raise ValueError("位置训练每次只能提升1点")
    focus["position_after"] = position_after
    focus["attribute_after"] = position_after
    focus["memory_result"] = {
        key: value for key, value in result.items()
        if not str(key).startswith("_")
    }
    target = int(focus.get("target_position") or position_after)
    single_stage = focus.get("duration_mode") == "until_increase"
    if not single_stage and position_after < target:
        completed_points = max(0, int(focus.get("progress_points") or 0))
        required_points = max(0, int(focus.get("required_points") or 0))
        focus["initial_position"] = position_after
        focus["initial_attribute"] = position_after
        focus["required_points"] = position_training_points_required(max(1, position_after))
        focus["progress_points"] = max(0, completed_points - required_points)
        focus["last_progress_on"] = game_date
        focus["stage_completed_on"] = game_date
        focus["stage_count"] = int(focus.get("stage_count") or 0) + 1
        return focus
    focus["status"] = "completed"
    focus["completed_on"] = game_date
    focus["completed_at"] = _now()
    return focus


def complete_position_training_focus(
    focus_id: str, game_date: str, result: dict[str, Any],
) -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="position",
            missing_message="位置训练任务已失效",
        )
        _settle_position_focus(focus, game_date, result)
        save_training_ground(payload)
        completed = dict(focus)
    if completed.get("status") == "completed":
        _archive_completed_focus(completed)
    return completed


def rebase_position_training_focus(
    focus_id: str, game_date: str, current_position: int,
) -> dict[str, Any]:
    current = max(1, int(current_position))
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="position",
            missing_message="位置训练任务已失效",
        )
        if not 1 <= current < 20:
            raise ValueError("当前位置熟练度不适合作为训练基线")
        old_target = int(focus.get("target_position") or 0)
        target = (
            current + 1 if focus.get("duration_mode") == "until_increase"
            else max(current + 1, old_target)
        )
        focus["initial_position"] = current
        focus["initial_attribute"] = current
        focus["target_position"] = min(20, target)
        focus["target_attribute"] = min(20, target)
        focus["required_points"] = position_training_points_required(current)
        focus["progress_points"] = min(
            int(focus["required_points"]),
            max(0, int(focus.get("progress_points") or 0)),
        )
        focus["last_progress_on"] = game_date
        focus["rebased_on"] = game_date
        save_training_ground(payload)
        return dict(focus)


def rebase_attribute_training_focus(
    focus_id: str, game_date: str, current_attribute: int,
) -> dict[str, Any]:
    """Rebase a task after an external attribute change without discarding progress."""
    current = int(current_attribute)
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="attribute",
            missing_message="专项训练任务已失效",
        )
        direction = -1 if int(focus.get("direction") or 1) < 0 else 1
        if not (1 <= current < 20 if direction > 0 else 1 < current <= 20):
            raise ValueError("当前属性不适合作为训练基线")
        previous = int(focus.get("initial_attribute") or 0)
        old_target = int(focus.get("target_attribute") or 0)
        if focus.get("duration_mode") in {"until_increase", "until_decrease"}:
            target = current + direction
        else:
            target = (
                max(current + 1, old_target) if direction > 0
                else min(current - 1, old_target)
            )
        required = training_points_required_for_direction(current, direction)
        focus["initial_attribute"] = current
        focus["target_attribute"] = max(1, min(20, target))
        focus["required_points"] = required
        focus["progress_points"] = min(required, max(0, int(focus.get("progress_points") or 0)))
        focus["last_progress_on"] = game_date
        focus["rebased_on"] = game_date
        focus["rebased_from_attribute"] = previous or None
        save_training_ground(payload)
        return dict(focus)


def _public_focus(row: dict[str, Any], game_date: str) -> dict[str, Any]:
    result = dict(row)
    try:
        current = date.fromisoformat(game_date) if game_date else date.fromisoformat(str(row["started_on"]))
        started = date.fromisoformat(str(row["started_on"]))
        days_used = max(0, (current - started).days)
    except (KeyError, TypeError, ValueError):
        days_used = 0
    duration_days = row.get("duration_days")
    if duration_days is not None:
        days_used = min(days_used, int(duration_days))
    result["days_used"] = days_used
    if row.get("focus_type") in {"attribute", "position"}:
        multiplier = _focus_training_multiplier(row)
        required = int(row.get("required_points") or 0)
        raw_progress = max(0, int(row.get("progress_points") or 0))
        progress = min(required, raw_progress)
        remaining = max(0, required - progress)
        result.update({
            "required_points": required,
            "progress_points": progress,
            "remaining_points": remaining,
            "multiplier": multiplier,
            "estimated_days": int(math.ceil(remaining / multiplier)),
            "estimated_total_days": (
                (estimated_position_training_days_to_target if row.get("focus_type") == "position" else estimated_training_days_to_target)(
                    int(
                        row.get("initial_position")
                        if row.get("focus_type") == "position"
                        else row.get("initial_attribute") or 0
                    ),
                    int(
                        row.get("target_position")
                        if row.get("focus_type") == "position"
                        else row.get("target_attribute") or 0
                    ), (
                        raw_progress
                        if row.get("focus_type") == "position" else progress
                    ), multiplier, int(row.get("direction") or 1),
                )
                if row.get("duration_mode") not in {"until_increase", "until_decrease"}
                and required else None
            ),
        })
    elif row.get("focus_type") == "coaching_license":
        try:
            due_on = date.fromisoformat(str(row.get("due_on") or ""))
            current = date.fromisoformat(game_date) if game_date else date.today()
            result["remaining_days"] = max(0, (due_on - current).days)
        except ValueError:
            result["remaining_days"] = None
    return result


def active_training_focuses(game_date: str) -> list[dict[str, Any]]:
    with _LOCK:
        payload = load_training_ground()
        if _expire_focuses(payload, game_date):
            save_training_ground(payload)
        return [
            dict(row) for row in payload["focuses"]
            if row.get("status") == "active"
        ]


def public_training_ground(game_date: str = "") -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        settings = load_settings()
        if _expire_focuses(payload, game_date):
            save_training_ground(payload)
        active = [row for row in payload["focuses"] if row.get("status") == "active"]
        recent_inactive = [
            row for row in reversed(payload["focuses"])
            if row.get("status") != "active"
        ][:50]
        return {
            "floors": _training_floor_status(payload, settings),
            "catalog": [
                {
                    "sku": sku, **details,
                    "price": local_purchase_price(details["price"], settings),
                }
                for sku, details in FACILITIES.items()
            ],
            "facilities": [
                *[dict(row) for row in payload["facilities"]],
                *_built_in_facilities(),
            ],
            "sessions": list(reversed(payload["sessions"][-50:])),
            "focuses": [
                _public_focus(row, game_date)
                for row in [*active, *recent_inactive]
            ],
            "training_durations": dict(TRAINING_DURATIONS),
            "training_multiplier": TRAINING_MULTIPLIER,
            "training_point_requirements": dict(TRAINING_POINT_REQUIREMENTS),
            "coaching_license_course_days": dict(COACHING_LICENSE_COURSE_DAYS),
        }


def purchase_facility(sku: str) -> dict[str, Any]:
    if sku not in FACILITIES:
        raise ValueError("训练器材不存在")
    if FACILITIES[sku].get("built_in"):
        raise ValueError("该训练区域由场地自带，无需购买")
    if FACILITIES[sku].get("available", True) is False:
        raise ValueError("该训练器材暂未开放购买")
    with _LOCK:
        payload = load_training_ground()
        facility = {
            "id": str(uuid.uuid4()), "sku": sku, "scene": FACILITIES[sku]["scene"],
            "status": "stored", "x": 50.0, "y": 50.0, "rotation": 0,
            "scale": 1.0,
            "purchased_at": _now(),
        }
        payload["facilities"].append(facility)
        payload["history"].append({"at": _now(), "type": "purchase", "sku": sku})
        payload["history"] = payload["history"][-500:]
        save_training_ground(payload)
        return dict(facility)


def remove_facility(facility_id: str) -> dict[str, Any]:
    if facility_id in _BUILT_IN_FACILITY_SKUS:
        raise ValueError("场地自带训练区域不能出售")
    with _LOCK:
        payload = load_training_ground()
        facility = next((row for row in payload["facilities"] if row.get("id") == facility_id), None)
        if not facility:
            raise ValueError("训练器材不存在")
        if any(
            row.get("status") == "active" and row.get("facility_id") == facility_id
            for row in payload["focuses"]
        ):
            raise ValueError("该器材仍有球员正在使用，不能出售")
        payload["facilities"].remove(facility)
        payload["history"].append({"at": _now(), "type": "sale", "sku": facility["sku"]})
        payload["history"] = payload["history"][-500:]
        save_training_ground(payload)
        return dict(facility)


def restore_facility(facility: dict[str, Any]) -> None:
    with _LOCK:
        payload = load_training_ground()
        if not any(row.get("id") == facility.get("id") for row in payload["facilities"]):
            payload["facilities"].append(dict(facility))
            save_training_ground(payload)


def _scene_accepts_product(scene: str, product_scene: str) -> bool:
    if product_scene == "indoor":
        return scene in {
            "indoor", "indoor_2f", "indoor_3f", "indoor_4f", "indoor_5f",
        }
    if product_scene == "office_6f":
        return scene in {"office_3f", "office_6f"}
    if product_scene == "comprehensive_7f":
        return scene == "comprehensive_7f"
    if product_scene == "outdoor":
        return scene in {"outdoor", "outdoor_2"}
    return scene == product_scene


def place_facility(
    facility_id: str, scene: str, x: Any, y: Any, rotation: Any = 0,
    scale: Any = None,
) -> dict[str, Any]:
    if facility_id in _BUILT_IN_FACILITY_SKUS:
        raise ValueError("场地自带训练区域无需放置")
    with _LOCK:
        payload = load_training_ground()
        facility = next((row for row in payload["facilities"] if row.get("id") == facility_id), None)
        if not facility:
            raise ValueError("训练器材不存在")
        product = FACILITIES[facility["sku"]]
        if not _scene_accepts_product(scene, str(product["scene"])):
            raise ValueError("该器材不能放置在这个场地")
        floor_scene = "office_6f" if scene == "office_3f" else scene
        if not _training_floor_unlocked(payload, floor_scene):
            option = TRAINING_FLOORS.get(floor_scene)
            raise ValueError(f"请先解锁{option['name'] if option else '该训练场楼层'}")
        position_x, position_y = float(x), float(y)
        if not 5 <= position_x <= 95 or not 8 <= position_y <= 92:
            raise ValueError("器材位置超出可用区域")
        resolved_scale = float(facility.get("scale") or 1.0) if scale is None else float(scale)
        if not 0.5 <= resolved_scale <= 2.0:
            raise ValueError("器材尺寸必须处于50%至200%")
        already_in_scene = facility.get("status") == "placed" and facility.get("scene") == scene
        if scene in {
            "indoor", "indoor_2f", "indoor_3f", "indoor_4f", "indoor_5f",
            "office_3f", "office_6f", "comprehensive_7f",
        } and not already_in_scene:
            placed_count = sum(
                row.get("status") == "placed" and row.get("scene") == scene
                for row in payload["facilities"]
            )
            if placed_count >= INDOOR_FACILITY_LIMIT:
                raise ValueError(f"室内每层最多放置{INDOOR_FACILITY_LIMIT}个器材")
        facility.update({
            "scene": scene, "status": "placed", "x": round(position_x, 2),
            "y": round(position_y, 2), "rotation": int(rotation) % 2,
            "scale": round(resolved_scale, 2),
        })
        save_training_ground(payload)
        return dict(facility)


def store_facility(facility_id: str) -> dict[str, Any]:
    if facility_id in _BUILT_IN_FACILITY_SKUS:
        raise ValueError("场地自带训练区域不能收纳")
    with _LOCK:
        payload = load_training_ground()
        facility = next((row for row in payload["facilities"] if row.get("id") == facility_id), None)
        if not facility:
            raise ValueError("训练器材不存在")
        if any(
            row.get("status") == "active" and row.get("facility_id") == facility_id
            for row in payload["focuses"]
        ):
            raise ValueError("该器材仍有球员正在使用，不能收回器材库")
        facility["status"] = "stored"
        save_training_ground(payload)
        return dict(facility)


def assert_training_available(
    facility_id: str, player_id: int, game_date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _LOCK:
        payload = load_training_ground()
        facility, product = _placed_facility_product(payload, facility_id)
        if not game_date:
            raise ValueError("尚未读取游戏日期")
        if _expire_focuses(payload, game_date):
            save_training_ground(payload)
        _assert_player_available(payload, player_id)
        _assert_facility_capacity(payload, facility, product)
        return dict(facility), dict(product)


def record_training_focus(
    facility_id: str, player_id: int, player_name: str, game_date: str,
    attribute_key: str, attribute_id: int | None, duration_mode: str,
    initial_attribute: int, allow_ca_over_pa: bool = False,
    client_submission_id: str | None = None,
    *, player_address: Any = None, player_team_id: int | None = None,
    player_team_name: str | None = None, player_team_type: str | None = None,
    attribute_kind: str = "visible", direction: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _LOCK:
        payload = load_training_ground()
        if _expire_focuses(payload, game_date):
            save_training_ground(payload)
        _assert_player_available(payload, player_id)
        facility, product = _placed_facility_product(payload, facility_id)
        resolved_kind = "hidden" if attribute_kind == "hidden" else "visible"
        resolved_direction = -1 if int(direction) < 0 else 1
        target_attribute = training_target_attribute(
            duration_mode, int(initial_attribute), resolved_direction,
        )
        _assert_facility_capacity(payload, facility, product)
        if not 1 <= int(initial_attribute) <= 20:
            raise ValueError("所选属性当前值无效")
        if resolved_direction > 0 and int(initial_attribute) >= 20:
            raise ValueError("该属性已经达到20，不能继续训练")
        if resolved_direction < 0 and int(initial_attribute) <= 1:
            raise ValueError("该属性已经达到1，不能继续降低")
        required_points = training_points_required_for_direction(
            int(initial_attribute), resolved_direction,
        )
        previous = next((
            row for row in reversed(payload["focuses"])
            if row.get("status") == "cancelled"
            and row.get("focus_type") == "attribute"
            and int(row.get("player_id") or 0) == int(player_id)
            and row.get("attribute_key") == attribute_key
            and str(row.get("attribute_kind") or "visible") == resolved_kind
            and int(row.get("direction") or 1) == resolved_direction
            and int(row.get("initial_attribute") or 0) == int(initial_attribute)
        ), None)
        carried_points = min(
            required_points - 1,
            max(0, int((previous or {}).get("progress_points") or 0)),
        )
        resolved_multiplier = max(1, int(
            product.get("training_multiplier") or TRAINING_MULTIPLIER,
        ))
        focus = {
            "id": str(uuid.uuid4()), "at": _now(), "status": "active",
            "focus_type": "attribute",
            "facility_id": facility_id, "player_id": int(player_id),
            "player_name": player_name, "attribute_key": attribute_key,
            "player_address": player_address,
            "player_team_id": int(player_team_id) if player_team_id else None,
            "player_team_name": player_team_name,
            "player_team_type": player_team_type,
            "attribute_id": int(attribute_id) if attribute_id is not None else None,
            "attribute_kind": resolved_kind, "direction": resolved_direction,
            "started_on": game_date,
            "duration_mode": duration_mode, "duration_days": None,
            "expires_on": None,
            "initial_attribute": int(initial_attribute),
            "started_attribute": int(initial_attribute),
            "target_attribute": target_attribute,
            "allow_ca_over_pa": bool(allow_ca_over_pa),
            "multiplier": resolved_multiplier,
            "required_points": required_points,
            "progress_points": carried_points,
            "last_progress_on": game_date,
            "training_content": str(attribute_key),
            **_training_archive_location(facility, product),
        }
        session = {
            "id": str(uuid.uuid4()), "at": _now(), "game_date": game_date,
            "facility_id": facility_id, "player_id": int(player_id),
            "player_name": player_name, "operation": "attribute_focus",
            "player_address": player_address,
            "player_team_id": int(player_team_id) if player_team_id else None,
            "player_team_name": player_team_name,
            "player_team_type": player_team_type,
            "attribute_key": attribute_key, "preferred_move_bit": None,
            "attribute_kind": resolved_kind, "direction": resolved_direction,
            "focus_id": focus["id"], "duration_mode": duration_mode,
            "allow_ca_over_pa": bool(allow_ca_over_pa),
            "multiplier": resolved_multiplier,
        }
        return _append_training_records(
            payload, focus, session, client_submission_id,
        )


def record_position_training_focus(
    facility_id: str, player_id: int, player_name: str, game_date: str,
    position_key: str, duration_mode: str, initial_position: int,
    client_submission_id: str | None = None, *, player_address: Any = None,
    player_team_id: int | None = None, player_team_name: str | None = None,
    player_team_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    position = str(position_key or "").upper()
    if position not in TRAINING_POSITION_NAMES:
        raise ValueError("所选训练位置无效")
    current = max(1, int(initial_position))
    target = 20 if current >= 20 else training_target_attribute(duration_mode, current)
    with _LOCK:
        payload = load_training_ground()
        if _expire_focuses(payload, game_date):
            save_training_ground(payload)
        _assert_player_available(payload, player_id)
        facility, product = _placed_facility_product(
            payload, facility_id, sku="position_training_area",
            missing_message="位置训练场当前不可用",
        )
        _assert_facility_capacity(payload, facility, product)
        required_points = position_training_points_required(current)
        previous = next((
            row for row in reversed(payload["focuses"])
            if row.get("status") == "cancelled"
            and row.get("focus_type") == "position"
            and int(row.get("player_id") or 0) == int(player_id)
            and str(row.get("position_key") or "") == position
            and int(row.get("initial_position") or 0) == current
        ), None)
        carried_points = (
            min(
                required_points - 1,
                max(0, int((previous or {}).get("progress_points") or 0)),
            )
            if required_points else 0
        )
        multiplier = max(1, int(
            product.get("training_multiplier") or POSITION_TRAINING_MULTIPLIER,
        ))
        focus = {
            "id": str(uuid.uuid4()), "at": _now(), "status": "active",
            "focus_type": "position", "facility_id": facility_id,
            "player_id": int(player_id), "player_name": str(player_name),
            "player_address": player_address,
            "player_team_id": int(player_team_id) if player_team_id else None,
            "player_team_name": player_team_name,
            "player_team_type": player_team_type,
            "position_key": position, "attribute_key": f"位置:{position}",
            "started_on": game_date, "duration_mode": duration_mode,
            "duration_days": None, "expires_on": None,
            "initial_position": current, "started_position": current,
            "target_position": target, "initial_attribute": current,
            "started_attribute": current, "target_attribute": target,
            "direction": 1, "multiplier": multiplier,
            "no_change": current >= 20,
            "required_points": required_points,
            "progress_points": carried_points,
            "last_progress_on": game_date,
            "training_content": f"{position} 位置熟练度",
            "archive_category": "equipment",
            **_training_archive_location(facility, product),
        }
        session = {
            "id": str(uuid.uuid4()), "at": _now(), "game_date": game_date,
            "facility_id": facility_id, "player_id": int(player_id),
            "player_name": str(player_name), "operation": "position_focus",
            "player_address": player_address,
            "player_team_id": int(player_team_id) if player_team_id else None,
            "player_team_name": player_team_name,
            "player_team_type": player_team_type,
            "position_key": position, "attribute_key": f"位置:{position}",
            "focus_id": focus["id"], "duration_mode": duration_mode,
            "multiplier": multiplier,
        }
        return _append_training_records(
            payload, focus, session, client_submission_id,
        )


def record_habit_training_focus(
    facility_id: str, player_id: int, player_name: str, game_date: str,
    preferred_move_bit: int, preferred_move_name: str, operation: str,
    client_submission_id: str | None = None,
    *, player_address: Any = None, player_team_id: int | None = None,
    player_team_name: str | None = None, player_team_type: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if operation not in {"learn", "unlearn"}:
        raise ValueError("请选择学习或戒除个人习惯")
    with _LOCK:
        payload = load_training_ground()
        if _expire_focuses(payload, game_date):
            save_training_ground(payload)
        _assert_player_available(payload, player_id)
        facility, product = _placed_facility_product(
            payload, facility_id, sku="habit_lab",
            missing_message="请先把习惯塑形机放置到场地",
        )
        _assert_facility_capacity(payload, facility, product)
        started = date.fromisoformat(game_date)
        expires_on = (started + timedelta(days=7)).isoformat()
        focus = {
            "id": str(uuid.uuid4()), "at": _now(), "status": "active",
            "focus_type": "preferred_move", "facility_id": facility_id,
            "player_id": int(player_id), "player_name": player_name,
            "player_address": player_address,
            "player_team_id": int(player_team_id) if player_team_id else None,
            "player_team_name": player_team_name,
            "player_team_type": player_team_type,
            "preferred_move_bit": int(preferred_move_bit),
            "preferred_move_name": preferred_move_name, "operation": operation,
            "started_on": game_date, "duration_mode": "week", "duration_days": 7,
            "expires_on": expires_on, "multiplier": 1,
            "training_content": (
                f"{'学习' if operation == 'learn' else '戒除'}个人习惯：{preferred_move_name}"
            ),
            **_training_archive_location(facility, product),
        }
        session = {
            "id": str(uuid.uuid4()), "at": _now(), "game_date": game_date,
            "facility_id": facility_id, "player_id": int(player_id),
            "player_name": player_name, "operation": operation,
            "player_address": player_address,
            "player_team_id": int(player_team_id) if player_team_id else None,
            "player_team_name": player_team_name,
            "player_team_type": player_team_type,
            "attribute_key": None, "preferred_move_bit": int(preferred_move_bit),
            "preferred_move_name": preferred_move_name, "focus_id": focus["id"],
            "duration_mode": "week",
        }
        return _append_training_records(
            payload, focus, session, client_submission_id,
        )


def _coaching_course_available(
    payload: dict[str, Any], facility_id: str, staff_id: int, game_date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    facility, product = _placed_facility_product(
        payload, facility_id, sku="coaching_desk",
        missing_message="请先把教练学习桌放置到3F办公室",
    )
    if not game_date:
        raise ValueError("尚未读取游戏日期")
    if any(
        row.get("status") == "active"
        and (
            (
                row.get("focus_type") == "coaching_license"
                and int(row.get("staff_id") or 0) == int(staff_id)
            )
            or (
                row.get("advisor_kind") == "staff"
                and int(row.get("advisor_id") or 0) == int(staff_id)
            )
        )
        for row in payload["focuses"]
    ):
        raise ValueError("该教练已有进行中的考证课程或综合训练指导任务")
    _assert_facility_capacity(
        payload, facility, product,
        message="该学习桌当前使用人数已满",
    )
    return dict(facility), dict(product)


def assert_coaching_course_available(
    facility_id: str, staff_id: int, game_date: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    with _LOCK:
        return _coaching_course_available(
            load_training_ground(), facility_id, staff_id, game_date,
        )


def record_coaching_license_focus(
    facility_id: str, staff_id: int, staff_name: str, staff_role: str,
    staff_address: Any, team_id: int, team_name: str, game_date: str,
    current_code: int, target_code: int,
    client_submission_id: str | None = None, subject_type: str = "staff",
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_code = int(current_code)
    target_code = int(target_code)
    if COACHING_LICENSE_NEXT.get(current_code) != target_code:
        raise ValueError("教练证书只能按顺序提升一级")
    if not game_date:
        raise ValueError("尚未读取游戏日期")
    duration_days = int(COACHING_LICENSE_COURSE_DAYS[current_code])
    due_on = (date.fromisoformat(game_date) + timedelta(days=duration_days)).isoformat()
    with _LOCK:
        payload = load_training_ground()
        facility, product = _coaching_course_available(
            payload, facility_id, int(staff_id), game_date,
        )
        focus = {
            "id": str(uuid.uuid4()), "at": _now(), "status": "active",
            "focus_type": "coaching_license", "facility_id": facility_id,
            "subject_type": (
                "player_manager" if subject_type == "player_manager" else "staff"
            ),
            "staff_id": int(staff_id), "staff_name": staff_name,
            "staff_role": staff_role, "staff_address": str(staff_address),
            "staff_team_id": int(team_id), "staff_team_name": team_name,
            "player_id": int(staff_id), "player_name": staff_name,
            "player_address": str(staff_address), "player_team_id": int(team_id),
            "player_team_name": team_name, "player_team_type": "club",
            "started_on": game_date, "duration_mode": "coaching_license",
            "duration_days": duration_days, "due_on": due_on, "expires_on": None,
            "initial_license_code": current_code,
            "target_license_code": target_code, "multiplier": 1,
            "training_content": "教练证书进修",
            **_training_archive_location(facility, product),
        }
        session = {
            "id": str(uuid.uuid4()), "at": _now(), "game_date": game_date,
            "facility_id": facility_id, "focus_id": focus["id"],
            "operation": "coaching_license", "staff_id": int(staff_id),
            "subject_type": (
                "player_manager" if subject_type == "player_manager" else "staff"
            ),
            "staff_name": staff_name, "staff_role": staff_role,
            "staff_address": str(staff_address), "team_id": int(team_id),
            "team_name": team_name, "initial_license_code": current_code,
            "target_license_code": target_code,
        }
        return _append_training_records(
            payload, focus, session, client_submission_id,
        )


def due_coaching_license_focuses(game_date: str) -> list[dict[str, Any]]:
    if not game_date:
        return []
    try:
        current = date.fromisoformat(game_date)
    except ValueError:
        return []
    with _LOCK:
        payload = load_training_ground()
        due: list[dict[str, Any]] = []
        for row in payload["focuses"]:
            if (
                row.get("status") != "active"
                or row.get("focus_type") != "coaching_license"
                or not row.get("due_on")
            ):
                continue
            try:
                is_due = date.fromisoformat(str(row["due_on"])) <= current
            except ValueError:
                is_due = False
            if is_due:
                due.append(dict(row))
        return due


def complete_coaching_license_focus(
    focus_id: str, game_date: str, result: dict[str, Any],
) -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="coaching_license",
            missing_message="教练考证任务已失效",
        )
        _complete_focus(focus, game_date, result)
        save_training_ground(payload)
        completed = dict(focus)
    _archive_completed_focus(completed)
    return completed


def find_training_submission(
    client_submission_id: str,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    submission_id = str(client_submission_id or "").strip()
    if not submission_id:
        return None
    with _LOCK:
        payload = load_training_ground()
        focus = next((
            row for row in reversed(payload["focuses"])
            if str(row.get("client_submission_id") or "") == submission_id
        ), None)
        if not focus:
            return None
        session = next((
            row for row in reversed(payload["sessions"])
            if str(row.get("client_submission_id") or "") == submission_id
        ), None)
        if not session:
            return None
        return dict(focus), dict(session)


def rebind_training_focus_player(focus_id: str, player_address: Any) -> dict[str, Any]:
    address = str(player_address or "").strip()
    if not address:
        raise ValueError("球员训练对象地址无效")
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, missing_message="训练任务已失效",
        )
        if str(focus.get("player_address") or "") == address:
            return dict(focus)
        focus["player_address"] = address
        for session in payload["sessions"]:
            if session.get("focus_id") == focus_id:
                session["player_address"] = address
        save_training_ground(payload)
        return dict(focus)


def rebind_training_focus_staff(focus_id: str, staff_address: Any) -> dict[str, Any]:
    address = str(staff_address or "").strip()
    if not address:
        raise ValueError("教练考证对象地址无效")
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="coaching_license",
            missing_message="教练考证任务已失效",
        )
        focus["staff_address"] = address
        focus["player_address"] = address
        for session in payload["sessions"]:
            if session.get("focus_id") == focus_id:
                session["staff_address"] = address
        save_training_ground(payload)
        return dict(focus)


def complete_habit_training_focus(
    focus_id: str, game_date: str, result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        focus = _active_focus(
            payload, focus_id, focus_type="preferred_move",
            missing_message="习惯训练任务已失效",
        )
        memory_result = {
            key: value for key, value in (result or {}).items()
            if key != "_original_mask"
        }
        _complete_focus(focus, game_date, memory_result)
        save_training_ground(payload)
        completed = dict(focus)
    _archive_completed_focus(completed)
    return completed


def rollback_training_focus(focus_id: str) -> None:
    with _LOCK:
        payload = load_training_ground()
        payload["focuses"] = [
            row for row in payload["focuses"] if row.get("id") != focus_id
        ]
        payload["sessions"] = [
            row for row in payload["sessions"] if row.get("focus_id") != focus_id
        ]
        save_training_ground(payload)


def cancel_training_focus(focus_id: str, game_date: str = "") -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        focus = next(
            (row for row in payload["focuses"] if row.get("id") == focus_id),
            None,
        )
        if not focus:
            raise ValueError("训练记录不存在")
        if focus.get("status") != "active":
            raise ValueError("该人员已不在使用此器材")
        focus["status"] = "cancelled"
        focus["cancelled_at"] = _now()
        focus["cancelled_on"] = game_date or None
        save_training_ground(payload)
        return dict(focus)


def record_training_session(
    facility_id: str, player_id: int, player_name: str, game_date: str,
    *, attribute_key: str | None = None, preferred_move_bit: int | None = None,
    operation: str = "attribute",
) -> dict[str, Any]:
    with _LOCK:
        payload = load_training_ground()
        row = {
            "id": str(uuid.uuid4()), "at": _now(), "game_date": game_date,
            "facility_id": facility_id, "player_id": int(player_id),
            "player_name": player_name, "operation": operation,
            "attribute_key": attribute_key, "preferred_move_bit": preferred_move_bit,
        }
        payload["sessions"].append(row)
        payload["sessions"] = payload["sessions"][-500:]
        save_training_ground(payload)
        return dict(row)
