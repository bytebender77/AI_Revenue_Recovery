# Simulator changelog

The response model is frozen by hash (`rr/sim/response_model.sha256`, checked by
`tests/test_response_model_frozen.py`). Every change to it must be recorded here
with a reason, so that "we tuned the world until the agent won" is not available
as a silent option.

## v1.0.0 — 2026-08-22
Initial freeze. Committed before any policy, baseline, or agent code exists.
Structure chosen to hide five decision-relevant facts from the agent: balance
trajectory (salary cycle), issuer retry tolerance, outage windows, true intent to
pay, and whether the payment self-heals with no intervention.
