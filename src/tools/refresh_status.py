"""An isolated, context-bound fallback view for lightweight refresh polling."""
from __future__ import annotations

from copy import deepcopy
from typing import Any


class RefreshStatusSnapshot:
    """Publish one owned copy; readers cannot mutate it or relabel its context.

    Instances are replaced, never updated. Unknown identities deliberately
    produce no cached fields. This is not a source of account balances.
    """

    def __init__(self, context_key: Any, payload: dict[str, Any]) -> None:
        self._context_key = deepcopy(context_key)
        self._payload = deepcopy(payload)

    def read(self, context_key: Any) -> dict[str, Any]:
        if context_key is None or self._context_key is None or context_key != self._context_key:
            return {}
        return deepcopy(self._payload)
