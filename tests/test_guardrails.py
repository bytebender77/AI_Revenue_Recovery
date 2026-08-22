"""The terminal-set guardrail, across every arm, plus proof the test can fail.

A guardrail test that has never been seen to go red is not evidence of anything.
`test_the_terminal_set_test_goes_red_when_the_gate_is_broken` deliberately empties
the NEVER_RETRY set and asserts that violations appear -- so the green result above
it means something.

Two counters, and the difference between them is a finding, not a bug:
  observable violations -- re-debits against a cause the gate could SEE was
      terminal. Must be zero for every gated arm. Always.
  true-cause violations -- re-debits against a cause that was terminal in ground
      truth. Cannot be zero: ~15% of failures arrive with a generic or unmapped
      code that hides one. A gate can only enforce what the signal reveals.
"""
import dataclasses

import pytest

from rr.agent.policy import EVPolicy
from rr.baselines.outage import B25GoodRulesPlusOutage, OutageDetector
from rr.baselines.rules import B0DoNothing, B1BlindLadder, B2GoodRules
from rr.config import CLOCK, CohortConfig
from rr.eval.agent_run import run_agent
from rr.model.beta_binomial import BetaBinomialModel
from rr.sim.cohort import generate_cohort
from rr.sim.world import run_arm
import rr.pipeline.eligibility as elig_mod
import rr.taxonomy as tax


@pytest.fixture(scope="module")
def cohort():
    cfg = dataclasses.replace(
        CohortConfig(), n_dev=900, n_merchants_dev=5,
        n_adv_fast_organic=30, n_adv_soft_mask_terminal=25, n_adv_outage_midwindow=25)
    intents = generate_cohort("dev", cfg, cfg.n_dev, cfg.n_merchants_dev, CLOCK.dev_hours, "dev")
    obs = [dataclasses.asdict(i.observed) for i in intents]
    for o in obs:
        o["method"], o["regime"] = o["method"].value, o["regime"].value
        o["consented_channels"] = [c.value for c in o["consented_channels"]]
    return obs, [i.latent for i in intents]


@pytest.fixture(scope="module")
def agent_policy():
    import pathlib
    p = pathlib.Path("models")
    if not (p / "success_model.json").exists():
        pytest.skip("run `make train` first")
    return EVPolicy(BetaBinomialModel.load(p / "success_model.json"),
                    BetaBinomialModel.load(p / "organic_model.json"))


GATED_ARMS = {
    "B0": lambda o, l: B0DoNothing(),
    "B2": lambda o, l: B2GoodRules(),
    "B2.5": (lambda d: lambda o, l: B25GoodRulesPlusOutage(d))(OutageDetector()),
}


@pytest.mark.parametrize("name", sorted(GATED_ARMS))
def test_no_gated_arm_ever_debits_a_visibly_terminal_cause(name, cohort):
    res = run_arm(*cohort, GATED_ARMS[name])
    assert sum(r.never_retry_violations_observable for r in res) == 0
    assert sum(r.unauthorized_debit_rejected for r in res) == 0


def test_agent_never_debits_a_visibly_terminal_cause(cohort, agent_policy):
    res, _ = run_agent(*cohort, agent_policy)
    assert sum(r.never_retry_violations_observable for r in res) == 0
    assert sum(r.unauthorized_debit_rejected for r in res) == 0


def test_ungated_arm_does_violate(cohort):
    """B1 has no gate. If this ever passes at zero, the counter is broken."""
    res = run_arm(*cohort, lambda o, l: B1BlindLadder())
    assert sum(r.never_retry_violations_observable for r in res) > 0
    assert sum(r.unauthorized_debit_rejected for r in res) > 0


def test_the_terminal_set_test_goes_red_when_the_gate_is_broken(cohort, agent_policy, monkeypatch):
    monkeypatch.setattr(tax, "NEVER_RETRY", frozenset())
    monkeypatch.setattr(elig_mod, "NEVER_RETRY", frozenset())
    res, _ = run_agent(*cohort, agent_policy)
    # The counter still measures against the REAL terminal set.
    real = frozenset({tax.FailureCause.LOST_STOLEN_FRAUD, tax.FailureCause.RISK_DECLINE,
                      tax.FailureCause.MANDATE_INVALID, tax.FailureCause.AMOUNT_EXCEEDS_MANDATE,
                      tax.FailureCause.METHOD_NOT_ENABLED, tax.FailureCause.CARD_EXPIRED,
                      tax.FailureCause.INVALID_CARD_DETAILS})
    leaked = sum(1 for r in res for rec in r.records
                 if rec.action_type in tax.DEBIT_ACTIONS
                 and tax.normalize_reason(
                     next(o for o in cohort[0] if o["intent_id"] == r.intent_id)["gateway_reason"]) in real)
    assert leaked > 0, "emptying NEVER_RETRY produced no violations -- the test cannot fail"


def test_true_cause_violations_are_nonzero_and_that_is_the_point(cohort):
    res = run_arm(*cohort, lambda o, l: B2GoodRules())
    assert sum(r.never_retry_violations_true for r in res) > 0
