"""Observable features for the success model.

Every function here reads only fields the agent legitimately has: the gateway
view, the clock, and its own merchant's failure stream. Nothing reaches into
ground truth -- tests/test_boundary.py greps this package for it.

The three features added in feature_cross_v2 exist to close specific, diagnosed
gaps from M4:

  dom_bucket   day of month at the moment the action would fire. Salary lands on
               a small number of days; retrying an insufficient_funds failure the
               day before payday and the day after are very different bets, and
               elapsed-time buckets cannot express the difference.
  hour_bucket  hour of day at fire time. Contact conversion collapses overnight.
               v1 had no way to learn this; B2 had it hard-coded.
  issuer_rate  how far above its own baseline this issuer's failure rate is right
               now. A proxy for "is the bank still down", which is the single
               thing v1's [0.7,0.8) reliability bin was missing.
"""
from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass

# TODO(citation): salary-cycle day bands are a population heuristic, not a
# regulatory or contractual value. Placeholder bands.
DOM_BANDS = ((3, "d01-03"), (9, "d04-09"), (24, "d10-24"), (28, "d25-28"))
# Quiet-hours band mirrors REGULATORY.quiet_hours_ist, which is itself UNVERIFIED.
HOUR_BANDS = ((9, "quiet"), (17, "business"), (21, "evening"))
ISSUER_RATE_BANDS = ((1.5, "low"), (3.0, "elevated"))
ISSUER_WINDOW_H = 2.0


def dom_bucket(at_h: float) -> str:
    dom = int(at_h // 24) % 30 + 1
    for edge, label in DOM_BANDS:
        if dom <= edge:
            return label
    return "d29-30"


def hour_bucket(at_h: float) -> str:
    hour = int(at_h % 24)
    for edge, label in HOUR_BANDS:
        if hour < edge:
            return label
    return "quiet"


@dataclass
class IssuerFailureIndex:
    """Failures per issuer over time, built from observed events only.

    `rate()` counts strictly inside [t - window, t), so a decision at time t never
    sees a failure that has not happened yet.
    """
    times: dict
    baseline_per_window: dict

    @classmethod
    def build(cls, obs_rows: list[dict], window_h: float = ISSUER_WINDOW_H) -> "IssuerFailureIndex":
        times: dict = defaultdict(list)
        for o in obs_rows:
            times[o["issuer_id"]].append(o["failed_at_h"])
        base = {}
        for issuer, ts in times.items():
            ts.sort()
            span = max(ts[-1] - ts[0], 1.0)
            base[issuer] = max(len(ts) / span * window_h, 0.5)
        return cls(dict(times), base)

    def rate(self, issuer_id: str, at_h: float, window_h: float = ISSUER_WINDOW_H) -> float:
        ts = self.times.get(issuer_id)
        if not ts:
            return 1.0
        n = bisect.bisect_left(ts, at_h) - bisect.bisect_left(ts, at_h - window_h)
        return n / self.baseline_per_window[issuer_id]

    def bucket(self, issuer_id: str, at_h: float) -> str:
        r = self.rate(issuer_id, at_h)
        for edge, label in ISSUER_RATE_BANDS:
            if r < edge:
                return label
        return "high"


def build_features(action_label: str, obs: dict, cause: str, attempt_index: int,
                   at_h: float, elapsed_bucket: str, issuer_index=None) -> dict:
    """The full v2 feature dict. v1 levels simply never reference the last three."""
    return {
        "action": action_label, "cause": cause, "method": obs["method"],
        "attempt_index": attempt_index, "time_bucket": elapsed_bucket,
        "regime": obs["regime"],
        "dom": dom_bucket(at_h), "hour": hour_bucket(at_h),
        "issuer_rate": (issuer_index.bucket(obs["issuer_id"], at_h)
                        if issuer_index is not None else "na"),
    }
