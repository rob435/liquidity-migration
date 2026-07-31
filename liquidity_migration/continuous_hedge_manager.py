"""Pure BTC/ETH beta-hedge target calculator for the continuous book.

Holds small LONG BTC/ETH positions sized to the continuous short book's causal
rolling beta, neutralizing its uncompensated alt-season exposure. This module
only calculates targets; the account execution owner handles venue rules,
orders, fills, protection, and attribution.

Sizing uses the profile's two-factor hedge rule and a commit-owned immutable
historical model prior. The runtime does not extend that prior: the bounded demo
data and current account projection do not reconstruct the strategy's causal
per-unit daily return, funding, and costs.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .continuous_rebalance import (
    ContinuousHedge2FState,
    ContinuousHedgeRule,
    ContinuousHedgeState,
    ContinuousRebalanceResizePlan,
    compute_continuous_hedge_ratio,
    compute_continuous_hedge_ratios_2f,
)
from .continuous_profile import active_hedge_regime, active_hedge_rule
from .continuous_regime import latest_btcvol_intensity

HEDGE_SYMBOL = "BTCUSDT"
HEDGE_SYMBOL_2 = "ETHUSDT"
ACTIVE_HEDGE_RULE = active_hedge_rule()
# The backtest book is 0.5-gross-short at scale 1; live H_equity_frac scales by the
# live book's actual gross-short fraction relative to that reference.
REFERENCE_GROSS_SHORT_FRAC = 0.5
HEDGE_MODEL_PRIOR_KIND = "immutable_historical_model_prior"
HEDGE_MODEL_PRIOR_LIMITATIONS = (
    "coefficients_are_not_extended_with_live_book_returns",
    "account_equity_and_current_targets_do_not_reconstruct_per_unit_book_returns",
    "not_current_calibration_or_performance_evidence",
)
_MODEL_PRIOR_COLUMNS = (
    "date",
    "unit_ret",
    "btc_ret",
    "eth_ret",
    "data_through_date",
    "source_summary_sha256",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _regime_hedge_intensity(btc_returns: list[float | None]) -> float:
    """Return today's causal BTC-vol hedge intensity, or 1.0 when disabled."""
    regime = active_hedge_regime()
    if not regime:
        return 1.0
    return latest_btcvol_intensity(
        btc_returns, regime["lam"], regime["vol_window"], regime["pct_window"]
    )


@dataclass(frozen=True, slots=True)
class ContinuousHedgeConfig:
    strategy_id: str = "continuous_btc_hedge_v2"
    max_hedge_equity_frac: float = 0.30  # hard sanity cap on TOTAL live hedge size


@dataclass(frozen=True, slots=True)
class HedgeModelPrior:
    """Immutable historical returns and their reconstructable provenance."""

    dates: tuple[date, ...]
    unit_returns: tuple[float, ...]
    btc_returns: tuple[float | None, ...]
    eth_returns: tuple[float | None, ...]
    data_through_date: date
    source_summary_sha256: str
    artifact_sha256: str

    def provenance(self) -> dict[str, object]:
        return {
            "model_prior_kind": HEDGE_MODEL_PRIOR_KIND,
            "model_prior_artifact_sha256": self.artifact_sha256,
            "model_prior_source_summary_sha256": self.source_summary_sha256,
            "model_prior_start_date": self.dates[0].isoformat(),
            "model_prior_data_through_date": self.data_through_date.isoformat(),
            "model_prior_rows": len(self.dates),
            "model_prior_live_extension": False,
            "model_prior_evidence_scope": "sizing_only_not_current_calibration_or_performance_evidence",
        }


def _model_prior_float(raw: object, *, row_number: int, column: str) -> float:
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"hedge model prior row {row_number} has empty {column}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"hedge model prior row {row_number} has invalid {column}") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"hedge model prior row {row_number} has non-finite {column}")
    return parsed


def _optional_model_prior_float(raw: object, *, row_number: int, column: str) -> float | None:
    if str(raw or "").strip() == "":
        return None
    return _model_prior_float(raw, row_number=row_number, column=column)


def load_hedge_model_prior(path: str | Path) -> HedgeModelPrior:
    """Load the strict commit-owned hedge model prior.

    Missing files, schema drift, malformed rows, inconsistent provenance, and
    reordered/duplicated dates are errors. Silently dropping a bad row would
    change the estimator while preserving a misleading artifact identity.
    """

    p = Path(path)
    payload = p.read_bytes()
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("hedge model prior is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text), strict=True)
    if tuple(reader.fieldnames or ()) != _MODEL_PRIOR_COLUMNS:
        raise ValueError("hedge model prior has an unexpected schema")

    dates: list[date] = []
    unit: list[float] = []
    btc: list[float | None] = []
    eth: list[float | None] = []
    boundaries: set[date] = set()
    source_hashes: set[str] = set()
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise ValueError("hedge model prior CSV is malformed") from exc
    if not rows:
        raise ValueError("hedge model prior has no rows")

    for row_number, row in enumerate(rows, start=2):
        if None in row:
            raise ValueError(f"hedge model prior row {row_number} has extra columns")
        raw_date = str(row.get("date") or "").strip()
        raw_boundary = str(row.get("data_through_date") or "").strip()
        try:
            row_date = date.fromisoformat(raw_date)
            boundary = date.fromisoformat(raw_boundary)
        except ValueError as exc:
            raise ValueError(f"hedge model prior row {row_number} has an invalid date") from exc
        if dates and row_date <= dates[-1]:
            raise ValueError("hedge model prior dates must be unique and strictly increasing")
        source_hash = str(row.get("source_summary_sha256") or "").strip()
        if _SHA256_RE.fullmatch(source_hash) is None:
            raise ValueError(f"hedge model prior row {row_number} has an invalid source summary hash")
        dates.append(row_date)
        unit.append(_model_prior_float(row.get("unit_ret"), row_number=row_number, column="unit_ret"))
        btc.append(_optional_model_prior_float(row.get("btc_ret"), row_number=row_number, column="btc_ret"))
        eth.append(_optional_model_prior_float(row.get("eth_ret"), row_number=row_number, column="eth_ret"))
        boundaries.add(boundary)
        source_hashes.add(source_hash)

    if len(boundaries) != 1 or len(source_hashes) != 1:
        raise ValueError("hedge model prior provenance must be constant across rows")
    data_through_date = next(iter(boundaries))
    if dates[-1] != data_through_date:
        raise ValueError("hedge model prior data_through_date must equal its final row date")
    return HedgeModelPrior(
        dates=tuple(dates),
        unit_returns=tuple(unit),
        btc_returns=tuple(btc),
        eth_returns=tuple(eth),
        data_through_date=data_through_date,
        source_summary_sha256=next(iter(source_hashes)),
        artifact_sha256=hashlib.sha256(payload).hexdigest(),
    )


def require_usable_hedge_model_prior(
    prior: HedgeModelPrior,
    *,
    as_of_date: date,
    hedge_rule: ContinuousHedgeRule = ACTIVE_HEDGE_RULE,
) -> HedgeModelPrior:
    """Fail closed unless the immutable prior can estimate at least BTC beta."""

    if prior.data_through_date >= as_of_date:
        raise ValueError("hedge model prior must end before the current UTC decision date")
    end = len(prior.unit_returns) - int(hedge_rule.beta_extra_lag_days)
    start = max(0, end - int(hedge_rule.beta_window_days))
    required = int(hedge_rule.beta_min_obs)
    if end < required:
        raise ValueError(f"hedge model prior has {max(end, 0)} usable rows; {required} required")
    btc_observations = sum(value is not None for value in prior.btc_returns[start:end])
    if btc_observations < required:
        raise ValueError(
            f"hedge model prior has {btc_observations} usable BTC rows in the beta window; "
            f"{required} required"
        )
    return prior


def _beta_window_joint_observation_count(
    btc_returns: list[float | None],
    eth_returns: list[float | None],
    hedge_rule: ContinuousHedgeRule,
) -> int:
    """Joint BTC+ETH observation count over the trailing window the 2f beta uses
    (rows in ``[lo, end)`` where both legs are known, ``end = len -
    beta_extra_lag_days``, ``lo = max(0, end - beta_window_days)``).

    A full-series count can miss a sparse trailing window and leave the book
    unhedged when the two-factor estimator returns zero betas."""
    end = len(btc_returns) - int(hedge_rule.beta_extra_lag_days)
    if end <= 0:
        return 0
    start = max(0, end - int(hedge_rule.beta_window_days))
    return sum(
        1
        for b, e in zip(btc_returns[start:end], eth_returns[start:end])
        if b is not None and e is not None
    )


@dataclass(slots=True)
class HedgeDecision2F:
    """The computed two-leg (BTC+ETH) hedge action for one target run (pure; no I/O)."""

    beta_window_days: int
    ratio_btc: float
    ratio_eth: float
    target_btc_usdt: float
    target_eth_usdt: float
    n_obs_joint: int
    plan_btc: ContinuousRebalanceResizePlan | None
    plan_eth: ContinuousRebalanceResizePlan | None
    fell_back_to_btc: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)


def compute_hedge_decision_2f(
    config: ContinuousHedgeConfig,
    *,
    unit_returns: list[float],
    btc_returns: list[float | None],
    eth_returns: list[float | None],
    live_gross_short_frac: float,
    btc_price: float,
    eth_price: float,
    current_btc_qty: float,
    current_eth_qty: float,
    equity_usdt: float,
) -> HedgeDecision2F:
    """Pure two-leg hedge sizing for one target reconciliation (no orders, no I/O).

    Per-leg ratios come from ``compute_continuous_hedge_ratios_2f``. When the
    joint BTC+ETH count within the trailing beta window is below
    ``beta_min_obs`` the twin returns (0, 0) and this falls back to the
    single-leg BTC decision, so a thin ETH window cannot leave the book
    unhedged. The gate uses that same trailing window, not the full series, so
    an ETH-thin window with deep history still falls back. The total is capped
    at ``max_hedge_equity_frac`` with legs scaled proportionally.
    """
    from .continuous_rebalance import plan_continuous_hedge_resize

    base_scale = max(live_gross_short_frac, 0.0) / REFERENCE_GROSS_SHORT_FRAC
    # One causal BTC-vol intensity scales both legs and the single-leg fallback.
    hedge_intensity = _regime_hedge_intensity(btc_returns)
    target_scale = base_scale * hedge_intensity
    n_joint = _beta_window_joint_observation_count(btc_returns, eth_returns, ACTIVE_HEDGE_RULE)
    state = ContinuousHedge2FState(
        prior_raw_returns=tuple(unit_returns),
        prior_hedge_returns_1=tuple(btc_returns),
        prior_hedge_returns_2=tuple(eth_returns),
    )
    r_btc, r_eth = compute_continuous_hedge_ratios_2f(state, ACTIVE_HEDGE_RULE, target_scale)
    fell_back = False
    # Any degenerate two-factor result falls back to the single-leg BTC hedge.
    # Genuine no-hedge cases also yield zero from that calculation.
    if r_btc == 0.0 and r_eth == 0.0:
        single = compute_continuous_hedge_ratio(
            ContinuousHedgeState(prior_raw_returns=tuple(unit_returns), prior_hedge_returns=tuple(btc_returns)),
            ACTIVE_HEDGE_RULE,
            target_scale,
        )
        r_btc, r_eth, fell_back = single, 0.0, True
    total = r_btc + r_eth
    if total > config.max_hedge_equity_frac and total > 0.0:
        shrink = config.max_hedge_equity_frac / total
        r_btc *= shrink
        r_eth *= shrink
    plan_btc = plan_eth = None
    if btc_price > 0.0:
        plan_btc = plan_continuous_hedge_resize(
            hedge_symbol=HEDGE_SYMBOL, current_qty=current_btc_qty, price=btc_price,
            equity_usdt=equity_usdt, hedge_ratio=r_btc,
        )
    if eth_price > 0.0:
        plan_eth = plan_continuous_hedge_resize(
            hedge_symbol=HEDGE_SYMBOL_2, current_qty=current_eth_qty, price=eth_price,
            equity_usdt=equity_usdt, hedge_ratio=r_eth,
        )
    eq = max(equity_usdt, 0.0)
    return HedgeDecision2F(
        beta_window_days=ACTIVE_HEDGE_RULE.beta_window_days,
        ratio_btc=r_btc,
        ratio_eth=r_eth,
        target_btc_usdt=eq * r_btc,
        target_eth_usdt=eq * r_eth,
        n_obs_joint=n_joint,
        plan_btc=plan_btc,
        plan_eth=plan_eth,
        fell_back_to_btc=fell_back,
        diagnostics={
            "target_scale": target_scale,
            "base_scale": base_scale,
            "hedge_intensity": hedge_intensity,
            "live_gross_short_frac": live_gross_short_frac,
            "history_days": len(unit_returns),
        },
    )
