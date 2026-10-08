"""The FM/ODD boundary is exercised with an in-memory fake, never a process."""
from contextlib import nullcontext
from copy import deepcopy

import pytest

import fm_odds_web as web


def sponsorship_case(monkeypatch, *, balance=100, native=None, fail_paid=False,
                     fail_rollback=False, already_paid=False):
    events = []
    funds = [balance]
    payment = {"payment_id": "p1", "amount": 25,
               "status": "paid" if already_paid else "pending_native_credit",
               "native": native or {}}
    state = web.LocalOddsState.__new__(web.LocalOddsState)
    state._timed_user_memory_operation = lambda _name: nullcontext()
    state._owned_world_club_target = lambda _team: (
        "account-a", {"game_date": "2028-01-01"}, {"address": 123},
    )
    monkeypatch.setattr(web, "accept_sponsorship_offer", lambda *a, **k: {
        "contract": {"contract_id": "c1"}, "payment": deepcopy(payment),
    })
    monkeypatch.setattr(web, "borrow_game_operation", lambda **k: nullcontext(object()))

    def read(*args, **kwargs):
        events.append(("read", funds[0]))
        return {"amount": funds[0]}

    def write(address, amount, *, expected, **kwargs):
        assert funds[0] == expected
        events.append(("write", amount))
        if fail_rollback and amount == 100:
            raise RuntimeError("rollback unavailable")
        funds[0] = amount
        return {"amount": amount}

    def mark(scope, payment_id, *, status, native):
        events.append(("mark", status))
        if fail_paid and status == "paid":
            raise RuntimeError("persist unavailable")
        payment.update(status=status, native=deepcopy(native))
        return deepcopy(payment)

    monkeypatch.setattr(web, "read_club_balance", read)
    monkeypatch.setattr(web, "write_club_balance", write)
    monkeypatch.setattr(web, "mark_sponsorship_payment", mark)
    monkeypatch.setattr(web, "rollback_sponsorship_acceptance",
                        lambda *a, **k: events.append(("rollback", "contract")))
    monkeypatch.setattr(web, "update_sponsorship_offer_mail",
                        lambda *a, **k: events.append(("mail", "accepted")))
    monkeypatch.setattr(web, "archive_sponsorship_contract_mail",
                        lambda *a, **k: events.append(("mail", "contract")))
    monkeypatch.setattr(web, "public_sponsorship_state", lambda *a: {})
    return state, events, funds


def accept(state):
    return state.accept_owned_world_club_sponsorship({"team_id": 10, "offer_id": "o1"})


def test_sponsorship_records_intent_before_native_write_and_commit(monkeypatch):
    state, events, funds = sponsorship_case(monkeypatch)
    result = accept(state)
    assert events == [
        ("read", 100), ("mark", "pending_native_credit"), ("write", 125),
        ("read", 125), ("mark", "paid"), ("mail", "accepted"), ("mail", "contract"),
    ]
    assert result["club_balance"] == funds[0] == 125


def test_sponsorship_retry_after_native_write_does_not_credit_twice(monkeypatch):
    state, events, funds = sponsorship_case(
        monkeypatch, balance=125, native={"before": 100, "after": 125},
    )
    assert accept(state)["club_balance"] == 125
    assert not any(event[0] == "write" for event in events)
    assert funds == [125]


def test_sponsorship_paid_retry_does_not_open_native_session(monkeypatch):
    state, events, funds = sponsorship_case(monkeypatch, already_paid=True)
    assert accept(state)["idempotent"]
    assert events == [("mail", "accepted")]
    assert funds == [100]


def test_sponsorship_persistence_failure_restores_native_then_contract(monkeypatch):
    state, events, funds = sponsorship_case(monkeypatch, fail_paid=True)
    with pytest.raises(RuntimeError, match="persist unavailable"):
        accept(state)
    assert funds == [100]
    assert events[-2:] == [("write", 100), ("rollback", "contract")]
    assert not any(event[0] == "mail" for event in events)


def test_sponsorship_failed_native_rollback_keeps_recovery_record(monkeypatch):
    state, events, funds = sponsorship_case(monkeypatch, fail_paid=True, fail_rollback=True)
    with pytest.raises(RuntimeError, match="rollback unavailable"):
        accept(state)
    assert funds == [125]
    assert ("rollback", "contract") not in events
    assert not any(event[0] == "mail" for event in events)
