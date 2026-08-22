"""B2.5 -- B2 plus a naive issuer-outage detector.

Exists to answer one question honestly: how much of the outage headroom does a
rule a competent engineer would write in an afternoon already capture? If B2.5
closes most of that cell, the agent's advantage there is not worth claiming.

The detector is deliberately naive. It counts ALL failures on an issuer inside a
window, not just issuer-attributable ones, which is what you would actually have
before building a diagnosis layer.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from typing import Optional

from rr.baselines.rules import B2GoodRules
from rr.budget import EscalationBudget
from rr.config import OUTAGE
from rr.contracts import ActionSpec
from rr.sim.world import RunState
from rr.taxonomy import ActionType


class OutageDetector:
    """Shared across intents in a run. Fed in chronological order by run_arm."""

    def __init__(self, cfg=OUTAGE):
        self.cfg = cfg
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._suspected_until: dict[str, float] = {}

    def observe_failure(self, issuer_id: str, at_h: float) -> None:
        times = self._failures[issuer_id]
        if times and at_h < times[-1]:
            bisect.insort(times, at_h)
        else:
            times.append(at_h)
        window_start = at_h - self.cfg.window_hours
        recent = len(times) - bisect.bisect_left(times, window_start)
        if recent >= self.cfg.consecutive_failures:
            self._suspected_until[issuer_id] = max(
                self._suspected_until.get(issuer_id, 0.0), at_h + self.cfg.backoff_hours)

    def defer_until(self, issuer_id: str, at_h: float) -> Optional[float]:
        until = self._suspected_until.get(issuer_id)
        return until if until is not None and until > at_h else None


class B25GoodRulesPlusOutage(B2GoodRules):
    name = "B2.5_rules_plus_outage"

    def __init__(self, detector: OutageDetector,
                 budget: Optional[EscalationBudget] = None):
        super().__init__(budget)
        self.detector = detector
        self._registered = False

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        if not self._registered:
            self.detector.observe_failure(obs["issuer_id"], obs["failed_at_h"])
            self._registered = True
        action = super().next_action(obs, st)
        if action is None or action.type not in (ActionType.RETRY_SAME,
                                                 ActionType.RETRY_ALTERNATE_METHOD):
            return action
        until = self.detector.defer_until(obs["issuer_id"], action.at_h)
        if until is None:
            return action
        # Do not burn an attempt into a suspected outage; slide it past the window.
        return ActionSpec(action.type, until + 0.5, action.channel)
