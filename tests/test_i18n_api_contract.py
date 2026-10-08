from __future__ import annotations

from tools.domain_errors import ValidationError
from tools.i18n_runtime import (
    DEFAULT_LOCALE,
    LocalizedMessage,
    MAIL_TEMPLATE_KEYS,
    mail_template_fields,
    normalize_locale,
    SUPPORTED_LOCALES,
)

import io
import json
import re
import fm_odds_web
from tools.i18n_runtime import MESSAGE_CATALOGS
from tools.preferred_moves import PreferredMoveStateError


def _handler(locale: str | None = None, *, app_locale: str | None = None) -> fm_odds_web.Handler:
    handler = fm_odds_web.Handler.__new__(fm_odds_web.Handler)
    handler.headers = {}
    if locale:
        handler.headers["Accept-Language"] = locale
    if app_locale:
        handler.headers["X-FMODD-Locale"] = app_locale
    return handler


def test_unknown_locale_and_missing_key_fall_back_to_authoritative_english() -> None:
    assert normalize_locale("it-IT,it;q=0.9") == DEFAULT_LOCALE
    assert LocalizedMessage("error.resource_not_found", {"resource": "Club"}).render(
        "it-IT",
    ) == "Club was not found."
    assert LocalizedMessage("error.unknown").render("zh-CN") == (
        "The request could not be completed."
    )
    assert normalize_locale("de-DE,de;q=0.9") == "de-DE"
    assert normalize_locale("zh-TW,zh;q=0.9") == "zh-TW"
    assert normalize_locale("zh-Hant-TW") == "zh-TW"
    assert normalize_locale("es-MX") == "es-ES"
    assert normalize_locale("fr-CA") == "fr-FR"
    assert normalize_locale("ru") == "ru-RU"
    assert normalize_locale("ja_JP") == "ja-JP"
    assert normalize_locale("pt-BR") == "pt-BR"
    assert normalize_locale("pt_BR") == "pt-BR"
    assert normalize_locale("pt-PT") == "pt-PT"


def test_api_catalog_keys_and_parameters_match_across_locales() -> None:
    assert set(MESSAGE_CATALOGS) == set(SUPPORTED_LOCALES)
    assert {"pt-BR", "pt-PT"} <= set(MESSAGE_CATALOGS)
    key_sets = {locale: set(catalog) for locale, catalog in MESSAGE_CATALOGS.items()}
    assert all(keys == key_sets["en-GB"] for keys in key_sets.values())
    for key in key_sets["en-GB"]:
        placeholders = {
            locale: set(re.findall(r"\{([A-Za-z0-9_]+)\}", catalog[key]))
            for locale, catalog in MESSAGE_CATALOGS.items()
        }
        assert all(value == placeholders["en-GB"] for value in placeholders.values()), key


def test_localized_message_interpolates_factual_parameters() -> None:
    assert LocalizedMessage(
        "error.resource_not_found", {"resource": "Team Aurora"},
    ).render("en-GB") == "Team Aurora was not found."


def test_korean_catalog_renders_the_same_semantic_key() -> None:
    assert LocalizedMessage("error.service_unavailable").render("ko-KR") == (
        "로컬 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도하세요."
    )


def test_traditional_chinese_catalog_uses_taiwanese_system_terms() -> None:
    assert LocalizedMessage("error.service_unavailable").render("zh-TW") == (
        "本機服務暫時無法使用，請稍後再試。"
    )


def test_explicit_app_locale_overrides_browser_accept_language() -> None:
    handler = _handler("zh-CN", app_locale="ko-KR")
    payload = handler._localized_error_payload(
        RuntimeError("diagnostic only"), operation="/api/funds",
    )
    assert payload["message"] == (
        "로컬 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도하세요."
    )


def test_localized_api_error_preserves_legacy_fields() -> None:
    error = ValidationError(
        "原有客户端错误文本", code="invalid_money", phase="validate_money",
        details={"field": "amount"},
    )

    payload = _handler()._localized_error_payload(error, operation="/api/funds")

    assert payload["error"] == "原有客户端错误文本"
    assert payload["error_code"] == "invalid_money"
    assert payload["error_phase"] == "validate_money"
    assert payload["retryable"] is False
    assert payload["details"] == {"field": "amount"}
    assert payload["message"] == "The request is invalid."
    assert payload["message_key"] == "error.invalid_request"
    assert payload["message_params"] == {}


def test_localized_message_never_renders_raw_exception_text() -> None:
    raw_exception = RuntimeError("database password=do-not-expose")

    payload = _handler("ko-KR")._localized_error_payload(
        raw_exception, operation="/api/funds",
    )

    assert payload["error"] == "database password=do-not-expose"
    assert "database password" not in payload["message"]
    assert "database password" not in payload["message_key"]
    assert "database password" not in str(payload["message_params"])
    assert payload["message"] == "로컬 서비스를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도하세요."


def test_domain_errors_carry_semantic_message_metadata() -> None:
    error = ValidationError(
        "legacy diagnostic", message_key="error.resource_not_found",
        message_params={"resource": "Club"},
    )
    payload = error.payload()
    assert payload["message_key"] == "error.resource_not_found"
    assert payload["message_params"] == {"resource": "Club"}


def test_preferred_move_state_uses_stable_codes_and_localized_messages() -> None:
    error = PreferredMoveStateError("learn")
    payload = _handler(app_locale="ko-KR")._localized_error_payload(
        error, operation="/api/training/run",
    )

    assert error.already_satisfied is True
    assert payload["error_code"] == "preferred_move_already_known"
    assert payload["error_phase"] == "validate_state"
    assert payload["details"] == {"operation": "learn", "already_satisfied": True}
    assert payload["message_key"] == "training.preferred_move.already_known"
    assert payload["message"] == "선수가 이미 이 개인 습관을 보유하고 있습니다."


def test_new_mail_metadata_uses_stable_versioned_semantic_keys() -> None:
    fields = mail_template_fields(
        "manager_salary", {"date": "2028-02-01", "amount": 1250.0},
    )

    assert fields == {
        "title_key": "mail.type.manager_salary.title",
        "message_key": "mail.type.manager_salary.message",
        "template_key": "mail.type.manager_salary.message",
        "template_version": 1,
        "template_params": {"date": "2028-02-01", "amount": 1250.0},
    }
    assert "sponsorship_payment" in MAIL_TEMPLATE_KEYS
    assert mail_template_fields("legacy_unknown", {"value": 1}) == {}


def test_legacy_handcrafted_http_errors_gain_safe_localized_metadata() -> None:
    handler = _handler("en-GB", app_locale="ko-KR")
    handler.command = "GET"
    handler.headers["Accept-Encoding"] = ""
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None
    handler.end_headers = lambda: None

    handler._json(fm_odds_web.HTTPStatus.NOT_FOUND, {"error": "내부 raw 中文"})

    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
    assert payload["error"] == "내부 raw 中文"
    assert payload["message"] == "요청한 리소스를 찾을 수 없습니다."
    assert payload["message_key"] == "error.not_found"
