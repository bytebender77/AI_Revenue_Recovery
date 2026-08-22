"""Tripwire, not a lock.

The response model is committed before any policy exists. This test makes a later
edit to it a visible, deliberate act rather than a quiet tuning pass. To change it:
bump RESPONSE_MODEL_VERSION, run `make freeze`, and record why in docs/CHANGELOG-sim.md.
"""
import hashlib
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "rr" / "sim" / "response_model.py"
PIN = REPO / "rr" / "sim" / "response_model.sha256"


def test_response_model_matches_recorded_hash():
    if not PIN.exists():
        pytest.fail("no frozen hash recorded -- run `make freeze`")
    expected = PIN.read_text().split()[0].strip()
    actual = hashlib.sha256(SRC.read_bytes()).hexdigest()
    assert actual == expected, (
        "response_model.py changed after freezing.\n"
        f"  recorded {expected}\n  actual   {actual}\n"
        "If intentional: bump the version, `make freeze`, and note it in docs/CHANGELOG-sim.md."
    )
