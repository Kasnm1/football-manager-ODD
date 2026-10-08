"""Exercise the actual HTTP serializer using memory streams and fake routes."""
import json
from contextvars import Context
from http import HTTPStatus
from io import BytesIO
from threading import RLock

import pytest

import fm_odds_web as web
from tools.api_routing import RouteRegistry


def request_case(monkeypatch, action):
    state = web.LocalOddsState.__new__(web.LocalOddsState)
    state.lock = RLock()
    state.output = {"save_instance_id": "career-a", "account_scope_id": "account-a"}
    state.connection_scope_id = "account-a"
    state.data_version = 7
    state._has_verified_save = lambda: True
    state._data_scope_id = lambda output: output["account_scope_id"]
    state._bind_request_scope = lambda: web.set_active_save_id("account-a")
    handler = web.Handler.__new__(web.Handler)
    handler.state = state
    handler.command = "POST"
    handler.path = "/api/test-mutation"
    handler.headers = {"Content-Length": "2"}
    handler.rfile = BytesIO(b"{}")
    handler.wfile = BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    routes = RouteRegistry()
    routes.add("POST", handler.path, lambda *args: action(state))
    monkeypatch.setattr(web, "API_ROUTES", routes)
    handler.do_POST()
    assert statuses == [HTTPStatus.OK]
    return json.loads(handler.wfile.getvalue())


@pytest.mark.parametrize("change", ["account", "version", "binding_round_trip"])
def test_changed_context_does_not_relabel_an_unproven_mutation_patch(monkeypatch, change):
    def mutate(state):
        result = {"economy": {"casino_balance": 150}}
        if change == "account":
            state.output["account_scope_id"] = "account-b"
        elif change == "version":
            state.data_version += 1
        else:
            web.set_active_save_id("account-b")
            web.set_active_save_id("account-a")
        return result

    assert request_case(monkeypatch, mutate) == {"economy": {"casino_balance": 150}}


def test_unchanged_context_keeps_the_existing_http_patch(monkeypatch):
    result = request_case(monkeypatch, lambda state: {"economy": {"casino_balance": 150}})
    assert result["state_patch"] == {"economy": {"casino_balance": 150}, "balance": 150.0}
    assert result["state_sync"] == {"data_version": 7, "data_scope_id": "account-a"}


def test_explicit_domain_provenance_is_not_rewritten_by_http(monkeypatch):
    expected = {"economy": {"casino_balance": 150}, "state_patch": {"balance": 150},
                "state_sync": {"data_version": 7, "data_scope_id": "account-a"}}

    def mutate(state):
        state.output["account_scope_id"] = "account-b"
        return expected

    assert request_case(monkeypatch, mutate) == expected


def test_rebinding_the_same_account_does_not_force_a_reload(monkeypatch):
    def mutate(state):
        web.set_active_save_id("account-a")
        return {"welfare": {"enabled": True}}

    assert request_case(monkeypatch, mutate)["state_sync"]["data_scope_id"] == "account-a"


def test_unbound_request_result_does_not_gain_account_metadata(monkeypatch):
    def mutate(state):
        web.set_active_save_id(None)
        return {"welfare": {"enabled": True}}

    assert request_case(monkeypatch, mutate) == {"welfare": {"enabled": True}}


def test_context_is_rechecked_after_optional_mail_projection(monkeypatch):
    def mutate(state):
        state.last_connection_clock = {"date": "2028-01-01"}

        def mail(date):
            state.data_version += 1
            return [{"id": "old-account-mail"}]

        monkeypatch.setattr(web, "visible_mail_records", mail)
        return {"notice": {"id": "n1"}}

    assert request_case(monkeypatch, mutate) == {"notice": {"id": "n1"}}


def test_binding_revision_tracks_transitions_but_not_identical_rebinding():
    from tools.app_paths import active_account_context, set_active_save_id

    def scenario():
        assert active_account_context() == (None, 0)
        set_active_save_id("account a")
        first = active_account_context()
        set_active_save_id("account_a")
        assert active_account_context() == first
        set_active_save_id("account-b")
        set_active_save_id("account_a")
        assert active_account_context() == (first[0], first[1] + 2)

    Context().run(scenario)


def test_account_binding_provenance_is_context_local():
    from tools.app_paths import active_account_context, set_active_save_id

    parent = Context()
    parent.run(set_active_save_id, "account-a")
    original = parent.run(active_account_context)
    child = parent.copy()
    child.run(set_active_save_id, "account-b")
    assert parent.run(active_account_context) == original
    assert child.run(active_account_context)[0] == "account-b"


def test_restore_account_binding_records_a_real_transition(monkeypatch):
    from tools import app_paths

    monkeypatch.setattr(app_paths, "_LAST_ACTIVE_SAVE_ID", "account-a")

    def scenario():
        app_paths.set_active_save_id("account-b")
        previous = app_paths.active_account_context()[1]
        assert app_paths.restore_active_save_id() == "account-a"
        assert app_paths.active_account_context() == ("account-a", previous + 1)

    Context().run(scenario)
