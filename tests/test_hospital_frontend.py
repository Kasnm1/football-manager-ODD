from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "app.css").read_text(encoding="utf-8")


def _hospital_renderer() -> str:
    return SCRIPT.split("function renderHospital()", 1)[1].split(
        "function renderInventory()", 1,
    )[0]


def test_hospital_builds_an_identity_bound_index() -> None:
    index = SCRIPT.split("function hospitalDataIndex()", 1)[1].split(
        "function hospitalViewState", 1,
    )[0]

    assert "sources?.profiles === profiles" in index
    assert "sources.welfare === welfare" in index
    assert "teamNamesByPlayerId" in index
    assert "teamIdByPlayerId" in index
    assert "treatmentsByPlayerId" in index
    assert "playersById" in index


def test_hospital_index_includes_players_from_a_managed_national_team() -> None:
    index_source = "function hospitalDataIndex()" + SCRIPT.split(
        "function hospitalDataIndex()", 1,
    )[1].split("function hospitalViewState", 1)[0]
    scenario = {
        "clubProfiles": [
            {
                "team": {"id": 10, "name": "Club", "team_type": "club"},
                "players": [{"id": 1, "name": "Club player"}],
            },
            {
                "team": {"id": 20, "name": "Nation", "team_type": "national"},
                "players": [{"id": 2, "name": "National player"}],
            },
        ],
        "state": {"welfare": {}},
    }
    node_script = f"""
const app = {json.dumps(scenario)};
{index_source}
const index = hospitalDataIndex();
process.stdout.write(JSON.stringify({{
  playerIds:index.players.map((player) => Number(player.id)),
  nationalTeamId:index.teamIdByPlayerId.get(2),
  nationalTeamNames:index.teamNamesByPlayerId.get(2),
}}));
"""

    completed = subprocess.run(
        ["node", "-e", node_script],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    result = json.loads(completed.stdout)

    assert result == {
        "playerIds": [1, 2],
        "nationalTeamId": 20,
        "nationalTeamNames": ["Nation"],
    }


def test_hospital_date_change_refreshes_every_managed_roster() -> None:
    clock_loader = SCRIPT.split("async function loadGameClock()", 1)[1].split(
        "async function pollRefreshStatus", 1,
    )[0]

    assert 'if (app.page === "hospital"' in clock_loader
    assert "ensureManagedRostersLoaded(false)" in clock_loader
    assert "ensureClubLoaded(false)" not in clock_loader


def test_hospital_renderer_uses_indexed_lookups_and_a_complete_signature() -> None:
    renderer = _hospital_renderer()
    view = SCRIPT.split("function hospitalViewState(index)", 1)[1].split(
        "function syncHospitalStatus", 1,
    )[0]

    assert "app.hospitalRenderSignature === view.signature" in renderer
    assert "index.teamIdByPlayerId.get(playerId)" in renderer
    assert "index.playersById.get(playerId)" in renderer
    assert "managedPlayerTeams(" not in renderer
    assert "allManagedPlayers(" not in renderer
    for field in (
        "data_scope_id", "moneyRenderSignature()", "bank_balance",
        "gameClock?.date", "default_treatment", "treatmentOptionsByMode",
        "medicalTreatmentBusy", "injuryRows",
    ):
        assert field in view


def test_hospital_exposes_busy_and_keyboard_feedback() -> None:
    renderer = _hospital_renderer()

    assert 'id="hospital-sync-status"' in HTML
    assert 'role="status"' in HTML
    assert 'aria-busy="false"' in HTML
    assert 'role="radiogroup"' in renderer
    assert 'aria-labelledby="${playerLabelId}"' in renderer
    assert "app.medicalTreatmentBusy.add(playerId)" in renderer
    assert "app.medicalTreatmentBusy.delete(playerId)" in renderer
    assert ".treatment-options label:focus-within" in CSS
    reduced_motion = CSS.split("@media (prefers-reduced-motion: reduce)", 1)[1]
    assert ".hospital-sync-status > i" in reduced_motion
