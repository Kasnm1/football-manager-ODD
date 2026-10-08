from pathlib import Path

from tools.betting_account import SETTLED_BET_HISTORY_LIMIT, public_bet_history


ROOT = Path(__file__).resolve().parents[1]


def test_public_history_keeps_all_pending_and_latest_500_settled() -> None:
    records = [
        {"bet_id": "pending-old", "status": "pending"},
        *(
            {"bet_id": f"settled-{index}", "status": "won"}
            for index in range(SETTLED_BET_HISTORY_LIMIT + 1)
        ),
        {"bet_id": "pending-new", "status": "pending"},
    ]

    visible = public_bet_history(records)

    assert visible[0]["bet_id"] == "pending-new"
    assert visible[-1]["bet_id"] == "pending-old"
    assert sum(record["status"] == "pending" for record in visible) == 2
    assert sum(record["status"] != "pending" for record in visible) == 500
    assert "settled-0" not in {record["bet_id"] for record in visible}
    assert "settled-500" in {record["bet_id"] for record in visible}


def test_new_bets_are_not_truncated_to_the_old_100_record_limit() -> None:
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    submission = script.split(
        "if (app.state && Array.isArray(result.bets)) {", 1,
    )[1].split("refreshSelectionUI();", 1)[0]

    assert ".slice(0, 100)" not in submission
