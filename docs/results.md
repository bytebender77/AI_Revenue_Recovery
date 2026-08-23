# Results — sealed test cohort

**Single source of truth.** Every number in the README, the architecture doc and the
pitch video must trace to this file. Raw output: `docs/results_test.json`,
`docs/results_dev.json`. Reproduce with `make sealed-report`.

The test cohort was generated in M1 with **disjoint merchants and customers**, a
**later time window** (sim days 21–30 vs 1–20), and an **outage pattern absent from
dev**. The success model was fit on a 50% hash split of dev only. It was read exactly
once, on 2026-08-23; `data/SEALED_RUN_RECEIPT.json` records the timestamp and the
cohort hash, and `rr/eval/sealed_run.py` refuses a second run.

Configuration is exactly [shipping-config.md](shipping-config.md): feature_cross_v1,
tail normaliser **OFF**, explainer ON, escalation capacity 5%, circuit breaker keyed
on `(cause, attempt_index)`. Nothing was tuned after seeing a number.

---

## 1. Headline — held-out, n = 3000

| arm | gross | incremental | 95% CI | net | debits | contacts | wasted% | oracle% |
|---|---|---|---|---|---|---|---|---|
| B0 do-nothing | 1,735,686 | 0 | — | 0 | 0 | 0 | — | 0% |
| B1 blind ladder | 2,670,318 | 934,631 | [785,796, 1,099,298] | **−147,259** | 7056 | 0 | — | 44.2% |
| B2 good rules | 3,235,206 | 1,499,519 | [1,293,729, 1,715,769] | 559,940 | 2915 | 1785 | 91.9% | 70.9% |
| B2.5 + outage rule | 3,301,323 | 1,565,637 | [1,359,492, 1,785,915] | 586,720 | 2753 | 1763 | 91.9% | 74.0% |
| **AGENT** | **3,475,288** | **1,739,601** | **[1,509,079, 1,975,881]** | **639,506** | 2850 | 1056 | 93.3% | **82.2%** |
| B3 greedy oracle | 3,850,914 | 2,115,228 | [1,866,097, 2,352,416] | 831,827 | 1950 | 1373 | 88.2% | 100% |

All figures INR. Incremental = recovered − what B0 would have recovered on the same
intent, paired per intent with common random numbers. `oracle%` is the share of
B3_greedy's incremental captured; B3 is **greedy**, so it is a lower bound on the
true ceiling — B2.5 beats it on some cells.

**B1 is net-negative (−147,259)** on 2.5× the agent's debit volume, once
terminal-retry penalties are counted. Indiscriminate dunning destroys value.

### Head to head, paired

| comparison | delta | 95% CI | verdict |
|---|---|---|---|
| AGENT vs B2 | +240,082 | [71,499, 414,677] | **beats** |
| AGENT vs B2.5 | +173,965 | [9,288, 336,556] | **beats**, narrowly |
| B2.5 vs B2 | +66,117 | [−1,057, 143,017] | **ties** — see §6 |

### Wasted contacts

| arm | wasted / sent | modelled annoyance cost |
|---|---|---|
| B2 | 1641 / 1785 | 14,769 |
| B2.5 | 1620 / 1763 | 14,580 |
| **AGENT** | **985 / 1056** | **8,865** |

The agent sends **41% fewer contacts than B2** for more incremental recovery. A
contact is "wasted" when it could not have changed the outcome — the payment was
going to recover anyway, or it never recovered. Measurable here only because the
simulator holds the counterfactual; in production the randomised control arm is what
estimates it.

---

## 2. Dev → test delta

**This is the most important table in the document.** Dev is in-sample for the
success model; test is not. Normalised per 1000 intents because n differs.

| metric | dev / 1k | test / 1k | change |
|---|---|---|---|
| incremental — AGENT | 689,185 | 579,867 | **−15.9%** |
| incremental — B2 | 542,340 | 499,840 | −7.8% |
| incremental — B2.5 | 559,514 | 521,879 | −6.7% |
| incremental — B3 oracle | 734,731 | 705,076 | −4.0% |
| incremental — B1 | 414,209 | 311,544 | −24.8% |
| net — AGENT | 259,697 | 213,169 | −17.9% |
| gross — AGENT | 1,240,387 | 1,158,429 | −6.6% |
| **AGENT vs B2 (paired)** | **146,845** | **80,027** | **−45.5%** |
| **AGENT vs B2.5 (paired)** | **129,671** | **57,988** | **−55.3%** |
| oracle share — AGENT | 93.8% | 82.2% | **−11.6pp** |
| oracle share — B2.5 | 76.2% | 74.0% | −2.1pp |
| Brier (held-out) | 0.1090 | 0.1007 | −0.0082 (better) |
| AGENT debits / 1k | 840 | 950 | +13.1% |
| AGENT contacts / 1k | 354 | 352 | −0.4% |
| NO_ACTION rate | 14.5% | 13.2% | −1.3pp |
| intents never acted on | 22.9% | 22.8% | −0.1pp |
| true NEVER_RETRY / 1k | 42.5 | 53.0 | +24.7% |
| circuit-breaker trips | 4 | 0 | — |

**The drop is the result, not an embarrassment.** Read it plainly:

- **The agent still wins on held-out data, significantly, against both baselines.**
- **Its margin over the baselines roughly halved.** Against B2.5 the paired advantage
  fell 55% and its CI now nearly touches zero. That is what a learned policy does
  when it meets merchants, customers, and an outage shape it was not fit on.
- **The baselines degraded less** (−7% vs −16%) because rules do not overfit. The
  agent's extra dev performance was partly cohort-specific.
- **The oracle degraded least** (−4%), so the gap that closed is the agent's, not the
  problem's. Oracle share falling 93.8% → 82.2% is the honest measure of
  generalisation loss.
- **Calibration held.** Brier *improved* slightly. The success model transferred; the
  policy's exploitation of it did not, fully.
- **Debits rose 13% per intent while contacts stayed flat** — on unfamiliar cells the
  agent retries more. This is also where the extra true violations come from (§5).

Anyone quoting the dev numbers as the headline is quoting an in-sample result.
**The numbers in §1 are the ones that ship.**

---

## 3. Per cause — where the agent wins and loses

AGENT vs B2.5, held-out. Losses first.

| cause | n | agent | B2.5 | delta |
|---|---|---|---|---|
| mandate_invalid | 260 | 47,643 | 85,695 | **−38,051** |
| upi_collect_expired | 107 | 26,764 | 45,787 | **−19,023** |
| limit_exceeded | 171 | 57,805 | 76,342 | **−18,537** |
| method_not_enabled | 32 | 45,980 | 58,185 | **−12,205** |
| invalid_card_details | 59 | 33,744 | 34,117 | −373 |
| lost_stolen_fraud | 34 | 0 | 0 | 0 |
| risk_decline | 79 | 4,661 | 2,516 | +2,144 |
| amount_exceeds_mandate | 94 | 127,923 | 122,912 | +5,011 |
| gateway_timeout | 241 | 175,548 | 167,078 | +8,470 |
| card_expired | 132 | 98,842 | 79,872 | +18,969 |
| auth_failed | 199 | 101,481 | 79,094 | +22,387 |
| do_not_honour | 348 | 137,138 | 102,754 | +34,384 |
| insufficient_funds | 827 | 405,391 | 346,091 | +59,301 |
| issuer_downtime | 417 | 476,682 | 365,194 | **+111,488** |

**It loses on four causes and we are not hiding them.**

- `mandate_invalid` (−38,051) is the **only loss that persists from dev**, where it
  was −95,708. B2 hard-codes a nudge for customer-fixable instruments; the agent has
  to learn the same thing from a sparse cell and does it worse. This is a known,
  reproducible weakness.
- `upi_collect_expired` and `limit_exceeded` **flipped from wins on dev to losses on
  test** — direct evidence of the generalisation loss in §2, localised.
- `issuer_downtime` (+111,488) is the largest single win and the clearest one: the
  agent times retries around outages it can infer from elapsed-time structure.

---

## 4. Calibration — held-out

| bin | n | predicted | observed |
|---|---|---|---|
| [0.0,0.1) | 1996 | 0.048 | 0.038 |
| [0.1,0.2) | 1475 | 0.151 | 0.121 |
| [0.2,0.3) | 601 | 0.254 | **0.180** |
| [0.3,0.4) | 474 | 0.323 | **0.234** |
| [0.4,0.5) | 67 | 0.460 | 0.493 |
| [0.5,0.6) | 93 | 0.536 | 0.495 |
| [0.6,0.7) | 31 | 0.630 | 0.516 |
| [0.7,0.8) | 258 | 0.761 | 0.795 |
| [0.8,0.9) | 55 | 0.834 | 0.800 |

**Brier 0.1007** (0 = perfect, 0.25 = always guessing 0.5). Better than dev's 0.1090.

The model is **systematically overconfident in the 0.2–0.4 band** — predicted 0.254
against an observed 0.180, and 0.323 against 0.234. That band carries 1075 attempts,
so it is not noise. It means the EV policy overvalues mid-probability actions on
unfamiliar cells, which is a coherent explanation for the +13% debit rate in §2.
Reported, not corrected: correcting it after seeing this number would make the run
unsealed.

---

## 5. Guardrails

### NEVER_RETRY violations by signal-quality tier

| tier | intents | true violations | observable violations |
|---|---|---|---|
| generic | 291 | 87 | **0** |
| unmapped | 178 | 62 | **0** |
| misleading | 135 | 10 | **0** |
| faithful | 2170 | **0** | **0** |
| reason_collides | 226 | **0** | **0** |

**Observable violations are 0 in every tier.** That is the number the gate is
accountable for: a re-debit against a cause it could see was terminal.

The table above is the **AGENT arm** — it is built from `runs["AGENT"]`, so it says
nothing about the other arms. Across arms, the gate is precisely what produces that
zero: **B1, which has no eligibility gate, commits 1674 observable violations.** B2,
B2.5, AGENT and B3 each commit **0**. That gap is the gate's contribution, measured.

**True violations are 159 and cannot be zero.** Every one sits in a tier where the
gateway hid the cause — a generic `payment_failed`, a bank-specific code outside the
taxonomy, or a terminal instrument wearing a soft decline. The `faithful` tier, which
is 72% of the cohort, has **zero**. An eligibility gate can enforce what the signal
reveals and nothing more; this table is the evidence, not an excuse.

### Unauthorised debits — re-debits with no standing mandate

| arm | count |
|---|---|
| B1 blind ladder | **1391** |
| B0, B2, B2.5, **AGENT**, B3 | **0** |

B1 has no regime check. Every other arm, including the agent, never once attempted to
debit a one-time checkout payment.

### Circuit breaker

**Zero trips on test** (four on dev). No cause-cell fell below the 1.5% floor over a
60-attempt window.

---

## 6. The outage rule did not generalise

B2.5 beat B2 by 103,048 on dev, CI [31,327, 186,427] — significant. On test:
**66,117, CI [−1,057, 143,017] — not significant.**

The test cohort has a different outage pattern from dev by construction. A naive
"N failures on one issuer → back off" rule tuned on one outage shape does not
transfer. The agent's `issuer_downtime` win (+111,488) held; the hand-written rule's
did not. That contrast is the argument for a learned timing policy over a threshold.

---

## 7. NO_ACTION — 13.2% of decisions, 22.8% of intents never touched

11,921 decisions taken; 1,574 ended in NO_ACTION.

| reason code | binding constraint | count |
|---|---|---|
| BLOCKED_BY_RULE | R001_no_mandate_no_debit | 337 |
| BLOCKED_BY_RULE | R002_terminal_cause_no_debit | 307 |
| CAPACITY_AUCTION_LOST | R011_capacity_auction_lost | 262 |
| BLOCKED_BY_RULE | R003_max_debits | 231 |
| BLOCKED_BY_RULE | R009_merchant_alert_once | 155 |
| NEGATIVE_EV_TERMINAL | EV_UPPER_BOUND_NOT_POSITIVE | 152 |
| BLOCKED_BY_RULE | R008_escalation_value_floor | 72 |
| BLOCKED_BY_RULE | R006_no_alternate_instrument | 37 |
| BLOCKED_BY_RULE | R004_max_nudges | 21 |

Three economically distinct reasons for doing nothing, never merged: **a rule removed
the option**, **someone else's payment outbid this one** (262), or **nothing available
cleared its own cost** (152).

Binding-constraint frequency across all 11,921 decisions — the auction is the single
most common constraint at 2041, ahead of the terminal-cause rule at 1274 and the
mandate rule at 1224. Escalation: 2183 claims, 142 granted, 2041 lost, budget 142/150.

`NEGATIVE_EV_TERMINAL` firing only 152 times shows the upper-bound stopping rule is
**conservative by design** — wide credible intervals on sparse cells keep options
alive, and the guardrails do most of the stopping.

---

## 8. What a judge should take away

1. **The agent beats a competent rules baseline on held-out data, and we can say by
   how much with a confidence interval.** +240,082 vs B2, +173,965 vs B2.5.
2. **It does so while acting less** — 41% fewer contacts than B2, and it declines to
   act on 22.8% of intents entirely.
3. **We report incremental, not gross.** Gross recovery on test was 3,475,288;
   incremental was 1,739,601. The 1.7M gap is what would have recovered anyway and we
   do not claim it.
4. **The margin halved out of sample and we lead with that**, alongside the four
   causes where the agent loses.
5. **The guardrails are absolute where they can be.** Zero observable terminal
   retries and zero unauthorised debits for every **gated** arm, in every tier.
   B1, which has no gate, has 1674 and 1391 — that gap is what the gate buys.
6. **Two components were built, measured, and shipped OFF** — feature_cross_v2 and the
   LLM tail normaliser both lost their ablations. Those are in
   [shipping-config.md](shipping-config.md).

## Known limitations

Carried from [shipping-config.md](shipping-config.md) §Known limitations, all of which
constrain how these numbers should be read: the greedy oracle is a lower bound;
`ESCALATE_HUMAN` is likely over-powered in the frozen response model; unmapped-code
descriptions are independent of the true cause by construction; the adapter execution
seam is unwired pending M7.
