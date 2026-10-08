import json
from pathlib import Path
from unittest.mock import patch

from tools import preview_cup_odds
from tools.result_history import ResultHistoryOwner
from tools.storage_retention import decode_result_history_bytes


def _result(
    date_text: str = "2028-06-01",
    *,
    competition_id: int = 300,
    home_goals: int = 1,
) -> dict:
    return {
        "date": date_text,
        "competition": {"id": competition_id},
        "home_team": {"id": 10},
        "away_team": {"id": 20},
        "home_goals": home_goals,
        "away_goals": 0,
    }


def _write_plain_history(path: Path, results: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results": results}), encoding="utf-8")


def test_owner_runs_independently_with_explicit_paths_and_backup_recovery(tmp_path) -> None:
    durable = tmp_path / "careers" / "career-one" / "result_history.json"
    legacy = tmp_path / "cache" / "career-one" / "results" / "fm26_result_history.json"
    owner = ResultHistoryOwner(
        save_instance_id="career-one",
        durable_path=durable,
        legacy_path=legacy,
    )
    legacy_result = _result("2028-05-31", competition_id=299)
    first_result = _result()
    second_result = _result("2028-06-02", competition_id=301)
    _write_plain_history(legacy, [legacy_result])

    owner.save([first_result])
    owner.save([second_result])

    payload = decode_result_history_bytes(durable.read_bytes())
    assert payload["legacy_history_imported"] is True
    assert [row["competition"]["id"] for row in payload["results"]] == [299, 300, 301]
    backup = durable.with_suffix(durable.suffix + ".backup")
    backup_bytes = backup.read_bytes()
    durable.write_bytes(b"corrupt")

    recovered = owner.load()

    assert [row["competition"]["id"] for row in recovered] == [299, 300]
    assert durable.read_bytes() == backup_bytes


def test_facade_patches_reach_owner_load_merge_and_save(tmp_path) -> None:
    durable = tmp_path / "careers" / "career-one" / "result_history.json"
    legacy = tmp_path / "cache" / "career-one" / "results" / "fm26_result_history.json"
    prior = _result("2028-05-31", competition_id=299)
    current = _result()

    with (
        patch.object(preview_cup_odds, "result_history_path", return_value=durable),
        patch.object(preview_cup_odds, "legacy_result_history_path", return_value=legacy),
        patch.object(preview_cup_odds, "read_result_history", return_value=[prior]) as load,
    ):
        merged = preview_cup_odds.merge_result_history([current], "career-one")

    load.assert_called_once_with("career-one")
    assert [row["competition"]["id"] for row in merged] == [299, 300]

    with (
        patch.object(preview_cup_odds, "result_history_path", return_value=durable),
        patch.object(preview_cup_odds, "legacy_result_history_path", return_value=legacy),
        patch.object(preview_cup_odds, "merge_result_history", return_value=[current]) as merge,
    ):
        preview_cup_odds.save_result_history([prior], "career-one")

    merge.assert_called_once_with([prior], "career-one")
    payload = decode_result_history_bytes(durable.read_bytes())
    assert payload["results"] == [current]
