# AI Revenue Recovery — failed subscription & auto-debit recovery

An agent that ingests failed payment events, diagnoses the cause, computes the set of
**legally permitted** actions, scores each by expected net value, picks one —
including doing nothing — executes it idempotently, and measures **incremental**
recovery against a randomised control arm on a sealed held-out cohort.

**Every number below traces to [docs/results.md](docs/results.md)**, the single source
of truth. It is generated from [docs/results_test.json](docs/results_test.json), the
raw output of one run on a cohort that was read exactly once.

---

## The result

Held-out test cohort, n = 3000. Disjoint merchants and customers from the training
data, a later time window, and an outage pattern that does not appear in dev.

| arm | incremental | 95% CI | net | debits | contacts |
|---|---|---|---|---|---|
| B1 blind ladder | 934,631 | [785,796, 1,099,298] | **−147,259** | 7056 | 0 |
| B2 good rules | 1,499,519 | [1,293,729, 1,715,769] | 559,940 | 2915 | 1785 |
| B2.5 + outage rule | 1,565,637 | [1,359,492, 1,785,915] | 586,720 | 2753 | 1763 |
| **AGENT** | **1,739,601** | **[1,509,079, 1,975,881]** | **639,506** | 2850 | **1056** |
| B3 greedy oracle | 2,115,228 | [1,866,097, 2,352,416] | 831,827 | 1950 | 1373 |

INR. **Beats the rules baseline by +240,082** [71,499, 414,677] while sending **41%
fewer customer contacts**, and captures **82.2%** of the greedy oracle's attainable
uplift.

**The margin roughly halved out of sample and we lead with that.** Per 1000 intents,
the paired advantage over B2.5 fell 55% dev → test and oracle share fell 93.8% →
82.2%. The baselines degraded less (−7% vs −16%) because rules do not overfit. Full
delta table in [results.md §2](docs/results.md).

We report **incremental**, not gross. Gross was 3,475,288; the 1.7M difference is what
would have recovered anyway and we do not claim it.

---

## Quick start

Requires Docker and Python 3.11+.

```bash
git clone <repo> && cd razorpay
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
docker compose up -d --wait db
make db-init && psql "$DATABASE_URL" -f sql/002_llm_call.sql && psql "$DATABASE_URL" -f sql/003_decision_seq.sql
make test          # 73 tests
make run           # end-to-end: 500 intents, every decision written to Postgres
```

> **Use the venv, not a conda base environment.** `openai` 3.x depends on **`httpx2`**,
> not `httpx`. In a polluted conda base the SDK raises
> `TypeError: process() takes no keyword arguments`, surfaced as `APIConnectionError`,
> *with correct versions installed* — `curl` works, raw `httpx2` works, only the SDK
> fails. Identical versions work in a clean venv. Run everything as `./.venv/bin/python`.

> **Database port.** Defaults to **55432** (`RR_DB_PORT`). 5433 and 5434 were both
> shadowed by a local Postgres bound to `127.0.0.1`, which wins over the container's
> `0.0.0.0` bind for localhost traffic and produces a confusing `role "rr" does not
> exist`.

Optional, for the LLM components: `cp .env.example .env` and add `OPENAI_API_KEY`
(unquoted — `make` loads `.env` via `include`, which keeps quote characters).

---

## What to look at

```bash
make report                          # docs/report.html — self-contained results page
make serve                           # then open /audit/dev_pi_000040/html
AUDIT=dev_pi_000040 make audit       # the same reconstruction in psql
make sealed-report                   # refuses: the cohort has already been read
```

### The demo row — `dev_pi_000040`

A ₹1,879.31 UPI checkout failure. The policy's **highest-scoring option was
`retry_same` at ₹142.45** (p=0.2534, from a 19,819-observation cell) and **the
eligibility gate removed it** — `R001_no_mandate_no_debit`, because a one-time
checkout carries no standing authority to re-debit. The alternate rail went the same
way; escalation fell below the value floor. It took the only permitted lever, an email
nudge at ₹55.66.

At fire time the executor re-read payment state and found the customer had already
paid **ten minutes earlier**. The nudge was aborted, not sent.

Final: recovered ₹1,879.31, `attribution = organic`, **0 debits, 0 contacts**. The
agent wanted three illegal things, took the one legal one, then correctly did not do
that either.

---

## How it works

Full structural account — the decision path, the boundaries, and what is deliberately not built — in **[docs/architecture.md](docs/architecture.md)**.

```
failure event → normalise cause → eligibility gate → EV policy → execute → outcome
                (deterministic)   (permitted set)    (argmax)    (idempotent)
```

**The gate runs before the policy and shapes the action space**, rather than vetoing
afterwards. The policy never sees an illegal action, so a missed check produces an
empty option set rather than a live breach.

**Expected net value**, not gross: `p_success × (1 − p_organic) × amount × margin −
attempt cost − contact cost − annoyance − chargeback risk`.

The `(1 − p_organic)` term is the objective agreeing with the metric. The literal
formula `p_success × amount × margin` maximises **gross** recovery — it pays an action
in full for succeeding on a payment that would have recovered on its own, which is the
exact number this project argues against reporting. Multiplying by `(1 − p_organic)`
means value accrues only on the branch where nothing else would have worked. Without
it, the agent optimises one quantity and is scored on another, and the gap between them
is pure claimed credit.

**We measured the difference rather than asserting it.** `POLICY.ev_objective="gross"`
runs as its own arm. On dev (n=6000, `make m4` — *not* the sealed cohort):
`ev_incremental` beats `ev_gross` by **16,407, 95% CI [−105,871, 139,349]** — a **tie**
on recovery. The honest reading is that the term does not buy measurable incremental
recovery; **it buys restraint.** The gross objective reaches the same recovery while
sending **8% more customer contacts** (2364 vs 2182), issuing more debits (5644 vs
5557), and committing more true `NEVER_RETRY` violations (329 vs 315) — it chases
payments that were going to land anyway. We ship `incremental` for that, and because an
agent scored on incremental recovery should be optimising incremental recovery.

**`NO_ACTION` is a scored candidate at exactly zero**, not a fallback branch. It wins
13.2% of decisions, and 22.8% of intents are never touched at all. Three
economically distinct reasons, never merged: a rule removed the option, **someone
else's payment outbid this one** in the escalation capacity auction, or nothing
available cleared its own cost.

**Success model** is an empirical-Bayes Beta-Binomial with hierarchical shrinkage —
not a fitted tree — so every score traces to a named cell with an observation count:
*"43 observations, 11 successes, posterior mean 0.26, 90% CI [0.17, 0.37], shrunk
toward its parent."* Brier 0.1007 on held-out. It is **systematically overconfident in
the 0.2–0.4 band** (predicted 0.254 vs observed 0.180 over 1075 attempts) — reported,
not corrected, because correcting after the sealed run would unseal it.

**Audit trail.** Every decision stores its full candidate set — including rejected
options and the rule that blocked each — plus the binding constraint, the reason code,
and a version triple. The ledger is append-only, hash-chained, and trigger-protected;
`make verify-chain` recomputes it.

---

## Where LLMs are used, and where they are not

Two places, neither of which can select an action.

| | tail normaliser | explainer |
|---|---|---|
| **Ships** | **OFF** | ON |
| Job | resolve gateway codes the deterministic map misses | render a committed decision as prose |
| Constraint | `json_schema`, `strict: true`, closed taxonomy enum + UNKNOWN | `json_schema`, single field |
| On failure | UNKNOWN → conservative path | templated fallback built from the record alone |

**The normaliser ships OFF because we measured it and it lost.** Live `gpt-4o`:
**−191,440, 95% CI [−370,675, −17,320]** — a significant loss. It resolved 676/676 of
the one slice with real signal, but resolving a cause removes the conservative UNKNOWN
chargeback pricing, so the agent debits more, and confident-wrong answers on the
no-signal slices outweigh the wins. **The UNKNOWN path is a load-bearing safety
mechanism, not a fallback.** `make ablation-normalizer RESOLVER=openai` is the
argument. Same story for `feature_cross_v2`: built, measured, lost, shipped off.

Containment is structural, not prompt-engineering. `rr/normalize/` and `rr/explain/`
cannot name an action — they are grepped for `ActionType`, `ActionSpec`,
`apply_action`, the executor and the adapters. `test_no_forced_cause_can_breach_the_gate`
forces the normaliser to return **every cause in the taxonomy**, one full run each, and
asserts the guardrail counters stay at zero. The gateway description is untrusted
bank- and merchant-influenced text: [docs/injection-surface.md](docs/injection-surface.md).

---

## Guardrails

Across every arm and every signal tier on the sealed cohort:

- **0 observable `NEVER_RETRY` violations** — re-debits against a cause the gate could
  see was terminal.
- **0 unauthorised debits** — re-debits with no standing mandate. B1 attempted 1391.

159 *true* violations remain and **cannot reach zero**: they sit entirely in tiers
where the gateway hid the cause. The `faithful` tier — 72% of the cohort — has none.
A gate can enforce what the signal reveals and nothing more; the tier table in
[results.md §5](docs/results.md) is the evidence.

Also enforced: per-intent debit and contact caps, quiet hours, consent, mandate
validity, a shared escalation capacity auction, and a circuit breaker keyed on
`(cause, attempt_index)` — keyed on the pair because pooling attempt indices let a
healthy cause trip on the strength of its own naturally-weak third attempt.

---

## Razorpay test mode

**Open question 9 is resolved: test mode can force a *specific* failure reason**, not
only a generic failure. Razorpay documents an Error Scenarios section with per-error
test cards, and the real `reason` enumeration is now mapped onto our taxonomy in
[docs/razorpay-reason-mapping.md](docs/razorpay-reason-mapping.md). Three of our
strings match Razorpay's exactly; several are ours, and that document says which.

**This repo has no Razorpay credentials, so the adapter has never made a live call.**
`make razorpay-probe` exits 3 with a clear message rather than pretending. With
`RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET` set it creates one ₹1 test-mode order — that
call would prove the adapter is not a stub, and nothing more. **Every number in this
README comes from the `sim` adapter on the sealed cohort; one live call cannot and
does not change any of them.**

---

## Honest limitations

Stated here rather than buried; full list in
[docs/shipping-config.md](docs/shipping-config.md).

1. **The agent loses on four causes.** `mandate_invalid` (−38,051) persists from dev
   and is a real weakness. `upi_collect_expired` and `limit_exceeded` flipped from
   wins on dev to losses on test.
2. **B2.5's outage rule stopped being significant on test** (CI spans zero) — a
   threshold tuned on one outage shape does not transfer. The agent's
   `issuer_downtime` win held.
3. **B3 is a *greedy* oracle**, so "82.2% of attainable" is measured against a lower
   bound on the true ceiling.
4. **`ESCALATE_HUMAN` is likely over-powered** in the frozen response model, making it
   +EV almost everywhere. Frozen before any result was seen; disclosed, not retuned.
5. **All regulatory values are UNVERIFIED config placeholders** with `TODO(citation)`
   slots — RBI e-mandate windows, card-network retry caps, TRAI quiet hours,
   chargeback economics. None is asserted as fact anywhere. See
   [docs/compliance-open-questions.md](docs/compliance-open-questions.md).
6. **Data is synthetic**, generated by a response model frozen and committed *before*
   any policy code existed and pinned by hash
   ([docs/CHANGELOG-sim.md](docs/CHANGELOG-sim.md)), so the world could not be tuned
   until the agent won.

---

## Repo map

| path | what |
|---|---|
| `rr/sim/` | frozen response model, cohort generator, sim executor adapter |
| `rr/agent/` | EV policy, decision record, observable features |
| `rr/pipeline/` | ingest, normalise, eligibility gate, Postgres sink |
| `rr/normalize/`, `rr/explain/` | the two LLM call sites, containment-tested |
| `rr/eval/` | tick loop, metrics, ablations, sealed run |
| `rr/api/`, `rr/report/` | audit endpoint, static HTML report |
| `rr/attic/` | the retired M2 rules port, unreachable and asserted so |
| `docs/architecture.md` | **how it fits together**, and what it cannot do |
| `docs/results.md` | **the numbers** |
| `docs/shipping-config.md` | exactly what M6 measured and the video shows |
