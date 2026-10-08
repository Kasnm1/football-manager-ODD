"""Versioned, paged training archives with deterministic server-side routing."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import uuid4

from tools.account_store import load_document, update_document

DOCUMENT_KEY = "training_archive"
SCHEMA_VERSION = 4
DETAIL_LIMIT = 2000
PAGE_SIZE_DEFAULT = 24
PAGE_SIZE_MAX = 50
EVENT_TYPES = frozenset({"equipment", "coaching", "comprehensive"})
COMPREHENSIVE_SCENES = frozenset({"comprehensive_7f", "relationship_8f"})
COMPREHENSIVE_SKUS = frozenset({
    "mind_room", "mentoring_room", "tactical_room",
    "video_analysis_room", "sports_science_room",
})

EVENT_ROLE_LABELS = {
    "mentor": "指导者", "coach": "指导教练", "analyst": "视频分析师",
    "scientist": "运动科学职员", "student": "被指导者",
    "player": "被指导球员", "player_a": "训练球员",
    "player_b": "训练球员", "manager": "玩家经理",
    "activity_target": "活动对象", "counselor": "辅导者",
    "counselee": "被辅导者", "trainee": "被指导者",
}
RELATIONSHIP_SOURCE_LABELS = {
    "equipment": "基础训练", "coaching": "教练进修",
    "comprehensive": "综合训练", "mind_room": "修心室",
    "mentoring_room": "传习室", "tactical_room": "战术指导室",
    "video_analysis_room": "视频分析室",
    "sports_science_room": "运动科学室",
}
GUIDER_ROLES = frozenset({"mentor", "coach", "analyst", "scientist", "manager", "counselor", "player_a"})
TRAINEE_ROLES = frozenset({"student", "player", "player_b", "activity_target", "counselee", "trainee"})


def _new_archive() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION, "events": [],
        "totals": {kind: 0 for kind in EVENT_TYPES}, "trimmed": 0,
        "indexes": {
            "people": {}, "pairs": {}, "types": {},
            "relationships": _new_relationship_index(),
        },
    }


def _new_relationship_index() -> dict[str, Any]:
    return {
        "event_count": 0, "total_growth": 0, "shared_days": 0,
        "basic_changed_sessions": 0, "pairs": {}, "guidance_pairs": {},
        "role_categories": {
            "guider": {"change_count": 0, "total_change": 0, "people": {}},
            "trainee": {"change_count": 0, "total_change": 0, "people": {}},
            "peer": {"change_count": 0, "total_change": 0, "people": {}},
        },
    }


def event_role(person: dict[str, Any]) -> str:
    return str(person.get("event_role") or person.get("role") or "")


def event_role_label(person: dict[str, Any]) -> str:
    role = event_role(person)
    return str(person.get("event_role_label") or person.get("role_label") or EVENT_ROLE_LABELS.get(role) or "参与者")


def event_role_lane(person: dict[str, Any]) -> str:
    role = event_role(person)
    if role in GUIDER_ROLES:
        return "guider"
    if role in TRAINEE_ROLES:
        return "trainee"
    return "peer"


def _normalize_participant(person: dict[str, Any], *, default_role: str = "") -> dict[str, Any]:
    row = deepcopy(person)
    role = str(row.get("event_role") or row.get("role") or default_role)
    if role:
        row["event_role"] = role
        row["event_role_label"] = event_role_label({**row, "event_role": role})
    return row


def _relationship_people(event: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {
        int(row.get("id") or 0): deepcopy(row)
        for row in event.get("participants") or []
        if int(row.get("id") or 0) > 0
    }


def _legacy_relationship_facility(
    participant_map: dict[int, dict[str, Any]],
) -> str:
    """Recover room identity from role-rich archives written before source metadata."""
    roles = {event_role(row) for row in participant_map.values()}
    if {"coach", "player"} <= roles:
        return "tactical_room"
    if {"mentor", "student"} <= roles:
        return "mentoring_room"
    if {"analyst", "player"} <= roles:
        return "video_analysis_room"
    if {"scientist", "player"} <= roles:
        return "sports_science_room"
    if {"player_a", "player_b"} <= roles:
        return "mind_room"
    return ""


def _timeline_direction(
    direction: dict[str, Any], from_person: dict[str, Any],
    to_person: dict[str, Any], facility: str, fallback_reason: Any = 0,
) -> dict[str, Any]:
    row = deepcopy(direction)
    before = int(row.get("signed_before", row.get("before", 0)) or 0)
    after = int(row.get("signed_after", row.get("after", 0)) or 0)
    if int(row.get("reason") or 0) == 0 and int(fallback_reason or 0) > 0:
        row["reason"] = int(fallback_reason)
    from_role = event_role(from_person)
    to_role = event_role(to_person)
    if facility == "tactical_room":
        if from_role == "coach" and to_role == "player":
            row["reason"] = 7
        elif from_role == "player" and to_role == "coach":
            row["reason"] = 8
    elif facility in {"mind_room", "mentoring_room"}:
        row["reason"] = 5
    elif facility in {"video_analysis_room", "sports_science_room"}:
        row["reason"] = 10
    row["changed"] = bool(_relationship_delta(row))
    return row


def _relationship_training_sessions(scope_id: str | None) -> dict[str, dict[str, Any]]:
    """Index recoverable room sessions written by pre-v4 training archives."""
    document = load_document("relationship_training_rooms", {}, scope_id)
    sessions: dict[str, dict[str, Any]] = {}
    for room in document.get("rooms") or []:
        for field in ("pending_session", "last_session"):
            session = room.get(field) or {}
            if not isinstance(session, dict):
                continue
            for value in (session.get("id"), session.get("client_submission_id")):
                key = str(value or "").strip()
                if key:
                    sessions[key] = session
    return sessions


def _restore_relationship_training_details(
    event: dict[str, Any], sessions: dict[str, dict[str, Any]],
) -> None:
    details = event.setdefault("details", {})
    keys = (
        str(event.get("source_focus_id") or "").strip(),
        str(event.get("client_submission_id") or "").strip(),
    )
    session = next((sessions[key] for key in keys if key and key in sessions), None)
    if not session:
        return
    attributes = [str(value) for value in session.get("attributes") or [] if str(value)]
    if attributes and not details.get("attributes"):
        details["attributes"] = attributes
    if not details.get("training_content"):
        content = str(session.get("training_content") or "").strip()
        if not content and attributes:
            content = "、".join(attributes)
        if content and content != "关系协作训练":
            details["training_content"] = content
    details["facility_sku"] = details.get("facility_sku") or session.get("room_sku")
    details["facility_id"] = details.get("facility_id") or session.get("room_id")
    details["facility_name"] = details.get("facility_name") or session.get("facility_name")


def _relationship_growth(direction: dict[str, Any]) -> int:
    before = int(direction.get("signed_before", direction.get("before", 0)) or 0)
    after = int(direction.get("signed_after", direction.get("after", 0)) or 0)
    return max(0, abs(after) - abs(before))


def _relationship_delta(direction: dict[str, Any]) -> int:
    before = int(direction.get("signed_before", direction.get("before", 0)) or 0)
    after = int(direction.get("signed_after", direction.get("after", 0)) or 0)
    return abs(after - before)


def _index_role_direction(index: dict[str, Any], person: dict[str, Any], direction: dict[str, Any]) -> None:
    delta = _relationship_delta(direction)
    if not delta:
        return
    lane = event_role_lane(person)
    category = index.setdefault("role_categories", {}).setdefault(
        lane, {"change_count": 0, "total_change": 0, "people": {}},
    )
    category["change_count"] = int(category.get("change_count") or 0) + 1
    category["total_change"] = int(category.get("total_change") or 0) + delta
    person_id = int(person.get("id") or 0)
    if person_id:
        category.setdefault("people", {})[str(person_id)] = {
            "id": person_id, "name": str(person.get("name") or person_id),
            "kind": str(person.get("kind") or ""),
            "event_role": event_role(person),
            "event_role_label": event_role_label(person),
        }


def _index_relationship_event(index: dict[str, Any], event: dict[str, Any]) -> None:
    changes = list(event.get("relationship_changes") or [])
    if not changes:
        return
    participant_map = _relationship_people(event)
    indexed = False
    duration = _duration_days(event)
    facility = str((event.get("details") or {}).get("facility_sku") or "")
    for change in changes:
        left = deepcopy(change.get("person_a") or {})
        right = deepcopy(change.get("person_b") or {})
        left_id = int(left.get("id") or 0)
        right_id = int(right.get("id") or 0)
        if not left_id or not right_id or left_id == right_id:
            continue
        left = {**participant_map.get(left_id, {}), **left}
        right = {**participant_map.get(right_id, {}), **right}
        _index_role_direction(index, left, change.get("a_to_b") or {})
        _index_role_direction(index, right, change.get("b_to_a") or {})
        ordered_ids = sorted((left_id, right_id))
        pair_key = f"{ordered_ids[0]}:{ordered_ids[1]}"
        people = [left, right] if left_id == ordered_ids[0] else [right, left]
        growth = _relationship_growth(change.get("a_to_b") or {}) + _relationship_growth(
            change.get("b_to_a") or {}
        )
        pairs = index.setdefault("pairs", {})
        row = pairs.setdefault(pair_key, {
            "pair_key": pair_key, "people": people, "sessions": 0,
            "days": 0, "growth": 0, "latest_event": {},
        })
        row["people"] = people
        row["sessions"] = int(row.get("sessions") or 0) + 1
        row["days"] = int(row.get("days") or 0) + duration
        row["growth"] = int(row.get("growth") or 0) + growth
        row["latest_event"] = {
            "event_id": event.get("id"), "game_date": event.get("game_date_completed"),
            "facility_sku": facility, "a_to_b": deepcopy(change.get("a_to_b") or {}),
            "b_to_a": deepcopy(change.get("b_to_a") or {}),
        }
        lanes = {event_role_lane(left), event_role_lane(right)}
        kinds = {str(left.get("kind") or ""), str(right.get("kind") or "")}
        if lanes == {"guider", "trainee"} or kinds == {"staff", "player"}:
            guides = index.setdefault("guidance_pairs", {})
            guide = guides.setdefault(pair_key, {
                "pair_key": pair_key, "people": people, "sessions": 0,
                "days": 0, "growth": 0, "latest_event": {},
            })
            guide.update({"people": people, "latest_event": deepcopy(row["latest_event"])})
            guide["sessions"] = int(guide.get("sessions") or 0) + 1
            guide["days"] = int(guide.get("days") or 0) + duration
            guide["growth"] = int(guide.get("growth") or 0) + growth
        index["total_growth"] = int(index.get("total_growth") or 0) + growth
        index["shared_days"] = int(index.get("shared_days") or 0) + duration
        indexed = True
    if indexed:
        index["event_count"] = int(index.get("event_count") or 0) + 1
        if str(event.get("event_type") or "") == "equipment" and len(participant_map) > 1:
            index["basic_changed_sessions"] = int(index.get("basic_changed_sessions") or 0) + 1


def _rebuild_relationship_index(document: dict[str, Any]) -> dict[str, Any]:
    index = _new_relationship_index()
    for event in document.get("events") or []:
        if str(event.get("event_type") or "") in EVENT_TYPES:
            _index_relationship_event(index, event)
    document.setdefault("indexes", {})["relationships"] = index
    document["schema_version"] = SCHEMA_VERSION
    return index


def _ensure_relationship_index(
    document: dict[str, Any], scope_id: str | None,
) -> dict[str, Any]:
    relationships = (document.get("indexes") or {}).get("relationships")
    if isinstance(relationships, dict) and int(document.get("schema_version") or 0) >= SCHEMA_VERSION:
        return document

    def migrate(current: dict[str, Any]) -> dict[str, Any]:
        _rebuild_relationship_index(current)
        return deepcopy(current)

    return update_document(DOCUMENT_KEY, _new_archive(), migrate, scope_id)


def training_archive_category(focus: dict[str, Any]) -> str:
    focus_type = str(focus.get("focus_type") or "")
    scene = str(focus.get("scene") or "")
    sku = str(focus.get("facility_sku") or focus.get("sku") or focus.get("room_sku") or "")
    explicit = str(focus.get("archive_category") or "")
    if focus_type == "coaching_license":
        return "coaching"
    if focus_type == "relationship_training":
        return "comprehensive"
    if explicit:
        if explicit not in EVENT_TYPES:
            raise ValueError("训练设施声明了无效的档案类别")
        return explicit
    if scene in COMPREHENSIVE_SCENES or sku in COMPREHENSIVE_SKUS:
        return "comprehensive"
    known_scenes = {
        "outdoor", "indoor", "indoor_2f", "indoor_3f", "indoor_4f",
        "indoor_5f", "office_6f", "",
    }
    if scene in known_scenes:
        return "equipment"
    raise ValueError("新训练场景必须声明 archive_category")


def training_archive_event(focus: dict[str, Any], *, scope_id: str = "") -> dict[str, Any]:
    category = training_archive_category(focus)
    source_focus_id = str(focus.get("id") or focus.get("focus_id") or "")
    completion = str(focus.get("completed_on") or focus.get("game_date_completed") or "")
    submission = str(focus.get("client_submission_id") or "")
    if not source_focus_id and not submission:
        raise ValueError("训练完成事件缺少幂等标识")
    participants = [deepcopy(row) for row in focus.get("participants") or []]
    if not participants:
        participant_id = focus.get("staff_id") if category == "coaching" else focus.get("player_id")
        participant_name = focus.get("staff_name") if category == "coaching" else focus.get("player_name")
        if participant_id:
            participants = [{
                "id": int(participant_id), "name": str(participant_name or ""),
                "kind": "staff" if category == "coaching" else "player",
            }]
    default_role = "trainee" if category == "coaching" else ""
    participants = [_normalize_participant(row, default_role=default_role) for row in participants]
    mentor = deepcopy(focus.get("mentor") or {})
    mentor_id = int(mentor.get("id") or 0)
    if category == "coaching" and mentor_id and all(int(row.get("id") or 0) != mentor_id for row in participants):
        mentor.setdefault("kind", "staff")
        participants.append(_normalize_participant(mentor, default_role="mentor"))
    memory = dict(focus.get("memory_result") or {})
    changes = list(focus.get("attribute_changes") or [])
    if not changes and focus.get("attribute_key"):
        changes = [{
            "attribute": focus.get("attribute_key"),
            "before": focus.get("initial_attribute"),
            "after": focus.get("attribute_after", memory.get("attribute_after")),
        }]
    return {
        "schema_version": SCHEMA_VERSION, "id": str(uuid4()),
        "event_type": category, "source_focus_id": source_focus_id,
        "client_submission_id": submission, "completion_token": completion,
        "scope_id": str(scope_id or ""),
        "game_date_started": focus.get("started_on") or focus.get("started_date"),
        "game_date_completed": completion or None,
        "real_archived_at": datetime.now().isoformat(timespec="seconds"),
        "club": deepcopy(focus.get("club") or {}),
        "squad": deepcopy(focus.get("squad") or {}),
        "participants": participants, "attribute_changes": changes,
        "relationship_changes": list(focus.get("relationship_changes") or []),
        "details": {
            "scene": focus.get("scene"), "floor": focus.get("floor"),
            "facility_sku": focus.get("facility_sku") or focus.get("sku") or focus.get("room_sku"),
            "facility_id": focus.get("facility_id") or focus.get("room_id"),
            "facility_name": focus.get("facility_name"),
            "training_content": focus.get("training_content"),
            "attributes": list(focus.get("attributes") or []),
            "attribute_key": focus.get("attribute_key"),
            "preferred_move_name": focus.get("preferred_move_name"),
            "operation": focus.get("operation"),
            "initial_license_code": focus.get("initial_license_code"),
            "target_license_code": focus.get("target_license_code"),
            "focus_type": focus.get("focus_type"),
            "duration_mode": focus.get("duration_mode"),
            "training_multiplier": focus.get("training_multiplier"),
            "elapsed_days": focus.get("elapsed_days"),
            "focus_coefficient": focus.get("focus_coefficient"),
            "stage_count": focus.get("stage_count"),
            "standard_days": focus.get("standard_days") or focus.get("base_days"),
            "mentor": mentor,
            "reputation_change": focus.get("reputation_change") or focus.get("mentor_reputation_reward"),
            "memory_result": memory,
            "historical_compat": bool(focus.get("historical_compat")),
        },
    }


def _dedupe_key(event: dict[str, Any]) -> tuple[str, ...]:
    submission = str(event.get("client_submission_id") or "")
    if submission:
        return ("submission", submission)
    return (
        "focus", str(event.get("source_focus_id") or ""),
        str(event.get("completion_token") or event.get("game_date_completed") or ""),
    )


def append_training_archive_event(
    document: dict[str, Any], event: dict[str, Any],
) -> dict[str, Any]:
    """Append one event to an already-owned account document."""
    document.setdefault("schema_version", SCHEMA_VERSION)
    events = document.setdefault("events", [])
    totals = document.setdefault("totals", {kind: 0 for kind in EVENT_TYPES})
    indexes = document.setdefault("indexes", {})
    if (
        not isinstance(indexes.get("relationships"), dict)
        or int(document.get("schema_version") or 0) < SCHEMA_VERSION
    ):
        _rebuild_relationship_index(document)
        indexes = document.setdefault("indexes", {})
    people_index = indexes.setdefault("people", {})
    pairs_index = indexes.setdefault("pairs", {})
    types_index = indexes.setdefault("types", {})
    key = _dedupe_key(event)
    existing = next((row for row in events if _dedupe_key(row) == key), None)
    if existing:
        return {"event": deepcopy(existing), "created": False}
    events.append(deepcopy(event))
    event_type = str(event["event_type"])
    if event_type in EVENT_TYPES:
        totals[event_type] = int(totals.get(event_type) or 0) + 1
        type_key = str((event.get("details") or {}).get("facility_sku") or event_type)
        types_index[type_key] = int(types_index.get(type_key) or 0) + 1
        people = [
            str(int(row.get("id") or 0)) for row in event.get("participants") or []
            if int(row.get("id") or 0) > 0
        ]
        for person_id in people:
            people_index[person_id] = int(people_index.get(person_id) or 0) + 1
        for index, left in enumerate(people):
            for right in people[index + 1:]:
                pair_key = ":".join(sorted((left, right), key=int))
                pairs_index[pair_key] = int(pairs_index.get(pair_key) or 0) + 1
        _index_relationship_event(indexes["relationships"], event)
    if len(events) > DETAIL_LIMIT:
        removed = len(events) - DETAIL_LIMIT
        del events[:removed]
        document["trimmed"] = int(document.get("trimmed") or 0) + removed
    document["schema_version"] = SCHEMA_VERSION
    return {"event": deepcopy(event), "created": True}


def archive_training_focus(focus: dict[str, Any], scope_id: str | None = None) -> dict[str, Any]:
    event = training_archive_event(focus, scope_id=str(scope_id or ""))
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        return append_training_archive_event(document, event)
    return update_document(DOCUMENT_KEY, _new_archive(), mutate, scope_id)


def archive_relationship_change_event(
    *, scope_id: str | None, source_key: str, source_label: str,
    source_event_id: str, game_date: str, participants: list[dict[str, Any]],
    relationship_changes: list[dict[str, Any]], details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Archive one non-training FModd event only when it changed a relationship."""
    changed = []
    for change in relationship_changes:
        a_to_b = deepcopy(change.get("a_to_b") or {})
        b_to_a = deepcopy(change.get("b_to_a") or {})
        if not _relationship_delta(a_to_b) and not _relationship_delta(b_to_a):
            continue
        a_to_b["changed"] = bool(_relationship_delta(a_to_b))
        b_to_a["changed"] = bool(_relationship_delta(b_to_a))
        changed.append({**deepcopy(change), "a_to_b": a_to_b, "b_to_a": b_to_a})
    if not changed:
        return {"created": False, "event": None}
    event = {
        "schema_version": SCHEMA_VERSION, "id": str(uuid4()),
        "event_type": "relationship", "source_focus_id": str(source_event_id),
        "client_submission_id": f"relationship:{source_event_id}",
        "completion_token": str(game_date), "scope_id": str(scope_id or ""),
        "game_date_started": str(game_date), "game_date_completed": str(game_date),
        "real_archived_at": datetime.now().isoformat(timespec="seconds"),
        "club": {}, "squad": {},
        "participants": [_normalize_participant(row) for row in participants],
        "attribute_changes": [], "relationship_changes": changed,
        "details": {
            **deepcopy(details or {}), "source_key": str(source_key),
            "source_label": str(source_label), "focus_type": "relationship_event",
        },
    }
    def mutate(document: dict[str, Any]) -> dict[str, Any]:
        return append_training_archive_event(document, event)
    return update_document(DOCUMENT_KEY, _new_archive(), mutate, scope_id)


def training_archive_summary(scope_id: str | None = None) -> dict[str, Any]:
    document = load_document(DOCUMENT_KEY, _new_archive(), scope_id)
    document = _ensure_relationship_index(document, scope_id)
    events = _combined_archive_events(document, scope_id)
    totals = {
        kind: int((document.get("totals") or {}).get(kind) or 0)
        for kind in EVENT_TYPES
    }
    for event in events:
        if str(event.get("id") or "").startswith("legacy:"):
            kind = str(event.get("event_type") or "equipment")
            totals[kind] = totals.get(kind, 0) + 1
    recent = [{key: value for key, value in row.items() if key != "details"}
              for row in events[-5:]][::-1]
    relationship_index = deepcopy((document.get("indexes") or {}).get("relationships") or {})
    best_partners = sorted(
        (relationship_index.get("pairs") or {}).values(),
        key=lambda row: (
            -int(row.get("sessions") or 0), -int(row.get("days") or 0),
            -int(row.get("growth") or 0), str(row.get("pair_key") or ""),
        ),
    )[:5]
    guidance_pairs = sorted(
        (relationship_index.get("guidance_pairs") or {}).values(),
        key=lambda row: (
            -int(row.get("sessions") or 0), -int(row.get("growth") or 0),
            str(row.get("pair_key") or ""),
        ),
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "totals": totals,
        "retained": len(events), "trimmed": int(document.get("trimmed") or 0),
        "recent": recent, "indexes": deepcopy(document.get("indexes") or {}),
        "relationship_archive": {
            "event_count": int(relationship_index.get("event_count") or 0),
            "total_growth": int(relationship_index.get("total_growth") or 0),
            "shared_days": int(relationship_index.get("shared_days") or 0),
            "basic_changed_sessions": int(
                relationship_index.get("basic_changed_sessions") or 0
            ),
            "best_partners": deepcopy(best_partners),
            "most_guidance_pair": deepcopy(guidance_pairs[0]) if guidance_pairs else None,
            "role_categories": [{
                "key": key,
                "label": {"guider": "指导者", "trainee": "被指导者", "peer": "同伴协作"}[key],
                "change_count": int(row.get("change_count") or 0),
                "total_change": int(row.get("total_change") or 0),
                "people": list((row.get("people") or {}).values()),
            } for key, row in (relationship_index.get("role_categories") or {}).items()],
        },
    }


def _legacy_archive_events(scope_id: str | None) -> list[dict[str, Any]]:
    ground = load_document("training_ground", {}, scope_id)
    rows = list(ground.get("archived_focuses") or []) if isinstance(ground, dict) else []
    events: list[dict[str, Any]] = []
    for index, source in enumerate(rows[-DETAIL_LIMIT:]):
        if not isinstance(source, dict):
            continue
        focus = deepcopy(source)
        focus["historical_compat"] = True
        focus.setdefault("id", f"legacy-{index}")
        try:
            training_archive_category(focus)
        except ValueError:
            focus["archive_category"] = "equipment"
        try:
            event = training_archive_event(focus, scope_id=str(scope_id or ""))
        except ValueError:
            continue
        completion = str(event.get("completion_token") or "unknown")
        event["id"] = f"legacy:{event['source_focus_id']}:{completion}"
        event["real_archived_at"] = str(
            focus.get("completed_at") or focus.get("archived_at") or ""
        )
        events.append(event)
    return events


def _combined_archive_events(
    document: dict[str, Any], scope_id: str | None,
) -> list[dict[str, Any]]:
    current = [deepcopy(row) for row in document.get("events") or []]
    known = {_dedupe_key(row) for row in current}
    for event in _legacy_archive_events(scope_id):
        if _dedupe_key(event) not in known:
            current.append(event)
            known.add(_dedupe_key(event))
    return current


def _duration_days(event: dict[str, Any]) -> int:
    try:
        started = datetime.fromisoformat(str(event.get("game_date_started") or "")).date()
        completed = datetime.fromisoformat(str(event.get("game_date_completed") or "")).date()
    except ValueError:
        return 0
    return max(0, (completed - started).days)


def _archive_statistics(rows: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
    if event_type == "equipment":
        facilities: dict[str, dict[str, int]] = {}
        people: dict[str, dict[str, int]] = {}
        for event in rows:
            sku = str((event.get("details") or {}).get("facility_sku") or "historical")
            facility = facilities.setdefault(sku, {"sessions": 0, "changes": 0, "days": 0})
            facility["sessions"] += 1
            facility["changes"] += len(event.get("attribute_changes") or [])
            facility["days"] += _duration_days(event)
            for person in event.get("participants") or []:
                key = str(int(person.get("id") or 0))
                row = people.setdefault(key, {"sessions": 0, "changes": 0, "days": 0})
                row["sessions"] += 1; row["changes"] += len(event.get("attribute_changes") or [])
                row["days"] += _duration_days(event)
        return {"by_facility": facilities, "by_person": people}
    if event_type == "coaching":
        desks: dict[str, dict[str, Any]] = {}
        for event in rows:
            details = event.get("details") or {}
            facility_id = str(details.get("facility_id") or "historical")
            sku = str(details.get("facility_sku") or "coaching_desk")
            desk = desks.setdefault(facility_id, {
                "facility_id": facility_id,
                "facility_sku": sku,
                "facility_name": str(details.get("facility_name") or ""),
                "sessions": 0,
                "days": 0,
                "people": set(),
            })
            desk["sessions"] += 1
            desk["days"] += _duration_days(event)
            desk["people"].update(
                int(person.get("id") or 0)
                for person in event.get("participants") or []
                if int(person.get("id") or 0) > 0
            )
        desk_rows = {
            key: {**row, "people": len(row["people"])}
            for key, row in desks.items()
        }
        return {"by_desk": desk_rows, "timeline": [{
            "event_id": event.get("id"), "participants": event.get("participants") or [],
            "standard_days": (event.get("details") or {}).get("standard_days"),
            "actual_days": _duration_days(event),
            "mentor": (event.get("details") or {}).get("mentor"),
            "reputation_change": (event.get("details") or {}).get("reputation_change"),
        } for event in rows]}
    rooms: dict[str, dict[str, Any]] = {}
    pairs: dict[str, dict[str, Any]] = {}
    for event in rows:
        details = event.get("details") or {}
        sku = str(details.get("facility_sku") or "historical")
        room = rooms.setdefault(sku, {
            "facility_sku": sku,
            "facility_name": str(details.get("facility_name") or ""),
            "sessions": 0,
            "days": 0,
            "people": set(),
            "instances": set(),
        })
        room["sessions"] += 1
        room["days"] += _duration_days(event)
        facility_id = str(details.get("facility_id") or "")
        if facility_id:
            room["instances"].add(facility_id)
        participants = list(event.get("participants") or [])
        room["people"].update(
            int(person.get("id") or 0)
            for person in participants
            if int(person.get("id") or 0) > 0
        )
        for index, left in enumerate(participants):
            for right in participants[index + 1:]:
                ids = sorted((int(left.get("id") or 0), int(right.get("id") or 0)))
                if not all(ids):
                    continue
                key = f"{ids[0]}:{ids[1]}"
                row = pairs.setdefault(key, {
                    "pair_key": key, "sessions": 0, "days": 0,
                    "people": [left.get("name"), right.get("name")],
                })
                row["sessions"] += 1; row["days"] += _duration_days(event)
    best = sorted(pairs.values(), key=lambda row: (-row["sessions"], -row["days"], row["pair_key"]))[:10]
    room_rows = {
        key: {
            **row,
            "people": len(row["people"]),
            "instances": len(row["instances"]),
        }
        for key, row in rooms.items()
    }
    return {"by_room": room_rows, "pairs": pairs, "best_partners": best}


def training_archive_page(payload: dict[str, Any], scope_id: str | None = None) -> dict[str, Any]:
    event_type = str(payload.get("event_type") or "equipment")
    if event_type not in EVENT_TYPES:
        raise ValueError("训练档案类型无效")
    page_size = max(1, min(PAGE_SIZE_MAX, int(payload.get("page_size") or PAGE_SIZE_DEFAULT)))
    page = max(1, int(payload.get("page") or 1))
    person_id = int(payload.get("person_id") or 0)
    document = load_document(DOCUMENT_KEY, _new_archive(), scope_id)
    rows = [row for row in reversed(_combined_archive_events(document, scope_id)) if row.get("event_type") == event_type]
    if person_id:
        rows = [row for row in rows if any(int(person.get("id") or 0) == person_id for person in row.get("participants") or [])]
    total = len(rows); pages = max(1, (total + page_size - 1) // page_size); page = min(page, pages)
    start = (page - 1) * page_size
    if event_type == "comprehensive":
        sessions = _relationship_training_sessions(scope_id)
        for event in rows:
            details = event.setdefault("details", {})
            _restore_relationship_training_details(event, sessions)
            if not details.get("facility_sku"):
                facility = _legacy_relationship_facility(_relationship_people(event))
                if facility:
                    details["facility_sku"] = facility
                    details["facility_name"] = RELATIONSHIP_SOURCE_LABELS.get(facility, facility)
            if details.get("facility_sku") in COMPREHENSIVE_SKUS:
                details["floor"] = details.get("floor") or "8F"
    page_rows = deepcopy(rows[start:start + page_size])
    for event in page_rows:
        event.setdefault("details", {})["actual_days"] = _duration_days(event)
    return {"event_type": event_type, "events": page_rows,
            "page": page, "page_size": page_size, "pages": pages, "total": total,
            "statistics": _archive_statistics(rows, event_type)}


def relationship_timeline(
    payload: dict[str, Any], scope_id: str | None = None,
) -> dict[str, Any]:
    """Return only relationship changes caused and archived by FModd."""
    page_size = max(1, min(PAGE_SIZE_MAX, int(payload.get("page_size") or 20)))
    page = max(1, int(payload.get("page") or 1))
    person_id = int(payload.get("person_id") or 0)
    other_id = int(payload.get("other_id") or 0)
    source = str(payload.get("source") or "").strip()
    document = load_document(DOCUMENT_KEY, _new_archive(), scope_id)
    document = _ensure_relationship_index(document, scope_id)
    rows: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    for event in reversed(_combined_archive_events(document, scope_id)):
        details = event.get("details") or {}
        participant_map = _relationship_people(event)
        facility = str(details.get("facility_sku") or "")
        if not facility and str(event.get("event_type") or "") == "comprehensive":
            facility = _legacy_relationship_facility(participant_map)
        source_key = str(details.get("source_key") or facility or event.get("event_type") or "unknown")
        source_label = str(details.get("source_label") or "")
        if not source_label:
            source_label = RELATIONSHIP_SOURCE_LABELS.get(
                facility or source_key, source_key,
            )
        actual_changes = [change for change in event.get("relationship_changes") or [] if (
            _relationship_delta(change.get("a_to_b") or {}) or _relationship_delta(change.get("b_to_a") or {})
        )]
        if actual_changes:
            source_row = sources.setdefault(source_key, {"key": source_key, "label": source_label, "count": 0})
            if facility:
                source_row["facility_sku"] = facility
            source_row["count"] = int(source_row.get("count") or 0) + len(actual_changes)
        if source and source_key != source:
            continue
        for change_index, change in enumerate(actual_changes):
            left = deepcopy(change.get("person_a") or {})
            right = deepcopy(change.get("person_b") or {})
            left_id = int(left.get("id") or 0)
            right_id = int(right.get("id") or 0)
            if not left_id or not right_id:
                continue
            left = {**participant_map.get(left_id, {}), **left}
            right = {**participant_map.get(right_id, {}), **right}
            ids = {left_id, right_id}
            if person_id and person_id not in ids:
                continue
            if other_id and other_id not in ids:
                continue
            rows.append({
                "id": f"{event.get('id')}:{change_index}",
                "event_id": event.get("id"),
                "game_date": event.get("game_date_completed"),
                "real_archived_at": event.get("real_archived_at"),
                "source": {
                    "event_type": event.get("event_type"),
                    "scene": details.get("scene"), "facility_sku": facility,
                    "facility_id": details.get("facility_id"),
                    "source_focus_id": event.get("source_focus_id"),
                    "key": source_key, "label": source_label,
                    "outcome": details.get("outcome"),
                },
                "person_a": {**left, "event_role": event_role(left), "event_role_label": event_role_label(left)},
                "person_b": {**right, "event_role": event_role(right), "event_role_label": event_role_label(right)},
                "a_to_b": _timeline_direction(
                    change.get("a_to_b") or {}, left, right, facility,
                    change.get("reason_a2b"),
                ),
                "b_to_a": _timeline_direction(
                    change.get("b_to_a") or {}, right, left, facility,
                    change.get("reason_b2a"),
                ),
            })
    total = len(rows)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "events": rows[start:start + page_size], "total": total,
        "page": page, "page_size": page_size, "pages": pages,
        "source_of_truth": "fmodd_training_archive",
        "sources": sorted(sources.values(), key=lambda row: (-int(row.get("count") or 0), str(row.get("label") or ""))),
    }


def training_archive_mode(payload: dict[str, Any], scope_id: str | None = None) -> dict[str, Any]:
    """Validate a requested archive tab and return its first page."""
    return training_archive_page({
        "event_type": payload.get("event_type"), "page": 1,
        "page_size": payload.get("page_size", PAGE_SIZE_DEFAULT),
        "person_id": payload.get("person_id", 0),
    }, scope_id)


def training_archive_batch(payload: dict[str, Any], scope_id: str | None = None) -> dict[str, Any]:
    ids = [str(value) for value in payload.get("event_ids") or []]
    if not ids or len(ids) > 100:
        raise ValueError("一次可读取 1 到 100 条训练档案")
    wanted = set(ids)
    document = load_document(DOCUMENT_KEY, _new_archive(), scope_id)
    by_id = {str(row.get("id") or ""): row for row in _combined_archive_events(document, scope_id)}
    events = [deepcopy(by_id[value]) for value in ids if value in by_id]
    return {"events": events, "requested": len(ids), "resolved": len(events)}
