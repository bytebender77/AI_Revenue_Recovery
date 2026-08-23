"""Canonical vocabulary shared by the simulator, the agent, and the eval harness.

Nothing here is Razorpay-specific. The mapping from real gateway error codes onto
FailureCause lives in rr/normalize/ (M2) and is versioned separately, because the
real enumerations still need verifying against Razorpay's error-code docs.
"""
from enum import Enum


class Regime(str, Enum):
    MERCHANT_INITIATED = "merchant_initiated"  # mandate / auto-debit: re-debit permitted
    CUSTOMER_INITIATED = "customer_initiated"  # one-time checkout: NO re-debit authority


class Method(str, Enum):
    UPI_AUTOPAY = "upi_autopay"
    CARD_MANDATE = "card_mandate"
    EMANDATE_NACH = "emandate_nach"
    UPI = "upi"
    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"


MANDATE_METHODS = frozenset(
    {Method.UPI_AUTOPAY, Method.CARD_MANDATE, Method.EMANDATE_NACH}
)


class FailureCause(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    ISSUER_DOWNTIME = "issuer_downtime"
    GATEWAY_TIMEOUT = "gateway_timeout"
    UPI_COLLECT_EXPIRED = "upi_collect_expired"
    AUTH_FAILED = "auth_failed"
    DO_NOT_HONOUR = "do_not_honour"
    CARD_EXPIRED = "card_expired"
    INVALID_CARD_DETAILS = "invalid_card_details"
    LOST_STOLEN_FRAUD = "lost_stolen_fraud"
    MANDATE_INVALID = "mandate_invalid"  # revoked or expired
    AMOUNT_EXCEEDS_MANDATE = "amount_exceeds_mandate"
    LIMIT_EXCEEDED = "limit_exceeded"
    RISK_DECLINE = "risk_decline"
    METHOD_NOT_ENABLED = "method_not_enabled"
    UNKNOWN = "unknown"  # observed-only. Never a ground-truth cause.


class Persistence(str, Enum):
    TRANSIENT = "transient"
    CONDITIONAL = "conditional"
    TERMINAL = "terminal"


PERSISTENCE: dict[FailureCause, Persistence] = {
    FailureCause.GATEWAY_TIMEOUT: Persistence.TRANSIENT,
    FailureCause.ISSUER_DOWNTIME: Persistence.TRANSIENT,
    FailureCause.INSUFFICIENT_FUNDS: Persistence.CONDITIONAL,
    FailureCause.UPI_COLLECT_EXPIRED: Persistence.CONDITIONAL,
    FailureCause.AUTH_FAILED: Persistence.CONDITIONAL,
    FailureCause.DO_NOT_HONOUR: Persistence.CONDITIONAL,
    FailureCause.LIMIT_EXCEEDED: Persistence.CONDITIONAL,
    FailureCause.CARD_EXPIRED: Persistence.TERMINAL,
    FailureCause.INVALID_CARD_DETAILS: Persistence.TERMINAL,
    FailureCause.LOST_STOLEN_FRAUD: Persistence.TERMINAL,
    FailureCause.MANDATE_INVALID: Persistence.TERMINAL,
    FailureCause.AMOUNT_EXCEEDS_MANDATE: Persistence.TERMINAL,
    FailureCause.RISK_DECLINE: Persistence.TERMINAL,
    FailureCause.METHOD_NOT_ENABLED: Persistence.TERMINAL,
    FailureCause.UNKNOWN: Persistence.CONDITIONAL,
}

# Causes where a re-debit must never be issued. A NUDGE may still be legal for
# some of them (card update, re-authorise mandate) -- drawing that line is the
# eligibility gate's job in M3, not the taxonomy's.
NEVER_RETRY = frozenset(
    {
        FailureCause.LOST_STOLEN_FRAUD,
        FailureCause.RISK_DECLINE,
        FailureCause.MANDATE_INVALID,
        FailureCause.AMOUNT_EXCEEDS_MANDATE,
        FailureCause.METHOD_NOT_ENABLED,
        FailureCause.CARD_EXPIRED,
        FailureCause.INVALID_CARD_DETAILS,
    }
)


class ActionType(str, Enum):
    NO_ACTION = "no_action"
    RETRY_SAME = "retry_same"
    RETRY_ALTERNATE_METHOD = "retry_alternate_method"
    NUDGE = "nudge"
    MERCHANT_ALERT = "merchant_alert"
    ESCALATE_HUMAN = "escalate_human"


DEBIT_ACTIONS = frozenset({ActionType.RETRY_SAME, ActionType.RETRY_ALTERNATE_METHOD})


class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    IN_APP = "in_app"


# Deterministic head of the error-code distribution. The LLM normaliser in M5
# handles only the tail this map misses -- that split is what keeps the LLM call
# load-bearing rather than decorative.
#
# NOTE: `payment_failed` deliberately maps to UNKNOWN, not DO_NOT_HONOUR. It is
# the generic catch-all reason and carries no information on its own; the
# distinguishing signal ("declined by issuing bank (do not honour)") lives in the
# free-text description, which is precisely what the tail normaliser reads.
# TODO(citation): verify against Razorpay's published error `reason` enumeration.
REASON_TO_CAUSE: dict[str, FailureCause] = {
    "insufficient_funds": FailureCause.INSUFFICIENT_FUNDS,
    "issuer_down": FailureCause.ISSUER_DOWNTIME,
    "gateway_timeout": FailureCause.GATEWAY_TIMEOUT,
    "payment_timeout": FailureCause.UPI_COLLECT_EXPIRED,
    "payment_authentication_failed": FailureCause.AUTH_FAILED,
    "card_expired": FailureCause.CARD_EXPIRED,
    "invalid_card_details": FailureCause.INVALID_CARD_DETAILS,
    "card_blocked": FailureCause.LOST_STOLEN_FRAUD,
    "mandate_revoked": FailureCause.MANDATE_INVALID,
    "amount_exceeds_mandate": FailureCause.AMOUNT_EXCEEDS_MANDATE,
    "payment_limit_exceeded": FailureCause.LIMIT_EXCEEDED,
    "payment_declined_risk": FailureCause.RISK_DECLINE,
    "method_not_enabled": FailureCause.METHOD_NOT_ENABLED,
}


# Razorpay's REAL card reason strings, read from their error documentation on
# 2026-08-23 (docs/razorpay-reason-mapping.md). Added so the live adapter can consume
# a real response. These strings do not occur in the synthetic corpus, so adding them
# changes no measured number -- verified by re-running the dev report either side.
RAZORPAY_REASON_TO_CAUSE: dict[str, FailureCause] = {
    "insufficient_funds": FailureCause.INSUFFICIENT_FUNDS,
    "card_expired": FailureCause.CARD_EXPIRED,
    "authentication_failed": FailureCause.AUTH_FAILED,
    "payment_timed_out": FailureCause.UPI_COLLECT_EXPIRED,
    "gateway_technical_error": FailureCause.GATEWAY_TIMEOUT,
    "bank_technical_error": FailureCause.ISSUER_DOWNTIME,
    "transaction_limit_exceeded": FailureCause.LIMIT_EXCEEDED,
    "payment_risk_check_failed": FailureCause.RISK_DECLINE,
    "card_disabled_for_online_payments": FailureCause.METHOD_NOT_ENABLED,
    "card_not_enrolled": FailureCause.METHOD_NOT_ENABLED,
    "debit_instrument_inactive": FailureCause.METHOD_NOT_ENABLED,
    "debit_instrument_blocked": FailureCause.LOST_STOLEN_FRAUD,
    "incorrect_cvv": FailureCause.INVALID_CARD_DETAILS,
    "card_declined": FailureCause.DO_NOT_HONOUR,
    # `payment_failed` and `payment_cancelled` are deliberately absent: Razorpay
    # defines payment_failed as "declined by the customer's bank", which names no
    # cause. It must reach UNKNOWN and take the conservative path.
}


def normalize_reason(gateway_reason: str) -> FailureCause:
    """Deterministic lookup over our synthetic strings and Razorpay's real ones.
    Anything unmapped is UNKNOWN, never a guess."""
    hit = REASON_TO_CAUSE.get(gateway_reason)
    if hit is not None:
        return hit
    return RAZORPAY_REASON_TO_CAUSE.get(gateway_reason, FailureCause.UNKNOWN)
