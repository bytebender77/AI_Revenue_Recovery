"""The latent/observable boundary. If these fail, every number downstream is void."""
import json
import pathlib
from dataclasses import fields

import pytest

from rr.contracts import AttemptContext, ObservedFailureEvent, assert_no_latent_leak
from rr.sim.latent import LATENT_FIELD_NAMES, LatentState

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_latent_and_observable_are_field_disjoint():
    observable = {f.name for f in fields(ObservedFailureEvent)} | {f.name for f in fields(AttemptContext)}
    assert not (observable & LATENT_FIELD_NAMES), (
        f"field(s) present on both sides of the boundary: {sorted(observable & LATENT_FIELD_NAMES)}"
    )


def test_assert_no_latent_leak_catches_a_handmade_dict():
    with pytest.raises(AssertionError, match="latent field"):
        assert_no_latent_leak({"intent_id": "x", "true_cause": "insufficient_funds"})


def test_observed_jsonl_carries_no_latent_field():
    path = REPO / "data" / "dev_observed.jsonl"
    if not path.exists():
        pytest.skip("run `make cohort` first")
    for line in path.read_text().splitlines()[:500]:
        assert_no_latent_leak(json.loads(line))


def test_no_agent_module_imports_latent_state():
    """Vacuous until rr/agent exists (M2). Kept here so it fails the day it stops being."""
    for pkg in ("agent", "policy", "normalize"):
        root = REPO / "rr" / pkg
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            src = py.read_text()
            assert "rr.sim.latent" not in src and "from rr.sim import latent" not in src, (
                f"{py} imports ground truth"
            )
