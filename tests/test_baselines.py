"""Invariants the gate depends on. If these break, the arm table is meaningless."""
import dataclasses
import pathlib

import pytest

from rr.baselines.oracle import B3Oracle
from rr.baselines.rules import B0DoNothing, B1BlindLadder, B2GoodRules
from rr.config import CLOCK, CohortConfig
from rr.contracts import AttemptOutcome
from rr.sim.cohort import generate_cohort
from rr.sim.world import run_arm
from rr.taxonomy import DEBIT_ACTIONS, Regime

REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cohort():
    cfg = dataclasses.replace(
        CohortConfig(), n_dev=800, n_merchants_dev=5,
        n_adv_fast_organic=30, n_adv_soft_mask_terminal=25, n_adv_outage_midwindow=25)
    intents = generate_cohort("dev", cfg, cfg.n_dev, cfg.n_merchants_dev, CLOCK.dev_hours, "dev")
    obs = [dataclasses.asdict(i.observed) for i in intents]
    for o in obs:  # match the json round-trip the real loader produces
        o["method"], o["regime"] = o["method"].value, o["regime"].value
        o["consented_channels"] = [c.value for c in o["consented_channels"]]
    return obs, [i.latent for i in intents]


def test_b0_takes_no_action(cohort):
    res = run_arm(*cohort, lambda o, l: B0DoNothing())
    assert sum(r.debits + r.contacts + r.other_actions for r in res) == 0


def test_b1_issues_unauthorized_debits_and_b2_b3_do_not(cohort):
    obs, lat = cohort
    b1 = run_arm(obs, lat, lambda o, l: B1BlindLadder())
    assert sum(r.unauthorized_debit_rejected for r in b1) > 0, "B1 should breach; it has no regime check"
    for policy in (lambda o, l: B2GoodRules(), lambda o, l: B3Oracle(l)):
        res = run_arm(obs, lat, policy)
        assert sum(r.unauthorized_debit_rejected for r in res) == 0


def test_unauthorized_rejection_is_a_distinct_terminal_event(cohort):
    obs, lat = cohort
    b1 = run_arm(obs, lat, lambda o, l: B1BlindLadder())
    for r in b1:
        for rec in r.records:
            if rec.action_type in DEBIT_ACTIONS and r.regime is Regime.CUSTOMER_INITIATED:
                assert rec.outcome is AttemptOutcome.UNAUTHORIZED_DEBIT_REJECTED
                assert rec.p_used == 0.0
            else:
                assert rec.outcome is not AttemptOutcome.UNAUTHORIZED_DEBIT_REJECTED


def test_oracle_never_retries_a_terminal_cause(cohort):
    res = run_arm(*cohort, lambda o, l: B3Oracle(l))
    assert sum(r.never_retry_violations_true for r in res) == 0


def test_b2_has_zero_violations_it_could_have_known_about(cohort):
    """B2's observable violation count must be 0. Its TRUE count is not 0, because
    ~15% of failures arrive with a generic or unmapped code that hides a terminal
    cause. That gap is a finding, not a bug -- an eligibility gate can only enforce
    what the signal reveals."""
    res = run_arm(*cohort, lambda o, l: B2GoodRules())
    assert sum(r.never_retry_violations_observable for r in res) == 0
    assert sum(r.never_retry_violations_true for r in res) > 0


def test_non_oracle_baselines_cannot_see_ground_truth():
    src = (REPO / "rr" / "baselines" / "rules.py").read_text()
    assert "latent" not in src, "B0/B1/B2 must not touch ground truth"
