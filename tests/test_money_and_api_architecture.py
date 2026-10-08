from __future__ import annotations

import re
import inspect
from io import BytesIO
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import fm_odds_web
from tools import betting_account, club_economy
from tools.api_routing import RouteRegistry
from tools.domain_errors import DomainError, ValidationError, normalize_error
from tools.local_state_services import AccountApplicationService, RefreshCoordinator
from tools.money import (
    from_minor, maximum_minor_for_product, multiply_minor, read_minor, to_minor,
)


def test_money_conversion_and_multiplication_never_use_binary_float_rounding():
    assert to_minor("0.105") == 11
    assert to_minor(0.1) + to_minor(0.2) == 30
    assert from_minor(multiply_minor(10_001, "1.25")) == 125.01
    assert maximum_minor_for_product(10_000, "1.25") == 8_000


def test_money_conversion_accepts_values_beyond_default_decimal_precision():
    assert to_minor("1e100") == 10 ** 102
    assert multiply_minor(10 ** 40, "1.25") == 125 * 10 ** 38
    assert maximum_minor_for_product(10 ** 40, "1.25") == 8 * 10 ** 39


def test_wallet_migration_does_not_abort_on_long_finite_legacy_amounts():
    wallet = {
        "schema_version": 1,
        "initial_balance": "1e100",
        "balance": "1e100",
        "transactions": [{"amount": "1e100", "balance_after": "1e100"}],
    }

    assert betting_account._normalize_wallet_money(wallet)
    assert wallet["balance_minor"] == 10 ** 102
    assert wallet["transactions"][0]["amount_minor"] == 10 ** 102


def test_wallet_money_migration_keeps_legacy_mirror_and_integer_source_in_sync():
    wallet = {
        "schema_version": 1,
        "initial_balance": 10000.0,
        "balance": 12.34,
        "transactions": [{"amount": 12.34, "balance_after": 12.34}],
    }

    assert betting_account._normalize_wallet_money(wallet)
    assert wallet["schema_version"] == 2
    assert wallet["balance_minor"] == 1234
    assert wallet["transactions"][0]["amount_minor"] == 1234

    # Once migrated, the integer field is authoritative and repairs stale mirrors.
    wallet["balance"] = 56.78
    assert read_minor(wallet, "balance") == 1234
    assert wallet["balance"] == 12.34


def test_economy_money_migration_covers_bank_credit_and_ledger():
    payload = club_economy._new_state()
    payload["schema_version"] = 3
    payload.pop("general_balance_minor", None)
    payload["general_balance"] = 123.45
    payload["credit"].update({
        "principal": 10.01, "original_principal": 10.01, "interest": 0.05,
    })
    payload["transactions"].append({
        "amount": -1.25, "general_balance_after": 122.20,
    })

    assert club_economy._normalize_economy_money(payload)
    assert payload["schema_version"] == 4
    assert payload["general_balance_minor"] == 12345
    assert payload["credit"]["principal_minor"] == 1001
    assert payload["transactions"][0]["amount_minor"] == -125


def test_domain_errors_have_stable_machine_readable_contract():
    error = ValidationError("金额无效", code="invalid_money", phase="validate_money")

    assert error.payload() == {
        "error": "金额无效",
        "error_code": "invalid_money",
        "retryable": False,
        "error_phase": "validate_money",
        "message_key": "error.invalid_request",
        "message_params": {},
    }
    assert normalize_error(RuntimeError("FM 暂不可用")).payload()["error_code"] == "service_unavailable"


def test_route_registry_rejects_duplicates_and_dispatches_exact_paths():
    registry = RouteRegistry()
    registry.add("POST", "/api/example", lambda _context, payload, _query: payload)

    assert registry.dispatch("POST", "/api/example", object(), {"ok": True}).payload == {"ok": True}
    assert registry.dispatch("GET", "/api/example", object()) is None
    try:
        registry.add("POST", "/api/example", lambda *_args: {})
    except ValueError as error:
        assert "重复路由" in str(error)
    else:
        raise AssertionError("duplicate route was accepted")


def test_json_routes_have_no_shadow_implementation_after_dispatch():
    for method in (fm_odds_web.Handler._do_GET, fm_odds_web.Handler.do_POST):
        source = inspect.getsource(method)
        after_dispatch = source.split("if route_result is not None:", 1)[1]
        assert not re.findall(r'if path == "(/api/[^"]+)"', after_dispatch)


def test_registered_state_routes_reference_real_methods_and_keep_status_contracts():
    for method, path, _name in fm_odds_web.API_ROUTES.describe():
        route = fm_odds_web.API_ROUTES.resolve(method, path)
        nonlocals = inspect.getclosurevars(route.handler).nonlocals
        method_name = nonlocals.get("method_name")
        if method_name:
            assert hasattr(fm_odds_web.LocalOddsState, method_name), (
                f"{method} {path} references missing LocalOddsState.{method_name}"
            )
    assert fm_odds_web.API_ROUTES.resolve(
        "POST", "/api/world-clubs/owned-refresh",
    ).success_status == HTTPStatus.OK


def test_update_reward_claim_route_is_registered_as_a_state_action():
    route = fm_odds_web.API_ROUTES.resolve(
        "POST", "/api/mail/version-update-reward/claim",
    )
    state = SimpleNamespace(
        claim_update_reward=Mock(return_value={"claimed": True}),
    )

    response = route.handler(
        SimpleNamespace(state=state), {"mail_id": "version_update_reward:2.2.0"}, {},
    )

    assert response == {"claimed": True}
    state.claim_update_reward.assert_called_once_with({
        "mail_id": "version_update_reward:2.2.0",
    })


def test_local_state_public_boundaries_delegate_to_services():
    state = fm_odds_web.LocalOddsState.__new__(fm_odds_web.LocalOddsState)
    assert isinstance(state._refresh_coordinator(), RefreshCoordinator)
    assert isinstance(state._account_application(), AccountApplicationService)

    refresh = Mock()
    refresh.start.return_value = True
    state.__dict__["_refresh_coordinator_service"] = refresh
    assert state.refresh_async("manual_fast") is True
    refresh.start.assert_called_once_with(
        "manual_fast", full=False, cancel_on_clock_change=False,
    )

    account = Mock()
    account.update_cheat_balance.return_value = {"balance": 1.0}
    state.__dict__["_account_application_service"] = account
    assert state.update_cheat_balance({"amount": 500}) == {"balance": 1.0}


def test_league_refresh_route_starts_standings_specific_refresh():
    refresh_async = Mock(return_value=True)
    handler = SimpleNamespace(
        state=SimpleNamespace(refresh_async=refresh_async),
    )

    result = fm_odds_web.API_ROUTES.dispatch(
        "POST", "/api/league/refresh", handler,
    )

    assert result is not None
    assert result.status == HTTPStatus.ACCEPTED
    assert result.payload == {"started": True}
    refresh_async.assert_called_once_with("manual_standings", full=False)


def test_player_movement_route_preserves_structured_domain_error():
    state = SimpleNamespace(
        move_owned_world_club_player=Mock(side_effect=RuntimeError("球队地址已变化")),
    )
    handler = SimpleNamespace(state=state)

    try:
        fm_odds_web._post_player_move_route(handler, {"mode": "transfer"}, {})
    except DomainError as error:
        payload = error.payload()
        assert payload["error_code"]
        assert "error_phase" in payload
        assert "retryable" in payload
    else:
        raise AssertionError("player movement failure was not normalized")


def test_unknown_api_and_non_object_json_use_structured_errors():
    handler = fm_odds_web.Handler.__new__(fm_odds_web.Handler)
    responses = []
    handler._json = lambda status, payload: responses.append((status, payload))
    handler.path = "/api/does-not-exist"
    handler._do_GET()
    assert int(responses[-1][0]) == 404
    assert responses[-1][1]["error_code"] == "not_found"

    handler.headers = {"Content-Length": "2"}
    handler.rfile = BytesIO(b"[]")
    handler._bind_request_scope = lambda: None
    handler.do_POST()
    assert int(responses[-1][0]) == 400
    assert responses[-1][1]["error_code"] == "invalid_request"


def test_get_validation_and_post_overflow_keep_structured_status_contracts():
    handler = fm_odds_web.Handler.__new__(fm_odds_web.Handler)
    responses = []
    handler._json = lambda status, payload: responses.append((status, payload))
    handler._bind_request_scope = lambda: None
    handler.path = "/api/team-results?team_id=invalid"
    handler.state = SimpleNamespace(public_team_results=lambda *_args: {})
    handler.do_GET()
    assert int(responses[-1][0]) == 400
    assert responses[-1][1]["error_code"] == "invalid_request"
    assert responses[-1][1]["retryable"] is False

    handler.path = "/api/funds"
    handler.headers = {"Content-Length": "12"}
    handler.rfile = BytesIO(b'{"amount":1}')
    handler.state = SimpleNamespace(
        add_funds=Mock(side_effect=OverflowError("too large")),
    )
    handler.do_POST()
    assert int(responses[-1][0]) == 400
    assert responses[-1][1]["error_code"] == "numeric_overflow"
    assert responses[-1][1]["error_phase"] == "validate_number"
