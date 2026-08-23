# Synthetic reasons vs Razorpay's real ones

Resolves open questions 6 and 9. Source: Razorpay's card error-reason documentation,
read 2026-08-23. **Not verified against a live API call** — this repo has no Razorpay
credentials.

## What was confirmed

The error **shape** the taxonomy was built against in M1 is correct. Razorpay returns
`code / description / field / source / step / reason / metadata`, and documents
`reason` as the field intended for programmatic handling. Building the normaliser
around `reason` with a free-text `description` fallback was the right call.

## The real card reason enumeration

`payment_timed_out`, `gateway_technical_error`, `payment_cancelled`, `card_declined`,
`insufficient_funds`, `card_not_enrolled`, `bank_technical_error`,
`card_disabled_for_online_payments`, `authentication_failed`,
`payment_risk_check_failed`, `payment_failed`, `incorrect_cvv`,
`debit_instrument_inactive`, `debit_instrument_blocked`, `card_expired`,
`transaction_limit_exceeded`.

## Mapping onto our taxonomy

| Razorpay `reason` | our `FailureCause` | match |
|---|---|---|
| `insufficient_funds` | `insufficient_funds` | **exact** |
| `card_expired` | `card_expired` | **exact** |
| `payment_failed` | `unknown` → conservative path | **exact string, and it is genuinely generic**: Razorpay defines it as "Payment was declined by the customer's bank" |
| `authentication_failed` | `auth_failed` | ours renamed |
| `payment_timed_out` | `upi_collect_expired` | ours renamed |
| `gateway_technical_error` | `gateway_timeout` | ours renamed |
| `bank_technical_error` | `issuer_downtime` | ours renamed |
| `transaction_limit_exceeded` | `limit_exceeded` | ours renamed |
| `payment_risk_check_failed` | `risk_decline` | ours renamed |
| `card_disabled_for_online_payments`, `card_not_enrolled`, `debit_instrument_inactive` | `method_not_enabled` | ours collapses three |
| `debit_instrument_blocked` | `lost_stolen_fraud` | ours is narrower than Razorpay's |
| `incorrect_cvv` | `invalid_card_details` | ours renamed |
| `card_declined` | `do_not_honour` | ours renamed |

## What we got wrong, stated plainly

- **`do_not_honour` is not a Razorpay reason string.** We invented it. Razorpay's
  nearest equivalent is `card_declined`. The *modelling* was right — a bank decline
  arriving under an uninformative reason is real, and Razorpay's own `payment_failed`
  ("declined by the customer's bank") is exactly that case — but the string is ours.
- **`mandate_revoked` and `amount_exceeds_mandate` do not appear** in the card reason
  list. E-mandate and UPI Autopay have their own reason sets that were not read within
  the timebox. Since the corpus is predominantly mandate-based, this is the largest
  remaining gap between our synthetic reasons and Razorpay's real ones.
- **`source` and `step` value enumerations were not found.** Our values
  (`customer`/`bank`/`gateway`/`business`/`NA`, `payment_initiation`/
  `payment_authentication`/`payment_authorization`) remain unverified.

## Effect on the measured results: none

`rr/taxonomy.py:REASON_TO_CAUSE` now also maps the real Razorpay strings, so the
adapter can consume a live response. **This changes no number in
[results.md](results.md)**: the synthetic corpus contains only the synthetic strings,
so the added rows are never hit on any cohort. Verified by re-running the sealed
report on dev before and after.
