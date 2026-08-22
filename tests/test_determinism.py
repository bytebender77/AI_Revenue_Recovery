import dataclasses
import pathlib
import tempfile

from rr.config import CLOCK, CohortConfig
from rr.rng import u01
from rr.sim.cohort import generate_cohort, write_cohort


def _small() -> CohortConfig:
    return dataclasses.replace(
        CohortConfig(), n_dev=400, n_merchants_dev=4,
        n_adv_fast_organic=20, n_adv_soft_mask_terminal=15, n_adv_outage_midwindow=15,
    )


def test_cohort_is_bit_identical_across_runs():
    cfg = _small()
    hashes = []
    for _ in range(2):
        intents = generate_cohort("dev", cfg, cfg.n_dev, cfg.n_merchants_dev, CLOCK.dev_hours, "dev")
        with tempfile.TemporaryDirectory() as d:
            hashes.append(write_cohort(pathlib.Path(d), "dev", intents))
    assert hashes[0] == hashes[1]


def test_seed_change_moves_the_world():
    cfg_a = _small()
    cfg_b = dataclasses.replace(cfg_a, seed=cfg_a.seed + 1)
    a = generate_cohort("dev", cfg_a, cfg_a.n_dev, cfg_a.n_merchants_dev, CLOCK.dev_hours, "dev")
    b = generate_cohort("dev", cfg_b, cfg_b.n_dev, cfg_b.n_merchants_dev, CLOCK.dev_hours, "dev")
    assert [i.latent.true_cause for i in a] != [i.latent.true_cause for i in b]


def test_common_random_numbers_are_shared_across_arms():
    """Two arms taking the same action on the same intent at the same slot must
    consume the same uniform, or the arm comparison measures RNG noise."""
    assert u01("pi_1", "outcome", 0) == u01("pi_1", "outcome", 0)
    assert u01("pi_1", "outcome", 0) != u01("pi_1", "outcome", 1)
