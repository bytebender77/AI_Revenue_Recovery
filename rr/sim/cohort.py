"""Cohort generator.

Emits N payment intents over a compressed 30-day clock, split into a dev cohort
(sim days 1-20) and a SEALED test cohort (days 21-30) with disjoint merchants and
customers. Two files per cohort:

    <name>_observed.jsonl   what the agent may read
    <name>_latent.jsonl     ground truth, simulator + eval only

The split is the boundary. Agent code opens the observed file and has no path to
the other one.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import pathlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from rr import rng
from rr.config import CLOCK, CohortConfig
from rr.contracts import ObservedFailureEvent
from rr.sim.latent import LatentState
from rr.sim.response_model import CAUSE_PARAMS, RESPONSE_MODEL_VERSION
from rr.taxonomy import Channel, FailureCause, Method, Regime

ISSUERS = [f"bank_{i:02d}" for i in range(8)]

METHOD_MIX = {
    Regime.MERCHANT_INITIATED: (
        [Method.UPI_AUTOPAY, Method.CARD_MANDATE, Method.EMANDATE_NACH], [0.50, 0.35, 0.15]),
    Regime.CUSTOMER_INITIATED: (
        [Method.UPI, Method.CARD, Method.NETBANKING, Method.WALLET], [0.60, 0.25, 0.10, 0.05]),
}

C = FailureCause
CAUSE_PRIORS: dict[Method, dict[FailureCause, float]] = {
    Method.UPI_AUTOPAY: {
        C.INSUFFICIENT_FUNDS: .30, C.MANDATE_INVALID: .12, C.ISSUER_DOWNTIME: .12,
        C.DO_NOT_HONOUR: .10, C.GATEWAY_TIMEOUT: .08, C.LIMIT_EXCEEDED: .08,
        C.AUTH_FAILED: .06, C.AMOUNT_EXCEEDS_MANDATE: .05, C.RISK_DECLINE: .04,
        C.METHOD_NOT_ENABLED: .02, C.LOST_STOLEN_FRAUD: .01, C.UPI_COLLECT_EXPIRED: .02,
    },
    Method.CARD_MANDATE: {
        C.INSUFFICIENT_FUNDS: .24, C.DO_NOT_HONOUR: .16, C.CARD_EXPIRED: .14,
        C.ISSUER_DOWNTIME: .10, C.MANDATE_INVALID: .08, C.GATEWAY_TIMEOUT: .07,
        C.INVALID_CARD_DETAILS: .06, C.LIMIT_EXCEEDED: .05, C.RISK_DECLINE: .04,
        C.LOST_STOLEN_FRAUD: .03, C.AMOUNT_EXCEEDS_MANDATE: .03,
    },
    Method.EMANDATE_NACH: {
        C.INSUFFICIENT_FUNDS: .45, C.MANDATE_INVALID: .15, C.ISSUER_DOWNTIME: .12,
        C.DO_NOT_HONOUR: .10, C.GATEWAY_TIMEOUT: .08, C.LIMIT_EXCEEDED: .06,
        C.AMOUNT_EXCEEDS_MANDATE: .04,
    },
    Method.UPI: {
        C.UPI_COLLECT_EXPIRED: .30, C.AUTH_FAILED: .20, C.INSUFFICIENT_FUNDS: .18,
        C.ISSUER_DOWNTIME: .10, C.GATEWAY_TIMEOUT: .10, C.LIMIT_EXCEEDED: .07,
        C.DO_NOT_HONOUR: .05,
    },
    Method.CARD: {
        C.AUTH_FAILED: .22, C.INSUFFICIENT_FUNDS: .18, C.DO_NOT_HONOUR: .16,
        C.CARD_EXPIRED: .12, C.INVALID_CARD_DETAILS: .12, C.GATEWAY_TIMEOUT: .08,
        C.ISSUER_DOWNTIME: .06, C.RISK_DECLINE: .04, C.LOST_STOLEN_FRAUD: .02,
    },
    Method.NETBANKING: {
        C.AUTH_FAILED: .30, C.ISSUER_DOWNTIME: .25, C.GATEWAY_TIMEOUT: .20,
        C.INSUFFICIENT_FUNDS: .20, C.LIMIT_EXCEEDED: .05,
    },
    Method.WALLET: {
        C.INSUFFICIENT_FUNDS: .40, C.AUTH_FAILED: .25, C.GATEWAY_TIMEOUT: .20,
        C.ISSUER_DOWNTIME: .15,
    },
}

# Causes for which a re-debit genuinely can never succeed.
TERMINAL_TRUTH_CAUSES = frozenset({
    C.LOST_STOLEN_FRAUD, C.RISK_DECLINE, C.MANDATE_INVALID,
    C.AMOUNT_EXCEEDS_MANDATE, C.METHOD_NOT_ENABLED,
})

# Observed gateway fields per cause. Shape mirrors what a PSP typically exposes
# (code / reason / source / step / description).
# TODO(citation): verify these against Razorpay's published error-code reference.
# The *values* here are placeholders; only the SHAPE is claimed to be realistic.
GATEWAY_VIEW: dict[FailureCause, tuple[str, str, str, str, str]] = {
    C.INSUFFICIENT_FUNDS:     ("BAD_REQUEST_ERROR", "insufficient_funds", "customer", "payment_authorization", "Your account does not have sufficient balance"),
    C.ISSUER_DOWNTIME:        ("GATEWAY_ERROR", "issuer_down", "bank", "payment_authorization", "Issuing bank is temporarily unavailable"),
    C.GATEWAY_TIMEOUT:        ("GATEWAY_ERROR", "gateway_timeout", "gateway", "payment_authorization", "Upstream did not respond in time"),
    C.UPI_COLLECT_EXPIRED:    ("BAD_REQUEST_ERROR", "payment_timeout", "customer", "payment_authentication", "Collect request expired before approval"),
    C.AUTH_FAILED:            ("BAD_REQUEST_ERROR", "payment_authentication_failed", "customer", "payment_authentication", "Authentication was not completed"),
    C.DO_NOT_HONOUR:          ("BAD_REQUEST_ERROR", "payment_failed", "bank", "payment_authorization", "Declined by issuing bank (do not honour)"),
    C.CARD_EXPIRED:           ("BAD_REQUEST_ERROR", "card_expired", "customer", "payment_authorization", "The card has expired"),
    C.INVALID_CARD_DETAILS:   ("BAD_REQUEST_ERROR", "invalid_card_details", "customer", "payment_initiation", "Card details could not be validated"),
    C.LOST_STOLEN_FRAUD:      ("BAD_REQUEST_ERROR", "card_blocked", "bank", "payment_authorization", "Card reported lost or stolen"),
    C.MANDATE_INVALID:        ("BAD_REQUEST_ERROR", "mandate_revoked", "customer", "payment_authorization", "Mandate is no longer active"),
    C.AMOUNT_EXCEEDS_MANDATE: ("BAD_REQUEST_ERROR", "amount_exceeds_mandate", "business", "payment_initiation", "Debit amount exceeds registered mandate limit"),
    C.LIMIT_EXCEEDED:         ("BAD_REQUEST_ERROR", "payment_limit_exceeded", "customer", "payment_authorization", "Transaction limit exceeded"),
    C.RISK_DECLINE:           ("BAD_REQUEST_ERROR", "payment_declined_risk", "gateway", "payment_authorization", "Blocked by risk evaluation"),
    C.METHOD_NOT_ENABLED:     ("BAD_REQUEST_ERROR", "method_not_enabled", "business", "payment_initiation", "Payment method not enabled for this account"),
}

GENERIC_VIEW = ("BAD_REQUEST_ERROR", "payment_failed", "NA", "payment_authorization", "Payment failed")

# Bank-specific codes absent from the taxonomy. The free text still carries a hint,
# which is what makes the M5 LLM normaliser load-bearing rather than decorative.
UNMAPPED_VIEWS = [
    ("BANK_ERR_7731", "u01", "bank", "payment_authorization", "TXN DECLINED - AVAIL BAL LOW, PLS RETRY"),
    ("NPCI_XR", "xr", "bank", "payment_authorization", "remitter bank offline, try after some time"),
    ("ISS_9042", "iss_9042", "bank", "payment_authorization", "card acct closed by issuer - do not resubmit"),
    ("SW_TIMEOUT_2", "sw_to", "gateway", "payment_authorization", "switch timeout, no response from acquirer"),
    ("MND_ERR_31", "mnd31", "customer", "payment_authorization", "umrn not found / cancelled at bank"),
]

AMOUNT_BANDS = [(9_900, 49_900), (49_900, 299_900), (299_900, 1_999_900)]
BILLING_ANCHOR_DAYS = [1, 2, 5, 7, 10, 15, 20, 25, 28]


@dataclass
class Intent:
    observed: ObservedFailureEvent
    latent: LatentState


# ------------------------------------------------------------------ helpers --

def _customer_profile(seed: int, cid: str) -> dict:
    channels = {}
    for ch, base in ((Channel.SMS, .55), (Channel.WHATSAPP, .68), (Channel.EMAIL, .38), (Channel.IN_APP, .60)):
        channels[ch.value] = rng.beta_like(base, 12.0, seed, cid, "resp", ch.value)
    consented = tuple(
        ch for ch, p in ((Channel.EMAIL, .90), (Channel.SMS, .80), (Channel.WHATSAPP, .55), (Channel.IN_APP, .40))
        if rng.bernoulli(p, seed, cid, "consent", ch.value)
    ) or (Channel.EMAIL,)
    return {
        "salary_day": rng.choice([1, 2, 5, 7, 25, 28], [.28, .12, .12, .13, .20, .15], seed, cid, "salary"),
        "capacity": rng.choice([2_000_000, 5_000_000, 12_000_000], [.45, .40, .15], seed, cid, "cap"),
        "intent_to_pay": rng.beta_like(0.72, 9.0, seed, cid, "intent"),
        "channel_responsiveness": channels,
        "consented_channels": consented,
        "has_alternate_instrument": rng.bernoulli(0.45, seed, cid, "alt"),
    }


def _draw_failure_time(seed: int, iid: str, regime: Regime, lo: float, hi: float) -> float:
    if regime is Regime.MERCHANT_INITIATED:
        # Auto-debits fire on billing anniversaries, in a batch window.
        day0 = int(lo // 24)
        span_days = max(int((hi - lo) // 24), 1)
        day = day0 + rng.choice(
            [d for d in range(span_days)],
            [3.0 if ((day0 + d) % 30 + 1) in BILLING_ANCHOR_DAYS else 1.0 for d in range(span_days)],
            seed, iid, "day",
        )
        hour = 2.0 + rng.u01(seed, iid, "hr") * 6.0   # overnight batch
    else:
        day = int(lo // 24) + int(rng.u01(seed, iid, "day") * max((hi - lo) // 24, 1))
        hour = 9.0 + rng.u01(seed, iid, "hr") * 13.0  # diurnal
    return min(max(day * 24.0 + hour, lo), hi - 1e-6)


def _self_heal_at(seed: int, iid: str, cause: FailureCause, failed_at: float, terminal: bool) -> Optional[float]:
    """Drawn once, at generation, independent of any policy. This is the counterfactual."""
    p = CAUSE_PARAMS[cause]
    rate = 0.005 if terminal else p.self_heal_rate
    if not rng.bernoulli(rate, seed, iid, "selfheal_hit"):
        return None
    offset = rng.exponential(p.self_heal_median_h / math.log(2), seed, iid, "selfheal_t")
    return failed_at + offset if offset <= CLOCK.recovery_horizon_hours else None


def _degrade_signal(seed: int, iid: str, cause: FailureCause, cfg: CohortConfig, masquerade: bool):
    if masquerade:
        return GATEWAY_VIEW[C.DO_NOT_HONOUR]
    u = rng.u01(seed, iid, "signal")
    if u < cfg.p_generic_code:
        return GENERIC_VIEW
    if u < cfg.p_generic_code + cfg.p_unmapped_code:
        return UNMAPPED_VIEWS[rng.randint(0, len(UNMAPPED_VIEWS) - 1, seed, iid, "unmapped")]
    return GATEWAY_VIEW[cause]


# ---------------------------------------------------------------- generator --

def generate_cohort(name: str, cfg: CohortConfig, n: int, n_merchants: int,
                    window: tuple[int, int], id_prefix: str, adv_scale: float = 1.0) -> list[Intent]:
    seed = cfg.seed
    lo, hi = float(window[0]), float(window[1])
    merchants = [f"{id_prefix}_merch_{i:03d}" for i in range(n_merchants)]

    outages = []
    n_out = cfg.n_outage_windows_dev if name == "dev" else cfg.n_outage_windows_test
    for k in range(n_out):
        issuer = ISSUERS[rng.randint(0, len(ISSUERS) - 1, seed, name, "outage_iss", k)]
        start = lo + rng.u01(seed, name, "outage_st", k) * (hi - lo - 24)
        dur = rng.randint(*cfg.outage_duration_hours, seed, name, "outage_dur", k)
        outages.append((issuer, start, start + dur))

    intents: list[Intent] = []
    for i in range(n):
        iid = f"{id_prefix}_pi_{i:06d}"
        merchant = merchants[rng.randint(0, len(merchants) - 1, seed, iid, "merch")]
        cust = f"{id_prefix}_cust_{rng.randint(0, cfg.customers_per_merchant - 1, seed, iid, 'cust'):04d}_{merchant[-3:]}"
        prof = _customer_profile(seed, cust)

        regime = rng.choice(list(Regime), list(cfg.regime_mix), seed, iid, "regime")
        methods, mweights = METHOD_MIX[regime]
        method = rng.choice(methods, mweights, seed, iid, "method")
        issuer = ISSUERS[rng.randint(0, len(ISSUERS) - 1, seed, iid, "issuer")]

        band = AMOUNT_BANDS[rng.choice([0, 1, 2], [.45, .40, .15], seed, iid, "band")]
        amount = int(band[0] + rng.u01(seed, iid, "amt") * (band[1] - band[0]))
        failed_at = _draw_failure_time(seed, iid, regime, lo, hi)

        priors = CAUSE_PRIORS[method]
        cause = rng.choice(list(priors.keys()), list(priors.values()), seed, iid, "cause")

        outage_start = outage_end = None
        for (oi, ost, oend) in outages:
            if oi == issuer and ost <= failed_at < oend:
                cause, outage_start, outage_end = C.ISSUER_DOWNTIME, ost, oend
                break
        if cause is C.ISSUER_DOWNTIME and outage_start is None:
            outage_start = failed_at - rng.u01(seed, iid, "os") * 1.0
            outage_end = outage_start + rng.exponential(2.5, seed, iid, "oe") + 0.5

        masquerade = rng.bernoulli(cfg.p_masquerade, seed, iid, "masq") and cause not in TERMINAL_TRUTH_CAUSES
        terminal_truth = cause in TERMINAL_TRUTH_CAUSES or masquerade

        code, reason, source, step, desc = _degrade_signal(seed, iid, cause, cfg, masquerade)
        tolerance = (
            rng.choice([0, 0, 1, 2, 3], [.22, .18, .25, .20, .15], seed, iid, "tol")
            if cause is C.DO_NOT_HONOUR
            else rng.choice([1, 2, 3, 4], [.15, .30, .35, .20], seed, iid, "tol")
        )

        observed = ObservedFailureEvent(
            intent_id=iid, merchant_id=merchant, customer_id=cust, issuer_id=issuer,
            amount_minor=amount, currency="INR", method=method, regime=regime,
            mandate_id=(f"mnd_{cust}" if regime is Regime.MERCHANT_INITIATED else None),
            instrument_ref=f"tok_{cust}_{method.value}",
            has_alternate_instrument=prof["has_alternate_instrument"],
            consented_channels=prof["consented_channels"],
            failed_at_h=round(failed_at, 4), attempt_index=0,
            gateway_code=code, gateway_reason=reason, gateway_source=source,
            gateway_step=step, gateway_description=desc,
            customer_prior_failures_30d=0, merchant_prior_failures_30d=0,
        )
        latent = LatentState(
            true_cause=cause, terminal_truth=terminal_truth,
            intent_to_pay=prof["intent_to_pay"], salary_day=prof["salary_day"],
            monthly_capacity_minor=prof["capacity"],
            channel_responsiveness=prof["channel_responsiveness"],
            issuer_retry_tolerance=tolerance,
            outage_start_h=outage_start, outage_end_h=outage_end,
            self_heal_at_h=_self_heal_at(seed, iid, cause, failed_at, terminal_truth),
            slice_tag="masquerade_natural" if masquerade else "base",
        )
        intents.append(Intent(observed, latent))

    _inject_adversarial(intents, cfg, seed, name, adv_scale)
    _backfill_prior_counts(intents)
    return intents


def _inject_adversarial(intents: list[Intent], cfg: CohortConfig, seed: int, name: str, scale: float) -> None:
    """Three slices engineered to punish policies that act reflexively."""
    n = len(intents)
    cursor = 0

    def take(count: int, predicate) -> list[int]:
        nonlocal cursor
        picked = []
        while cursor < n and len(picked) < count:
            if predicate(intents[cursor]):
                picked.append(cursor)
            cursor += 1
        return picked

    # 1. Pays organically ~20 minutes later. Any action here is pure cost.
    for idx in take(int(cfg.n_adv_fast_organic * scale),
                    lambda it: not it.latent.terminal_truth):
        it = intents[idx]
        intents[idx].latent = dataclasses.replace(
            it.latent, self_heal_at_h=it.observed.failed_at_h + 0.33, slice_tag="adv_fast_organic")

    # 2. Terminal truth wearing a soft do_not_honour code. Retries can never work.
    for idx in take(int(cfg.n_adv_soft_mask_terminal * scale),
                    lambda it: it.latent.true_cause is C.DO_NOT_HONOUR and not it.latent.terminal_truth):
        code, reason, source, step, desc = GATEWAY_VIEW[C.DO_NOT_HONOUR]
        intents[idx].observed = dataclasses.replace(
            intents[idx].observed, gateway_code=code, gateway_reason=reason,
            gateway_source=source, gateway_step=step, gateway_description=desc)
        intents[idx].latent = dataclasses.replace(
            intents[idx].latent, terminal_truth=True, issuer_retry_tolerance=0,
            self_heal_at_h=None, slice_tag="adv_soft_mask_terminal")

    # 3. Outage that resolves 30h in: retry before is futile, after is near-certain.
    for idx in take(int(cfg.n_adv_outage_midwindow * scale), lambda it: not it.latent.terminal_truth):
        f = intents[idx].observed.failed_at_h
        code, reason, source, step, desc = GATEWAY_VIEW[C.ISSUER_DOWNTIME]
        intents[idx].observed = dataclasses.replace(
            intents[idx].observed, gateway_code=code, gateway_reason=reason,
            gateway_source=source, gateway_step=step, gateway_description=desc)
        intents[idx].latent = dataclasses.replace(
            intents[idx].latent, true_cause=C.ISSUER_DOWNTIME, terminal_truth=False,
            outage_start_h=f - 0.5, outage_end_h=f + 30.0,
            self_heal_at_h=None, slice_tag="adv_outage_midwindow")


def _backfill_prior_counts(intents: list[Intent]) -> None:
    by_cust: dict[str, int] = {}
    by_merch: dict[str, int] = {}
    for it in sorted(intents, key=lambda x: x.observed.failed_at_h):
        c, m = it.observed.customer_id, it.observed.merchant_id
        it.observed = dataclasses.replace(
            it.observed,
            customer_prior_failures_30d=by_cust.get(c, 0),
            merchant_prior_failures_30d=by_merch.get(m, 0),
        )
        by_cust[c] = by_cust.get(c, 0) + 1
        by_merch[m] = by_merch.get(m, 0) + 1


# ---------------------------------------------------------------------- io --

def _enc(v):
    if hasattr(v, "value"):
        return v.value
    if isinstance(v, (tuple, list)):
        return [_enc(x) for x in v]
    if isinstance(v, dict):
        return {k: _enc(x) for k, x in v.items()}
    return v


def _write(path: pathlib.Path, rows: list) -> str:
    h = hashlib.sha256()
    with path.open("w") as fh:
        for r in rows:
            line = json.dumps({k: _enc(v) for k, v in dataclasses.asdict(r).items()}, sort_keys=True)
            fh.write(line + "\n")
            h.update(line.encode())
    return h.hexdigest()


def write_cohort(out: pathlib.Path, name: str, intents: list[Intent]) -> dict:
    obs_hash = _write(out / f"{name}_observed.jsonl", [i.observed for i in intents])
    lat_hash = _write(out / f"{name}_latent.jsonl", [i.latent for i in intents])
    return {"n": len(intents), "observed_sha256": obs_hash, "latent_sha256": lat_hash}


def load_observed(path: pathlib.Path) -> list[dict]:
    """The ONLY loader agent code may call."""
    return [json.loads(l) for l in path.read_text().splitlines() if l]


def load_latent(path: pathlib.Path) -> list[dict]:
    """Simulator and eval harness only. Never import this from rr/agent/."""
    return [json.loads(l) for l in path.read_text().splitlines() if l]


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate dev + sealed test cohorts.")
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--seed", type=int, default=CohortConfig.seed)
    ap.add_argument("--n-dev", type=int, default=CohortConfig.n_dev)
    ap.add_argument("--n-test", type=int, default=CohortConfig.n_test)
    args = ap.parse_args()

    cfg = dataclasses.replace(CohortConfig(), seed=args.seed, n_dev=args.n_dev, n_test=args.n_test)
    args.out.mkdir(parents=True, exist_ok=True)

    dev = generate_cohort("dev", cfg, cfg.n_dev, cfg.n_merchants_dev, CLOCK.dev_hours, "dev", 1.0)
    test = generate_cohort("test", cfg, cfg.n_test, cfg.n_merchants_test, CLOCK.test_hours, "test", 0.6)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": cfg.seed,
        "response_model_version": RESPONSE_MODEL_VERSION,
        "clock": dataclasses.asdict(CLOCK),
        "config": dataclasses.asdict(cfg),
        "dev": write_cohort(args.out, "dev", dev),
        "test": write_cohort(args.out, "test", test),
    }
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    (args.out / "test_cohort.SEALED.json").write_text(json.dumps({
        "sealed_at": manifest["generated_at"],
        "seed": cfg.seed,
        "test": manifest["test"],
        "rule": "The test cohort is read exactly once, at the end, by the final eval "
                "run. Any tuning decision taken after reading it invalidates the "
                "reported numbers. Hashes above detect regeneration.",
    }, indent=2))

    dm = set(i.observed.merchant_id for i in dev)
    tm = set(i.observed.merchant_id for i in test)
    dc = set(i.observed.customer_id for i in dev)
    tc = set(i.observed.customer_id for i in test)
    assert not (dm & tm), "merchant leakage between dev and test"
    assert not (dc & tc), "customer leakage between dev and test"
    print(json.dumps({k: manifest[k] for k in ("seed", "response_model_version", "dev", "test")}, indent=2))


if __name__ == "__main__":
    main()
