"""Pure mutation response projection and request-origin validity contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MutationContext:
    state_key: tuple
    account_binding: tuple[str | None, int]

    def matches(self, state_key: tuple, account_binding: tuple[str | None, int]) -> bool:
        return bool(self.account_binding[0]) and (
            self.state_key == state_key and self.account_binding == account_binding
        )


def mutation_patch_fields(response: dict[str, Any]) -> dict[str, Any]:
    """Project only fields already in the result; never read or mutate storage."""
    patch: dict[str, Any] = {}
    economy = response.get("economy")
    if isinstance(economy, dict):
        patch["economy"] = economy
        if "casino_balance" in economy:
            patch["balance"] = float(economy["casino_balance"])
    elif (
        isinstance(response.get("inventory"), list)
        and ("casino_balance" in response or "bank_balance" in response)
    ):
        patch["economy"] = response
        if "casino_balance" in response:
            patch["balance"] = float(response["casino_balance"])
    for field in (
        "training_ground", "welfare", "league_standings", "bets",
        "match_integrity", "settings",
    ):
        if isinstance(response.get(field), (dict, list)):
            patch[field] = response[field]
    if isinstance(response.get("mail"), list):
        patch["mail"] = response["mail"]
    return patch
