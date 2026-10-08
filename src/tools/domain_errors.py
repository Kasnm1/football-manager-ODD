from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Mapping

from tools.money import InvalidMoney


@dataclass(eq=False)
class DomainError(Exception):
    code: str
    message: str
    status: HTTPStatus = HTTPStatus.BAD_REQUEST
    phase: str | None = None
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    message_key: str | None = None
    message_params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "error": self.message,
            "error_code": self.code,
            "retryable": bool(self.retryable),
        }
        if self.phase:
            result["error_phase"] = self.phase
        if self.details:
            result["details"] = dict(self.details)
        if self.message_key:
            result["message_key"] = self.message_key
            result["message_params"] = dict(self.message_params)
        return result


class ValidationError(DomainError):
    def __init__(self, message: str, *, code: str = "invalid_request", phase: str = "validate", details: Mapping[str, Any] | None = None, message_key: str = "error.invalid_request", message_params: Mapping[str, Any] | None = None) -> None:
        super().__init__(code, message, HTTPStatus.BAD_REQUEST, phase, False, details or {}, message_key, message_params or {})


class ConflictError(DomainError):
    def __init__(self, message: str, *, code: str = "state_conflict", phase: str = "commit", retryable: bool = True, details: Mapping[str, Any] | None = None, message_key: str = "error.request_failed", message_params: Mapping[str, Any] | None = None) -> None:
        super().__init__(code, message, HTTPStatus.CONFLICT, phase, retryable, details or {}, message_key, message_params or {})


class NotFoundError(DomainError):
    def __init__(self, message: str, *, code: str = "not_found", details: Mapping[str, Any] | None = None, message_key: str = "error.not_found", message_params: Mapping[str, Any] | None = None) -> None:
        super().__init__(code, message, HTTPStatus.NOT_FOUND, "resolve", False, details or {}, message_key, message_params or {})


class UnavailableError(DomainError):
    def __init__(self, message: str, *, code: str = "service_unavailable", phase: str = "runtime", retryable: bool = True, details: Mapping[str, Any] | None = None, message_key: str = "error.service_unavailable", message_params: Mapping[str, Any] | None = None) -> None:
        super().__init__(code, message, HTTPStatus.SERVICE_UNAVAILABLE, phase, retryable, details or {}, message_key, message_params or {})


def normalize_error(error: Exception, *, operation: str | None = None) -> DomainError:
    if isinstance(error, DomainError):
        return error
    details = {"operation": operation} if operation else {}
    if isinstance(error, (InvalidMoney, ValueError, KeyError, TypeError)):
        return ValidationError(str(error), details=details)
    if isinstance(error, FileNotFoundError):
        return NotFoundError(str(error), details=details)
    if isinstance(error, (TimeoutError, ConnectionError)):
        return UnavailableError(str(error), details=details)
    if isinstance(error, RuntimeError):
        return UnavailableError(str(error), details=details)
    return DomainError(
        "internal_error", str(error) or type(error).__name__,
        HTTPStatus.INTERNAL_SERVER_ERROR, "internal", False, details,
        "error.internal_error", {},
    )
