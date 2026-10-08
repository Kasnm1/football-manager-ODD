from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class PlayerEffectOperationError(RuntimeError):
    """A classified failure raised by the shared player-effect boundary."""

    def __init__(
        self, message: str, *, code: str, phase: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = str(code)
        self.error_phase = str(phase)
        self.retryable = bool(retryable)


class PlayerEffectRollbackError(PlayerEffectOperationError):
    """The native effect failed to restore after persistence failed."""

    def __init__(self, operation_error: Exception, rollback_error: Exception) -> None:
        super().__init__(
            f"效果后续提交失败，且内存恢复未完成：{rollback_error}",
            code="rollback_incomplete", phase="rollback",
        )
        self.operation_error = operation_error
        self.rollback_error = rollback_error


@dataclass(frozen=True)
class PlayerEffectTarget:
    """One live player resolved from stable player/team identities."""

    player_id: int
    team_id: int
    address: Any
    profile: dict[str, Any]
    player: dict[str, Any]
    team: dict[str, Any]


@dataclass(frozen=True)
class PlayerEffectTransactionResult:
    """Native result plus the durable result committed after it."""

    native: Any
    persisted: Any


def commit_player_effect(
    *, apply_effect: Callable[[], Any],
    persist_effect: Callable[[Any], Any],
    rollback_effect: Callable[[Any], None],
) -> PlayerEffectTransactionResult:
    """Apply native state, persist its settlement, and restore on failure."""
    native = apply_effect()
    try:
        persisted = persist_effect(native)
    except Exception as error:
        try:
            rollback_effect(native)
        except Exception as rollback_error:
            raise PlayerEffectRollbackError(error, rollback_error) from error
        raise
    return PlayerEffectTransactionResult(native=native, persisted=persisted)


def describe_player_effect_error(
    error: Exception, *, operation: str = "player_effect",
) -> dict[str, Any]:
    """Return stable diagnostics without exposing process addresses."""
    message = str(error).strip() or type(error).__name__
    code = str(getattr(error, "error_code", "") or "")
    phase = str(getattr(error, "error_phase", "") or "")
    retryable = bool(getattr(error, "retryable", False))

    if code:
        phase = phase or "apply"
    elif isinstance(error, OSError):
        code, phase, retryable = "native_access", "native_access", True
    elif isinstance(error, (ValueError, KeyError, TypeError)):
        code, phase = "invalid_request", "request"
    else:
        code, phase = "effect_failed", "apply"

    return {
        "error": message,
        "error_code": code,
        "error_phase": phase or "apply",
        "retryable": retryable,
        "operation": str(operation or "player_effect"),
        "error_type": type(error).__name__,
    }


__all__ = [
    "PlayerEffectOperationError",
    "PlayerEffectRollbackError",
    "PlayerEffectTarget",
    "PlayerEffectTransactionResult",
    "commit_player_effect",
    "describe_player_effect_error",
]
