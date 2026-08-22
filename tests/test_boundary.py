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
    """The pipeline runs on observed fields only. Ground truth enters exactly once,
    through rr/sim/adapter.py, which is the executor seam and is injected."""
    for pkg in ("agent", "policy", "normalize", "pipeline", "adapters", "baselines/rules.py"):
        root = REPO / "rr" / pkg
        if not root.exists():
            continue
        files = [root] if root.is_file() else list(root.rglob("*.py"))
        for py in files:
            src = py.read_text()
            assert "rr.sim.latent" not in src and "from rr.sim import latent" not in src, (
                f"{py} imports ground truth"
            )
