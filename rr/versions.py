"""The version strings stamped on every decision.

A decision that cannot name the code that produced it is not auditable.

NOTE ON THE TWO POLICIES. The repo currently contains two decision layers:
`rr/pipeline/policy.py` (the M2 rules port, used by `make run` and the Postgres
demo path) and `rr/agent/policy.py` (the M4 EV policy, used by the eval harness).
They stamp different POLICY_VERSION values because they are different code. That
divergence is an open blocker for the submission -- see docs/shipping-config.md.
"""
TAXONOMY_VERSION = "taxonomy-v1.0.0"

# Deterministic map only. The LLM tail normaliser ships OFF -- see the ablation.
NORMALIZER_VERSION = "map-v1.0.0"

# feature_cross_v1 ships. v2 is retained and runnable but lost its ablation.
MODEL_VERSION = "beta-binomial-featurecross-v1.0.0"

# Stamped by rr/pipeline/policy.py (the demo path).
POLICY_VERSION = "rules-b2-port-v1.0.0"

# Stamped by rr/agent/policy.py (the measured path, M4 EV policy).
EV_POLICY_VERSION = "ev-policy-v1.0.0"

EXPLAINER_VERSION = "explainer-v1.0.0"
