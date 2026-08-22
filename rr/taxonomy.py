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
