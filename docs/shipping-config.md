# Shipping configuration — frozen for M6

This is the exact configuration the sealed-cohort run measures and the demo video
shows. If those two ever diverge, the submission is not honest. Changing anything
here after the sealed run invalidates the reported numbers.

Frozen: 2026-08-23. Commit: see `git log -1 -- docs/shipping-config.md`.

---

## ⚠️ OPEN BLOCKER — two decision layers, only one is measured

The repo contains **two** decision layers and they are not the same code:

| Path | File | Used by | Stamps |
|---|---|---|---|
| Demo | `rr/pipeline/policy.py` | `make run`, Postgres, `make audit` | `rules-b2-port-v1.0.0` |
| Measured | `rr/agent/policy.py` | eval harness, all ablations | `ev-policy-v1.0.0` |

**The video would demonstrate the M2 rules port while the numbers come from the M4
EV policy.** That is exactly the divergence this document exists to prevent. It must
be closed before the sealed run — port the EV policy into the pipeline so one system
is both measured and demonstrated. Until then, every number below describes the
*measured* path only.

---

## What ships

| Component | Setting | Why |
|---|---|---|
| Feature cross | **v1** | v2 (`+day_of_month, hour_of_day, issuer_recent_failure_rate`) is a strict refinement and still lost: −182,774, 95% CI [−392,789, 26,647], Brier 0.1168 vs 0.1090. Retained and runnable via `make ablation-features`. |
| Tail normaliser | **OFF** | Live gpt-4o: −191,440, 95% CI [−370,675, −17,320]. Significant loss. Resolving a cause removes the conservative UNKNOWN chargeback pricing, so the agent debits more (5040 → 5361), and confident-wrong on the no-signal slices outweighs 100% resolution on the signal slice. Retained; `make ablation-normalizer` is the argument. |
| Explainer | **ON** | Read-only, validated, templated fallback. Does not affect any decision, so it cannot affect any number. |
| Success model | Beta-Binomial, empirical-Bayes | `shrinkage_k=25.0`, `min_cell_observations=8`, fit on a 50% hash split of dev under CRN namespace `explore{0..19}` |
| Escalation | Capacity auction | 5% of cohort, allocated per tick to highest `expected_net` claimants |
| Circuit breaker | `(cause, attempt_index)` | window 60, floor 0.015, arms after 40. Keyed on the pair, **not** cause alone — pooling indices let a healthy cause trip on its own naturally-weak third attempt. |

## Version strings

| Key | Value |
|---|---|
| `TAXONOMY_VERSION` | `taxonomy-v1.0.0` |
| `NORMALIZER_VERSION` | `map-v1.0.0` (deterministic map only) |
| `MODEL_VERSION` | `beta-binomial-featurecross-v1.0.0` |
| `POLICY_VERSION` (demo) | `rules-b2-port-v1.0.0` |
| `EV_POLICY_VERSION` (measured) | `ev-policy-v1.0.0` |
| `EXPLAINER_VERSION` | `explainer-v1.0.0` |
| Response model | `v1.0.0`, sha256 `0fd67f8b673f08b2…` — frozen before any policy code existed |

## Config values

```
POLICY.tick_hours                  6.0
POLICY.ev_objective                incremental      # not gross; see docs/ev-objective.md
POLICY.p_chargeback_known_soft     0.002
POLICY.p_chargeback_unknown_cause  0.075            # priced high on purpose
COSTS.escalation_capacity_pct      0.05
ESCALATE_MIN_AMOUNT_MINOR          200_000
BREAKER.window_attempts            60
BREAKER.min_success_rate           0.015
BREAKER.min_attempts_before_arming 40
MODEL.shrinkage_k                  25.0
MODEL.min_cell_observations        8
MODEL.fit_split_fraction           0.50
OUTAGE (B2.5 baseline only)        8 failures / 2.0h window / 6.0h backoff
```

Every regulatory value stays a config with a `TODO(citation)` slot and is
**UNVERIFIED** — RBI e-mandate windows, card-network retry caps, TRAI quiet hours,
chargeback economics. See `docs/compliance-open-questions.md`. None is asserted as
fact anywhere in the repo or the writeup.

## LLM configuration

| | Normaliser | Explainer |
|---|---|---|
| Ships | **OFF** | **ON** |
| Provider | `openai` / `anthropic` / `offline` | `openai` / `none` |
| Model | `gpt-4o-2024-08-06` (`RR_OPENAI_MODEL`) | same |
| Constraint | `response_format.json_schema`, `strict: true`, closed enum + UNKNOWN | `json_schema`, single `explanation` field |
| Temperature | 0.0 logged (OpenAI exposes it; claude-opus-5 returns 400 and logs `null` + note) | same |
| Cache key | `<kind>:<model_id>:<input_hash>`, `CACHE_FORMAT=2` | same |
| On failure | UNKNOWN → conservative path | templated fallback |
| Failure bound | `MAX_FAILURES_PER_KEY=2` per input per run; errors never persisted | same |

**Neither LLM can select an action.** `rr/normalize/` and `rr/explain/` are grepped
for `ActionType`, `ActionSpec`, `apply_action`, the executor, and the adapters;
`test_no_forced_cause_can_breach_the_gate` forces the normaliser to return every
cause in the taxonomy and asserts the guardrail counters stay at zero.

## Environment

- **Use `.venv`, not conda base.** `openai` 3.x uses `httpx2`; in the conda base env
  the SDK raised `TypeError: process() takes no keyword arguments` (surfaced as
  `APIConnectionError`) with correct versions installed. Identical versions work in
  a clean venv.
- DB port default **55432** (`RR_DB_PORT`). 5433 and 5434 were both shadowed by a
  local Postgres bound to `127.0.0.1`.
- `.env` is loaded by `make`; never committed. Do not quote values.

## Reproduce

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
docker compose up -d --wait db && make db-init
psql "$DATABASE_URL" -f sql/002_llm_call.sql
make test
make ablation-features        # v1 vs v2
make ablation-normalizer RESOLVER=openai
```

## Known limitations, stated rather than buried

1. **Two decision layers** (above). Blocker.
2. **Unmapped-code descriptions are independent of the true cause** in the frozen
   cohort generator, so chance (~1/15) is the ceiling on that slice for any
   classifier. Documented, not fixed — fixing it would invalidate M1–M5.
3. **True `NEVER_RETRY` violations cannot reach zero.** ~15% of failures arrive with
   a generic or unmapped code hiding a terminal cause. Gate-visible violations are 0
   and must stay 0; true-cause violations are irreducible given the signal.
4. **`ESCALATE_HUMAN` is likely over-powered** in the frozen response model
   (p ≈ 0.23 at ₹40), making it +EV almost everywhere. Frozen before any results
   were seen; disclosed rather than retuned.
5. **B3 is a greedy oracle**, so it is a lower bound on the ceiling — B2.5 beats it
   on `auth_failed`.
6. **Explainer rejection rate was 0/40 on live gpt-4o**, but those were
   `rule_priority` records carrying no `p_success`, so the strongest check was not
   exercised. Expect a nonzero rate on M6's EV records; report whatever it is.
