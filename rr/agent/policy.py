"""The EV policy. Deterministic arithmetic over a calibrated success model.

No ground truth, no LLM, no ranking heuristic. Every candidate gets an expected
net value in rupees with a credible interval attached, and the winner is the
argmax. NO_ACTION is a scored candidate at exactly zero, so choosing it is an
economic statement -- "nothing available was worth its cost" -- rather than a
branch someone fell through.

Two subtleties worth reading:

  * OBJECTIVE. The literal formula p_success x amount x margin maximises GROSS
    recovery, which is the exact number this project argues against reporting. An
    action that succeeds on a payment that would have recovered anyway earns
    nothing incremental. So the default objective multiplies by (1 - p_organic):
    value accrues only on the branch where nothing else would have worked. The
    gross objective is retained behind POLICY.ev_objective so the difference can
    be measured rather than asserted.

  * STOPPING. Candidates are pruned on the UPPER bound of their interval, not the
    mean. A cell with four observations and a wide interval survives; a cell with
    four hundred observations whose whole interval sits below zero does not. That
    is the entire reason for carrying uncertainty around.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rr.config import COSTS, POLICY
from rr.model.beta_binomial import BetaBinomialModel, CellPosterior, time_bucket
from rr.taxonomy import ActionType, Channel, DEBIT_ACTIONS, FailureCause

MARGIN = COSTS.default_margin_bps / 10_000


@dataclass
class Candidate:
    action: str
    channel: Optional[str]
    at_h: Optional[float]
    ev_mean: float
    ev_lo: float
    ev_hi: float
    evidence: dict
    components: dict
    permitted: bool = True
    blocked_by: Optional[str] = None
    chosen: bool = False

    def as_record(self) -> dict:
        return {"action": self.action, "channel": self.channel,
                "at_h": round(self.at_h, 3) if self.at_h is not None else None,
                "score": round(self.ev_mean / 100, 2),          # rupees
                "ev_ci90_inr": [round(self.ev_lo / 100, 2), round(self.ev_hi / 100, 2)],
                "evidence": self.evidence, "components_inr": self.components,
                "permitted": self.permitted, "blocked_by": self.blocked_by,
                "chosen": self.chosen}


NO_ACTION_CANDIDATE = dict(
    action=ActionType.NO_ACTION.value, channel=None, at_h=None,
    ev_mean=0.0, ev_lo=0.0, ev_hi=0.0,
    evidence={"note": "the value of leaving this payment alone, by definition"},
    components={})


def _action_label(action: str, channel: Optional[str]) -> str:
    return f"{action}:{channel}" if channel else action


class EVPolicy:
    def __init__(self, success: BetaBinomialModel, organic: BetaBinomialModel,
                 cfg=POLICY, costs=COSTS):
        self.success, self.organic, self.cfg, self.costs = success, organic, cfg, costs

    # ------------------------------------------------------------ components --
    def _cost_minor(self, action: str, channel: Optional[str]) -> int:
        if action in (ActionType.RETRY_SAME.value, ActionType.RETRY_ALTERNATE_METHOD.value):
            return self.costs.debit_attempt_cost_minor
        if action == ActionType.NUDGE.value:
            return self.costs.contact_cost_minor[channel]
        if action == ActionType.MERCHANT_ALERT.value:
            return self.costs.merchant_alert_cost_minor
        if action == ActionType.ESCALATE_HUMAN.value:
            return self.costs.human_escalation_cost_minor
        return 0

    def p_organic(self, obs: dict, cause: FailureCause) -> float:
        return self.organic.lookup(action="organic", cause=cause.value, method=obs["method"],
                                   attempt_index=0, time_bucket="all",
                                   regime=obs["regime"]).mean

    def _ev(self, p: float, obs: dict, cause: FailureCause, action: str,
            channel: Optional[str], p_org: float) -> tuple[float, dict]:
        amount = obs["amount_minor"]
        gross = p * amount * MARGIN
        value = gross * (1.0 - p_org) if self.cfg.ev_objective == "incremental" else gross
        cost = self._cost_minor(action, channel)

        annoyance = 0.0
        if action == ActionType.NUDGE.value:
            # The contact was wasted unless it caused the recovery: either they were
            # going to pay anyway, or the nudge did not land.
            p_wasted = p_org + (1.0 - p_org) * (1.0 - p)
            annoyance = p_wasted * self.costs.wasted_contact_cost_minor

        chargeback = 0.0
        if action in (ActionType.RETRY_SAME.value, ActionType.RETRY_ALTERNATE_METHOD.value):
            # Not learnable: the label never arrives in production either.
            rate = (self.cfg.p_chargeback_unknown_cause if cause is FailureCause.UNKNOWN
                    else self.cfg.p_chargeback_known_soft)
            chargeback = rate * self.costs.terminal_retry_penalty_minor

        ev = value - cost - annoyance - chargeback
        return ev, {"value": round(value / 100, 2), "cost": round(cost / 100, 2),
                    "annoyance": round(-annoyance / 100, 2),
                    "chargeback": round(-chargeback / 100, 2),
                    "p_organic": round(p_org, 4), "objective": self.cfg.ev_objective}

    # ------------------------------------------------------------- candidates --
    def score(self, obs: dict, cause: FailureCause, st, now_h: float,
              permitted: set[str], blocking, breaker=None) -> list[Candidate]:
        t0 = obs["failed_at_h"]
        p_org = self.p_organic(obs, cause)
        channels = list(obs["consented_channels"])[: self.cfg.max_nudge_channels]
        out: list[Candidate] = [Candidate(**NO_ACTION_CANDIDATE)]

        variants: list[tuple[str, Optional[str]]] = [
            (ActionType.RETRY_SAME.value, None),
            (ActionType.RETRY_ALTERNATE_METHOD.value, None),
            (ActionType.MERCHANT_ALERT.value, None),
            (ActionType.ESCALATE_HUMAN.value, None),
        ] + [(ActionType.NUDGE.value, c) for c in channels]

        for offset in self.cfg.candidate_grid_h:
            at = t0 + offset
            if at < now_h or at > t0 + 168.0:
                continue
            for action, channel in variants:
                cell = self.success.lookup(
                    action=_action_label(action, channel), cause=cause.value,
                    method=obs["method"], attempt_index=st.retry_index,
                    time_bucket=time_bucket(at - t0), regime=obs["regime"])
                ev, comp = self._ev(cell.mean, obs, cause, action, channel, p_org)
                ev_lo, _ = self._ev(cell.lo, obs, cause, action, channel, p_org)
                ev_hi, _ = self._ev(cell.hi, obs, cause, action, channel, p_org)
                permitted_here = action in permitted
                blocked_by = None if permitted_here else blocking(action)
                if breaker is not None and action in (a.value for a in DEBIT_ACTIONS) \
                        and breaker.is_open(cause.value, st.retry_index):
                    permitted_here, blocked_by = False, "R012_circuit_breaker_open"
                out.append(Candidate(action, channel, at, ev, ev_lo, ev_hi,
                                     cell.as_evidence(), comp, permitted_here, blocked_by))
        return out

    def choose(self, candidates: list[Candidate], exclude: tuple[str, ...] = ()) -> Candidate:
        """Prune on the upper bound; argmax on the mean; NO_ACTION if nothing survives."""
        live = [c for c in candidates
                if c.permitted and c.action not in exclude
                and c.action != ActionType.NO_ACTION.value and c.ev_hi > 0]
        best = max(live, key=lambda c: c.ev_mean) if live else None
        if best is None or best.ev_mean <= 0:
            best = next(c for c in candidates if c.action == ActionType.NO_ACTION.value)
        best.chosen = True
        return best
