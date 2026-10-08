"""Short-lived public-view coalescing, not an account or FM fact cache."""
from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any


@dataclass(frozen=True)
class PublicationCandidate:
    """Payload provenance captured alongside its source under the owner lock."""
    key: Hashable
    payload: dict[str, Any]


@dataclass(frozen=True)
class _PublishedState:
    key: Hashable
    payload: dict[str, Any]
    published_at: float


class CoalescedStatePublisher:
    """Own one snapshot and serialize builders without knowing domain state.

    The caller supplies a cheap, side-effect-free context/version key. A build
    may coordinate business work, so it is never retried implicitly. If its
    context changes, that response remains the original caller's snapshot but
    is not retained for later readers. Capture-time provenance also prevents an
    A-to-B-to-A context round trip from publishing B as A. Payloads are read-only.
    """

    def __init__(self, *, lifetime: float = 0.75,
                 clock: Callable[[], float] = monotonic) -> None:
        self._lifetime = lifetime
        self._clock = clock
        self._build_lock = Lock()
        self._published: _PublishedState | None = None

    def _current(self, key: Hashable) -> dict[str, Any] | None:
        published = self._published
        if (published is not None and published.key == key
                and self._clock() - published.published_at < self._lifetime):
            return published.payload
        return None

    def read(self, context_key: Callable[[], Hashable],
             build: Callable[[], PublicationCandidate]) -> dict[str, Any]:
        payload = self._current(context_key())
        if payload is not None:
            return payload
        with self._build_lock:
            # A waiting reader must re-read identity, version and expiry.
            key = context_key()
            payload = self._current(key)
            if payload is not None:
                return payload
            self._published = None
            candidate = build()
            if candidate.key == key == context_key():
                self._published = _PublishedState(key, candidate.payload, self._clock())
            return candidate.payload
