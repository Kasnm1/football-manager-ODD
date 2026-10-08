from __future__ import annotations

from types import SimpleNamespace
from threading import RLock

import pytest


def _host_for_scope_b() -> SimpleNamespace:
    return SimpleNamespace(
        output={"save_instance_id": "save-b", "account_scope_id": "scope-b"},
        connection_scope_id="scope-b",
        refreshing=True,
        reconciling=False,
        refresh_mode="fast",
        cache_verified=True,
        save_change_pending=True,
        data_version=8,
        status="正在切换存档",
        error=None,
        last_refresh_error_stage=None,
        refresh_reason="identity_change_full",
        refresh_stage="publish",
        refresh_progress={"phase": "publish"},
        _refresh_progress_updated_at=0.0,
        _refresh_stage_started_at=0.0,
        connection_requested=True,
        _public_state_context_key=lambda: ("scope-b",),
    )


@pytest.fixture
def scope_b_coordinator(tmp_path, monkeypatch):
    data_root = tmp_path / "fmodd-data"
    data_root.mkdir()
    monkeypatch.setenv("FMODD_DATA_ROOT", str(data_root))

    from tools.local_state_services import RefreshCoordinator
    from tools.refresh_status import RefreshStatusSnapshot

    coordinator = RefreshCoordinator(_host_for_scope_b())
    coordinator._last_status_snapshot = RefreshStatusSnapshot("old-context-a", {
        "wallet_ready": True,
        "wallet_scope_id": "scope-a",
        "wallet_balance": 875.0,
        "betting_ready": True,
        "save_change_pending": False,
        "manager_club_change_confirmation": {
            "scope_id": "scope-a",
            "team_id": 10,
        },
        "error_traceback": "scope-a refresh traceback",
        "operation_error_traceback": "scope-a operation traceback",
    })
    return coordinator


def test_contended_status_drops_cached_account_fields_after_scope_change(
    scope_b_coordinator,
):
    payload = scope_b_coordinator._contended_status(10.0)

    assert payload["status_snapshot_delayed"] is True
    assert payload["betting_ready"] is False
    assert payload.get("wallet_ready") is not True
    for field in (
        "wallet_scope_id",
        "wallet_balance",
        "error_traceback",
        "operation_error_traceback",
    ):
        assert field not in payload
    assert payload.get("manager_club_change_confirmation") is None


def test_contended_status_uses_current_save_change_pending(scope_b_coordinator):
    payload = scope_b_coordinator._contended_status(10.0)

    assert payload["status_snapshot_delayed"] is True
    assert payload["save_change_pending"] is True


def test_normal_status_does_not_relabel_old_wallet_balance(scope_b_coordinator):
    from tools.refresh_status import RefreshStatusSnapshot

    coordinator = scope_b_coordinator
    host = coordinator.host
    host.lock = RLock()
    host._has_verified_save = lambda: True
    host._data_scope_id = lambda output: output["account_scope_id"]
    host._refresh_state = lambda: {"data_version": host.data_version}
    host._refresh_trace_snapshot = lambda value: dict(value)
    host._public_state_context_key = lambda: (host.output["account_scope_id"],)
    host.output["account_scope_id"] = "scope-a"
    coordinator._last_status_snapshot = RefreshStatusSnapshot(
        coordinator._status_context_key(),
        {"wallet_scope_id": "scope-a", "wallet_balance": 875},
    )
    host.output["account_scope_id"] = "scope-b"

    payload = coordinator.status()

    assert payload["wallet_scope_id"] == "scope-b"
    assert "wallet_balance" not in payload
    assert not payload["status_snapshot_delayed"]


def test_unknown_context_never_reuses_a_cached_status(scope_b_coordinator):
    coordinator = scope_b_coordinator
    coordinator.host._public_state_context_key = lambda: None
    assert coordinator._status_context_key() is None
    payload = coordinator._contended_status(10)
    assert "wallet_scope_id" not in payload
    assert not payload["betting_ready"]


def test_refresh_snapshot_owns_its_input_and_each_returned_copy():
    from tools.refresh_status import RefreshStatusSnapshot

    source = {"wallet_scope_id": "scope-a", "details": {"count": 1}}
    snapshot = RefreshStatusSnapshot("scope-a", source)
    source["details"]["count"] = 2
    returned = snapshot.read("scope-a")
    assert returned["details"]["count"] == 1
    returned["details"]["count"] = 3
    assert snapshot.read("scope-a")["details"]["count"] == 1
    assert snapshot.read("scope-b") == {}
    assert snapshot.read(None) == {}
