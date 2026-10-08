"""Account application boundary tests: no disk account or native session."""
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

import fm_odds_web as web
from tools import account_application as services


class OperationLock:
    def __init__(self):
        self.depth = 0

    def __enter__(self):
        self.depth += 1

    def __exit__(self, *args):
        self.depth -= 1


def account_state():
    state = web.LocalOddsState.__new__(web.LocalOddsState)
    state.lock = OperationLock()
    state.output = {"save_instance_id": "career-a", "game_date": "2028-01-01"}
    state.data_version = 7
    state.connection_scope_id = "account-a"
    state.club_contexts = {}
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda _output: "account-a"
    state._bind_current_save = Mock(return_value="account-a")
    state._bind_wallet_scope = Mock(return_value="account-a")
    return state


def test_wallet_update_holds_account_guard_through_write_and_projection(monkeypatch):
    state = account_state()

    def change_wallet(**kwargs):
        assert state.lock.depth > 0, "account guard released before wallet write"
        assert kwargs == {"amount": 250.0, "clear": False}
        return {"balance": 250.0}

    def economy():
        assert state.lock.depth > 0, "account guard released before projection"
        return {"casino_balance": 250.0}

    monkeypatch.setattr(services, "cheat_balance", change_wallet)
    monkeypatch.setattr(services, "public_economy", economy)
    result = state.update_cheat_balance({"amount": 250})
    assert result == {
        "balance": 250.0,
        "state_patch": {"balance": 250.0, "economy": {"casino_balance": 250.0}},
        "state_sync": {"data_version": 7, "data_scope_id": "account-a"},
    }
    assert state.lock.depth == 0


@pytest.mark.parametrize("method,payload,target,args,kwargs,transient", [
    ("shop_purchase", {"sku": "item-a", "quantity": "2", "free_purchase": True},
     "buy_item", ("item-a", 2), {"free": True}, ("payment",)),
    ("shop_batch_purchase", {"items": [{"sku": "item-a", "quantity": 2}]},
     "buy_items", ([{"sku": "item-a", "quantity": 2}],), {"free": False},
     ("payment", "purchases")),
    ("transfer_wallet", {"direction": "recharge", "amount": "25", "data_scope_id": "account-a"},
     "transfer_wallet", ("recharge", 25.0), {}, ()),
    ("grant_temporary_bank_cheat", {}, "adjust_bank_balance",
     (100_000_000_000_000, "temporary_bank_cheat"), {"source": "bank_cheat_button"}, ()),
])
def test_economy_mutations_keep_arguments_guard_and_transient_projection(
    monkeypatch, method, payload, target, args, kwargs, transient,
):
    state = account_state()
    response = {"casino_balance": 75, "bank_balance": 125,
                "payment": {"id": "p1"}, "purchases": ["item-a"]}

    def mutate(*received_args, **received_kwargs):
        assert state.lock.depth == 1
        assert received_args == args
        assert received_kwargs == kwargs
        return dict(response)

    monkeypatch.setattr(services, target, mutate)
    result = getattr(state._account_application(), method)(payload)
    assert {key: result[key] for key in response} == response
    assert result["state_patch"] == {
        "balance": 75.0,
        "economy": {key: value for key, value in response.items() if key not in transient},
    }
    assert result["state_sync"] == {"data_version": 7, "data_scope_id": "account-a"}
    assert state.lock.depth == 0
    if method in {"transfer_wallet", "grant_temporary_bank_cheat"}:
        state._bind_wallet_scope.assert_called_once_with(payload.get("data_scope_id", ""))
        state._bind_current_save.assert_not_called()
    else:
        state._bind_current_save.assert_called_once_with()
        state._bind_wallet_scope.assert_not_called()


def test_credit_clear_uses_guarded_context_and_preserves_status(monkeypatch):
    state = account_state()
    state._total_weekly_salary = Mock(return_value=120.0)
    monkeypatch.setattr(services, "clear_credit_cheat", lambda: {"bank_balance": 900})
    monkeypatch.setattr(services, "credit_effective_bet_count", lambda: 12)

    def status(game_date, salary, effective_bets):
        assert state.lock.depth == 1
        assert (game_date, salary, effective_bets) == ("2028-01-01", 120.0, 12)
        return {"eligible": True}

    monkeypatch.setattr(services, "credit_status", status)
    result = state._account_application().clear_credit()
    assert result["credit"] == {"eligible": True}
    assert result["state_patch"]["economy"]["credit"] == result["credit"]
    state._total_weekly_salary.assert_called_once_with(state.club_contexts)
    assert state.lock.depth == 0


def test_account_guard_rejects_scope_before_any_wallet_write(monkeypatch):
    state = account_state()
    state._bind_wallet_scope.side_effect = ValueError("scope changed")
    transfer = Mock()
    monkeypatch.setattr(services, "transfer_wallet", transfer)
    with pytest.raises(ValueError, match="scope changed"):
        state._account_application().transfer_wallet({"data_scope_id": "other"})
    transfer.assert_not_called()
    assert state.lock.depth == 0


def test_wallet_response_is_tagged_with_the_account_actually_bound(monkeypatch):
    state = account_state()
    state._bind_wallet_scope.return_value = "account-wallet"
    monkeypatch.setattr(services, "transfer_wallet", lambda *args: {"casino_balance": 25})
    result = state._account_application().transfer_wallet({
        "data_scope_id": "account-wallet", "direction": "recharge", "amount": 25,
    })
    assert result["state_sync"] == {"data_version": 7, "data_scope_id": "account-wallet"}


def test_welfare_patch_is_bound_before_http_response_finishing(monkeypatch):
    state = account_state()
    monkeypatch.setattr(services, "set_free_services", lambda enabled: {"enabled": enabled})
    monkeypatch.setattr(services, "public_economy", lambda: {"casino_balance": 150})
    response = state.update_free_services({"enabled": True})
    # The response serializer can run after a concurrent manager/account switch.
    state._data_scope_id = lambda output: "account-b"
    finished = state._mutation_patch_response(response)
    assert finished["state_sync"]["data_scope_id"] == "account-a"
    assert finished["state_patch"] == {
        "economy": {"casino_balance": 150}, "balance": 150.0,
        "welfare": {"enabled": True},
    }


def test_storage_failure_releases_guard_without_retry_or_projection(monkeypatch):
    state = account_state()
    write = Mock(side_effect=OSError("failed account commit"))
    project = Mock()
    monkeypatch.setattr(services, "cheat_balance", write)
    monkeypatch.setattr(services, "public_economy", project)
    with pytest.raises(OSError, match="failed account commit"):
        state.update_cheat_balance({"clear": True, "amount": "ignored"})
    write.assert_called_once_with(amount=None, clear=True)
    project.assert_not_called()
    assert state.lock.depth == 0


def test_account_service_runs_with_explicit_context_without_runtime_host(monkeypatch):
    events = []

    @contextmanager
    def operation(scope):
        events.append(("enter", scope))
        try:
            yield services.AccountOperation(
                scope_id="account-a", game_date="2028-01-01",
                weekly_salary=lambda: 0, temporary_bank_cheat=(1, "test"),
                state_patch=lambda response, patch: {**response, "patch": patch},
                economy_patch=lambda response, **kwargs: response,
            )
        finally:
            events.append(("exit", scope))

    def add(amount):
        assert events[-1] == ("enter", None)
        return {"balance": amount}

    monkeypatch.setattr(services, "add_wallet_funds", add)
    monkeypatch.setattr(services, "set_free_services", lambda enabled: {"enabled": enabled})
    monkeypatch.setattr(services, "public_economy", lambda: {"bank_balance": 100})
    service = services.AccountApplicationService(operation)
    assert service.add_funds(50) == {"balance": 50}
    assert service.update_free_services({"enabled": True}) == {
        "economy": {"bank_balance": 100}, "welfare": {"enabled": True},
        "patch": {"economy": {"bank_balance": 100}, "welfare": {"enabled": True}},
    }
    assert events == [("enter", None), ("exit", None)] * 2
