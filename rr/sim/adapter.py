"""The `sim` executor adapter. Holds ground truth; the pipeline never does.

This is the seam. Everything upstream of here -- ingest, normalise, gate, policy
-- runs on observed fields only, and swapping this object for the Razorpay
adapter changes nothing above it. The CRN key matches rr/sim/world.py exactly, so
the pipeline and the offline eval harness roll identical outcomes.
"""
from __future__ import annotations

import pathlib
from typing import Optional

from rr import rng
from rr.adapters.base import (
    REVALIDATION_ALREADY_RECOVERED, REVALIDATION_OK, ExecutionRequest, ExecutionResult,
)
from rr.contracts import ActionSpec, AttemptContext
from rr.sim.latent import LatentState
from rr.sim.response_model import p_recovery, resolution_lag_hours
from rr.taxonomy import ActionType, Channel, DEBIT_ACTIONS, Method, Regime


class SimAdapter:
    name = "sim"

    def __init__(self, latents: dict[str, LatentState]):
        self._lat = latents
        self._agent_success: dict[str, float] = {}

    @classmethod
    def from_cohort(cls, data_dir: pathlib.Path, cohort: str = "dev") -> "SimAdapter":
        """Ground truth is loaded HERE and nowhere else. The pipeline receives an
        adapter, never a LatentState, which is why rr/pipeline can be grepped for
        ground-truth imports and come back clean."""
        from rr.sim.cohort import load_latent, load_observed
        from rr.taxonomy import FailureCause
        obs = load_observed(data_dir / f"{cohort}_observed.jsonl")
        lat = load_latent(data_dir / f"{cohort}_latent.jsonl")
        return cls({o["intent_id"]: LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})
                    for o, d in zip(obs, lat)})

    def counterfactual_self_heals(self, payment_intent_id: str) -> bool:
        """Simulator-only diagnostic. Used to sanity-check the treatment/control
        estimator, never to compute the reported number."""
        return self._lat[payment_intent_id].self_heal_at_h is not None

    def _recovered_by(self, iid: str, at_h: float) -> Optional[float]:
        times = []
        heal = self._lat[iid].self_heal_at_h
        if heal is not None and heal <= at_h:
            times.append(heal)
        agent = self._agent_success.get(iid)
        if agent is not None and agent <= at_h:
            times.append(agent)
        return min(times) if times else None

    def revalidate(self, payment_intent_id: str, at_h: float) -> str:
        return (REVALIDATION_ALREADY_RECOVERED
                if self._recovered_by(payment_intent_id, at_h) is not None
                else REVALIDATION_OK)

    def execute(self, req: ExecutionRequest) -> ExecutionResult:
        lat = self._lat[req.payment_intent_id]
        action_type = ActionType(req.action_type)
        regime = Regime(req.regime)

        if action_type in DEBIT_ACTIONS and regime is Regime.CUSTOMER_INITIATED:
            return ExecutionResult(
                outcome="unauthorized_debit_rejected", p_used=0.0,
                response={"error": "no standing mandate for this payment intent",
                          "gateway_rejected": True})

        action = ActionSpec(action_type, req.at_h,
                            Channel(req.channel) if req.channel else None)
        ctx = AttemptContext(
            sim_time_h=req.at_h, elapsed_h=0.0, retry_index=req.retry_index,
            nudges_sent=req.nudges_sent, amount_minor=req.amount_minor,
            method=Method(req.method), regime=regime,
            has_alternate_instrument=action_type is ActionType.RETRY_ALTERNATE_METHOD,
        )
        p = p_recovery(action, lat, ctx)
        hit = rng.u01(req.payment_intent_id, "outcome", req.slot) < p
        if hit:
            self._agent_success[req.payment_intent_id] = req.at_h + resolution_lag_hours(action, lat)
        return ExecutionResult(
            outcome="success" if hit else "failed", p_used=round(p, 6),
            response={"simulated": True, "p": round(p, 6),
                      "crn_key": f"{req.payment_intent_id}|outcome|{req.slot}"})

    def poll_outcome(self, payment_intent_id: str, until_h: float):
        candidates = []
        agent = self._agent_success.get(payment_intent_id)
        if agent is not None and agent <= until_h:
            candidates.append((agent, "agent"))
        heal = self._lat[payment_intent_id].self_heal_at_h
        if heal is not None and heal <= until_h:
            candidates.append((heal, "organic"))
        return min(candidates) if candidates else (None, None)
