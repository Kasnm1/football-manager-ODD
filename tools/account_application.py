"""ODD account use cases running inside an explicit, scoped operation guard."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ContextManager

from tools.betting_account import (
    add_funds as add_wallet_funds,
    cheat_balance, credit_effective_bet_count,
)
from tools.club_economy import (
    adjust_bank_balance, buy_item, buy_items, clear_credit_cheat,
    credit_status, public_economy, set_free_services, transfer_wallet,
)


@dataclass(frozen=True)
class AccountOperation:
    """Capabilities valid only inside the owning runtime's account guard.

    Storage still uses the bound account ContextVar and existing transactions.
    Response callbacks execute before the guard exits, so their scope/version
    cannot be taken from a different account. No process or FM state is exposed.
    """
    scope_id: str
    game_date: str
    weekly_salary: Callable[[], float]
    temporary_bank_cheat: tuple[int, str]
    state_patch: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    economy_patch: Callable[..., dict[str, Any]]


class AccountApplicationService:
    """Own account use cases, not the runtime host or its mutable attributes."""

    def __init__(self, operation: Callable[[str | None], ContextManager[AccountOperation]]):
        self._operation = operation

    def update_cheat_balance(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._operation(None) as operation:
            clear = bool(payload.get("clear"))
            amount = None if clear else float(payload.get("amount", 0))
            wallet = cheat_balance(amount=amount, clear=clear)
            balance = float(wallet["balance"])
            return operation.state_patch(
                {"balance": balance},
                {"balance": balance, "economy": public_economy()},
            )

    def add_funds(self, amount: float) -> dict[str, Any]:
        with self._operation(None):
            wallet = add_wallet_funds(amount)
            return {"balance": wallet["balance"]}

    def clear_credit(self) -> dict[str, Any]:
        with self._operation(None) as operation:
            result = clear_credit_cheat()
            result["credit"] = credit_status(
                operation.game_date, operation.weekly_salary(),
                credit_effective_bet_count(),
            )
            return operation.economy_patch(result)

    def update_free_services(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._operation(None) as operation:
            welfare = set_free_services(bool(payload.get("enabled")))
            economy = public_economy()
            patch = {"economy": economy, "welfare": welfare}
            if "casino_balance" in economy:
                patch["balance"] = float(economy["casino_balance"])
            # This is the same HTTP patch formerly attached by the serializer,
            # now tagged before an account switch can change its identity.
            return operation.state_patch({"economy": economy, "welfare": welfare}, patch)

    def shop_purchase(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._operation(None) as operation:
            result = buy_item(
                str(payload.get("sku", "")), int(payload.get("quantity", 1)),
                free=bool(payload.get("free_purchase")),
            )
            return operation.economy_patch(result, transient=("payment",))

    def shop_batch_purchase(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._operation(None) as operation:
            result = buy_items(
                payload.get("items"), free=bool(payload.get("free_purchase")),
            )
            return operation.economy_patch(result, transient=("payment", "purchases"))

    def transfer_wallet(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._operation(str(payload.get("data_scope_id") or "")) as operation:
            result = transfer_wallet(
                str(payload.get("direction", "")), float(payload.get("amount", 0)),
            )
            return operation.economy_patch(result)

    def grant_temporary_bank_cheat(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._operation(str(payload.get("data_scope_id") or "")) as operation:
            amount, kind = operation.temporary_bank_cheat
            result = adjust_bank_balance(amount, kind, source="bank_cheat_button")
            return operation.economy_patch(result)
