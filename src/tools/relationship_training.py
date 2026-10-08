from __future__ import annotations

import threading
import uuid
import hashlib
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from tools.account_store import load_document, update_document, update_documents
from tools.app_settings import load_settings, local_purchase_price
from tools.training_archive import (
    _new_archive, append_training_archive_event, training_archive_event,
)


SCENE_KEY = "relationship_8f"
ROOM_LIMIT = 15
_LOCK = threading.RLock()
INTIMACY_PER_DAY = 0.33
SPORTS_SCIENCE_JOB_TYPES = {40, 48}
ANALYSIS_JOB_TYPES = {58, 60, 62}
COACHING_JOB_TYPES = {2, 16, 20, 22, 26, 34}
RELATION_REASON_MULTIPLIERS = {
    8: 1.0, 11: 1.0, 5: 1.0, 6: 1.5, 7: 1.5, 12: 1.5,
    10: 1.25, 4: 1.25, 1: 1.75, 2: 1.75, 3: 1.75, 9: 2.0,
}
LICENSE_FM_TO_TIER = {0: 1, 7: 2, 6: 3, 5: 4, 4: 5, 3: 6, 2: 7, 1: 8}


def validate_relationship_training_staff(
    room_sku: str, role: str, person: dict[str, Any],
) -> None:
    if role not in {"coach", "analyst", "scientist"}:
        return
    job_type = int(person.get("job_type") if person.get("job_type") is not None else -1)
    if room_sku == "tactical_room":
        license_code = int((person.get("coaching_license") or {}).get("code") or 0)
        if job_type not in COACHING_JOB_TYPES or license_code == 0:
            raise ValueError("战术指导室导师只能选择教练团队的持证职员")
    elif room_sku == "video_analysis_room" and job_type not in ANALYSIS_JOB_TYPES:
        raise ValueError("视频分析室只能选择分析团队职员")
    elif room_sku == "sports_science_room" and job_type not in SPORTS_SCIENCE_JOB_TYPES:
        raise ValueError("运动科学室只能选择运动科学团队职员")


def relationship_training_guidance_allowed(
    mentor: dict[str, Any], student: dict[str, Any],
) -> bool:
    """Return whether a mentor may guide a student within the managed team tree."""
    mentor_team_id = int(mentor.get("team_id") or 0)
    student_team_id = int(student.get("team_id") or 0)
    mentor_team_type = str(mentor.get("team_type") or "club")
    student_team_type = str(student.get("team_type") or "club")
    if (
        not mentor_team_id or not student_team_id
        or mentor_team_id != student_team_id
        or mentor_team_type != student_team_type
    ):
        return False

    mentor_squad_id = int(mentor.get("squad_team_id") or mentor_team_id)
    student_squad_id = int(student.get("squad_team_id") or student_team_id)
    if mentor_squad_id == student_squad_id:
        return True

    mentor_squad_type = int(mentor.get("squad_type_code") or 0)
    mentor_is_senior = mentor_squad_id == mentor_team_id or mentor_squad_type == 0
    student_is_subordinate = student_squad_id != student_team_id
    return mentor_is_senior and student_is_subordinate


def validate_relationship_training_guidance(
    room_sku: str, participants: list[dict[str, Any]],
) -> None:
    role_pairs = {
        "mentoring_room": ("mentor", "student"),
        "tactical_room": ("coach", "player"),
        "video_analysis_room": ("analyst", "player"),
        "sports_science_room": ("scientist", "player"),
    }
    guide_role, student_role = role_pairs.get(room_sku, (None, None))
    if not guide_role:
        return
    mentors = [row for row in participants if row.get("role") == guide_role]
    students = [row for row in participants if row.get("role") == student_role]
    for mentor in mentors:
        for student in students:
            if relationship_training_guidance_allowed(mentor, student):
                continue
            mentor_squad = str(mentor.get("squad_label") or "未识别梯队")
            student_squad = str(student.get("squad_label") or "未识别梯队")
            raise ValueError(
                f"{mentor.get('name') or '导师'}（{mentor_squad}）不能指导"
                f"{student.get('name') or '学员'}（{student_squad}）："
                "指导只能发生在同一主体队伍；一线队可向下指导青训梯队，"
                "青训球员只能指导本梯队球员"
            )


ROOMS: dict[str, dict[str, Any]] = {
    "mind_room": {
        "name": "修心室", "scene": SCENE_KEY, "unlock_cost": 5_000_000.0,
        "icon": "heart-handshake", "available": True,
        "description": "通过专注训练与心理调适，系统提升球员的心理素质与职业心态。",
        "participant_label": "单人 / 双人（可选伙伴）",
        "reason_a2b": 5, "reason_b2a": 5,
        "roles": [
            {"key": "player_a", "label": "球员 A", "kind": "player", "min": 1, "max": 1},
            {"key": "player_b", "label": "球员 B", "kind": "player", "min": 0, "max": 1},
        ],
        "scope_attributes": [
            {"key": "hidden:适应性", "direction": "inc", "kind": "hidden"},
            {"key": "hidden:抗压能力", "direction": "inc", "kind": "hidden"},
            {"key": "hidden:情绪控制", "direction": "inc", "kind": "hidden"},
            {"key": "hidden:体育精神", "direction": "inc", "kind": "hidden"},
            {"key": "精神:意志力", "direction": "inc", "kind": "visible"},
            {"key": "hidden:争论", "direction": "dec", "kind": "hidden"},
            {"key": "hidden:肮脏动作", "direction": "dec", "kind": "hidden"},
        ],
    },
    "mentoring_room": {
        "name": "传习室", "scene": SCENE_KEY, "unlock_cost": 20_000_000.0,
        "icon": "users-round", "available": True,
        "description": "由资深球员担任导师，面向同队学员开展能力传承与职业经验指导。",
        "participant_label": "1 导师 + 1~4 学员",
        "reason_a2b": 5, "reason_b2a": 5,
        "roles": [
            {"key": "mentor", "label": "导师", "kind": "player", "min": 1, "max": 1},
            {"key": "student", "label": "学员", "kind": "player", "min": 1, "max": 4},
        ],
        "scope_attributes": [
            *[
                {"key": f"hidden:{name}", "direction": "inc", "kind": "hidden"}
                for name in ("适应性", "雄心", "忠诚", "抗压能力", "职业素养", "体育精神", "情绪控制")
            ],
            {"key": "精神:领导力", "direction": "inc", "kind": "visible"},
            {"key": "精神:意志力", "direction": "inc", "kind": "visible"},
            {"key": "hidden:争论", "direction": "dec", "kind": "hidden"},
            {"key": "hidden:肮脏动作", "direction": "dec", "kind": "hidden"},
            {"key": "hidden:受伤倾向", "direction": "dec", "kind": "hidden"},
        ],
    },
    "tactical_room": {
        "name": "战术指导室", "scene": SCENE_KEY, "unlock_cost": 15_000_000.0,
        "icon": "clipboard-list", "available": True,
        "description": "由教练团队开展专项战术辅导，提升球员的比赛理解、适应能力与稳定表现。",
        "participant_label": "1 教练 + 1~3 球员",
        "reason_a2b": 7, "reason_b2a": 8,
        "roles": [
            {"key": "coach", "label": "教练", "kind": "staff", "min": 1, "max": 1},
            {"key": "player", "label": "球员", "kind": "player", "min": 1, "max": 3},
        ],
        "scope_attributes": [
            {"key": f"hidden:{name}", "direction": "inc", "kind": "hidden"}
            for name in ("适应性", "雄心", "忠诚", "职业素养", "稳定性", "大赛发挥", "多面性")
        ] + [{"key": "精神:团队合作", "direction": "inc", "kind": "visible"}],
    },
    "video_analysis_room": {
        "name": "视频分析室", "scene": SCENE_KEY, "unlock_cost": 25_000_000.0,
        "icon": "video", "available": True,
        "description": "由分析团队结合比赛录像开展针对性复盘，强化球员的决策、预判与空间理解。",
        "participant_label": "1 分析师 + 1~3 球员",
        "reason_a2b": 10, "reason_b2a": 10,
        "roles": [
            {"key": "analyst", "label": "分析师", "kind": "staff", "min": 1, "max": 1},
            {"key": "player", "label": "球员", "kind": "player", "min": 1, "max": 3},
        ],
        "scope_attributes": [
            {"key": key, "direction": "inc", "kind": "visible"} for key in (
                "精神:集中", "精神:决断", "精神:预判", "精神:视野",
                "精神:镇定", "精神:防守站位", "精神:无球跑动", "精神:团队合作",
            )
        ] + [
            {"key": "hidden:稳定性", "direction": "inc", "kind": "hidden"},
            {"key": "hidden:大赛发挥", "direction": "inc", "kind": "hidden"},
        ],
    },
    "sports_science_room": {
        "name": "运动科学室", "scene": SCENE_KEY, "unlock_cost": 30_000_000.0,
        "icon": "activity", "available": True,
        "description": "由运动科学团队制定个体化体能方案，改善身体素质、负荷管理与伤病风险。",
        "participant_label": "1 运动科学职员 + 1~3 球员",
        "reason_a2b": 10, "reason_b2a": 10,
        "roles": [
            {"key": "scientist", "label": "运动科学职员", "kind": "staff", "min": 1, "max": 1},
            {"key": "player", "label": "球员", "kind": "player", "min": 1, "max": 3},
        ],
        "scope_attributes": [
            {"key": "精神:勇敢", "direction": "inc", "kind": "visible"},
            {"key": "身体:体质", "direction": "inc", "kind": "visible"},
            {"key": "physical:height", "direction": "inc", "kind": "height"},
            {"key": "hidden:受伤倾向", "direction": "dec", "kind": "hidden"},
        ],
    },
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": 2, "scene": SCENE_KEY, "rooms": [], "submissions": [],
        "attribute_progress": {}, "intimacy_progress": {}, "height_baselines": {},
    }


def _normalize_state(value: Any) -> dict[str, Any]:
    state = value if isinstance(value, dict) else _new_state()
    state["schema_version"] = 2
    state["scene"] = SCENE_KEY
    state.setdefault("rooms", [])
    state.setdefault("submissions", [])
    state.setdefault("attribute_progress", {})
    state.setdefault("intimacy_progress", {})
    state.setdefault("height_baselines", {})
    state["rooms"] = [
        row for row in state["rooms"]
        if isinstance(row, dict) and row.get("sku") in ROOMS
    ]
    state["submissions"] = list(state["submissions"])[-500:]
    state["attribute_progress"] = dict(state["attribute_progress"])
    state["intimacy_progress"] = dict(state["intimacy_progress"])
    state["height_baselines"] = {
        str(key): int(value) for key, value in dict(state["height_baselines"]).items()
        if 100 <= int(value) <= 250
    }
    return state


def compute_team_capabilities(staff: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project the progress-document team scores from one cached staff roster."""
    sports: list[float] = []
    analysis: list[float] = []
    for row in staff:
        job_type = int(row.get("job_type") if row.get("job_type") is not None else -1)
        abilities = row.get("abilities") or {}
        if job_type in SPORTS_SCIENCE_JOB_TYPES:
            sports.append(float(abilities.get("运动科学") or 5))
        if job_type in ANALYSIS_JOB_TYPES:
            analysis.append(
                float(abilities.get("数据分析") or 5) * 0.50
                + float(abilities.get("判断球员能力") or 5) * 0.20
                + float(abilities.get("判断球员潜力") or 5) * 0.15
                + float(abilities.get("战术知识") or 5) * 0.15
            )

    def score(values: list[float]) -> dict[str, Any]:
        capability = sum(values) / len(values) if values else 5.0
        return {
            "cap": round(capability, 2),
            "bonus": round((capability - 10.0) / 20.0, 4),
            "members": len(values),
        }

    return {"sports_science": score(sports), "analysis": score(analysis)}


def sports_science_height_limit(baseline_height: int) -> int:
    baseline = int(baseline_height)
    return baseline + (25 if baseline < 175 else 20 if baseline < 185 else 10 if baseline < 200 else 5)


def sports_science_height_probability(
    age: int, height_cm: int, sports_bonus: float, intimacy_bonus: float,
) -> float:
    if int(age) > 24:
        return 0.0
    height_factor = max(0.30, min(2.00, 1.0 + (190 - int(height_cm)) / 30.0))
    return max(0.0, min(1.0,
        0.03 * (25 - int(age)) * height_factor
        * (1.0 + float(sports_bonus)) * (1.0 + float(intimacy_bonus))
    ))


def deterministic_settlement_roll(session_id: str, player_id: int, purpose: str) -> float:
    digest = hashlib.sha256(
        f"{session_id}:{int(player_id)}:{purpose}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def relationship_intimacy_bonus(detail: dict[str, Any] | None) -> float:
    if not detail or int(detail.get("relation_type", 1)) != 1:
        return 0.0
    level = max(0, min(100, int(detail.get("level") or 0)))
    multiplier = RELATION_REASON_MULTIPLIERS.get(int(detail.get("reason") or 0), 1.0)
    return (level / 100.0) * multiplier


def coach_composite_multiplier(license_code: int, current_reputation: int) -> float:
    license_tier = LICENSE_FM_TO_TIER.get(int(license_code), 1)
    reputation = max(0, min(10_000, int(current_reputation)))
    reputation_tier = next((
        tier for maximum, tier in (
            (1_000, 1), (2_500, 2), (5_000, 3),
            (6_500, 4), (8_000, 5), (10_000, 6),
        ) if reputation <= maximum
    ), 1)
    return round((1.0 + (license_tier - 4) * 0.10) * (1.0 + (reputation_tier - 1) * 0.10), 4)


def directed_progress_key(person_from: dict[str, Any], person_to: dict[str, Any]) -> str:
    return f"{person_from.get('kind')}:{int(person_from.get('id') or 0)}>{person_to.get('kind')}:{int(person_to.get('id') or 0)}"


def attribute_progress_key(room_sku: str, person: dict[str, Any], attribute_key: str) -> str:
    return f"{room_sku}:{person.get('kind')}:{int(person.get('id') or 0)}:{attribute_key}"


def relationship_training_progress() -> dict[str, Any]:
    state = load_relationship_training_rooms()
    return {
        "attribute_progress": {
            str(key): float(value)
            for key, value in state.get("attribute_progress", {}).items()
        },
        "intimacy_progress": {
            str(key): float(value)
            for key, value in state.get("intimacy_progress", {}).items()
        },
        "height_baselines": dict(state.get("height_baselines") or {}),
    }


def estimate_player_status_tier(player: dict[str, Any]) -> int:
    attributes = player.get("attributes") or {}
    leadership = int((attributes.get("精神") or {}).get("领导力") or 0)
    ca = int(player.get("ca") or 0)
    age = int(player.get("age") or 0)
    reputation = max(
        int(player.get("home_reputation") or 0),
        int(player.get("current_reputation") or 0),
        int(player.get("world_reputation") or 0),
    )
    if ca >= 160 and age >= 28 and leadership >= 16 or reputation >= 8_000:
        return 0
    if ca >= 145 or age >= 29 and leadership >= 14 or reputation >= 6_500:
        return 1
    if ca >= 125 or leadership >= 12 or reputation >= 4_000:
        return 2
    return 3


def load_relationship_training_rooms() -> dict[str, Any]:
    with _LOCK:
        return _normalize_state(load_document("relationship_training_rooms", None))


def relationship_training_catalog() -> dict[str, Any]:
    state = load_relationship_training_rooms()
    settings = load_settings()
    instances = [dict(row) for row in state["rooms"]]
    rows = []
    for sku, definition in ROOMS.items():
        owned = [row for row in instances if row.get("sku") == sku]
        rows.append({
            "sku": sku, **definition,
            "unlock_cost": local_purchase_price(definition["unlock_cost"], settings),
            "instances": owned, "unlock_count": len(owned),
            "idle_count": sum(row.get("status") == "idle" for row in owned),
        })
    return {
        "scene": SCENE_KEY, "floor_limit": ROOM_LIMIT,
        "unlocked_total": len(instances), "rooms": rows,
    }


def unlock_relationship_training_room(room_sku: str) -> dict[str, Any]:
    sku = str(room_sku or "")
    definition = ROOMS.get(sku)
    if not definition:
        raise ValueError("房间不存在")
    if not definition.get("available", False):
        raise ValueError(str(definition.get("unavailable_reason") or "该房间暂未开放"))

    def mutate(raw: Any) -> dict[str, Any]:
        state = _normalize_state(raw)
        if len(state["rooms"]) >= ROOM_LIMIT:
            raise ValueError(f"关系综训楼 8F 已达到 {ROOM_LIMIT} 间上限")
        instance = {
            "id": str(uuid.uuid4()), "sku": sku, "scene": SCENE_KEY,
            "status": "idle", "unlocked_at": _now(), "participants": [],
            "attributes": [], "started_on": None, "entered_at": None,
            "client_submission_id": None,
        }
        state["rooms"].append(instance)
        return dict(instance)

    with _LOCK:
        return update_document("relationship_training_rooms", _new_state(), mutate)


def _normalized_participants(
    definition: dict[str, Any], participants: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    allowed_roles = {str(row["key"]): row for row in definition["roles"]}
    by_role: dict[str, list[dict[str, Any]]] = {key: [] for key in allowed_roles}
    seen: set[tuple[str, int]] = set()
    for source in participants:
        if not isinstance(source, dict):
            raise ValueError("参与者信息无效")
        role = str(source.get("role") or "")
        role_definition = allowed_roles.get(role)
        if not role_definition:
            raise ValueError("参与者角色无效")
        kind = str(source.get("kind") or "")
        person_id = int(source.get("id") or 0)
        identity = (kind, person_id)
        if kind != role_definition["kind"] or person_id <= 0 or not source.get("name"):
            raise ValueError("参与者信息不完整")
        if identity in seen:
            raise ValueError("同一人不能重复参与")
        seen.add(identity)
        by_role[role].append(source)
    normalized: list[dict[str, Any]] = []
    for role, role_definition in allowed_roles.items():
        selected = by_role[role]
        minimum = int(role_definition.get("min") or 0)
        maximum = int(role_definition.get("max") or 1)
        if not minimum <= len(selected) <= maximum:
            raise ValueError(
                f"「{role_definition['label']}」需要 {minimum} 至 {maximum} 人"
            )
        for source in selected:
            normalized.append({
                "role": role, "role_label": str(role_definition["label"]),
                "kind": str(source["kind"]), "id": int(source["id"]),
                "name": str(source["name"]),
                "person_key": str(source.get("person_key") or ""),
                "address": source.get("address"),
                "team_id": int(source.get("team_id") or 0) or None,
                "team_name": source.get("team_name"),
            })
    return normalized


def enter_relationship_training_room(
    room_id: str, participants: list[dict[str, Any]], attributes: list[str],
    game_date: str, *, client_submission_id: str = "",
) -> dict[str, Any]:
    submission_id = str(client_submission_id or "").strip()
    if len(submission_id) > 128:
        raise ValueError("训练请求编号过长")

    def mutate(raw: Any) -> dict[str, Any]:
        state = _normalize_state(raw)
        if submission_id:
            prior = next((
                row for row in state["rooms"]
                if row.get("client_submission_id") == submission_id
            ), None)
            if prior:
                return dict(prior)
        room = next((row for row in state["rooms"] if row.get("id") == room_id), None)
        if not room:
            raise ValueError("房间实例不存在")
        if room.get("status") != "idle":
            raise ValueError("该房间正在被使用")
        definition = ROOMS[str(room["sku"])]
        if not definition.get("available", False):
            raise ValueError(str(definition.get("unavailable_reason") or "该房间暂未开放"))
        normalized = _normalized_participants(definition, participants)
        occupied = {
            (str(person.get("kind")), int(person.get("id") or 0))
            for other in state["rooms"] if other.get("status") == "occupied"
            for person in other.get("participants") or []
        }
        if any((row["kind"], row["id"]) in occupied for row in normalized):
            raise ValueError("参与者已在其他 8F 综训房间中")
        scope = {str(row["key"]) for row in definition["scope_attributes"]}
        chosen = list(dict.fromkeys(str(key) for key in attributes if str(key) in scope))
        if not chosen:
            raise ValueError("请至少选择一项训练属性")
        room.update({
            "status": "occupied", "participants": normalized,
            "attributes": chosen, "started_on": str(game_date or "") or None,
            "entered_at": _now(), "client_submission_id": submission_id or None,
        })
        if submission_id:
            state["submissions"].append(submission_id)
            state["submissions"] = state["submissions"][-500:]
        return dict(room)

    with _LOCK:
        return update_document("relationship_training_rooms", _new_state(), mutate)


def release_relationship_training_room(room_id: str, game_date: str) -> dict[str, Any]:
    def mutate(raw: Any) -> dict[str, Any]:
        state = _normalize_state(raw)
        room = next((row for row in state["rooms"] if row.get("id") == room_id), None)
        if not room:
            raise ValueError("房间实例不存在")
        if room.get("status") != "occupied":
            raise ValueError("该房间当前未被占用")
        session = {
            "id": str(uuid.uuid4()), "focus_type": "relationship_training",
            "scene": SCENE_KEY, "room_id": str(room["id"]), "room_sku": str(room["sku"]),
            "facility_name": str(ROOMS[str(room["sku"])]["name"]),
            "participants": [dict(row) for row in room.get("participants") or []],
            "attributes": list(room.get("attributes") or []),
            "training_content": "、".join(str(value) for value in room.get("attributes") or [])
            or "未记录具体训练属性",
            "started_on": room.get("started_on"), "completed_on": str(game_date or ""),
            "completed_at": _now(), "status": "pending_settlement",
            "client_submission_id": room.get("client_submission_id"),
            "relationship_rules": [
                {
                    "from_role": str(ROOMS[str(room["sku"])]["roles"][0]["key"]),
                    "to_role": str(ROOMS[str(room["sku"])]["roles"][-1]["key"]),
                    "default_reason": int(ROOMS[str(room["sku"])]["reason_a2b"]),
                },
                {
                    "from_role": str(ROOMS[str(room["sku"])]["roles"][-1]["key"]),
                    "to_role": str(ROOMS[str(room["sku"])]["roles"][0]["key"]),
                    "default_reason": int(ROOMS[str(room["sku"])]["reason_b2a"]),
                },
            ],
        }
        room.update({
            "status": "settling", "pending_session": session,
            "settlement_started_at": _now(),
        })
        return session

    with _LOCK:
        return update_document("relationship_training_rooms", _new_state(), mutate)


def relationship_training_effect_plan(session: dict[str, Any]) -> dict[str, Any]:
    """Build the deterministic, address-free settlement plan for one session."""
    sku = str(session.get("room_sku") or "")
    definition = ROOMS.get(sku)
    if not definition or not definition.get("available", False):
        raise ValueError("综训房间结算类型无效")
    try:
        started = date.fromisoformat(str(session.get("started_on") or ""))
        completed = date.fromisoformat(str(session.get("completed_on") or ""))
    except ValueError as error:
        raise ValueError("综训记录缺少有效的训练日期") from error
    elapsed = max(0, (completed - started).days)
    participants = [dict(row) for row in session.get("participants") or []]
    attributes = list(dict.fromkeys(str(key) for key in session.get("attributes") or []))
    scope = {
        str(row["key"]): dict(row)
        for row in definition.get("scope_attributes") or []
    }
    if set(attributes) - set(scope):
        raise ValueError("综训属性作用域已变化")
    beneficiary_roles = {
        str(row["key"]) for row in definition.get("roles") or []
        if row.get("kind") == "player"
    }
    if sku == "mentoring_room":
        beneficiary_roles.discard("mentor")
    beneficiaries = [row for row in participants if row.get("role") in beneficiary_roles]
    guiders = [row for row in participants if row.get("role") not in beneficiary_roles]
    selected_count = len(attributes)
    scope_count = len(scope)
    focus_coefficient = (
        max(0.5, 1.0 - 0.5 * (selected_count - 1) / max(1, scope_count - 1))
        if scope_count > 1 else 1.0
    )
    attribute_points = max(0.0, 3.0 * elapsed * focus_coefficient)
    pairs: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    def add_pair(left: dict[str, Any], right: dict[str, Any], reason_a: int, reason_b: int) -> None:
        left_key = f"{left.get('kind')}:{int(left.get('id') or 0)}"
        right_key = f"{right.get('kind')}:{int(right.get('id') or 0)}"
        pair_key = tuple(sorted((left_key, right_key)))
        if left_key == right_key or pair_key in seen_pairs:
            return
        seen_pairs.add(pair_key)
        pairs.append({
            "person_a": deepcopy(left), "person_b": deepcopy(right),
            "reason_a2b": int(reason_a), "reason_b2a": int(reason_b),
            "base_increment": round(INTIMACY_PER_DAY * elapsed, 6),
        })

    for guider in guiders:
        for beneficiary in beneficiaries:
            add_pair(
                guider, beneficiary,
                int(definition.get("reason_a2b") or 5),
                int(definition.get("reason_b2a") or 5),
            )
    if sku == "mind_room":
        for index, left in enumerate(beneficiaries):
            for right in beneficiaries[index + 1:]:
                add_pair(left, right, 5, 5)
    return {
        "elapsed_days": elapsed,
        "focus_coefficient": round(focus_coefficient, 4),
        "attribute_points": round(attribute_points, 4),
        "attributes": [deepcopy(scope[key]) for key in attributes],
        "beneficiaries": beneficiaries, "guiders": guiders, "pairs": pairs,
    }


def finalize_relationship_training_room(
    room_id: str, session_id: str, settlement: dict[str, Any],
) -> dict[str, Any]:
    def mutate(raw: Any) -> dict[str, Any]:
        state = _normalize_state(raw)
        room = next((row for row in state["rooms"] if row.get("id") == room_id), None)
        pending = (room or {}).get("pending_session") or {}
        if not room or room.get("status") != "settling" or pending.get("id") != session_id:
            raise ValueError("综训结算状态已失效")
        completed = {**pending, "status": "completed", "effects": dict(settlement)}
        room.update({
            "status": "idle", "participants": [], "attributes": [],
            "started_on": None, "entered_at": None, "client_submission_id": None,
            "pending_session": None, "settlement_started_at": None,
            "last_session": completed,
        })
        return completed

    with _LOCK:
        return update_document("relationship_training_rooms", _new_state(), mutate)


def commit_relationship_training_settlement(
    room_id: str, session_id: str, settlement: dict[str, Any], *, scope_id: str,
    attribute_progress: dict[str, float] | None = None,
    intimacy_progress: dict[str, float] | None = None,
    height_baselines: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Commit room completion and its archive event in one account write."""
    def mutate(documents: dict[str, Any]) -> dict[str, Any]:
        rooms = _normalize_state(documents["relationship_training_rooms"])
        room = next((row for row in rooms["rooms"] if row.get("id") == room_id), None)
        pending = (room or {}).get("pending_session") or {}
        if not room or room.get("status") != "settling" or pending.get("id") != session_id:
            raise ValueError("综训结算状态已失效")
        completed = {**deepcopy(pending), "status": "completed", **deepcopy(settlement)}
        event = training_archive_event(completed, scope_id=scope_id)
        archived = append_training_archive_event(documents["training_archive"], event)
        if attribute_progress:
            rooms["attribute_progress"].update({
                str(key): round(max(0.0, float(value)), 6)
                for key, value in attribute_progress.items()
            })
        if intimacy_progress:
            rooms["intimacy_progress"].update({
                str(key): round(max(0.0, float(value)), 6)
                for key, value in intimacy_progress.items()
            })
        if height_baselines:
            rooms["height_baselines"].update({
                str(key): int(value) for key, value in height_baselines.items()
            })
        room.update({
            "status": "idle", "participants": [], "attributes": [],
            "started_on": None, "entered_at": None, "client_submission_id": None,
            "pending_session": None, "settlement_started_at": None,
            "last_session": completed,
        })
        return {"completed": completed, "archive": archived}

    with _LOCK:
        return update_documents({
            "relationship_training_rooms": _new_state(),
            "training_archive": _new_archive(),
        }, mutate, scope_id)


def restore_relationship_training_room(room_id: str) -> dict[str, Any]:
    def mutate(raw: Any) -> dict[str, Any]:
        state = _normalize_state(raw)
        room = next((row for row in state["rooms"] if row.get("id") == room_id), None)
        if not room or room.get("status") != "settling":
            raise ValueError("综训结算状态已失效")
        room["status"] = "occupied"
        room.pop("pending_session", None)
        room.pop("settlement_started_at", None)
        return dict(room)

    with _LOCK:
        return update_document("relationship_training_rooms", _new_state(), mutate)
