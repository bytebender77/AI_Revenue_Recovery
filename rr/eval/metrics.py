"""Arm aggregation, paired bootstrap, per-cause attribution of the B3-B2 gap.

The only number this project is allowed to headline is INCREMENTAL recovery:
sum over intents of (what this arm recovered) - (what B0 would have recovered on
the same intent). B0 recovers exactly the self-healing set, so an arm that
"succeeds" on a payment that was going to recover anyway scores gross and earns
zero incremental. That is the intended behaviour, not a rounding error.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from rr.config import COSTS
from rr.sim.world import IntentResult
from rr.taxonomy import ActionType, FailureCause

MARGIN = COSTS.default_margin_bps / 10_000


def delta_vector(results: Sequence[IntentResult]) -> np.ndarray:
    """Per-intent incremental recovered value, in minor units."""
    return np.array(
        [r.recovered_value_minor - r.counterfactual_value_minor for r in results], dtype=np.int64
    )


def action_cost_minor(r: IntentResult) -> int:
    cost = 0
    for rec in r.records:
        if rec.action_type in (ActionType.RETRY_SAME, ActionType.RETRY_ALTERNATE_METHOD):
            cost += COSTS.debit_attempt_cost_minor
        elif rec.action_type is ActionType.NUDGE:
            cost += COSTS.contact_cost_minor[rec.channel.value]
        elif rec.action_type is ActionType.MERCHANT_ALERT:
            cost += COSTS.merchant_alert_cost_minor
        elif rec.action_type is ActionType.ESCALATE_HUMAN:
            cost += COSTS.human_escalation_cost_minor
    return cost


@dataclass
class ArmSummary:
    name: str
    n: int
    gross_minor: int
    incremental_minor: int
    ci_lo_minor: float
    ci_hi_minor: float
    debits: int
    contacts: int
    other_actions: int
    never_retry_true: int
    never_retry_observable: int
    unauthorized_debits: int
    action_cost_minor: int
    penalty_minor: int

    @property
    def net_minor(self) -> float:
        return self.incremental_minor * MARGIN - self.action_cost_minor - self.penalty_minor


def bootstrap_ci(deltas: np.ndarray, idx: np.ndarray, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile CI on the arm total, using a shared resample index matrix so
    every arm is bootstrapped on the SAME resamples -- paired, like the CRNs."""
    totals = deltas[idx].sum(axis=1)
    return float(np.percentile(totals, 100 * alpha / 2)), float(np.percentile(totals, 100 * (1 - alpha / 2)))


def make_resamples(n: int, reps: int = 2000, seed: int = 7) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, n, size=(reps, n))


def summarize(name: str, results: Sequence[IntentResult], idx: np.ndarray) -> ArmSummary:
    d = delta_vector(results)
    lo, hi = bootstrap_ci(d, idx)
    nrt = sum(r.never_retry_violations_true for r in results)
    return ArmSummary(
        name=name, n=len(results),
        gross_minor=int(sum(r.recovered_value_minor for r in results)),
        incremental_minor=int(d.sum()), ci_lo_minor=lo, ci_hi_minor=hi,
        debits=sum(r.debits for r in results),
        contacts=sum(r.contacts for r in results),
        other_actions=sum(r.other_actions for r in results),
        never_retry_true=nrt,
        never_retry_observable=sum(r.never_retry_violations_observable for r in results),
        unauthorized_debits=sum(r.unauthorized_debit_rejected for r in results),
        action_cost_minor=sum(action_cost_minor(r) for r in results),
        penalty_minor=nrt * COSTS.terminal_retry_penalty_minor,
    )


@dataclass
class CauseGap:
    cause: FailureCause
    n: int
    b2_incremental_minor: int
    b3_incremental_minor: int

    @property
    def gap_minor(self) -> int:
        return self.b3_incremental_minor - self.b2_incremental_minor


def gap_by_cause(b2: Sequence[IntentResult], b3: Sequence[IntentResult]) -> list[CauseGap]:
    d2, d3 = delta_vector(b2), delta_vector(b3)
    buckets: dict[FailureCause, list[int]] = {}
    for i, r in enumerate(b2):
        b = buckets.setdefault(r.true_cause, [0, 0, 0])
        b[0] += 1
        b[1] += int(d2[i])
        b[2] += int(d3[i])
    rows = [CauseGap(c, v[0], v[1], v[2]) for c, v in buckets.items()]
    return sorted(rows, key=lambda r: -r.gap_minor)


def paired_gap_ci(a: Sequence[IntentResult], b: Sequence[IntentResult],
                  idx: np.ndarray) -> tuple[int, float, float]:
    """CI on (arm A total) - (arm B total), paired per intent."""
    d = delta_vector(a) - delta_vector(b)
    lo, hi = bootstrap_ci(d, idx)
    return int(d.sum()), lo, hi


def rupees(minor: float) -> str:
    return f"{minor / 100:,.0f}"
