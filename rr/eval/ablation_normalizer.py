"""LLM tail normaliser, on vs off. Dev cohort only.

Both arms are the identical agent, identical cohort, identical common random
numbers. The only difference is whether the eligibility gate is handed a resolved
cause or an UNKNOWN for the ~20% of the corpus the deterministic map cannot read.

A documented limitation shapes what this can show. In the frozen cohort generator,
`_degrade_signal` picks an entry from UNMAPPED_VIEWS keyed on the intent id, NOT on
the true cause -- so on that slice the description is statistically independent of
the answer and chance is the ceiling for any classifier. The generic view's
description is the literal string "Payment failed", which carries no signal either,
and there abstention is the correct behaviour. The real headroom is a third slice:
DO_NOT_HONOUR intents, whose FAITHFUL description names the cause outright while
their `reason` field is the generic `payment_failed`. The report separates all three.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

from rr.agent.features import IssuerFailureIndex
from rr.agent.policy import EVPolicy
from rr.config import COSTS, POLICY
from rr.contracts import AttemptOutcome
from rr.eval.agent_run import run_agent
from rr.eval.metrics import delta_vector, make_resamples, paired_gap_ci, rupees, summarize
from rr.model.beta_binomial import BetaBinomialModel
from rr.normalize.llm_tail import TailNormalizer
from rr.normalize.resolvers import build_resolver
from rr.pipeline.normalize import diagnose
from rr.sim.cohort import UNMAPPED_VIEWS, load_latent, load_observed
from rr.sim.latent import LatentState
from rr.taxonomy import DEBIT_ACTIONS, FailureCause, NEVER_RETRY, normalize_reason

UNMAPPED_CODES = {v[0] for v in UNMAPPED_VIEWS}


def slice_of(o: dict, l: LatentState) -> str:
    """Which tail bucket an intent falls in, if any."""
    if normalize_reason(o["gateway_reason"]) is not FailureCause.UNKNOWN:
        return "mapped"
    if o["gateway_code"] in UNMAPPED_CODES:
        return "unmapped"          # description independent of cause by construction
    if o["gateway_source"] == "NA":
        return "generic"           # GENERIC_VIEW: description is "Payment failed"
    return "dnh_generic_reason"    # faithful description, generic `reason` field


def chargeback_charge_minor(results, diag_by_id: dict) -> int:
    """What the EV policy priced into its debits for terminal-retry risk.

    Not a realised cost -- it is the configured prior, which is exactly why
    resolving a cause out of the UNKNOWN tail is worth money: it reprices every
    subsequent debit on that intent from the conservative rate to the soft rate."""
    total = 0
    for r in results:
        cause = diag_by_id[r.intent_id]
        rate = (POLICY.p_chargeback_unknown_cause if cause is FailureCause.UNKNOWN
                else POLICY.p_chargeback_known_soft)
        debits = sum(1 for rec in r.records if rec.action_type in DEBIT_ACTIONS)
        total += int(debits * rate * COSTS.terminal_retry_penalty_minor)
    return total


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--models", type=pathlib.Path, default=pathlib.Path("models"))
    ap.add_argument("--resolver", default="offline", choices=("offline", "anthropic", "openai"))
    ap.add_argument("--cohort", default="dev")
    args = ap.parse_args()
    if args.cohort != "dev":
        print("REFUSED: ablations run on dev. The test cohort is read once, in M6.",
              file=sys.stderr)
        sys.exit(2)

    obs = load_observed(args.data / "dev_observed.jsonl")
    lat = [LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})
           for d in load_latent(args.data / "dev_latent.jsonl")]
    n = len(obs)
    idx = make_resamples(n, reps=2000)
    issuer_index = IssuerFailureIndex.build(obs)
    success = BetaBinomialModel.load(args.models / "success_model_v1.json")
    organic = BetaBinomialModel.load(args.models / "organic_model_v2.json")

    def fresh_policy():
        return EVPolicy(success, organic, issuer_index=issuer_index)

    tail = TailNormalizer(build_resolver(args.resolver),
                          cache_path=args.models / "normalizer_cache.json")

    print(f"\n=== tail normaliser ablation -- dev, n={n} ===\n")
    if args.resolver == "offline":
        print("  RESOLVER: offline keyword matcher. NOT AN LLM. It exercises the")
        print("  enum constraint, confidence floor, cache and audit log, and estimates")
        print("  an upper bound for a model on these few unambiguous strings.")
        print("  Set ANTHROPIC_API_KEY and pass --resolver anthropic for live numbers.\n")

    off, _ = run_agent(obs, lat, fresh_policy(), normalizer=None)
    on, _ = run_agent(obs, lat, fresh_policy(), normalizer=tail)
    tail.save_cache()
    tail.write_call_log(args.models / "llm_calls.jsonl")

    diag_off = {o["intent_id"]: diagnose(o, None).failure_cause for o in obs}
    diag_on = {o["intent_id"]: diagnose(o, tail).failure_cause for o in obs}

    # ------------------------------------------------------------- headline --
    s_off, s_on = summarize("off", off, idx), summarize("on", on, idx)
    d, lo, hi = paired_gap_ci(on, off, idx)
    verdict = "BEATS" if lo > 0 else ("LOSES TO" if hi < 0 else "ties with")
    hdr = f"  {'':<6}{'incremental':>13}{'net':>13}{'debits':>8}{'contacts':>9}"
    print("=== HEADLINE ===\n")
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for label, s in (("off", s_off), ("on", s_on)):
        print(f"  {label:<6}{rupees(s.incremental_minor):>13}{rupees(s.net_minor):>13}"
              f"{s.debits:>8}{s.contacts:>9}")
    print(f"\n  normaliser ON {verdict} OFF: INR {rupees(d)}  95% CI [{rupees(lo)}, {rupees(hi)}]")

    # -------------------------------------------------- the recoverable slice --
    dnh = [o for o, l in zip(obs, lat) if slice_of(o, l) == "dnh_generic_reason"]
    resolved = sum(1 for o in dnh if diag_on[o["intent_id"]] is FailureCause.DO_NOT_HONOUR)
    print(f"\n=== the slice with actual signal ===\n")
    print(f"  do_not_honour carrying the generic `payment_failed` reason : {len(dnh)}")
    print(f"  resolved correctly by the normaliser                       : {resolved}"
          f"  ({resolved / max(len(dnh), 1):.1%})")

    # ----------------------------------------------------- chargeback pricing --
    cb_off = chargeback_charge_minor(off, diag_off)
    cb_on = chargeback_charge_minor(on, diag_on)
    print(f"\n=== chargeback cost priced into EV (configured prior, not realised) ===\n")
    print(f"  off INR {rupees(cb_off):>10}   on INR {rupees(cb_on):>10}   "
          f"delta INR {rupees(cb_on - cb_off):>10}")
    print(f"  Resolved causes price at {POLICY.p_chargeback_known_soft:.3f} instead of the")
    print(f"  UNKNOWN-tail rate {POLICY.p_chargeback_unknown_cause:.3f}.")

    # -------------------------------------------------- abstention behaviour --
    print(f"\n=== abstention: correct UNKNOWN vs confident-wrong, by slice ===\n")
    print(f"  {'slice':<22}{'n':>6}{'abstained':>11}{'correct':>9}{'wrong':>7}{'acc':>7}")
    buckets = collections.defaultdict(list)
    for o, l in zip(obs, lat):
        s = slice_of(o, l)
        if s != "mapped":
            buckets[s].append((o, l))
    for name in ("dnh_generic_reason", "generic", "unmapped"):
        rows = buckets.get(name, [])
        if not rows:
            continue
        abst = corr = wrong = 0
        for o, l in rows:
            got = diag_on[o["intent_id"]]
            if got is FailureCause.UNKNOWN:
                abst += 1
            elif got is l.true_cause:
                corr += 1
            else:
                wrong += 1
        acc = corr / max(len(rows), 1)
        print(f"  {name:<22}{len(rows):>6}{abst:>11}{corr:>9}{wrong:>7}{acc:>7.1%}")
    print("\n  generic  : description is \"Payment failed\". Abstention is the correct")
    print("             answer; a high abstention count here is a pass, not a miss.")
    print("  unmapped : description is drawn independently of the true cause in the")
    print("             frozen generator, so ~1/15 is the ceiling. Confident-wrong")
    print("             here is a property of the corpus, not of the resolver.")

    # ------------------------------------------------------- confusion matrix --
    print(f"\n=== diagnosis vs latent true cause (tail slices only, normaliser ON) ===\n")
    cm = collections.Counter()
    for o, l in zip(obs, lat):
        if slice_of(o, l) == "mapped":
            continue
        cm[(l.true_cause.value, diag_on[o["intent_id"]].value)] += 1
    print(f"  {'true cause':<24}{'diagnosed':<24}{'n':>6}")
    for (true_c, got_c), cnt in sorted(cm.items(), key=lambda kv: -kv[1])[:14]:
        mark = "  <- correct" if true_c == got_c else ""
        print(f"  {true_c:<24}{got_c:<24}{cnt:>6}{mark}")

    # --------------------------------------------------------------- cost --
    print(f"\n=== LLM cost ===\n")
    print(f"  distinct gateway responses (cache keys) : {tail.distinct_inputs}")
    print(f"  live calls                              : {tail.live_calls}")
    print(f"  total cost                              : ${tail.total_cost_usd:.4f}")
    print(f"  cost per 1000 intents                   : "
          f"${tail.total_cost_usd / max(n, 1) * 1000:.4f}")
    print("  The synthetic corpus holds only a handful of distinct descriptions, so")
    print("  the cache collapses 6000 intents to single-digit calls. That is an")
    print("  artefact of the corpus, not a cost estimate for production.")

    # ------------------------------------------------------------- secondary --
    print(f"\n=== secondary: NEVER_RETRY violations ===\n")
    print(f"  true-cause violations   off {s_off.never_retry_true:>5}   "
          f"on {s_on.never_retry_true:>5}")
    print(f"  gate-visible violations off {s_off.never_retry_observable:>5}   "
          f"on {s_on.never_retry_observable:>5}   (must be 0 in both)")
    print("\n  Expect the true count to be roughly unchanged, and say so plainly: every")
    print("  one of those violations sits on a generic or unmapped view whose")
    print("  description cannot identify the terminal cause. They are irreducible")
    print("  given the signal available, not a normaliser failure.\n")


if __name__ == "__main__":
    main()
