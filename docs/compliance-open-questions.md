# Open compliance questions

Unresolved items. Every one is a config field with a `TODO(citation)` slot in
`rr/config.py` — none is asserted as fact anywhere in the repo or the writeup.

| # | Question | Where it binds | Status |
|---|---|---|---|
| 1 | RBI e-mandate pre-debit notification lead time | `pre_debit_notification_hours` | UNVERIFIED |
| 2 | RBI e-mandate AFA-exempt per-debit ceiling (revised more than once) | `afa_exempt_amount_minor` | UNVERIFIED |
| 3 | Card network cap on retry attempts per instrument per 30d — Visa and Mastercard differ and both have been revised | `network_retry_cap_per_instrument_30d` | UNVERIFIED |
| 4 | TRAI/DLT quiet hours for commercial communications | `quiet_hours_ist` | UNVERIFIED |
| 5 | Is a payment-recovery message carrying a pay-link transactional or promotional under DLT? Determines whether quiet hours and DND scrubbing apply at all | `recovery_message_class` | UNRESOLVED |
| 6 | Razorpay's actual error `code`/`reason`/`source`/`step` enumerations | `rr/sim/cohort.py:GATEWAY_VIEW` | SHAPE assumed realistic, VALUES are placeholders |
| 7 | Does Razorpay levy a fee on failed attempts? Affects the retry cost coefficient | `debit_attempt_cost_minor` | UNVERIFIED |
| 8 | Human review capacity as a share of failed volume — an ops staffing decision, not a regulatory one, but it materially bounds the policy | `escalation_capacity_pct` | PLACEHOLDER (5%) |
| 9 | Can Razorpay test mode force a *specific* failure reason, or only a generic failure? Determines how faithful the live demo call can be | `rr/adapters/razorpay_test.py` | UNVERIFIED, blocks M7 |
