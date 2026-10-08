from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.relationship_network import (
    enrich_relationship_page, page_relationship_people, relationship_scopes,
    resolve_relationship_entities, select_relationship_rows,
)


def profile():
    return {
        "team": {"id": 10, "name": "主队", "team_type": "club"},
        "players": [{"id": 1, "name": "Alpha"}, {"id": 2, "name": "Beta"}],
        "staff": [{"id": 3, "name": "Coach"}],
        "squads": [{
            "team_id": 11, "label": "U21", "player_count": 1,
            "players": [{"id": 4, "name": "Youth"}],
        }],
    }


def test_scopes_are_metadata_only_and_do_not_expand_acquired_clubs():
    acquired = [{"team_id": 20, "team_name": "集团队", "available": True}]
    result = relationship_scopes([profile()], acquired, data_version=7)
    assert result["data_version"] == 7
    assert result["managed"][0]["squads"] == [
        {"team_id": 11, "label": "U21", "person_count": 1},
    ]
    assert "players" not in result["managed"][0]
    assert result["acquired"][0]["team_id"] == 20


def test_youth_scope_is_selected_without_merging_first_team():
    rows, source = select_relationship_rows(profile(), {
        "scope_kind": "youth", "team_id": 10, "squad_team_id": 11,
        "person_kind": "player",
    })
    assert [row["id"] for row in rows] == [4]
    assert source["scope_kind"] == "managed_youth"
    assert source["squad_label"] == "U21"


def test_people_are_filtered_and_paginated_with_source_projection():
    result = page_relationship_people(profile(), {
        "team_id": 10, "person_kind": "player", "search": "a",
        "page": 2, "page_size": 1,
    })
    assert result["total"] == 2
    assert result["page"] == 2
    assert result["people"][0]["name"] == "Beta"
    assert result["people"][0]["source"]["club_id"] == 10
    assert result["people"][0]["source"]["team_type"] == "club"
    assert result["people"][0]["team_name"] == "主队"
    assert result["scope_people"][0]["team_id"] == 10
    assert result["people"][0]["person_key"] is None
    assert [row["id"] for row in result["scope_people"]] == [1, 2]


def test_people_can_be_returned_on_one_classified_frontend_page():
    source = profile()
    source["players"] = [
        {"id": index, "name": f"Player {index}"} for index in range(1, 73)
    ]
    result = page_relationship_people(source, {
        "team_id": 10, "person_kind": "player", "all": True,
        "page": 3, "page_size": 1,
    })
    assert result["total"] == 72
    assert result["page"] == result["pages"] == 1
    assert result["page_size"] == 72
    assert len(result["people"]) == 72


def test_page_enrichment_resolves_full_scope_identity_but_only_page_relations():
    source = profile()
    for index, row in enumerate(source["players"], start=1):
        row["address"] = hex(0x1000 + index)
    page = page_relationship_people(source, {
        "team_id": 10, "person_kind": "player", "page": 1, "page_size": 1,
    })
    reader = object()
    with (
        patch("tools.relationship_network.resolve_public_person_address",
              side_effect=lambda _reader, row, _kind: 0x2000 + row["id"]),
        patch("tools.relationship_network.read_person_relationships", return_value={
            "person_relations": {"0x2002": 50},
            "person_relations_detail": {
                "0x2002": {"score": 50, "reason": 5, "object_type": 3},
            },
        }) as read_relations,
    ):
        result = enrich_relationship_page(reader, page)
    assert [row["person_key"] for row in result["scope_people"]] == ["0x2001", "0x2002"]
    assert result["people"][0]["person_key"] == "0x2001"
    read_relations.assert_called_once_with(reader, 0x2001)


def test_entity_resolution_can_include_external_relationship_second_source():
    reader = object()
    with (
        patch("tools.relationship_network.read_person_public_profile", return_value={
            "name": "External", "uid": 99, "kind": "staff",
        }),
        patch("tools.relationship_network.read_person_relationships", return_value={
            "person_relations": {"0x20": -40},
            "person_relations_detail": {"0x20": {"score": -40, "reason": 10}},
        }) as read_relations,
    ):
        result = resolve_relationship_entities(reader, ["0x10"], include_relationships=True)
    assert result["entities"]["0x10"]["person_relations"]["0x20"] == -40
    read_relations.assert_called_once_with(reader, 0x10)


@pytest.mark.parametrize("payload", [
    {"team_id": 99}, {"team_id": 10, "scope_kind": "all"},
    {"team_id": 10, "person_kind": "agent"},
])
def test_invalid_scope_requests_are_rejected(payload):
    with pytest.raises(ValueError):
        select_relationship_rows(profile(), payload)
