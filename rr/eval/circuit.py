"""Portfolio-level circuit breaker.

Per-transaction rules can every one of them pass while a whole cause-cell quietly
stops converting -- an issuer changes a policy, a token batch goes stale. This
watches the realised success rate per cause over a rolling window and halts the
re-debit arm for that cause when it falls through the floor. Once open it stays
open for the run: re-arming automatically is how you build an oscillator.
"""
from __future__ import annotations

from collections import defaultdict, deque

from rr.config import BREAKER


class CircuitBreaker:
    def __init__(self, cfg=BREAKER):
        self.cfg = cfg
        self._window: dict[str, deque] = defaultdict(lambda: deque(maxlen=cfg.window_attempts))
        self._open: set[str] = set()
        self.trips: list[tuple[str, int, float]] = []

    def record(self, cause: str, attempt_index: int, success: bool) -> None:
        cell = f"{cause}#idx{attempt_index}"
        w = self._window[cell]
        w.append(bool(success))
        if cell in self._open or len(w) < self.cfg.min_attempts_before_arming:
            return
        rate = sum(w) / len(w)
        if rate < self.cfg.min_success_rate:
            self._open.add(cell)
            self.trips.append((cell, len(w), rate))

    def is_open(self, cause: str, attempt_index: int) -> bool:
        return f"{cause}#idx{attempt_index}" in self._open
