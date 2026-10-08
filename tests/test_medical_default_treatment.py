from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tools.club_economy import (
    _new_state,
    apply_default_medical_treatments,
    medical_status,
    set_default_medical_treatment,
)
from tools.money import to_minor, write_minor


ROOT = (Path(__file__).resolve().parents[1] / "src")


def _injured_player(player_id: int) -> dict[str, object]:
    return {
        "id": player_id,
        "name": f"Player {player_id}",
        "team_id": 20,
        "address": hex(0x1000 + player_id),
        "availability": {"injury_count": 1},
    }


def test_default_treatment_is_persisted_in_the_economy_account() -> None:
    state = _new_state()
    with patch("tools.club_economy.load_economy", return_value=state), patch(
        "tools.club_economy.save_economy"
    ) as save:
        status = set_default_medical_treatment("aggressive")

    assert state["medical_default_treatment"] == "aggressive"
    assert status["default_treatment"] == "aggressive"
    save.assert_called_once_with(state)


def test_auto_treatment_stops_cleanly_when_combined_funds_run_out() -> None:
    state = _new_state()
    state["medical_default_treatment"] = "conservative"
    write_minor(state, "general_balance", to_minor(10_000.0))
    transformed: list[int] = []

    def transform(player: dict[str, object], _old: float, _new: float) -> dict[str, object]:
        transformed.append(int(player["id"]))
        return {
            "injuries": [{
                "start_date": "2028-01-01",
                "estimated_return_to": "2028-01-15",
            }],
        }

    with patch("tools.club_economy.load_economy", return_value=state), patch(
        "tools.club_economy.save_economy"
    ), patch("tools.club_economy.available_balance", return_value=0.0), patch(
        "tools.club_economy.load_wallet", return_value={"balance": 0.0},
    ):
        result = apply_default_medical_treatments(
            "2028-01-02", [_injured_player(1), _injured_player(2)], transform,
        )

    assert result["applied"] == [1]
    assert result["skipped"] == [2]
    assert transformed == [1]
    assert state["general_balance"] == 0.0
    assert set(state["medical_treatments"]) == {"1"}


def test_medical_status_does_not_build_unrelated_welfare_projection() -> None:
    state = _new_state()
    state["medical_default_treatment"] = "conservative"
    state["medical_treatments"]["7"] = {
        "player_id": 7, "mode": "conservative", "factor": 1.5,
    }

    with patch("tools.club_economy.load_economy", return_value=state), patch(
        "tools.club_economy.load_settings",
        side_effect=AssertionError("medical projection must not load settings"),
    ):
        status = medical_status()

    assert status["default_treatment"] == "conservative"
    assert set(status["treatment_options"]) == {"conservative", "aggressive"}
    assert status["treatments"]["7"]["player_id"] == 7


def test_default_treatment_scan_uses_the_medical_projection_only() -> None:
    state = _new_state()
    state["medical_default_treatment"] = "conservative"
    state["medical_treatments"]["1"] = {
        "player_id": 1, "mode": "conservative", "factor": 1.5,
    }

    with patch("tools.club_economy.load_economy", return_value=state), patch(
        "tools.club_economy.load_settings",
        side_effect=AssertionError("default treatment scan must not load settings"),
    ):
        result = apply_default_medical_treatments(
            "2028-01-02", [_injured_player(1)],
            lambda *_args: (_ for _ in ()).throw(AssertionError("already treated")),
        )

    assert result == {
        "mode": "conservative", "applied": [], "skipped": [], "errors": [],
    }


def test_hospital_frontend_exposes_the_default_treatment_control() -> None:
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="hospital-default-treatment"' in html
    assert 'value="manual"' in html
    assert 'value="conservative"' in html
    assert 'value="aggressive"' in html
    assert 'request("/api/hospital/default-treatment"' in script
    assert "treatmentOptionsByMode.get(mode)?.daily_cost" in script
