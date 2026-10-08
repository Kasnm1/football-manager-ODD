from http import HTTPStatus
from io import BytesIO
from contextlib import contextmanager
from types import SimpleNamespace
import json

import pytest

import fm_odds_web as web
from tools.api_routing import RouteRegistry
from tools.domain_errors import ValidationError


def handler_for(method, path):
    handler = web.Handler.__new__(web.Handler)
    handler.command = method
    handler.path = path
    handler.headers = {"Content-Length": "2"}
    handler.rfile = BytesIO(b"{}")
    handler._bind_request_scope = lambda: None
    responses = []
    handler._json = lambda status, payload: responses.append((status, payload))
    return handler, responses


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_registered_json_request_dispatches_once(monkeypatch, method):
    routes = RouteRegistry()
    calls = []

    def action(handler, payload, query):
        calls.append((payload, query))
        return {"result": "registered"}

    routes.add(method, "/api/unit", action, status=HTTPStatus.ACCEPTED)
    monkeypatch.setattr(web, "API_ROUTES", routes)
    handler, responses = handler_for(method, "/api/unit?q=1")
    (handler._do_GET if method == "GET" else handler.do_POST)()
    assert responses == [(HTTPStatus.ACCEPTED, {"result": "registered"})]
    assert calls == [({}, {"q": ["1"]} if method == "GET" else {})]


@pytest.mark.parametrize("method,path", [("GET", "/api/health"), ("POST", "/api/funds")])
def test_missing_registration_cannot_fall_through_to_a_second_implementation(monkeypatch, method, path):
    monkeypatch.setattr(web, "API_ROUTES", RouteRegistry())
    handler, responses = handler_for(method, path)
    (handler._do_GET if method == "GET" else handler.do_POST)()
    assert int(responses[0][0]) == 404
    assert responses[0][1]["error_code"] == "not_found"


def test_static_resource_fallback_still_bypasses_json_routes(monkeypatch):
    monkeypatch.setattr(web, "API_ROUTES", RouteRegistry())
    handler, responses = handler_for("GET", "/index.html")
    assets = []
    handler._static = assets.append
    handler._do_GET()
    assert assets == ["/index.html"]
    assert not responses


def test_post_dispatch_marks_memory_acquisitions_as_foreground(monkeypatch):
    events = []

    class RequestAwareLock:
        @contextmanager
        def foreground_request(self):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

    routes = RouteRegistry()

    def action(_handler, _payload, _query):
        events.append("dispatch")
        return {"ok": True}

    routes.add("POST", "/api/unit", action)
    monkeypatch.setattr(web, "API_ROUTES", routes)
    handler, responses = handler_for("POST", "/api/unit")
    handler.state = SimpleNamespace(memory_lock=RequestAwareLock())
    handler.do_POST()

    assert responses == [(HTTPStatus.OK, {"ok": True})]
    assert events == ["enter", "dispatch", "exit"]


@pytest.mark.parametrize("path", ["/api/bets", "/api/inventory/use"])
def test_authored_business_validation_errors_allow_specific_ui_detail(path):
    payload = web.Handler._post_error_payload(path, ValueError("具体失败原因"))

    assert payload["error"] == "具体失败原因"
    assert payload["message_key"] == "error.invalid_request"
    assert payload["error_detail_safe"] is True


@pytest.mark.parametrize("path", ["/api/bets", "/api/inventory/use"])
def test_post_dispatch_preserves_specific_business_reason_for_shared_request_ui(
    monkeypatch, path,
):
    routes = RouteRegistry()

    def reject(_handler, _payload, _query):
        raise ValueError("盘口或道具的具体失败原因")

    routes.add("POST", path, reject)
    monkeypatch.setattr(web, "API_ROUTES", routes)
    handler, responses = handler_for("POST", path)

    handler.do_POST()

    status, payload = responses[0]
    assert status == HTTPStatus.BAD_REQUEST
    assert payload["message_key"] == "error.invalid_request"
    assert payload["message"] == "The request is invalid."
    assert payload["error"] == "盘口或道具的具体失败原因"
    assert payload["error_detail_safe"] is True


@pytest.mark.parametrize(
    "error",
    [
        TypeError("internal type detail"),
        KeyError("internal key detail"),
        RuntimeError("internal runtime detail"),
        json.JSONDecodeError("internal JSON detail", "{", 1),
        ValidationError("semantic detail", message_key="error.resource_not_found"),
    ],
)
def test_untrusted_or_semantic_errors_do_not_override_localized_messages(error):
    payload = web.Handler._post_error_payload("/api/bets", error)

    assert "error_detail_safe" not in payload
