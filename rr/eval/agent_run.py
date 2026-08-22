"""Tick-based agent runner with a capacity auction for human escalation.

The auction is the reason this is a tick loop rather than a per-intent loop.
Escalation capacity is scarce and shared, so first-come-first-served gives the
slot to whichever payment happened to fail earliest -- which is an arbitrary
allocation of a scarce resource. Instead, every intent that wants an escalation
in a tick states its expected net value, the tick's quota goes to the highest
claimants, and a losing intent records "someone else's payment was worth more"
as its binding constraint. That is a second, distinct economic justification for
NO_ACTION, alongside "nothing was worth its cost".
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from rr.budget import EscalationBudget
from rr.config import CLOCK, COSTS, POLICY
from rr.contracts import ActionSpec, AttemptOutcome
from rr.eval.circuit import CircuitBreaker
from rr.pipeline.eligibility import PolicyState, evaluate
from rr.pipeline.normalize import diagnose
from rr.sim.latent import LatentState
from rr.sim.response_model import resolution_lag_hours
from rr.sim.world import IntentResult, RunState, advance_state, apply_action, settle
from rr.taxonomy import ActionType, DEBIT_ACTIONS, NEVER_RETRY, Regime, normalize_reason

AUCTION_RULE = "R011_capacity_auction_lost"
MAX_SLOTS = 6


@dataclass
class Runtime:
    obs: dict
    latent: LatentState
    st: RunState
    next_due_h: float
    active: bool = True
    agent_recovery_at: Optional[float] = None
    debits: int = 0
    contacts: int = 0
    others: int = 0
    v_true: int = 0
    v_obs: int = 0
    v_unauth: int = 0
    decisions: list = field(default_factory=list)

    @property
    def horizon_end(self) -> float:
        return self.obs["failed_at_h"] + CLOCK.recovery_horizon_hours


def _no_action_reason(cands, chosen, lost_auction: bool) -> tuple[str, Optional[str]]:
    if lost_auction:
        return "CAPACITY_AUCTION_LOST", AUCTION_RULE
    blocked_but_wanted = [c for c in cands if not c.permitted and c.ev_hi > 0]
    if blocked_but_wanted:
        best = max(blocked_but_wanted, key=lambda c: c.ev_mean)
        return "BLOCKED_BY_RULE", best.blocked_by
    return "NEGATIVE_EV_TERMINAL", "EV_UPPER_BOUND_NOT_POSITIVE"


def run_agent(obs_rows: list[dict], latents: list[LatentState], policy,
              crn_ns: str = "outcome", normalizer=None
              ) -> tuple[list[IntentResult], dict]:
    """`normalizer` is an optional TailNormalizer. It affects DIAGNOSIS ONLY.

    Every legality decision below is taken by the eligibility gate from the
    diagnosed cause; swapping the normaliser in or out changes which cause the
    gate is given, never what the gate is allowed to permit."""
    if getattr(policy, "issuer_index", None) is None:
        from rr.agent.features import IssuerFailureIndex
        policy.issuer_index = IssuerFailureIndex.build(obs_rows)
    rts = [Runtime(o, l, RunState(failed_at_h=o["failed_at_h"]), o["failed_at_h"])
           for o, l in zip(obs_rows, latents)]

    # Resolve each intent's cause exactly once: the normaliser's own cache makes
    # repeat calls cheap, but they would duplicate rows in the audit log.
    _resolved: dict[str, object] = {}

    def cause_of(obs: dict):
        iid = obs["intent_id"]
        if iid not in _resolved:
            _resolved[iid] = diagnose(obs, normalizer)
        return _resolved[iid]
    budget = EscalationBudget.for_cohort(len(rts), COSTS.escalation_capacity_pct)
    breaker = CircuitBreaker()

    t = min(r.obs["failed_at_h"] for r in rts)
    t_max = max(r.horizon_end for r in rts)
    n_ticks = max(math.ceil((t_max - t) / POLICY.tick_hours), 1)
    quota_pool = 0.0
    auction_stats = Counter()

    while t < t_max:
        tick_end = t + POLICY.tick_hours
        quota_pool += budget.capacity / n_ticks
        proposals = []

        for rt in rts:
            if not rt.active or rt.next_due_h >= tick_end or len(rt.st.history) >= MAX_SLOTS:
                continue
            heal = rt.latent.self_heal_at_h
            if heal is not None and heal <= max(rt.next_due_h, t):
                rt.active = False           # recovered on its own; nothing to decide
                continue
            if rt.next_due_h > rt.horizon_end:
                rt.active = False
                continue

            diag = cause_of(rt.obs)
            cause = diag.failure_cause
            st = PolicyState(slot=len(rt.st.history), retry_index=rt.st.retry_index,
                             nudges_sent=rt.st.nudges_sent,
                             merchant_alerted=rt.st.merchant_alerted,
                             escalated=rt.st.escalated)
            elig = evaluate(rt.obs, diag, st, budget)
            cands = policy.score(rt.obs, cause, rt.st, max(rt.next_due_h, t),
                                 set(elig.permitted), 
                                 lambda a: elig.blocking_rule(ActionType(a)), breaker)
            best = policy.choose(cands)
            proposals.append((rt, cands, best))

        # ---- capacity auction: highest claimants win this tick's quota --------
        esc = [(rt, c, b) for rt, c, b in proposals
               if b.action == ActionType.ESCALATE_HUMAN.value and b.at_h is not None
               and b.at_h < tick_end]
        esc.sort(key=lambda x: -x[2].ev_mean)
        grant = min(len(esc), int(quota_pool), budget.remaining)
        quota_pool -= grant
        losers = set(id(rt) for rt, _, _ in esc[grant:])
        auction_stats["claims"] += len(esc)
        auction_stats["granted"] += grant
        auction_stats["lost"] += len(esc) - grant
        for _ in range(grant):
            budget.consume()

        for rt, cands, best in proposals:
            lost = id(rt) in losers and best.action == ActionType.ESCALATE_HUMAN.value
            if lost:
                best.chosen = False
                for c in cands:
                    if c.action == ActionType.ESCALATE_HUMAN.value:
                        c.permitted, c.blocked_by = False, AUCTION_RULE
                best = policy.choose(cands, exclude=(ActionType.ESCALATE_HUMAN.value,))

            if best.action == ActionType.NO_ACTION.value:
                code, binding = _no_action_reason(cands, best, lost)
                rt.decisions.append({"slot": len(rt.st.history), "action": best.action,
                                     "reason_code": code, "binding_constraint": binding,
                                     "ev_inr": 0.0, "n_candidates": len(cands)})
                rt.active = False
                continue

            rt.decisions.append({"slot": len(rt.st.history), "action": best.action,
                                 "reason_code": "POSITIVE_EXPECTED_NET_VALUE",
                                 "binding_constraint": AUCTION_RULE if lost else None,
                                 "ev_inr": round(best.ev_mean / 100, 2),
                                 "n_candidates": len(cands)})

            if best.at_h > tick_end:
                rt.next_due_h = best.at_h
                continue
            # Fire-time check: never act on a payment that already recovered.
            if rt.latent.self_heal_at_h is not None and rt.latent.self_heal_at_h <= best.at_h:
                rt.active = False
                continue

            action = ActionSpec(ActionType(best.action), best.at_h,
                                _channel(best.channel))
            slot = len(rt.st.history)
            rec = apply_action(rt.obs, rt.latent, action, rt.st, slot, crn_ns)
            rt.st.history.append(rec)

            if action.type in DEBIT_ACTIONS:
                rt.debits += 1
                if rt.latent.true_cause in NEVER_RETRY:
                    rt.v_true += 1
                # Judged against the cause the gate ACTUALLY SAW, which is the
                # diagnosed one. With the normaliser on, a cause it resolves into
                # the terminal set is a cause the gate would have blocked -- so a
                # nonzero count here means the gate leaked, in either configuration.
                if cause_of(rt.obs).failure_cause in NEVER_RETRY:
                    rt.v_obs += 1
                if rec.outcome is AttemptOutcome.UNAUTHORIZED_DEBIT_REJECTED:
                    rt.v_unauth += 1
                else:
                    breaker.record(cause_of(rt.obs).failure_cause.value,
                                   rt.st.retry_index,
                                   rec.outcome is AttemptOutcome.SUCCESS)
            elif action.type is ActionType.NUDGE:
                rt.contacts += 1
            else:
                rt.others += 1

            if rec.outcome is AttemptOutcome.SUCCESS:
                rt.agent_recovery_at = best.at_h + resolution_lag_hours(action, rt.latent)
                rt.active = False
            else:
                advance_state(rt.st, rec)
                rt.next_due_h = best.at_h + 0.01
        t = tick_end

    results = []
    for rt in rts:
        recovered_at, attribution = settle(rt.obs, rt.latent, rt.agent_recovery_at)
        results.append(IntentResult(
            intent_id=rt.obs["intent_id"], merchant_id=rt.obs["merchant_id"],
            amount_minor=rt.obs["amount_minor"], true_cause=rt.latent.true_cause,
            slice_tag=rt.latent.slice_tag, regime=Regime(rt.obs["regime"]),
            recovered_at_h=recovered_at, attribution=attribution,
            counterfactual_self_heals=rt.latent.self_heal_at_h is not None,
            debits=rt.debits, contacts=rt.contacts, other_actions=rt.others,
            never_retry_violations_true=rt.v_true,
            never_retry_violations_observable=rt.v_obs,
            unauthorized_debit_rejected=rt.v_unauth, records=rt.st.history))
    return results, {"auction": dict(auction_stats), "breaker_trips": breaker.trips,
                     "budget": (budget.used, budget.capacity),
                     "decisions": [d for rt in rts for d in rt.decisions]}


def _channel(name: Optional[str]):
    from rr.taxonomy import Channel
    return Channel(name) if name else None
