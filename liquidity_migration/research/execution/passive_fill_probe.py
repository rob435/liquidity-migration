"""Passive-vs-taker execution A/B probe: pure measurement logic (no venue I/O).

Research surfaces are priced at the measured 15.56 bp taker round trip; the
passive floor is 5.40 bp. Whether that floor is reachable is an execution
question, and this probe manufactures a powered sample in hours by placing its
own min-notional orders. The in-flow A/B instrument (arm B, retired with the
paper fleet 2026-08-03 at 2/8 fills; thresholds in
``docs/research_findings.md`` §1) accrued too slowly to grade. Probe attempts
sample ordinary market states while CONTINUOUS entries sample pumps, so a probe
result bounds the mechanism; only an in-flow experiment grades the flow.

Design (the commit of this file is the registration):

- Paired arms: each attempt hashes ``sha256(salt:symbol:attempt_index)`` to TAKER
  or POST_ONLY.
- Primary metric: intention-to-treat per-side execution cost in bp vs the
  decision mid, including fees. An unfilled post-only attempt is charged the
  taker fallback at the terminal quote, so waiting drift is counted.
- Powered sample: ``REGISTERED_MIN_ATTEMPTS_PER_ARM`` resolved attempts per arm
  before any conclusion.
- Kill criteria: post-only fill rate < ``KILL_MIN_FILL_RATE`` within the timeout,
  or ITT cost difference (post_only - taker) >= 0 at the powered sample, keeps
  the taker basis. Anything else re-prices the ledger and records a change point.

Scope limits: probes are minimum-notional, so the measured number is a floor on
passive cost at deployed size. Sell-side entries are probed because every
deployed and researched book enters short. The result grades the demo flow it
measures. The 60 s timeout exceeds the in-flow arm B's 20 s chase window so
fill-rate-at-20s is derivable from the recorded time-to-fill curve.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

ARM_TAKER = "taker"
ARM_POST_ONLY = "post_only"

# Registered probe parameters. Changing any of these is a new experiment.
REGISTERED_POST_ONLY_TIMEOUT_SECONDS = 60.0
REGISTERED_ADVERSE_SELECTION_DELAY_SECONDS = 30.0
REGISTERED_MIN_ATTEMPTS_PER_ARM = 100
REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT = 25.0
KILL_MIN_FILL_RATE = 0.40

# "rejected_would_cross" is a post-only order Bybit refused at create time for
# taking liquidity: a passive failure, not an error.
TERMINAL_FILLED = "filled"
TERMINAL_TIMEOUT_CANCELLED = "timeout_cancelled"
TERMINAL_REJECTED_WOULD_CROSS = "rejected_would_cross"
TERMINAL_STATES = frozenset(
    {TERMINAL_FILLED, TERMINAL_TIMEOUT_CANCELLED, TERMINAL_REJECTED_WOULD_CROSS}
)


def allocate_arm(*, salt: str, symbol: str, attempt_index: int) -> str:
    """Deterministic 50/50 arm allocation; auditable from the receipt alone."""
    if attempt_index < 0:
        raise ValueError("attempt_index must be nonnegative")
    digest = hashlib.sha256(f"{salt}:{symbol.upper()}:{attempt_index}".encode()).digest()
    return ARM_TAKER if digest[0] % 2 == 0 else ARM_POST_ONLY


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One resolved A/B attempt. All prices are quote-currency floats."""

    symbol: str
    arm: str
    side: str  # "Sell" (registered probe side) or "Buy"
    attempt_index: int
    decision_ts_ns: int
    decision_bid: float
    decision_ask: float
    terminal_state: str
    terminal_ts_ns: int
    fill_price: float | None = None
    fill_fee_bp: float | None = None  # observed fee in bp of notional; None if unresolved
    fee_observed: bool = False
    terminal_bid: float | None = None
    terminal_ask: float | None = None
    post_delay_mid: float | None = None  # mid ADVERSE_SELECTION_DELAY after terminal
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def decision_mid(self) -> float:
        return 0.5 * (self.decision_bid + self.decision_ask)

    @property
    def decision_spread_bp(self) -> float:
        mid = self.decision_mid
        return (self.decision_ask - self.decision_bid) / mid * 1e4 if mid > 0 else float("nan")

    @property
    def time_to_terminal_s(self) -> float:
        return (self.terminal_ts_ns - self.decision_ts_ns) / 1e9

    def __post_init__(self) -> None:
        if self.arm not in (ARM_TAKER, ARM_POST_ONLY):
            raise ValueError(f"unknown arm: {self.arm}")
        if self.side not in ("Buy", "Sell"):
            raise ValueError(f"unknown side: {self.side}")
        if self.terminal_state not in TERMINAL_STATES:
            raise ValueError(f"unknown terminal state: {self.terminal_state}")
        if not (math.isfinite(self.decision_bid) and math.isfinite(self.decision_ask)):
            raise ValueError("decision quote must be finite")
        if self.decision_bid <= 0 or self.decision_ask < self.decision_bid:
            raise ValueError("decision quote must satisfy 0 < bid <= ask")
        if self.terminal_state == TERMINAL_FILLED and self.fill_price is None:
            raise ValueError("filled attempt requires a fill price")
        if self.terminal_state != TERMINAL_FILLED and self.fill_price is not None:
            raise ValueError("unfilled attempt cannot carry a fill price")


def price_cost_bp(*, side: str, reference_mid: float, fill_price: float) -> float:
    """Signed execution cost vs a reference mid, in bp. Positive = paid."""
    if reference_mid <= 0:
        raise ValueError("reference mid must be positive")
    signed = (fill_price - reference_mid) / reference_mid
    return (signed if side == "Buy" else -signed) * 1e4


def itt_cost_bp(record: AttemptRecord, *, taker_fee_bp_fallback: float) -> float | None:
    """Intention-to-treat cost of one attempt, in bp vs the decision mid.

    - Filled (either arm): realized price cost plus the observed fee, or the
      registered taker fallback fee when the venue never reported one on a
      taker fill (a maker fill without an observed fee returns None rather
      than inventing a rebate).
    - Unfilled post-only: charged as an immediate taker at the terminal quote
      (cross the terminal spread) plus the taker fee — the price of having
      tried to be passive and failed, including any drift while waiting.
    """
    if record.terminal_state == TERMINAL_FILLED:
        assert record.fill_price is not None
        price = price_cost_bp(
            side=record.side, reference_mid=record.decision_mid, fill_price=record.fill_price
        )
        if record.fee_observed and record.fill_fee_bp is not None:
            return price + record.fill_fee_bp
        if record.arm == ARM_TAKER:
            return price + taker_fee_bp_fallback
        return None  # maker fill with unresolved fee: do not fabricate a rebate
    # Passive failure -> taker fallback at the terminal quote.
    if record.terminal_bid is None or record.terminal_ask is None:
        return None
    fallback_fill = record.terminal_ask if record.side == "Buy" else record.terminal_bid
    price = price_cost_bp(
        side=record.side, reference_mid=record.decision_mid, fill_price=fallback_fill
    )
    return price + taker_fee_bp_fallback


def adverse_selection_bp(record: AttemptRecord) -> float | None:
    """Post-fill mid drift against the position, in bp (maker fills only)."""
    if record.terminal_state != TERMINAL_FILLED or record.post_delay_mid is None:
        return None
    assert record.fill_price is not None
    drift = (record.post_delay_mid - record.fill_price) / record.fill_price
    against = drift if record.side == "Sell" else -drift
    return against * 1e4


def _mean_std(values: list[float]) -> tuple[float, float]:
    if not values:
        return float("nan"), float("nan")
    if len(values) == 1:
        return values[0], float("nan")
    return statistics.fmean(values), statistics.stdev(values)


def summarize(
    records: Iterable[AttemptRecord],
    *,
    taker_fee_bp_fallback: float,
) -> dict[str, Any]:
    """Per-arm ITT cost and fill rate."""
    rows = list(records)
    out: dict[str, Any] = {"n_total": len(rows), "arms": {}}
    costs: dict[str, list[float]] = {ARM_TAKER: [], ARM_POST_ONLY: []}
    for arm in (ARM_TAKER, ARM_POST_ONLY):
        arm_rows = [row for row in rows if row.arm == arm]
        filled = [row for row in arm_rows if row.terminal_state == TERMINAL_FILLED]
        arm_costs = [
            cost
            for row in arm_rows
            if (cost := itt_cost_bp(row, taker_fee_bp_fallback=taker_fee_bp_fallback)) is not None
        ]
        costs[arm] = arm_costs
        mean, std = _mean_std(arm_costs)
        adverse = [
            drift for row in filled if (drift := adverse_selection_bp(row)) is not None
        ]
        adverse_mean, _ = _mean_std(adverse)
        fill_times = sorted(row.time_to_terminal_s for row in filled)
        out["arms"][arm] = {
            "attempts": len(arm_rows),
            "filled": len(filled),
            "fill_rate": (len(filled) / len(arm_rows)) if arm_rows else float("nan"),
            "itt_cost_bp_n": len(arm_costs),
            "itt_cost_bp_mean": mean,
            "itt_cost_bp_std": std,
            "adverse_selection_bp_mean": adverse_mean,
            "adverse_selection_n": len(adverse),
            "median_time_to_fill_s": (
                fill_times[len(fill_times) // 2] if fill_times else float("nan")
            ),
        }
    taker_costs, passive_costs = costs[ARM_TAKER], costs[ARM_POST_ONLY]
    if len(taker_costs) >= 2 and len(passive_costs) >= 2:
        diff = statistics.fmean(passive_costs) - statistics.fmean(taker_costs)
        pooled = math.sqrt(
            statistics.variance(passive_costs) / len(passive_costs)
            + statistics.variance(taker_costs) / len(taker_costs)
        )
        out["itt_diff_bp_post_only_minus_taker"] = diff
        out["itt_diff_t_stat"] = diff / pooled if pooled > 0 else float("nan")
    powered = all(
        out["arms"][arm]["itt_cost_bp_n"] >= REGISTERED_MIN_ATTEMPTS_PER_ARM
        for arm in (ARM_TAKER, ARM_POST_ONLY)
    )
    fill_rate = out["arms"][ARM_POST_ONLY]["fill_rate"]
    out["powered"] = powered
    out["kill_low_fill_rate"] = (
        bool(fill_rate < KILL_MIN_FILL_RATE) if not math.isnan(fill_rate) and powered else None
    )
    out["kill_no_cost_edge"] = (
        bool(out.get("itt_diff_bp_post_only_minus_taker", float("inf")) >= 0.0)
        if powered
        else None
    )
    out["registered"] = {
        "post_only_timeout_seconds": REGISTERED_POST_ONLY_TIMEOUT_SECONDS,
        "adverse_selection_delay_seconds": REGISTERED_ADVERSE_SELECTION_DELAY_SECONDS,
        "min_attempts_per_arm": REGISTERED_MIN_ATTEMPTS_PER_ARM,
        "max_attempt_notional_usdt": REGISTERED_MAX_ATTEMPT_NOTIONAL_USDT,
        "kill_min_fill_rate": KILL_MIN_FILL_RATE,
        "taker_fee_bp_fallback": taker_fee_bp_fallback,
    }
    return out
