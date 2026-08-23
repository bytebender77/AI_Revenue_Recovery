# Architecture

A structural account of how one failed payment becomes one decision. **No numbers
live here** — every quantitative claim is in [results.md](results.md), which is
generated from a single sealed run.

If you have ten minutes and want to find the weak parts, they are
[§7](#7-deliberately-not-built) and [§8](#8-known-limitations), and they are
signposted rather than buried. [§2](#2-why-the-gate-runs-before-the-policy) is the
design decision the submission actually rests on.

---

## 1. The decision path

One intent, from gateway failure to settled outcome.

```
        ┌────────────────────────────────────────────────────────────────┐
        │  OBSERVED FAILURE EVENT                                        │
        │  amount · method · regime · mandate_id · consented_channels    │
        │  gateway_code / reason / source / step / description           │
        │  No ground truth. The description may be generic, absent from  │
        │  our taxonomy, or actively misleading. That is the point.      │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  1. NORMALISE          rr/pipeline/normalize.py                │
        │     reason string ──► FailureCause + PersistenceClass          │
        │     Deterministic table first. If it misses, UNKNOWN.          │
        │     (An LLM tail resolver exists here and ships OFF — §3.)     │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  2. ELIGIBILITY GATE   rr/pipeline/eligibility.py              │
        │     Emits the PERMITTED ACTION SET — not a veto. R001…R011.    │
        │     Records passes as well as blocks, each with the threshold  │
        │     and the observed value that was compared to it.            │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  3. SCORE              rr/agent/policy.py                      │
        │     Every permitted action × channel × fire-time gets an       │
        │     expected net value in rupees, with a 90% credible interval │
        │     from rr/model/beta_binomial.py.                            │
        │     NO_ACTION is a scored candidate at exactly 0.0.            │
        │     Prune on ev_hi ≤ 0 (upper bound). Choose argmax ev_mean.   │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  4. CAPACITY AUCTION   rr/eval/agent_run.py · rr/budget.py     │
        │     Human escalation is scarce and SHARED across intents, so   │
        │     it cannot be decided per-intent. Each tick, claimants are  │
        │     ranked by expected net and the quota is granted top-down.  │
        │     A loser re-scores without escalation and records           │
        │     R011_capacity_auction_lost as its binding constraint.      │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  5. COMMIT DECISION    rr/agent/decision.py                    │
        │     One immutable record: full candidate set incl. rejected    │
        │     options and what blocked each, the binding constraint, the │
        │     reason code, and a taxonomy/model/policy version triple.   │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  6. FIRE               at the scheduled hour, not now          │
        │     REVALIDATE: re-read payment state. If it already           │
        │     recovered, ABORT — the decision stands, the action does    │
        │     not fire, and the abort is recorded.                       │
        │     Idempotency: UNIQUE(idempotency_key) on `attempt`.         │
        └───────────────────────────┬────────────────────────────────────┘
                                    ▼
        ┌────────────────────────────────────────────────────────────────┐
        │  7. OUTCOME            terminal state · recovered amount       │
        │     attribution (agent | organic) · debits · contacts          │
        │     Next slot, or settle. Max 6 slots per intent.              │
        └────────────────────────────────────────────────────────────────┘
```

Steps 1–7 are one **tick**. The loop is tick-based rather than per-intent for one
reason: step 4. Escalation capacity is a shared resource, and a per-intent loop
would allocate it to whichever payment happened to fail earliest — an arbitrary
allocation of a scarce good dressed up as a policy.

The explainer ([§3](#3-what-the-llm-cannot-reach)) reads a committed record from
step 5 and writes prose. It runs after the fact and changes nothing.

A worked instance of this path, with real values at every step, is
`dev_pi_000040` — see the README's demo row, or `make serve` then
`/audit/dev_pi_000040/html`, which renders the same seven stages from Postgres.

---

## 2. Why the gate runs before the policy

The gate emits a **set of permitted actions**. It does not inspect the policy's
choice and veto it. Structurally:

```
    ours:   gate ──► permitted set ──► policy ──► choice
    common: policy ──► choice ──► gate ──► allow / veto ──► fallback?
```

This is the one architectural decision worth arguing about, so here is what it
buys. Three consequences, in increasing order of how much they matter:

**(a) The policy can reason about the cost of constrained options.** Because
blocked candidates are still *scored* — priced and then marked unavailable — the
policy knows what the constraint cost. `R008_escalation_value_floor` removing a
₹900 option is a different economic situation from there being no good option at
all, and the record distinguishes them.

**(b) The audit trail says "never available, and here is the binding constraint"
rather than "chose, then blocked".** A veto architecture produces a log in which
the system selected an illegal action and something else caught it. That is
accurate about the code and useless to a compliance reviewer, because it cannot
distinguish "the check worked" from "the check nearly didn't". Under this
ordering, an illegal action never enters the choice set, and the reason it was
absent is recorded next to its price. Rule *passes* are stored too — "we checked
mandate validity and it was fine" is evidence; silence is not.

**(c) A missed check degrades to an empty option set, not a live breach.** This is
the real reason. If a rule is wrong or absent under veto-ordering, the failure
mode is that an illegal action *executes* and is caught afterwards, if at all.
Under gate-ordering the failure mode is that the permitted set is empty or
too small, and the agent does nothing. **The system fails toward inaction.** For
a component whose actions are debits against customer accounts, that is the only
acceptable direction to fail in.

The corollary is that `NO_ACTION` cannot be a fallback branch — it has to be a
priced candidate at zero, or "the gate removed everything" and "nothing was worth
its cost" would be indistinguishable in the record. They are separate reason
codes, and so is "someone else's payment outbid this one".

---

## 3. What the LLM cannot reach

An LLM is used in exactly two places, and **neither can select an action**. That
is enforced structurally, not by prompt instruction — a prompt saying "do not
choose an action" is worth nothing against an input the merchant partly controls.

| | tail normaliser `rr/normalize/` | explainer `rr/explain/` |
|---|---|---|
| Reads | gateway code/reason/description | one committed decision record |
| Writes | a `FailureCause` enum value | a prose string in a separate table |
| Ships | **OFF** — measured and lost | ON |
| Deleting it costs | nothing (UNKNOWN path) | readability only |

Four enforcement mechanisms, from weakest to strongest:

**Package greps.** `rr/normalize/` and `rr/explain/` are scanned for
`ActionType`, `ActionSpec`, `apply_action`, `ExecutionRequest`, `rr.adapters` and
the executor. If a module cannot *name* an action, it cannot pick one.
`test_normalizer_containment.py::test_normalizer_package_cannot_name_an_action`,
and the explainer's equivalent, which also forbids `EVPolicy`. This is the weakest
layer — a grep is defeated by `getattr` and string building, and it caught nothing
that the next mechanism would have missed.

**The parametrised containment test.** This is the one that matters.
`test_no_forced_cause_can_breach_the_gate` is parametrised over **every member of
`FailureCause`**. For each, a resolver is forced to return that cause for every
event in a cohort — including the terminal causes, including the most permissive
soft cause — a full run executes, and the guardrail counters must stay at zero.
So a resolver claiming `insufficient_funds` for a genuinely revoked mandate still
cannot authorise a re-debit: the regime and cap rules are evaluated by the gate,
not by the model. The test proves the *gate*, not the model, decides legality —
which is the claim the containment story actually needs.

**Explanations live in their own table.** `decision_explanation` is separate from
`decision`. Nothing in the explainer's path holds a writable reference to a
decision, and the record is deep-compared before and after explaining — including
under a resolver that returns hostile text. `decision_reason_code` on the
`decision` row remains the source of truth for *why*; the prose is a rendering of
it and never an input to it.

**`resolver=None` renders every required fact.** The explainer's fallback is a
template built from the record alone. Delete the LLM entirely and the audit trail
is still complete — the prose gets worse, and nothing else changes. A deterministic
validator checks that every fact in `required_facts` appears in the returned text
and rejects the draft otherwise; rejections are counted and disclosed rather than
retried until clean.

The untrusted-input surface — `gateway_description` is bank- and
merchant-influenced free text — is analysed in
[injection-surface.md](injection-surface.md).

---

## 4. Where state lives

Three stores, on purpose.

**Postgres** — the demo and audit surface. `failure_event`, `diagnosis`,
`eligibility_snapshot`, `decision`, `attempt`, `outcome`, `llm_call`,
`decision_explanation`, `ledger_entry`. Two guarantees are enforced by the
database rather than by application code, because application code is what you are
auditing:

- `UNIQUE(idempotency_key)` on `attempt` — a replayed tick cannot double-debit.
- `ledger_entry` is hash-chained and carries a `BEFORE UPDATE OR DELETE` trigger
  that raises unconditionally. Not "we don't update it" — *it cannot be updated*.
  `make verify-chain` recomputes the chain. When the equivalence test needed to
  clean up after itself, the trigger refused, and the test was changed to use a
  fresh `run_id` — the test does not get an exemption from a guarantee the
  system advertises.

**Disk (JSONL + JSON)** — the cohorts, the frozen response model, the trained
success models, the LLM call log and cache. `data/{cohort}_observed.jsonl` and
`data/{cohort}_latent.jsonl` are **separate files**, which is the first line of
defence in [§5](#5-the-observablelatent-boundary).

**The eval harness deliberately does not go through Postgres.** It runs
in-process over the JSONL files. Three reasons, in order:

1. *A database round-trip per decision would make the bootstrap unaffordable.*
   Results are 2000 paired resamples across six arms plus sensitivity sweeps.
2. *The harness must run on a clean clone with no container.* `make test` and the
   ablations work with no database at all; the DB-dependent tests skip.
3. *A shared mutable store between arms is a correctness hazard.* Arms are compared
   under common random numbers; a persistent store is exactly the kind of shared
   state that leaks one arm's history into another's decisions.

The obvious objection is that this makes the measured system and the demonstrated
system two different things. That objection is correct, and [§6](#6-one-decision-layer)
is the answer to it.

---

## 5. The observable/latent boundary

Every number is void if agent code can see ground truth. `LatentState` — true
cause, whether the payment self-heals and when, the customer's real balance
trajectory — must never reach the policy. Three enforcement layers:

**Layer 1 — field-disjoint dataclasses.** `ObservedFailureEvent` /
`AttemptContext` in `rr/contracts.py` versus `LatentState` in `rr/sim/latent.py`.
`test_latent_and_observable_are_field_disjoint` fails if a field name appears on
both sides, so a latent field cannot be quietly mirrored into the observable
struct under the same name.

**Layer 2 — separate files, plus a runtime tripwire.** Observed and latent are
written to different JSONL files; the agent's loader only opens one.
`assert_no_latent_leak()` re-checks any dict handed to agent code, which catches
hand-assembled dicts that the dataclass boundary would not see.

**Layer 3 — import greps.** `rr/agent`, `rr/pipeline`, `rr/normalize`,
`rr/adapters` and `rr/baselines/rules.py` are scanned for any import of
`rr.sim.latent`. Ground truth enters exactly once, through `rr/sim/adapter.py`,
which is the executor seam and is injected.

### The leak that got past all three

`rr/pipeline/runner.py` reached into `adapter._lat` directly.

Every layer above missed it. The dataclasses were still disjoint. The files were
still separate. The grep looked for *imports of the latent module* — and
`runner.py` imported no such thing, it read a private attribute off an object it
had legitimately been handed. Ground truth was laundered through the seam that
was supposed to contain it.

It was found by reading the audit output, not by a test.

The fix was to give the operation a name: `SimAdapter.latents_for(obs_rows)`, with
a docstring saying why it is public and that the harness genuinely needs it. The
harness drives a simulated world and must be handed latent state to roll outcomes
against; the problem was never that the dependency existed, it was that it was
*invisible*. Naming it makes it greppable, reviewable, and obvious in a diff.

**Be clear about the strength of that closure: it is a naming convention, not a
test.** Nothing currently fails if a future caller reaches into `_lat` again. The
other two boundary properties have tests; this one has a docstring. It is listed
in [§8](#8-known-limitations) rather than presented as solved.

---

## 6. One decision layer

There used to be two policies. `rr/pipeline/policy.py` (an M2 rules port — that
path no longer exists) sat behind `make run`, and `rr/agent/policy.py` (the EV
policy) sat behind every measured number. The demo would have shown one system while the results described
a different one — and nothing in the repo would have said so.

That is closed, and the artifact that closes it is
[`tests/test_one_decision_layer.py`](../tests/test_one_decision_layer.py):

- It runs **one cohort through both entry points** — the eval harness in memory,
  the demo path through `PostgresSink` — and deep-compares decision records field
  by field: every candidate's score, permission flag, `blocked_by`, the binding
  constraint, the reason code, the version triple.
- Both sides are **round-tripped through JSON** first, so float formatting and
  jsonb key ordering cannot masquerade as either agreement or disagreement.
- The rules port was **retired to `rr/attic/`**, and a test asserts no live
  package imports it. Dead code reachable only by its own tests is worse than no
  code, because a reader cannot tell it is dead.
- `score_basis` is checked repo-wide: if anything still emits `rule_priority`, the
  old layer is alive somewhere.

This is what makes "measured" and "demonstrated" the same claim. The eval harness
bypasses Postgres ([§4](#4-where-state-lives)) — the equivalence test is the
reason that is a performance decision rather than a credibility hole.

---

## 7. Deliberately not built

Named here so the omissions are choices rather than gaps you found.

**Cross-rail routing.** The obvious next move for a recovery agent is to retry a
failed card mandate over UPI Autopay, or move a customer to a different acquirer
after issuer-side declines. **This system is single-PSP, so it cannot do that**,
and `RETRY_ALTERNATE_METHOD` is limited to an alternate instrument already on
file for the same customer under the same PSP. Cross-rail retry needs multi-PSP
routing, per-rail mandate portability, and a per-rail success model — none of
which are here, and none of which the synthetic corpus could honestly evaluate.
The action space is six actions for the same reason: each one has to be something
this architecture could actually execute.

**A learned action model.** The success model is an empirical-Bayes
Beta-Binomial over named cells, not a gradient-boosted tree. Every score traces to
a cell with an observation count, which is what makes the audit trail readable —
"*n* observations, *k* successes, shrunk toward its parent" is inspectable in a
way a leaf index is not. A tree would very likely score better on dev. That is the
trade, made deliberately.

**An agent framework.** No planner, no tool-calling loop, no scratchpad. State is
a Postgres row and a batch tick. An LLM that can call tools is an LLM that can
take actions, which is precisely the property [§3](#3-what-the-llm-cannot-reach)
exists to prevent.

**Live execution.** The `revalidate`/`execute`/`poll_outcome` contract in
`rr/adapters/base.py` is defined and unwired — the tick loop executes and
`PostgresSink` records. See [§8](#8-known-limitations).

---

## 8. Known limitations

The full list is in [shipping-config.md](shipping-config.md#known-limitations-stated-rather-than-buried).
The four that bear on *architecture* rather than on results:

**1. Some of our reason strings are invented, and the largest gap is in the
majority cohort.** `do_not_honour` is not a Razorpay reason string — we invented
it; the nearest real one is `card_declined`. Worse, `mandate_revoked` and
`amount_exceeds_mandate` do not appear in the card reason list at all: **the
e-mandate and UPI Autopay reason sets were not read within the timebox**, and the
corpus is predominantly mandate-based. So the reason vocabulary is least verified
exactly where the corpus is densest. Full mapping, including which strings are
Razorpay's and which are ours, in
[razorpay-reason-mapping.md](razorpay-reason-mapping.md). This does not move any
measured number — the synthetic corpus contains only the synthetic strings — but
it does mean the normalisation table would need real-corpus work before this ran
against production traffic.

**2. The Razorpay adapter has never made a live call.** This repo has no
credentials; `make razorpay-probe` exits 3 with a clear message rather than
pretending. Every number comes from the `sim` adapter. With credentials it would
create one ₹1 test-mode order, which would prove the adapter is not a stub and
would prove nothing else.

**3. The success model is overconfident in the 0.2–0.4 band.** Predicted
probabilities in that range exceed observed rates on held-out attempts — see
[results.md §4](results.md#4-calibration--held-out). This is a live architectural
consequence, not a footnote: the policy prunes on `ev_hi` and chooses on
`ev_mean`, so a mis-centred posterior in the busiest probability band biases
toward acting. It is reported uncorrected, because recalibrating after the sealed
run would unseal it.

**4. The private-attribute boundary leak is closed by naming, not by a test** —
[§5](#5-the-observablelatent-boundary). Of everything in this document, this is
the gap I would close first.

All regulatory values — retry caps, e-mandate notification windows, quiet hours,
chargeback economics — are **UNVERIFIED config placeholders** carrying
`TODO(citation)` slots in `rr/config.py`. None is asserted as fact anywhere in
this repo. See [compliance-open-questions.md](compliance-open-questions.md).
