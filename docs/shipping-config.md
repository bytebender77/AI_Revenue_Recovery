# Shipping configuration — frozen for M6

This is the exact configuration the sealed-cohort run measures and the demo video
shows. If those two ever diverge, the submission is not honest. Changing anything
here after the sealed run invalidates the reported numbers.

Frozen: 2026-08-23. Commit: see `git log -1 -- docs/shipping-config.md`.

---

## ✅ CLOSED — one decision layer

There were **two** decision layers: an M2 rules port behind `make run` and the M4 EV
policy behind every number, so the video would have demonstrated one system while
the results described another.

Closed. `rr/pipeline/runner.py` now calls the same tick loop as the eval harness and
supplies only a Postgres sink; `rr/agent/policy.py` scores every candidate and
`rr/agent/decision.py` serialises every record. The rules port is retired to
`rr/attic/rules_port_policy.py` and is not importable from any live package.

**Evidence: `tests/test_one_decision_layer.py`.** It runs one cohort through both
entry points — the eval harness in memory, the demo path through Postgres — and
deep-compares every decision record field by field: every candidate's score,
`permitted`, `blocked_by`, the binding constraint, the reason code, and all three
version strings. Both sides are round-tripped through JSON so a jsonb ordering or
float difference cannot masquerade as agreement. Companion tests assert the retired
module is unreachable and that nothing still emits `score_basis="rule_priority"`.

Two defects surfaced while proving it, both now fixed:

- **`(payment_intent_id, slot)` was not unique.** A deferred action is re-decided at
  the same slot when its tick arrives, so a legitimate re-decision was
  indistinguishable from a duplicate row. Added `decision_seq`, monotonic per intent.
- **`decision` had no `run_id`**, reachable only via a four-table join, which left
  `decision_seq` uniqueness unscoped across runs. Denormalised onto the row.

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
| `POLICY_VERSION` | `ev-policy-v1.0.0` — one layer, one value |
| `EXPLAINER_VERSION` | `explainer-v1.0.0` |
| Response model | `v1.0.0`, sha256 `0fd67f8b673f08b2…` — frozen before any policy code existed |

## Config values

```
POLICY.tick_hours                  6.0
POLICY.ev_objective                incremental      # not gross; rationale + the
                                                    # measured tie are in README.md
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

## Demo intent

`dev_pi_000040` — reproduce with `--run-id fulldev --limit 6000`, then
`AUDIT=dev_pi_000040 make audit`.

₹1,879.31 UPI checkout failure, `customer_initiated`, `auth_failed`. The policy's
highest-scoring option was `retry_same` at ₹142.45 (p=0.2534 from a 19,819-observation
cell) and **the gate removed it** — `R001_no_mandate_no_debit`, because there is no
standing authority to re-debit a one-time checkout. `retry_alternate_method` blocked
by the same rule; `escalate_human` blocked by the value floor. It chose the only
permitted lever, an email nudge at ₹55.66, scheduled for h=261.14.

At fire time the executor re-read payment state and found the customer had already
paid at h=260.97 — ten minutes earlier. The nudge was **aborted**, not sent.

Final: recovered ₹1,879.31, `attribution = organic`, **0 debits, 0 contacts**. The
agent's best behaviour on this row was to want three illegal things, take the one
legal one, and then not even do that.

Selected from 52 fire-time aborts on full dev; the 500-intent sample had 4, none of
which had a debit blocked by the gate.

## Known limitations, stated rather than buried

1. **Unmapped-code descriptions are independent of the true cause** in the frozen
   cohort generator, so chance (~1/15) is the ceiling on that slice for any
   classifier. Documented, not fixed — fixing it would invalidate M1–M5.
2. **True `NEVER_RETRY` violations cannot reach zero.** ~15% of failures arrive with
   a generic or unmapped code hiding a terminal cause. Gate-visible violations are 0
   and must stay 0; true-cause violations are irreducible given the signal.
3. **`ESCALATE_HUMAN` is likely over-powered** in the frozen response model
   (p ≈ 0.23 at ₹40), making it +EV almost everywhere. Frozen before any results
   were seen; disclosed rather than retuned.
4. **B3 is a greedy oracle**, so it is a lower bound on the ceiling — B2.5 beats it
   on `auth_failed`.
5. **The adapter execution seam is unwired.** `rr/pipeline/executor.py` was dead
   and has been deleted. `rr/adapters/base.py` and `rr/sim/adapter.py` still define
   `revalidate`/`execute`/`poll_outcome` with no live caller — the tick loop
   executes and `PostgresSink` records. They are retained as M7's contract for the
   one live Razorpay test-mode call and carry a header saying so. Idempotency,
   fire-time revalidation and `attempt` rows are preserved in the sink and asserted.
6. **Explainer rejection rate is 0% on EV records** (0/54 live gpt-4o calls), with
   `p_success` in the required set. Not a check that never runs: falsifying
   `p_mean` against all 60 stored drafts rejects 60/60. gpt-4o quotes the record
   verbatim reliably when the prompt demands it.
