"""Apply one ODD sponsorship payment to FM with recoverable intent records.

This use case owns the cross-system ordering, not locks, version layouts,
account formats or native memory mechanics. Its caller validates ownership and
holds the existing foreground operation gate; injected I/O retains those checks.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ContextManager


@dataclass(frozen=True)
class SponsorshipPaymentIO:
    borrow_operation: Callable[..., ContextManager[Any]]
    read_balance: Callable[..., dict[str, Any]]
    write_balance: Callable[..., dict[str, Any]]
    mark_payment: Callable[..., dict[str, Any]]
    rollback_acceptance: Callable[..., Any]


def credit_sponsorship_payment(*, scope_id: str, team_id: int, offer_id: str,
                               team_address: Any, payment: dict[str, Any],
                               io: SponsorshipPaymentIO) -> tuple[dict[str, Any], int]:
    """Credit an accepted, unpaid payment; preserve its recovery record on doubt."""
    amount = int(payment["amount"])
    before: int | None = None
    after: int | None = None
    rollback_before: int | None = None
    rollback_after: int | None = None
    native = dict(payment.get("native") or {})
    try:
        with io.borrow_operation(write_memory=True) as operation:
            current = io.read_balance(team_address, team_id, operation=operation)
            before = int(current["amount"])
            recorded_before = native.get("before")
            recorded_after = native.get("after")
            if recorded_before is not None and recorded_after is not None:
                rollback_before = int(recorded_before)
                rollback_after = int(recorded_after)
                if before == rollback_after:
                    after = rollback_after
                elif before == rollback_before:
                    after = rollback_after
                else:
                    raise RuntimeError("俱乐部结余已发生未预期变化，请刷新后重试")
            else:
                rollback_before = before
                after = before + amount
                rollback_after = after
                native = {
                    "team_id": team_id, "before": rollback_before,
                    "after": rollback_after, "amount": amount,
                    "source": "odd_simulated",
                }
                # Persist the intended native transition before the
                # write.  A retry can then distinguish "not written"
                # from "written but not yet marked paid" without
                # crediting the amount twice.
                payment = io.mark_payment(
                    scope_id, str(payment["payment_id"]),
                    status="pending_native_credit", native=native,
                )
            if before == rollback_before:
                written = io.write_balance(
                    team_address, after, team_id=team_id,
                    expected=before, operation=operation,
                )
                after = int(written["amount"])
                verified = io.read_balance(team_address, team_id, operation=operation)
                if int(verified["amount"]) != after:
                    raise RuntimeError("赞助费写入后回读不一致")
        payment = io.mark_payment(
            scope_id, str(payment["payment_id"]), status="paid", native=native,
        )
    except Exception as error:
        # io.write_balance already restores failed writes.  If this
        # attempt did write successfully but persistence failed, put
        # the native field back before deleting the ODD contract.
        if (
            rollback_before is not None and rollback_after is not None
            and rollback_after != rollback_before
        ):
            try:
                with io.borrow_operation(write_memory=True) as operation:
                    current = io.read_balance(
                        team_address, team_id, operation=operation,
                    )
                    current_amount = int(current["amount"])
                    if current_amount == rollback_after:
                        io.write_balance(
                            team_address, rollback_before, team_id=team_id,
                            expected=rollback_after, operation=operation,
                        )
                    elif current_amount != rollback_before:
                        raise RuntimeError("俱乐部结余已变化，无法安全恢复赞助入账")
            except Exception as rollback_error:
                raise RuntimeError(
                    f"赞助费写入后持久化失败，且余额回滚失败：{rollback_error}"
                ) from error
        io.rollback_acceptance(
            scope_id, team_id=team_id, offer_id=offer_id,
        )
        raise
    return payment, after
