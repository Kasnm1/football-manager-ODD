from tools import training_archive


def test_archive_routing_prioritizes_training_type_then_scene():
    assert training_archive.training_archive_category({"focus_type":"coaching_license","scene":"relationship_8f"}) == "coaching"
    assert training_archive.training_archive_category({"focus_type":"relationship_training","scene":"indoor"}) == "comprehensive"
    assert training_archive.training_archive_category({"focus_type":"attribute","scene":"comprehensive_7f"}) == "comprehensive"
    assert training_archive.training_archive_category({"focus_type":"attribute","room_sku":"mind_room"}) == "comprehensive"
    assert training_archive.training_archive_category({"focus_type":"attribute","scene":"office_6f"}) == "equipment"


def test_unknown_future_scene_requires_explicit_category():
    try:
        training_archive.training_archive_category({"scene":"future_9f"})
    except ValueError as error:
        assert "archive_category" in str(error)
    else:
        raise AssertionError("future scene was silently classified")


def test_archive_is_idempotent_and_keeps_irreversible_totals(monkeypatch):
    document = training_archive._new_archive()
    def update(_key, _default, mutator, _scope):
        return mutator(document)
    monkeypatch.setattr(training_archive, "update_document", update)
    focus = {"id":"focus-1","client_submission_id":"request-1","focus_type":"attribute","scene":"indoor","player_id":7,"player_name":"A","completed_on":"2028-01-02"}
    assert training_archive.archive_training_focus(focus)["created"] is True
    assert training_archive.archive_training_focus(focus)["created"] is False
    assert len(document["events"]) == 1
    assert document["totals"]["equipment"] == 1


def test_archive_page_is_server_side_and_type_specific(monkeypatch):
    events = [{"id":str(i),"event_type":"equipment","participants":[{"id":7}]} for i in range(30)]
    events.append({"id":"coach","event_type":"coaching","participants":[{"id":9}]})
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: {"events":events})
    result = training_archive.training_archive_page({"event_type":"equipment","page":2,"page_size":20,"person_id":7})
    assert result["total"] == 30
    assert len(result["events"]) == 10
    assert all(row["event_type"] == "equipment" for row in result["events"])


def test_archive_page_projects_single_session_duration(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "focus-duration", "focus_type": "attribute",
        "started_on": "2026-08-01", "completed_on": "2026-08-09",
        "player_id": 10, "player_name": "球员 A",
    })
    training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(
        training_archive, "load_document",
        lambda key, *_args, **_kwargs: document if key == "training_archive" else {},
    )

    result = training_archive.training_archive_page({"event_type": "equipment"})

    assert result["events"][0]["details"]["actual_days"] == 8


def test_legacy_archived_focuses_are_projected_without_mutation(monkeypatch):
    legacy = {
        "archived_focuses": [{
            "id": "old-1", "focus_type": "attribute", "scene": "indoor",
            "player_id": 7, "player_name": "A", "started_on": "2026-01-01",
            "completed_on": "2026-01-04", "attribute_key": "精神:意志力",
            "initial_attribute": 10, "attribute_after": 11,
        }],
    }
    archive = training_archive._new_archive()
    monkeypatch.setattr(
        training_archive, "load_document",
        lambda key, *_args, **_kwargs: legacy if key == "training_ground" else archive,
    )
    result = training_archive.training_archive_page({"event_type": "equipment"})
    assert result["total"] == 1
    assert result["events"][0]["details"]["historical_compat"] is True
    assert result["statistics"]["by_person"]["7"]["days"] == 3
    assert "schema_version" not in legacy


def test_irreversible_indexes_survive_detail_trimming(monkeypatch):
    document = training_archive._new_archive()
    monkeypatch.setattr(training_archive, "DETAIL_LIMIT", 1)
    for identifier in (1, 2):
        event = training_archive.training_archive_event({
            "id": f"focus-{identifier}", "focus_type": "relationship_training",
            "scene": "relationship_8f", "completed_on": f"2026-01-0{identifier}",
            "participants": [
                {"id": 10, "name": "A"}, {"id": 20, "name": "B"},
            ],
        })
        training_archive.append_training_archive_event(document, event)
    assert len(document["events"]) == 1
    assert document["trimmed"] == 1
    assert document["indexes"]["people"]["10"] == 2
    assert document["indexes"]["pairs"]["10:20"] == 2


def test_partial_legacy_indexes_are_upgraded_before_append():
    document = {
        "events": [], "totals": {}, "trimmed": 0,
        "indexes": {"people": {"10": 4}},
    }
    event = training_archive.training_archive_event({
        "id": "focus-new", "focus_type": "relationship_training",
        "scene": "relationship_8f", "completed_on": "2026-08-20",
        "participants": [{"id": 10}, {"id": 20}],
    })
    training_archive.append_training_archive_event(document, event)
    assert document["indexes"]["people"]["10"] == 5
    assert document["indexes"]["pairs"]["10:20"] == 1
    assert document["indexes"]["types"]["comprehensive"] == 1


def test_comprehensive_archive_keeps_actual_room_and_effect_metadata():
    event = training_archive.training_archive_event({
        "id": "focus-room", "focus_type": "relationship_training",
        "scene": "relationship_8f", "room_sku": "tactical_room",
        "completed_on": "2026-08-21", "elapsed_days": 6,
        "focus_coefficient": 0.75,
        "participants": [{"id": 10, "name": "A"}],
    })
    assert event["event_type"] == "comprehensive"
    assert event["details"]["facility_sku"] == "tactical_room"
    assert event["details"]["elapsed_days"] == 6
    assert event["details"]["focus_coefficient"] == 0.75


def test_comprehensive_page_recovers_room_for_role_rich_legacy_event(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "legacy-peer-room", "focus_type": "relationship_training",
        "scene": "relationship_8f", "completed_on": "2026-08-21",
        "participants": [
            {"id": 10, "name": "A", "kind": "player", "role": "player_a"},
            {"id": 20, "name": "B", "kind": "player", "role": "player_b"},
        ],
    })
    event["details"].pop("facility_sku", None)
    training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)

    result = training_archive.training_archive_page({"event_type": "comprehensive"})

    details = result["events"][0]["details"]
    assert details["facility_sku"] == "mind_room"
    assert details["facility_name"] == "修心室"
    assert details["floor"] == "8F"
    assert "facility_sku" not in document["events"][0]["details"]


def test_comprehensive_statistics_group_real_usage_by_room_category(monkeypatch):
    document = training_archive._new_archive()
    for identifier, sku, room_id, person_id in (
        ("a", "mind_room", "mind-1", 10),
        ("b", "mind_room", "mind-2", 20),
        ("c", "tactical_room", "tactical-1", 10),
    ):
        event = training_archive.training_archive_event({
            "id": identifier, "focus_type": "relationship_training",
            "scene": "relationship_8f", "room_sku": sku, "room_id": room_id,
            "facility_name": {"mind_room": "修心室", "tactical_room": "战术指导室"}[sku],
            "started_on": "2026-08-20", "completed_on": "2026-08-22",
            "participants": [{"id": person_id, "name": str(person_id), "kind": "player"}],
        })
        training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)

    statistics = training_archive.training_archive_page(
        {"event_type": "comprehensive"}, "scope-1",
    )["statistics"]

    assert statistics["by_room"]["mind_room"] == {
        "facility_sku": "mind_room", "facility_name": "修心室",
        "sessions": 2, "days": 4, "people": 2, "instances": 2,
    }
    assert statistics["by_room"]["tactical_room"]["sessions"] == 1


def test_coaching_statistics_group_real_usage_by_desk_instance(monkeypatch):
    document = training_archive._new_archive()
    for identifier, desk_id, staff_id in (
        ("a", "desk-1", 10), ("b", "desk-1", 20), ("c", "desk-2", 10),
    ):
        event = training_archive.training_archive_event({
            "id": identifier, "focus_type": "coaching_license",
            "facility_sku": "coaching_desk", "facility_id": desk_id,
            "facility_name": "教练学习桌",
            "started_on": "2026-08-20", "completed_on": "2026-08-23",
            "staff_id": staff_id, "staff_name": str(staff_id),
        })
        training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)

    statistics = training_archive.training_archive_page(
        {"event_type": "coaching"}, "scope-1",
    )["statistics"]

    assert statistics["by_desk"]["desk-1"] == {
        "facility_id": "desk-1", "facility_sku": "coaching_desk",
        "facility_name": "教练学习桌", "sessions": 2, "days": 6, "people": 2,
    }
    assert statistics["by_desk"]["desk-2"]["people"] == 1


def test_comprehensive_page_recovers_exact_attributes_from_legacy_room_session(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "session-mind", "focus_type": "relationship_training",
        "scene": "relationship_8f", "completed_on": "2026-08-21",
        "participants": [
            {"id": 10, "name": "A", "kind": "player", "role": "player_a"},
            {"id": 20, "name": "B", "kind": "player", "role": "player_b"},
        ],
    })
    event["source_focus_id"] = "session-mind"
    event["details"].pop("facility_sku", None)
    event["details"].pop("attributes", None)
    event["details"].pop("training_content", None)
    training_archive.append_training_archive_event(document, event)
    rooms = {"rooms": [{"last_session": {
        "id": "session-mind", "room_id": "mind-1", "room_sku": "mind_room",
        "facility_name": "修心室", "attributes": ["hidden:适应性"],
        "training_content": "hidden:适应性",
    }}]}
    monkeypatch.setattr(
        training_archive, "load_document",
        lambda key, *_args, **_kwargs: rooms if key == "relationship_training_rooms" else document,
    )

    result = training_archive.training_archive_page({"event_type": "comprehensive"})

    details = result["events"][0]["details"]
    assert details["facility_sku"] == "mind_room"
    assert details["attributes"] == ["hidden:适应性"]
    assert details["training_content"] == "hidden:适应性"
    assert "attributes" not in document["events"][0]["details"]


def test_archive_event_keeps_training_location_and_content():
    event = training_archive.training_archive_event({
        "id": "focus-location", "focus_type": "attribute",
        "scene": "indoor_2f", "facility_id": "facility-1",
        "facility_sku": "treadmill", "facility_name": "专业跑步机",
        "floor": "2F", "training_content": "身体：速度",
        "attribute_key": "身体：速度", "completed_on": "2026-08-22",
        "player_id": 10, "player_name": "球员 A",
    })
    details = event["details"]
    assert details["facility_id"] == "facility-1"
    assert details["facility_sku"] == "treadmill"
    assert details["facility_name"] == "专业跑步机"
    assert details["floor"] == "2F"
    assert details["training_content"] == "身体：速度"


def test_relationship_archive_is_incremental_and_timeline_is_directional(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "focus-rel", "focus_type": "relationship_training",
        "scene": "relationship_8f", "room_sku": "tactical_room",
        "started_on": "2026-08-01", "completed_on": "2026-08-07",
        "participants": [
            {"id": 10, "name": "教练", "kind": "staff"},
            {"id": 20, "name": "球员", "kind": "player"},
        ],
        "relationship_changes": [{
            "person_a": {"id": 10, "name": "教练", "kind": "staff"},
            "person_b": {"id": 20, "name": "球员", "kind": "player"},
            "a_to_b": {"signed_before": 0, "signed_after": 6, "reason": 7},
            "b_to_a": {"signed_before": -3, "signed_after": -8, "reason": 9},
        }],
    })
    training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(
        training_archive, "load_document",
        lambda key, *_args, **_kwargs: document if key == "training_archive" else {},
    )
    summary = training_archive.training_archive_summary("scope-1")
    archive = summary["relationship_archive"]
    assert archive["event_count"] == 1
    assert archive["total_growth"] == 11
    assert archive["shared_days"] == 6
    assert archive["best_partners"][0]["people"][0]["name"] == "教练"
    assert archive["most_guidance_pair"]["sessions"] == 1

    timeline = training_archive.relationship_timeline({
        "person_id": 10, "other_id": 20, "source": "tactical_room",
    }, "scope-1")
    assert timeline["source_of_truth"] == "fmodd_training_archive"
    assert timeline["total"] == 1
    assert timeline["events"][0]["a_to_b"]["reason"] == 7
    assert timeline["events"][0]["b_to_a"]["signed_after"] == -8
    assert timeline["events"][0]["person_a"]["kind"] == "staff"
    assert timeline["events"][0]["person_b"]["kind"] == "player"


def test_timeline_recovers_source_and_categories_for_legacy_guidance_event(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "legacy-tactical", "focus_type": "relationship_training",
        "scene": "relationship_8f", "completed_on": "2026-08-20",
        "participants": [
            {"id": 10, "name": "教练", "kind": "staff", "role": "coach"},
            {"id": 20, "name": "球员", "kind": "player", "role": "player"},
        ],
        "relationship_changes": [{
            "person_a": {"id": 10, "name": "教练", "kind": "staff"},
            "person_b": {"id": 20, "name": "球员", "kind": "player"},
            "a_to_b": {"signed_before": 0, "signed_after": 3},
            "b_to_a": {"signed_before": 0, "signed_after": 2},
        }],
    })
    event["details"].pop("facility_sku", None)
    training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)

    timeline = training_archive.relationship_timeline({}, "scope-1")

    assert timeline["sources"] == [{
        "key": "tactical_room", "label": "战术指导室", "count": 1,
        "facility_sku": "tactical_room",
    }]
    row = timeline["events"][0]
    assert row["source"]["facility_sku"] == "tactical_room"
    assert row["a_to_b"]["reason"] == 7
    assert row["b_to_a"]["reason"] == 8


def test_timeline_uses_direction_reasons_saved_on_legacy_pair(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "legacy-existing-relation", "focus_type": "relationship_training",
        "scene": "relationship_8f", "room_sku": "mentoring_room",
        "completed_on": "2026-08-21",
        "participants": [
            {"id": 10, "name": "导师", "kind": "player", "role": "mentor"},
            {"id": 20, "name": "学员", "kind": "player", "role": "student"},
        ],
        "relationship_changes": [{
            "person_a": {"id": 10, "name": "导师", "kind": "player"},
            "person_b": {"id": 20, "name": "学员", "kind": "player"},
            "reason_a2b": 5, "reason_b2a": 5,
            "a_to_b": {"signed_before": 30, "signed_after": 33},
            "b_to_a": {"signed_before": 20, "signed_after": 23},
        }],
    })
    training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)

    row = training_archive.relationship_timeline({}, "scope-1")["events"][0]

    assert row["a_to_b"]["reason"] == 5
    assert row["b_to_a"]["reason"] == 5


def test_basic_relationship_summary_counts_only_changed_multi_person_sessions():
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "focus-basic", "focus_type": "attribute", "scene": "indoor",
        "started_on": "2026-08-01", "completed_on": "2026-08-02",
        "participants": [{"id": 1}, {"id": 2}],
        "relationship_changes": [{
            "person_a": {"id": 1}, "person_b": {"id": 2},
            "a_to_b": {"before": 0, "after": 1},
            "b_to_a": {"before": 0, "after": 1},
        }],
    })
    training_archive.append_training_archive_event(document, event)
    assert document["indexes"]["relationships"]["basic_changed_sessions"] == 1


def test_non_training_relationship_events_only_enter_timeline(monkeypatch):
    document = training_archive._new_archive()

    def update(_key, _default, mutator, _scope):
        return mutator(document)

    monkeypatch.setattr(training_archive, "update_document", update)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)
    participants = [
        {"id": 10, "name": "玩家", "kind": "staff", "event_role": "manager"},
        {"id": 20, "name": "球员", "kind": "player", "event_role": "activity_target"},
    ]
    unchanged = [{
        "person_a": participants[0], "person_b": participants[1],
        "a_to_b": {"signed_before": 4, "signed_after": 4},
        "b_to_a": {"signed_before": 8, "signed_after": 8},
    }]
    assert training_archive.archive_relationship_change_event(
        scope_id="scope-1", source_key="activity:dinner",
        source_label="活动中心 · 聚餐", source_event_id="activity-no-change",
        game_date="2026-08-20", participants=participants,
        relationship_changes=unchanged,
    )["created"] is False
    assert document["events"] == []

    changed = [{
        "person_a": participants[0], "person_b": participants[1],
        "a_to_b": {"signed_before": 4, "signed_after": 7, "reason": 6},
        "b_to_a": {"signed_before": 8, "signed_after": 8, "reason": 6},
    }]
    assert training_archive.archive_relationship_change_event(
        scope_id="scope-1", source_key="activity:dinner",
        source_label="活动中心 · 聚餐", source_event_id="activity-changed",
        game_date="2026-08-21", participants=participants,
        relationship_changes=changed, details={"outcome": "relieved"},
    )["created"] is True
    assert document["totals"] == {kind: 0 for kind in training_archive.EVENT_TYPES}
    assert document["indexes"]["relationships"]["event_count"] == 0

    timeline = training_archive.relationship_timeline({}, "scope-1")
    assert timeline["total"] == 1
    assert timeline["sources"] == [{
        "key": "activity:dinner", "label": "活动中心 · 聚餐", "count": 1,
    }]
    event = timeline["events"][0]
    assert event["source"]["label"] == "活动中心 · 聚餐"
    assert event["source"]["outcome"] == "relieved"
    assert event["person_a"]["event_role_label"] == "玩家经理"
    assert event["person_b"]["event_role_label"] == "活动对象"
    assert event["a_to_b"]["changed"] is True
    assert event["b_to_a"]["changed"] is False


def test_relationship_summary_groups_guiders_and_trainees(monkeypatch):
    document = training_archive._new_archive()
    event = training_archive.training_archive_event({
        "id": "focus-guidance", "focus_type": "relationship_training",
        "scene": "relationship_8f", "room_sku": "tactical_room",
        "started_on": "2026-08-20", "completed_on": "2026-08-21",
        "participants": [
            {"id": 10, "name": "教练", "kind": "staff", "role": "coach"},
            {"id": 20, "name": "球员", "kind": "player", "role": "player"},
        ],
        "relationship_changes": [{
            "person_a": {"id": 10, "name": "教练", "kind": "staff"},
            "person_b": {"id": 20, "name": "球员", "kind": "player"},
            "a_to_b": {"signed_before": 0, "signed_after": 5},
            "b_to_a": {"signed_before": 0, "signed_after": 3},
        }],
    })
    training_archive.append_training_archive_event(document, event)
    monkeypatch.setattr(training_archive, "load_document", lambda *_args, **_kwargs: document)

    summary = training_archive.training_archive_summary("scope-1")
    categories = {
        row["key"]: row for row in summary["relationship_archive"]["role_categories"]
    }
    assert categories["guider"]["change_count"] == 1
    assert categories["guider"]["people"][0]["event_role_label"] == "指导教练"
    assert categories["trainee"]["change_count"] == 1
    assert categories["trainee"]["people"][0]["event_role_label"] == "被指导球员"
