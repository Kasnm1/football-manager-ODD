from __future__ import annotations

from copy import deepcopy
from contextlib import nullcontext
import threading
from unittest.mock import patch

import fm_odds_web
from tools import club_legacy


class MemoryDocuments:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], object] = {}

    def load(self, key, default, scope_id=None, **_kwargs):
        return deepcopy(self.documents.get((str(scope_id), str(key)), default))

    def save(self, key, value, scope_id=None, **_kwargs):
        self.documents[(str(scope_id), str(key))] = deepcopy(value)


def profile(game_date: str, players: list[dict], *, team_id: int = 9, team_type: str = "club") -> dict:
    return {
        "game_date": game_date,
        "team": {"id": team_id, "name": "测试俱乐部", "team_type": team_type, "address": "0xTEAM"},
        "players": players,
    }


def test_player_snapshot_preserves_native_previous_club_state() -> None:
    snapshot = club_legacy._player_snapshot({
        "id": 7,
        "name": "测试球员",
        "previous_club_state": "available",
        "previous_club_id": 791,
        "previous_club_name": "东京绿茵",
    }, {"id": 1190, "name": "柏太阳神"}, "2026-09-07")
    assert snapshot["previous_club_state"] == "available"
    assert snapshot["previous_club_id"] == 791
    assert snapshot["previous_club_name"] == "东京绿茵"


def test_best_team_keeps_season_snapshots_and_validates_scope() -> None:
    memory = MemoryDocuments()
    season_stats = {
        "source": "fm26_native_current_season", "scope": "current_season_total",
        "appearances": 20, "starts": 18, "minutes": 1600, "goals": 5,
        "assists": 4, "average_rating": 7.2,
    }
    first = player(1, "赛季球员", season_stats=season_stats)
    second = player(2, "历史球员", season_stats={**season_stats, "goals": 9})
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile(
            "career-a", profile("2027-06-30", [first, second]),
            season_start="2026-07-01", season_end="2027-06-30",
        )
        club_legacy.sync_club_profile(
            "career-a", profile("2027-07-01", [first]),
            season_start="2027-07-01", season_end="2028-06-30",
        )
        club_legacy.set_club_legacy_preference("career-a", "account-a", 9, 2, inducted=True)
        all_time = club_legacy.set_club_legacy_best_team(
            "career-a", "account-a", 9, lineup_kind="all_time", formation="4231",
            assignments={"st": 2},
        )
        season = club_legacy.set_club_legacy_best_team(
            "career-a", "account-a", 9, lineup_kind="season", season_key="2026-2027",
            formation="4231", assignments={"st": 1},
        )

    assert [row["key"] for row in season["seasons"]] == ["2027-2028", "2026-2027"]
    assert all_time["best_teams"]["all_time"]["filled"] == 1
    assert season["best_teams"]["seasons"][0]["season_key"] == "2026-2027"
    assert season["best_teams"]["seasons"][0]["assignments"][0]["player_id"] == 1
    assert next(row for row in season["players"] if row["id"] == 2)["season_snapshots"][0]["season_key"] == "2026-2027"


def player(
    player_id: int, name: str, *, ca: int = 120, injured: bool = False,
    loaned: bool = False, season_stats: dict | None = None,
) -> dict:
    payload = {
        "id": player_id,
        "name": name,
        "address": f"0x{player_id:X}",
        "positions": ["MC"],
        "primary_positions": ["MC"],
        "nationality": "中国",
        "ca": ca,
        "pa": 150,
        "availability": {"injury_count": int(injured), "injuries": []},
        "is_loaned_out": loaned,
        "loan": {
            "team_name": "租借球队",
            "team_address": "0xLOAN",
            "is_loaned_out": loaned,
        },
        "player_contract": {
            "expiry_date": "2030-06-30",
            "address": "0xCONTRACT",
        },
    }
    if season_stats is not None:
        payload["season_stats"] = season_stats
    return payload


def test_tracks_current_and_historical_roster_without_claiming_transfer() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-01", [player(1, "甲"), player(2, "乙")]),
            game_layout="fm24",
        )
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-02", [player(1, "甲", ca=121, injured=True)]),
            game_layout="fm24",
        )
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    assert payload["summary"] == {
        "total": 2, "current": 1, "history": 1, "inducted": 0, "favorites": 0,
    }
    historical = next(row for row in payload["players"] if row["id"] == 2)
    assert historical["status"] == "history"
    assert historical["status_label"] == "历史成员"
    left = next(event for event in historical["events"] if event["type"] == "left_current_roster")
    assert left["title"] == "离开当前一线名单"
    assert "转会、下放、外租或名单调整" in left["detail"]
    current = next(row for row in payload["players"] if row["id"] == 1)
    assert {event["type"] for event in current["events"]} >= {"first_observed", "ca_changed", "injury_started"}


def test_records_later_native_join_as_incoming_transfer_not_roster_departure() -> None:
    memory = MemoryDocuments()
    incumbent = player(1, "原有球员")
    incoming = player(2, "细谷真大")
    incoming["player_contract"]["joined_club_date"] = "2028-07-10"
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-01", [incumbent]), game_layout="fm26",
        )
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-11", [incumbent, incoming]), game_layout="fm26",
        )
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    row = next(item for item in payload["players"] if item["id"] == 2)
    joined = next(event for event in row["events"] if event["type"] == "joined_current_club")
    assert joined["game_date"] == "2028-07-10"
    assert joined["title"] == "转入本队"
    assert "加盟测试俱乐部" in joined["detail"]
    assert joined["observed_game_date"] == "2028-07-11"
    assert joined["source"] == "native_joined_club_date"
    assert not any(event["type"] == "left_current_roster" for event in row["events"])


def test_new_roster_observation_with_old_join_date_is_not_claimed_as_transfer() -> None:
    memory = MemoryDocuments()
    incumbent = player(1, "原有球员")
    promoted = player(2, "旧合同新入一线队")
    promoted["player_contract"]["joined_club_date"] = "2027-01-01"
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-07-01", [incumbent]))
        club_legacy.sync_club_profile("career-a", profile("2028-07-11", [incumbent, promoted]))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    row = next(item for item in payload["players"] if item["id"] == 2)
    assert any(event["type"] == "first_observed" for event in row["events"])
    assert not any(event["type"] == "joined_current_club" for event in row["events"])


def test_backfills_incoming_transfer_missed_by_older_tracker() -> None:
    memory = MemoryDocuments()
    incumbent = player(1, "原有球员")
    incoming = player(2, "细谷真大")
    incoming["player_contract"]["joined_club_date"] = "2028-07-10"
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-07-01", [incumbent]))
        with patch.object(club_legacy, "_joined_club_date", return_value=""):
            club_legacy.sync_club_profile(
                "career-a", profile("2028-07-11", [incumbent, incoming]),
            )
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-12", [incumbent, incoming]),
        )
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    row = next(item for item in payload["players"] if item["id"] == 2)
    joined = [event for event in row["events"] if event["type"] == "joined_current_club"]
    assert len(joined) == 1
    assert joined[0]["game_date"] == "2028-07-10"
    assert joined[0]["observed_game_date"] == "2028-07-11"


def test_injury_timeline_keeps_specific_injury_name_and_return_window() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        healthy = player(1, "甲")
        injured = player(1, "甲", injured=True)
        injured["availability"]["injuries"] = [{
            "type": "腘绳肌撕裂", "start_date": "2028-08-10",
            "estimated_return_to": "2028-09-20", "address": "0xBAD",
        }]
        club_legacy.sync_club_profile("career-a", profile("2028-08-09", [healthy]))
        club_legacy.sync_club_profile("career-a", profile("2028-08-11", [injured]))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    event = next(item for item in payload["players"][0]["events"] if item["type"] == "injury_started")
    assert "腘绳肌撕裂" in event["detail"]
    assert "2028-09-20" in event["detail"]
    assert event["injuries"] == [{
        "type": "腘绳肌撕裂", "start_date": "2028-08-10",
        "estimated_return_to": "2028-09-20",
    }]


def test_snapshots_remove_session_addresses_and_normalize_both_game_layouts() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-01", [player(1, "甲")]), game_layout="fm24",
        )
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-02", [player(1, "甲", loaned=True)]), game_layout="fm26",
        )
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    encoded = str(payload["players"][0]["snapshot"])
    assert "address" not in encoded.casefold()
    assert payload["players"][0]["status"] == "loaned"
    assert payload["coverage"]["observed_layouts"] == ["fm24", "fm26"]


def test_monthly_attribute_snapshots_replace_same_month_and_follow_rewind() -> None:
    memory = MemoryDocuments()

    def attributed(game_date: str, passing: int, ca: int) -> dict:
        row = player(1, "月度球员", ca=ca)
        row["attributes"] = {"技术": {"传球": passing, "停球": passing - 1}}
        row["hidden_attributes"] = {"职业素养": 15}
        row["training_ca"] = ca - 2
        row["debug_address"] = "0xPLAYER"
        return profile(game_date, [row])

    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", attributed("2028-07-01", 10, 120))
        club_legacy.sync_club_profile("career-a", attributed("2028-07-20", 11, 121))
        club_legacy.sync_club_profile("career-a", attributed("2028-08-03", 12, 122))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

        snapshots = payload["players"][0]["attribute_snapshots"]
        assert [item["month"] for item in snapshots] == ["2028-08", "2028-07"]
        assert snapshots[1]["observed_game_date"] == "2028-07-20"
        assert snapshots[1]["attributes"]["技术"]["传球"] == 11
        assert snapshots[0]["hidden_attributes"]["职业素养"] == 15
        assert snapshots[0]["ca"] == 122
        assert "address" not in str(snapshots).casefold()

        club_legacy.sync_club_profile("career-a", attributed("2028-07-15", 9, 119))
        rewound = club_legacy.public_club_legacy("career-a", "account-a", 9)

    snapshots = rewound["players"][0]["attribute_snapshots"]
    assert [(item["month"], item["observed_game_date"]) for item in snapshots] == [
        ("2028-07", "2028-07-15"),
    ]
    assert snapshots[0]["attributes"]["技术"]["传球"] == 9


def test_preserves_last_observed_native_season_total_for_historical_member() -> None:
    memory = MemoryDocuments()
    native = {
        "source": "fm26_native_current_season",
        "scope": "current_season_total",
        "appearances": 41,
        "goals": 33,
        "assists": 6,
        "average_rating": 7.43,
        "debug_address": "0xB200",
    }
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-01", [player(1, "档案球员", season_stats=native)]),
            game_layout="fm26",
        )
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-02", []), game_layout="fm26",
        )
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    archived = payload["players"][0]
    assert archived["status"] == "history"
    assert archived["snapshot"]["season_stats"]["goals"] == 33
    assert "address" not in str(archived["snapshot"]["season_stats"]).casefold()


def test_return_to_roster_is_recorded() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-07-01", [player(1, "甲")]))
        club_legacy.sync_club_profile("career-a", profile("2028-07-02", []))
        club_legacy.sync_club_profile("career-a", profile("2028-07-03", [player(1, "甲")]))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    row = payload["players"][0]
    assert row["status"] == "current"
    assert any(event["type"] == "returned_to_roster" for event in row["events"])


def test_preferences_are_account_scoped() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-07-01", [player(1, "甲")]))
        selected = club_legacy.set_club_legacy_preference(
            "career-a", "account-a", 9, 1,
            inducted=True, favorite=True, tier="legend", note="队史核心",
        )
        other = club_legacy.public_club_legacy("career-a", "account-b", 9)

    assert selected["players"][0]["inducted"] is True
    assert selected["players"][0]["favorite"] is True
    assert selected["players"][0]["tier"] == "legend"
    assert selected["players"][0]["note"] == "队史核心"
    assert other["players"][0]["inducted"] is False
    assert other["players"][0]["note"] == ""


def test_world_search_player_can_be_persisted_as_account_scoped_watch() -> None:
    memory = MemoryDocuments()
    source = player(700, "世界球员", ca=145)
    source.update({
        "team_id": 88, "team_name": "海外俱乐部",
        "attributes": {"技术": {"传球": 16}}, "asking_price": 42_000_000,
    })
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_world_watch_player(
            "career-a", source, game_date="2030-08-12", game_layout="fm26",
        )
        payload = club_legacy.set_club_legacy_preference(
            "career-a", "account-a", club_legacy.WORLD_WATCH_TEAM_ID, 700,
            favorite=True,
        )
        other = club_legacy.public_club_legacy(
            "career-a", "account-b", club_legacy.WORLD_WATCH_TEAM_ID,
        )
        index = club_legacy.public_club_legacy_index("career-a", "account-a")

    watched = payload["players"][0]
    assert payload["club"] == {"id": -1, "name": "世界关注"}
    assert watched["favorite"] is True
    assert watched["status"] == "watched"
    assert watched["snapshot"]["observed_team_name"] == "海外俱乐部"
    assert watched["value_history"][0]["asking_price"] == 42_000_000
    assert other["players"][0]["favorite"] is False
    assert -1 in {row["id"] for row in index["clubs"]}


def test_world_watch_preference_reads_uid_detail_before_persisting() -> None:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.output = {
        "save_instance_id": "career-a", "game_date": "2030-08-12",
        "game_layout": "fm26",
    }
    state._data_scope_id = lambda _output: "account-a"
    state._timed_user_memory_operation = lambda _name: nullcontext()
    detail = {"id": 700, "name": "世界球员", "team_id": 88, "team_name": "海外俱乐部"}
    response = {"club": {"id": -1}, "players": [{"id": 700, "favorite": True}]}
    with patch.object(fm_odds_web, "read_world_player_profile", return_value=detail), patch.object(
        fm_odds_web, "apply_player_aliases",
    ), patch.object(fm_odds_web, "sync_world_watch_player") as sync, patch.object(
        fm_odds_web, "set_club_legacy_preference", return_value=response,
    ) as save:
        result = state.update_club_legacy_preference({
            "team_id": -1, "player_id": 700, "favorite": True,
        })

    sync.assert_called_once_with(
        "career-a", detail, game_date="2030-08-12", game_layout="fm26",
    )
    save.assert_called_once_with(
        "career-a", "account-a", -1, 700, favorite=True,
    )
    assert result == response


def test_world_watch_refresh_reobserves_only_followed_players_and_is_fault_tolerant() -> None:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    watched = {
        "players": [
            {"id": 700, "favorite": True},
            {"id": 701, "favorite": False},
            {"id": 702, "favorite": True},
        ],
    }
    details = {
        700: {"id": 700, "name": "世界球员", "season_stats": {"goals": 9}},
    }

    def read_detail(player_id: int) -> dict:
        if player_id == 702:
            raise RuntimeError("暂时无法读取")
        return details[player_id]

    with patch.object(
        fm_odds_web, "load_public_club_legacy", return_value=watched,
    ), patch.object(
        fm_odds_web, "read_world_player_profile", side_effect=read_detail,
    ) as read, patch.object(
        fm_odds_web, "apply_player_aliases",
    ), patch.object(
        fm_odds_web, "sync_world_watch_player",
    ) as sync:
        errors = state._refresh_world_watch_players(
            "career-a", "account-a", "2030-09-01", "fm26",
        )

    assert [call.args[0] for call in read.call_args_list] == [700, 702]
    sync.assert_called_once_with(
        "career-a", details[700], game_date="2030-09-01", game_layout="fm26",
    )
    assert errors == ["世界关注球员 702: 暂时无法读取"]


def test_tracks_shirt_and_asking_price_history_and_manual_career_record() -> None:
    memory = MemoryDocuments()
    first = player(1, "档案球员")
    first.update({"shirt_number": 17, "asking_price": 12_000_000})
    second = player(1, "档案球员")
    second.update({"shirt_number": 10, "asking_price": 18_000_000})
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-07-01", [first]))
        club_legacy.sync_club_profile("career-a", profile("2029-07-01", [second]))
        payload = club_legacy.set_club_legacy_preference(
            "career-a", "account-a", 9, 1,
            career_record={
                "appearances": 80, "goals": 24, "assists": 31,
                "average_rating": 7.26, "transfer_in_fee": 9_000_000,
                "transfer_out_fee": 25_000_000,
                "honors_text": "2028/29 联赛冠军",
                "post_departure_text": "2031/32 欧冠冠军",
            },
        )

    archived = payload["players"][0]
    assert [row["number"] for row in archived["shirt_numbers"]] == [17, 10]
    assert [row["asking_price"] for row in archived["value_history"]] == [12_000_000, 18_000_000]
    assert archived["career_record"]["goals"] == 24
    assert archived["career_record"]["average_rating"] == 7.26
    assert archived["career_record_updated_at"]


def test_records_asking_price_at_most_once_per_ninety_days() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        for game_date, asking_price in (
            ("2025-08-01", 10_000_000),
            ("2025-09-01", 11_000_000),
            ("2025-10-31", 12_000_000),
            ("2025-11-01", 13_000_000),
            ("2026-01-30", 14_000_000),
        ):
            current = player(1, "季度球员")
            current["asking_price"] = asking_price
            club_legacy.sync_club_profile("career-a", profile(game_date, [current]))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    history = payload["players"][0]["value_history"]
    assert [row["game_date"] for row in history] == ["2025-08-01", "2025-10-31", "2026-01-30"]
    assert all("market_value" not in row for row in history)


def test_records_and_deduplicates_match_moment_in_player_archive() -> None:
    memory = MemoryDocuments()
    moment = {
        "game_date": "2028-08-12", "competition_id": 77, "competition_name": "联赛",
        "home": {"id": 9, "name": "测试俱乐部"}, "away": {"id": 10, "name": "对手"},
        "home_goals": 2, "away_goals": 1, "tags": ["关键球", "制胜球"],
        "minute": 73, "note": "完成逆转",
    }
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-08-13", [player(1, "甲")]))
        first = club_legacy.add_club_legacy_match_moment("career-a", "account-a", 9, 1, moment)
        second = club_legacy.add_club_legacy_match_moment("career-a", "account-a", 9, 1, moment)

    assert len(first["players"][0]["match_moments"]) == 1
    assert first["players"][0]["match_moments"][0]["fixture_key"] == "2028-08-12|77|9|10"
    assert first["players"][0]["match_moments"][0]["tags"] == ["关键球", "制胜球"]
    assert len(second["players"][0]["match_moments"]) == 1


def test_late_goal_is_auto_classified_as_winner_or_equalizer() -> None:
    memory = MemoryDocuments()
    base = {
        "game_date": "2028-08-12", "competition_id": 77,
        "home": {"id": 9, "name": "测试俱乐部"}, "away": {"id": 10, "name": "对手"},
        "home_goals": 2, "away_goals": 1, "tags": ["进球"],
        "minute": 88, "player_team_id": 9,
    }
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-08-13", [player(1, "甲")]))
        winner = club_legacy.add_club_legacy_match_moment("career-a", "account-a", 9, 1, base)
        equalizer = club_legacy.add_club_legacy_match_moment(
            "career-a", "account-a", 9, 1,
            {**base, "game_date": "2028-08-14", "home_goals": 2, "away_goals": 2},
        )
    moments = equalizer["players"][0]["match_moments"]
    assert any("绝杀球" in item["tags"] for item in winner["players"][0]["match_moments"])
    assert any("绝平球" in item["tags"] for item in moments)


def test_native_goal_events_prove_only_the_decisive_late_goal() -> None:
    events = [
        {"team_side": "home", "scorer": {"id": 2}, "time": {"minute": 12}},
        {"team_side": "away", "scorer": {"id": 3}, "time": {"minute": 74}},
        {"team_side": "home", "scorer": {"id": 1}, "time": {"minute": 88}},
    ]
    assert club_legacy.classify_late_goal(
        minute=88, player_team_id=9, player_id=1,
        home_id=9, away_id=10, home_goals=2, away_goals=1,
        goal_events=events,
    ) == ["绝杀球"]
    native_moment = club_legacy._normalize_match_moment({
        "game_date": "2028-08-12", "competition_id": 77,
        "home": {"id": 9}, "away": {"id": 10},
        "home_goals": 2, "away_goals": 1, "tags": ["进球"],
        "minute": 88, "player_team_id": 9, "goal_events": events,
    }, player_id=1)
    assert native_moment["auto_tag_source"] == "native_goal_events"
    # A late goal that leaves an already-held lead is not a winner.
    assert club_legacy.classify_late_goal(
        minute=89, player_team_id=9, player_id=4,
        home_id=9, away_id=10, home_goals=3, away_goals=1,
        goal_events=[
            {"team_side": "home", "scorer": {"id": 1}, "time": {"minute": 10}},
            {"team_side": "away", "scorer": {"id": 3}, "time": {"minute": 20}},
            {"team_side": "home", "scorer": {"id": 4}, "time": {"minute": 89}},
        ],
    ) == []


def test_native_goal_events_prove_late_equalizer_and_do_not_fallback_on_miss() -> None:
    assert club_legacy.classify_late_goal(
        minute=90, player_team_id=9, player_id=1,
        home_id=9, away_id=10, home_goals=2, away_goals=2,
        goal_events=[
            {"team_side": "away", "scorer": {"id": 3}, "time": {"minute": 31}},
            {"team_side": "home", "scorer": {"id": 1}, "time": {"minute": 90}},
            {"team_side": "home", "scorer": {"id": 5}, "time": {"minute": 60}},
            {"team_side": "away", "scorer": {"id": 4}, "time": {"minute": 75}},
        ],
    ) == ["绝平球"]
    # Presence of a native vector is authoritative; an unmatched scorer must
    # not be guessed from the final 1-1 scoreline.
    assert club_legacy.classify_late_goal(
        minute=90, player_team_id=9, player_id=99,
        home_id=9, away_id=10, home_goals=1, away_goals=1,
        goal_events=[
            {"team_side": "home", "scorer": {"id": 1}, "time": {"minute": 15}},
            {"team_side": "away", "scorer": {"id": 3}, "time": {"minute": 90}},
        ],
    ) == []


def test_match_moment_reads_only_selected_native_result_on_submit() -> None:
    result = {
        "date": "2028-08-12", "competition": {"id": 77},
        "home_team": {"id": 9, "name": "Home"},
        "away_team": {"id": 10, "name": "Away"},
        "home_goals": 2, "away_goals": 1,
        "result_source": "fixture_result_archive",
        "result_address": "0x2000",
    }
    events = [{"time": {"minute": 88}, "scorer": {"id": 1}}]
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.memory_lock = threading.Lock()
    state.output = {
        "save_instance_id": "career-a",
        "season_results": [],
    }
    state._data_scope_id = lambda _output: "account-a"
    payload = {
        "team_id": 9, "player_id": 1,
        "moment": {
            "game_date": "2028-08-12", "competition_id": 77,
            "home": {"id": 9, "name": "Home"},
            "away": {"id": 10, "name": "Away"},
            "home_goals": 2, "away_goals": 1,
            "tags": ["进球"], "minute": 88,
            "goal_events": [{"client": "must not be trusted"}],
        },
    }

    with (
        patch("fm_odds_web.read_result_history", return_value=[result]),
        patch("fm_odds_web.read_result_goal_events", return_value=events) as read_events,
        patch(
            "fm_odds_web.add_club_legacy_match_moment",
            return_value={"ok": True},
        ) as save_moment,
    ):
        response = state.add_club_legacy_match_moment(payload)

    assert response == {"ok": True}
    read_events.assert_called_once_with(result)
    saved = save_moment.call_args.args[4]
    assert saved["goal_events"] == events
    assert {"client": "must not be trusted"} not in saved["goal_events"]


def test_repeated_injury_type_is_marked_as_recurrence() -> None:
    memory = MemoryDocuments()
    def injured_item(start: str) -> dict:
        item = player(1, "甲")
        item["availability"] = {"injury_count": 1, "injuries": [{"type": "腿筋拉伤", "start_date": start, "estimated_return_to": start}]}
        return item
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-08-01", [injured_item("2028-08-01")]))
        club_legacy.sync_club_profile("career-a", profile("2028-08-10", [player(1, "甲")]))
        club_legacy.sync_club_profile("career-a", profile("2028-09-01", [injured_item("2028-09-01")]))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)
    row = payload["players"][0]
    assert row["injury_summary"]["recurring_count"] == 1
    assert row["injury_history"][-1]["recurring"] is True


def test_match_moment_rejects_invalid_tags_and_minute() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-08-13", [player(1, "甲")]))
        base = {"game_date": "2028-08-12", "competition_id": 77, "home": {"id": 9}, "away": {"id": 10}}
        try:
            club_legacy.add_club_legacy_match_moment("career-a", "account-a", 9, 1, {**base, "tags": ["不存在"]})
            raise AssertionError("invalid tag should fail")
        except ValueError as error:
            assert "贡献类型" in str(error)
        try:
            club_legacy.add_club_legacy_match_moment("career-a", "account-a", 9, 1, {**base, "tags": ["进球"], "minute": 131})
            raise AssertionError("invalid minute should fail")
        except ValueError as error:
            assert "分钟" in str(error)


def test_match_moment_accepts_quick_football_tags() -> None:
    memory = MemoryDocuments()
    moment = {
        "game_date": "2028-08-12", "competition_id": 77,
        "home": {"id": 9, "name": "测试俱乐部"}, "away": {"id": 10, "name": "对手"},
        "home_goals": 2, "away_goals": 1,
        "tags": ["游龙", "倒钩", "世界波", "关键解围", "绝妙助攻"],
    }
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2028-08-13", [player(1, "甲")]))
        payload = club_legacy.add_club_legacy_match_moment("career-a", "account-a", 9, 1, moment)
    assert payload["players"][0]["match_moments"][0]["tags"] == ["游龙", "倒钩", "世界波", "关键解围", "绝妙助攻"]


def test_rewind_supersedes_future_events_and_marks_unseen_players_unknown() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile("career-a", profile("2029-01-01", [player(1, "甲")]))
        club_legacy.sync_club_profile("career-a", profile("2029-01-11", [player(1, "甲", injured=True)]))
        club_legacy.sync_club_profile("career-a", profile("2029-01-05", []))
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    row = payload["players"][0]
    assert row["status"] == "unknown"
    assert all(event["game_date"] <= "2029-01-05" for event in row["events"])
    assert row["last_seen_game_date"] is None
    assert row["snapshot"]["rewind_unverified"] is True


def test_national_team_profile_is_not_added_to_club_archive() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        assert club_legacy.sync_club_profile(
            "career-a", profile("2028-07-01", [player(1, "甲")], team_type="national"),
        ) == {}
        payload = club_legacy.public_club_legacy("career-a", "account-a", 9)

    assert payload["players"] == []


def test_career_index_keeps_previous_club_after_manager_changes_team() -> None:
    memory = MemoryDocuments()
    with patch.object(club_legacy, "load_document", side_effect=memory.load), patch.object(
        club_legacy, "save_document", side_effect=memory.save,
    ):
        club_legacy.sync_club_profile(
            "career-a", profile("2028-07-01", [player(1, "旧队球员")], team_id=9),
            game_layout="fm24",
        )
        next_profile = profile("2029-01-01", [player(2, "新队球员")], team_id=10)
        next_profile["team"]["name"] = "新俱乐部"
        club_legacy.sync_club_profile("career-a", next_profile, game_layout="fm24")
        index = club_legacy.public_club_legacy_index("career-a", "account-a")
        old_club = club_legacy.public_club_legacy("career-a", "account-a", 9)

    assert index["summary"] == {"clubs": 2, "players": 2, "inducted": 0}
    assert {row["id"] for row in index["clubs"]} == {9, 10}
    assert old_club["players"][0]["snapshot"]["name"] == "旧队球员"


def test_state_index_marks_current_club_without_hiding_previous_clubs() -> None:
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    state.lock = threading.RLock()
    state.club_profiles = {}
    state.club_loading_profiles = {}
    state.club_refreshing = False
    state.output = {
        "save_instance_id": "career-a",
        "account_scope_id": "account-a",
        "account_scope_manager_id": 7,
        "selected_manager_id": 7,
        "game_layout": "fm26",
        "managed_team": {"id": 10, "team_type": "club"},
        "managed_teams": [{"id": 10, "team_type": "club"}],
    }
    stored = {
        "schema_version": 1,
        "clubs": [{"id": 9, "name": "旧俱乐部"}, {"id": 10, "name": "新俱乐部"}],
        "summary": {"clubs": 2},
    }
    with patch.object(fm_odds_web, "load_public_club_legacy_index", return_value=stored):
        result = state.public_club_legacy(0)

    assert result["current_team_id"] == 10
    assert [row["id"] for row in result["clubs"]] == [9, 10]
