"""The version strings stamped on every decision.

A decision that cannot name the code that produced it is not auditable.

ONE DECISION LAYER. `rr/agent/policy.py` scores every candidate, whichever entry
point asked. The M2 rules port is retired to rr/attic/ and unreachable from any
live package -- tests/test_one_decision_layer.py asserts both the unreachability
and the record-for-record equivalence of the two entry points.
"""
TAXONOMY_VERSION = "taxonomy-v1.0.0"

# Deterministic map only. The LLM tail normaliser ships OFF -- see the ablation.
NORMALIZER_VERSION = "map-v1.0.0"

# feature_cross_v1 ships. v2 is retained and runnable but lost its ablation.
MODEL_VERSION = "beta-binomial-featurecross-v1.0.0"

# Stamped on every decision row by every entry point.
EV_POLICY_VERSION = "ev-policy-v1.0.0"
POLICY_VERSION = EV_POLICY_VERSION   # alias; there is only one policy now

EXPLAINER_VERSION = "explainer-v1.0.0"
