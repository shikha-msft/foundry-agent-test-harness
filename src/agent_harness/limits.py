"""Token budgets for a chat session or a batch run."""

from __future__ import annotations

import threading


class TokenBudget:
    """Thread-safe running total. A limit of None means unlimited.

    exhausted() is checked before each call, so the last call may overshoot
    the limit by its own token count.
    """

    def __init__(self, limit: int | None, scope: str = "session") -> None:
        self.limit = limit
        self.scope = scope
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int | None:
        if self.limit is None:
            return None
        return max(self.limit - self.used, 0)

    def exhausted(self) -> bool:
        return self.limit is not None and self.used >= self.limit

    def consume(self, tokens: int) -> None:
        with self._lock:
            self._used += max(tokens, 0)
