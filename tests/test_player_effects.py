from __future__ import annotations

import pytest

from fm_odds_web import Handler
from tools.player_effects import (
    PlayerEffectOperationError,
    PlayerEffectRollbackError,
    commit_player_effect,
    describe_player_effect_error,
)
from tools.training_runtime import TrainingRosterIndex


def test_player_effect_commits_native_result_before_persistence() -> None:
    events: list[object] = []

    transaction = commit_player_effect(
        apply_effect=lambda: events.append("apply") or {"after": 20},
        persist_effect=lambda result: events.append(("persist", result["after"])) or {"saved": True},
        rollback_effect=lambda _result: events.append("rollback"),
    )

    assert events == ["apply", ("persist", 20)]
    assert transaction.native == {"after": 20}
    assert transaction.persisted == {"saved": True}


def test_player_effect_restores_native_state_when_persistence_fails() -> None:
    restored = []

    with pytest.raises(RuntimeError, match="inventory save failed"):
        commit_player_effect(
            apply_effect=lambda: {"before": 10, "after": 20},
            persist_effect=lambda _result: (_ for _ in ()).throw(
                RuntimeError("inventory save failed"),
            ),
            rollback_effect=restored.append,
        )

    assert restored == [{"before": 10, "after": 20}]


def test_player_effect_reports_incomplete_rollback() -> None:
    with pytest.raises(PlayerEffectRollbackError) as captured:
        commit_player_effect(
            apply_effect=lambda: {"after": 20},
            persist_effect=lambda _result: (_ for _ in ()).throw(
                RuntimeError("settlement failed"),
            ),
            rollback_effect=lambda _result: (_ for _ in ()).throw(
                OSError("restore failed"),
            ),
        )

    detail = describe_player_effect_error(
        captured.value, operation="training_settlement",
    )
    assert detail["error_code"] == "rollback_incomplete"
    assert detail["error_phase"] == "rollback"
    assert detail["retryable"] is False


@pytest.mark.parametrize(
    ("error", "code", "phase", "retryable"),
    [
        (PlayerEffectOperationError("stale", code="stale_context", phase="resolve_context", retryable=True), "stale_context", "resolve_context", True),
        (PlayerEffectOperationError("write mismatch", code="post_write_mismatch", phase="verify"), "post_write_mismatch", "verify", False),
        (PlayerEffectOperationError("unsupported layout", code="layout_mismatch", phase="validate_layout"), "layout_mismatch", "validate_layout", False),
        (OSError("OpenProcess failed"), "native_access", "native_access", True),
    ],
)
def test_player_effect_errors_have_stable_codes(error, code, phase, retryable) -> None:
    detail = describe_player_effect_error(error, operation="sea_cucumber")

    assert detail["error_code"] == code
    assert detail["error_phase"] == phase
    assert detail["retryable"] is retryable
    assert detail["operation"] == "sea_cucumber"


def test_due_training_reuses_the_live_player_address_resolver() -> None:
    player = {"id": 42, "name": "Player", "address": "0xOLD"}
    rebound: list[tuple[str, str]] = []
    calls: list[tuple[int, object, int, object]] = []
    index = TrainingRosterIndex(
        [{"team": {"id": 9, "address": "0xTEAM"}, "players": [player]}],
        [{"id": 9, "address": "0xTEAM"}],
        read_roster=lambda _address: pytest.fail("live resolver should avoid full roster expansion"),
        resolve_player_address=lambda team_id, team_address, player_id, hint: (
            calls.append((team_id, team_address, player_id, hint)) or "0xNEW"
        ),
        player_manager_candidate=lambda _team_id: None,
        rebind_player=lambda focus_id, address: rebound.append((focus_id, address)) or {},
        rebind_staff=lambda _focus_id, _address: {},
    )
    focus = {
        "id": "focus-1", "player_id": 42, "player_team_id": 9,
        "player_address": "0xOLD", "player_name": "Player",
    }

    resolved = index.resolve_player(focus, force_live=True)
    address = index.bind_player_address(focus, resolved)

    assert address == "0xNEW"
    assert calls == [(9, "0xTEAM", 42, "0xOLD")]
    assert rebound == [("focus-1", "0xNEW")]


def test_player_effect_routes_return_structured_diagnostics() -> None:
    payload = Handler._post_error_payload(
        "/api/training/run", PlayerEffectOperationError(
            "write mismatch", code="post_write_mismatch", phase="verify",
        ),
    )

    assert payload["error_code"] == "post_write_mismatch"
    assert payload["error_phase"] == "verify"
    assert payload["operation"] == "run"
    generic = Handler._post_error_payload(
        "/api/funds", RuntimeError("failed"),
    )
    assert generic == {
        "error": "failed",
        "error_code": "service_unavailable",
            "retryable": True,
            "error_phase": "runtime",
            "details": {"operation": "/api/funds"},
            "message_key": "error.service_unavailable",
            "message_params": {},
        }
