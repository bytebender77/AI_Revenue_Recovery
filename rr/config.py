"""Tunables.

RULE FOR THIS FILE: any value that corresponds to a real-world regulatory or card
network constraint is a config field carrying a TODO(citation) slot, and is NOT
asserted anywhere in the repo as fact. The numbers below are placeholders chosen
to be conservative. They are load-bearing for the demo and must be replaced with
cited values before any claim is made about compliance.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RegulatoryConfig:
    # TODO(citation): RBI e-mandate pre-debit notification lead time. UNVERIFIED.
    pre_debit_notification_hours: int = 24
    # TODO(citation): RBI e-mandate AFA-exempt per-debit ceiling, minor units. UNVERIFIED.
    afa_exempt_amount_minor: int = 1_500_000
    # TODO(citation): card network cap on retry attempts per instrument / 30d.
    # Visa and Mastercard publish different caps and these have been revised.
    # UNVERIFIED -- treat as a configurable ceiling, not a known constant.
    network_retry_cap_per_instrument_30d: int = 4
    # TODO(citation): TRAI/DLT quiet hours for commercial communications, IST.
    quiet_hours_ist: tuple[int, int] = (21, 9)
    # TODO(citation): OPEN QUESTION -- is a payment-recovery message carrying a
    # pay-link classified transactional or promotional under DLT? Affects whether
    # quiet hours and DND scrubbing apply at all. See docs/compliance-open-questions.md
    recovery_message_class: str = "UNRESOLVED"


@dataclass(frozen=True)
class SimClock:
    """Compressed 30-day clock. All times are float hours since t=0."""
    total_hours: int = 720
    dev_hours: tuple[int, int] = (0, 480)     # sim days 1-20
    test_hours: tuple[int, int] = (480, 720)  # sim days 21-30
    # Per-intent observation window. An outcome after this is not counted.
    recovery_horizon_hours: float = 168.0
    tick_hours: float = 1.0


@dataclass(frozen=True)
class CostConfig:
    """Minor units (paise). Used by the eval harness in step 4, not by the sim."""
    debit_attempt_cost_minor: int = 200
    contact_cost_minor: dict = field(
        default_factory=lambda: {"sms": 15, "whatsapp": 65, "email": 3, "in_app": 1}
    )
    human_escalation_cost_minor: int = 4_000
    merchant_alert_cost_minor: int = 500
    # Modelled annoyance cost of a contact that reached someone who was going to
    # pay anyway, or was never going to. Deliberately a config knob: it is a
    # business preference, not a measurable, and the eval reports sensitivity to it.
    wasted_contact_cost_minor: int = 900
    # TODO(citation): expected chargeback cost of a debit attempt against a
    # terminal decline. Placeholder; real value depends on network + merchant MCC.
    terminal_retry_penalty_minor: int = 25_000
    default_margin_bps: int = 4_000  # 40% contribution margin on recovered revenue
    # Share of failed intents an ops team can actually work. Applied identically
    # to every arm so that no arm wins on permission rather than judgement.
    # TODO(citation): staffing decision, merchant-specific. Placeholder.
    escalation_capacity_pct: float = 0.05


@dataclass(frozen=True)
class CohortConfig:
    seed: int = 20260822
    n_dev: int = 6000
    n_test: int = 3000
    n_merchants_dev: int = 12
    n_merchants_test: int = 8
    customers_per_merchant: int = 260
    regime_mix: tuple[float, float] = (0.82, 0.18)  # merchant_initiated, customer_initiated

    # --- signal degradation -------------------------------------------------
    p_generic_code: float = 0.10      # gateway returns an uninformative code
    p_unmapped_code: float = 0.06     # bank-specific code absent from the taxonomy
    p_masquerade: float = 0.03        # terminal truth wearing a soft-looking code

    # --- adversarial slices (forced counts, dev cohort; test gets 60%) -------
    n_adv_fast_organic: int = 150     # self-heals ~20 min after failure
    n_adv_soft_mask_terminal: int = 120
    n_adv_outage_midwindow: int = 120

    n_outage_windows_dev: int = 2
    n_outage_windows_test: int = 1
    outage_duration_hours: tuple[int, int] = (3, 9)


@dataclass(frozen=True)
class OutageDetectorConfig:
    """Naive issuer-outage heuristic used by B2.5. Tuning, not regulation."""
    consecutive_failures: int = 8
    window_hours: float = 2.0
    backoff_hours: float = 6.0


@dataclass(frozen=True)
class ModelConfig:
    """Empirical-Bayes Beta-Binomial over the observable feature cross."""
    shrinkage_k: float = 25.0          # pseudo-counts pulled from the parent cell
    ci_samples: int = 4000             # random.betavariate draws per cell
    ci_alpha: float = 0.10             # 90% credible interval
    fit_split_fraction: float = 0.50   # share of dev used to fit; rest is a leakage check
    # A cell with one observation is a rumour, not evidence. Below this count the
    # lookup falls through to the parent, which has enough data to be worth using.
    min_cell_observations: int = 8
    time_buckets_h: tuple = (2.0, 8.0, 24.0, 48.0, 96.0)


@dataclass(frozen=True)
class PolicyConfig:
    tick_hours: float = 6.0
    candidate_grid_h: tuple = (0.5, 2, 6, 12, 24, 36, 48, 72, 96, 120, 144, 168)
    max_nudge_channels: int = 2
    # "incremental" scores an action by the value it ADDS over doing nothing.
    # "gross" is the literal p_success x amount formula, kept so the difference
    # can be measured rather than asserted. See docs/ev-objective.md.
    ev_objective: str = "incremental"
    # Chargeback risk is NOT learnable from what the agent observes -- the label
    # never arrives in production either -- so it is a configured prior.
    # TODO(citation): real rates are network- and MCC-specific. Placeholder.
    p_chargeback_known_soft: float = 0.002
    # An UNKNOWN cause is a cause that might be hiding something terminal. Priced at
    # the rate at which that actually happens -- measurable in production by manually
    # diagnosing a sample of unknown-code failures, which is why this is estimable
    # rather than guessed. See the sensitivity table in `make m4`: raising it from
    # 0.010 costs no incremental recovery and cuts terminal-retry exposure by a third.
    # TODO(citation): network- and MCC-specific chargeback economics still UNVERIFIED.
    p_chargeback_unknown_cause: float = 0.075


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Portfolio-level halt. Per-transaction rules can all pass while a whole
    cause-cell quietly stops converting."""
    # Keyed on (cause, attempt_index), NOT cause alone. Third attempts convert at
    # 3-7% by nature; pooling them with first attempts lets a healthy cell trip the
    # breaker on the strength of its own tail. Halting one rung is a real control;
    # halting an entire cause because its tail is weak is a bug -- and was one.
    window_attempts: int = 60
    min_success_rate: float = 0.015      # below the weakest natural cell (limit_exceeded idx2, 3.3%)
    min_attempts_before_arming: int = 40


REGULATORY = RegulatoryConfig()
OUTAGE = OutageDetectorConfig()
MODEL = ModelConfig()
POLICY = PolicyConfig()
BREAKER = CircuitBreakerConfig()
CLOCK = SimClock()
COSTS = CostConfig()
