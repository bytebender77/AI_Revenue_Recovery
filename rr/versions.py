"""The version triple stamped on every decision.

A decision that cannot name the code that produced it is not auditable. These
strings are written into every `decision` row so a result can be tied back to an
exact policy, taxonomy, and success model.
"""
TAXONOMY_VERSION = "taxonomy-v1.0.0"
NORMALIZER_VERSION = "map-v1.0.0"        # deterministic lookup only; M5 adds the LLM tail
MODEL_VERSION = "none-m2"                # no success model yet; M4 replaces this
POLICY_VERSION = "rules-b2-port-v1.0.0"  # M4 replaces with the EV policy
