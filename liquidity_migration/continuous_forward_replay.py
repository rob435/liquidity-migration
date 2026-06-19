"""No-order signal-replay forward collector for the banked continuous object.

Design receipt: docs/preregistration/continuous-forward-clock-spec-2026-06-09.md
(R3, live-readiness program). This module accrues SIGNAL forward evidence for the
frozen winner+hedge configuration by re-running the same code path that produced the
banked receipts and appending only out-of-sample days to a persistent forward ledger.

Safety properties (all tested):
- the frozen configuration is hash-pinned: a state dir created under one config
  refuses to update under another;
- overlap days are re-verified against the stored ledger on every run (any drift is
  a same-code regression alarm -> hard error, nothing appended). The ONLY exception
  is the explicit, operator-gated ``allow_history_revision`` self-heal path: when a
  data root is legitimately revised (a late-arriving/backfilled partition, not a code
  regression), the drifting overlap days are re-based to the rebuilt values and the
  rewrite is logged instead of raising. The default stays hard-error;
- appends are idempotent (re-running with the same end day adds nothing).

This collector emits NO orders. It is the STATE.md-compliant "no-order paper
evidence collector" for the continuous candidate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from .continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    combine_continuous_components,
)
from .continuous_regime import FROZEN_BTCVOL_REGIME
from .trade_lifecycle import annualized_sharpe  # metrics-3: shared Sharpe convention

MS_PER_DAY = 86_400_000

# The banked object, frozen. Receipts: continuous-winner-robustness-2026-06-09,
# continuous-hedge-{overlay,engine}-2026-06-09, continuous-walkforward-allocator-2026-06-09
# (frozen-weights policy). Changing ANY value here voids the accrued forward ledger.
FROZEN_FORWARD_CONFIG: dict[str, Any] = {
    "object": "continuous_winner_uptrend_ensemble_btc_hedged",
    # Current three-component object frozen 2026-06-18. Renormalized = old/0.90.
    # NOTE: this changes frozen_config_hash -> the prior continuous forward ledger is VOIDED;
    # archive the old state dir + regenerate the hedge warmstart + start a fresh clock on deploy.
    "weights": {"turn3p3": 0.3333333333333333, "turn4p3": 0.2222222222222222, "turn4p5": 0.4444444444444444},
    # Official v2 entry sizing, promoted 2026-06-18
    # (docs/preregistration/2026-06-18-continuous-v2-invvol-max4-replay.md).
    # The frozen component source ledgers were generated with this recipe; keep
    # it explicit here so promoted.continuous_profile() is not ambiguous.
    "entry_sizing": {
        "mode": "inverse_vol",
        "target_vol_per_name": 0.01,
        "vol_weight_clamp": 2.0,
    },
    "rebalance": {
        # OPERATOR OVERRIDE 2026-06-19: daily volatility adjuster DISABLED
        # (enabled=False -> constant gross, no vol-target / drawdown-halving). This
        # un-confounds signal/exit research from the path-dependent rebalance and
        # adopts TP12; it is RESEARCH-PHASE and reversible (flip enabled=True + retune
        # when the volatility control is reworked). Changing this block changes
        # frozen_config_hash -> the prior forward ledger is VOIDED (archive the state
        # dir + start a fresh clock on deploy). The remaining params are retained
        # verbatim so re-enabling is a one-line flip. Honest caveat: this removes the
        # book's only daily risk control and lowers MAR on both venues; receipt
        # docs/preregistration/2026-06-19-operator-override-disable-voladjuster-tp12.md.
        "enabled": False,
        "realized_vol_window_days": 90,
        "target_daily_vol": 0.045,
        "max_scale": 4.0,
        "drawdown_half_threshold": -0.04,
        "drawdown_zero_threshold": None,
        "resize_cost_bps": 10.0,
        "strategy_momentum_window_days": 0,
    },
    "hedge": {
        # BTC+ETH 2f hedge — tracks the SAME object the live demo book executes
        # (deploy/systemd/liquidity-migration-continuous-hedge.service, HEDGE_MODE=2f,
        # banked 2026-06-10), so forward readiness validates the deployed strategy
        # (operator decision 2026-06-14, forward-replay-1; receipt
        # docs/preregistration/2026-06-14-forward-clock-2f.md). frozen_hedge_mode()
        # reads "2f" from instrument2 and stamps it into every readiness summary.
        # NOTE: adding instrument2 changes frozen_config_hash, so the prior BTC-only
        # forward ledger is VOIDED by the config-hash pin — init_or_check_state will
        # refuse the old state dir. The operator archives the btc_only state dir and
        # starts a fresh 2f clock at the next data-root refresh (a clean reset, not a
        # drift alarm). build_full_ledger wires the second (ETH) leg from the
        # orchestrator's hedge_returns_2 / hedge_funding_2 inputs.
        #
        # 'regime' (operator-approved 2026-06-15; receipt
        # docs/preregistration/2026-06-15-forward-btcvol-regime-hedge.md): the BTC-vol
        # regime-hedge overlay. A causal, mean-1 daily intensity (continuous_regime.
        # btcvol_intensity_series) multiplies BOTH 2f hedge legs — hedge more in
        # turbulence, less in calm. Embedding it here puts it in frozen_config_hash, so
        # turning it on VOIDS the prior 2f forward ledger (another clean reset: archive
        # the old state dir, start a fresh clock). intensity=1 everywhere reduces to the
        # plain 2f hedge byte-for-byte. The live demo book applies the identical signal
        # (continuous_hedge_manager), so demo and forward track one hedge object.
        "instrument": "BTCUSDT",
        "instrument2": "ETHUSDT",
        "beta_window_days": 90,
        "beta_min_obs": 60,
        "hedge_cap": 2.0,
        "cost_bps": 5.0,
        "regime": FROZEN_BTCVOL_REGIME,
    },
    "inception_day_ms": 1_680_307_200_000,  # 2023-04-01 (ledger history start)
}

# basket_return is a few-term per-day sum — a tight pure-absolute tol catches a
# genuine input/config regression. equity is a cum_prod over up to ~700 rows, so a
# pure-absolute tol is stricter than the repo's progressive-equivalence policy
# (AGENTS.md: np.allclose, "last-bit float-order differences carry no alpha"): a
# legitimate alpha-neutral reassociation of the compounding path (vectorised/polars
# cum_prod, numpy version bump) could exceed 1e-9 absolute on a >1.0 equity and trip
# a spurious hard-drift stall. Use a relative+absolute tolerance for the cumulative
# column so real regressions still alarm but last-bit reassociation does not.
OVERLAP_ABS_TOL = 1e-9
EQUITY_REL_TOL = 1e-9
ANN_DAYS = 365.25

_logger = logging.getLogger(__name__)


def frozen_hedge_mode(config: dict[str, Any] | None = None) -> str:
    """Object-identity tag for the hedge leg(s): 'btc_only' or '2f'.

    Derived (not hashed) so a reader/audit can tell this forward clock apart from
    the live BTC+ETH 2f demo book without voiding the accrued ledger. A second
    instrument in the frozen hedge block ('instrument2') marks the 2f object.
    """
    hedge = (config or FROZEN_FORWARD_CONFIG).get("hedge", {})
    return "2f" if hedge.get("instrument2") else "btc_only"


def frozen_hedge_instruments(config: dict[str, Any] | None = None) -> list[str]:
    hedge = (config or FROZEN_FORWARD_CONFIG).get("hedge", {})
    legs = [hedge.get("instrument")]
    if hedge.get("instrument2"):
        legs.append(hedge["instrument2"])
    return [leg for leg in legs if leg]


def frozen_hedge_regime(config: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The active hedge-intensity regime (or None when the plain hedge is frozen).

    Part of frozen_config_hash via the hedge block. The orchestrator builds the
    intensity series from these params; the live hedge manager applies the same.
    """
    return (config or FROZEN_FORWARD_CONFIG).get("hedge", {}).get("regime")


def frozen_config_hash(config: dict[str, Any] | None = None) -> str:
    payload = json.dumps(config or FROZEN_FORWARD_CONFIG, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def frozen_rebalance_rule() -> ContinuousRebalanceRule:
    return ContinuousRebalanceRule(**FROZEN_FORWARD_CONFIG["rebalance"])


def frozen_hedge_rule() -> ContinuousHedgeRule:
    h = FROZEN_FORWARD_CONFIG["hedge"]
    return ContinuousHedgeRule(
        beta_window_days=h["beta_window_days"],
        beta_min_obs=h["beta_min_obs"],
        hedge_cap=h["hedge_cap"],
        cost_bps=h["cost_bps"],
    )


@dataclass(frozen=True)
class ForwardUpdateResult:
    venue: str
    appended_days: int
    verified_overlap_days: int
    total_days: int
    last_day_ms: int | None
    # Count of stored overlap days re-based to the rebuilt values under an
    # explicit history-revision opt-in (default 0 = no re-base, the only path
    # that runs unless ``allow_history_revision=True``). Trailing field with a
    # default so existing positional/keyword constructions stay valid.
    rebased_days: int = 0


def _ledger_path(state_dir: Path, venue: str) -> Path:
    return Path(state_dir) / venue / "forward_ledger.csv"


def _config_path(state_dir: Path) -> Path:
    return Path(state_dir) / "config.json"


def init_or_check_state(state_dir: Path) -> None:
    """Create the state dir pinned to the frozen config, or verify the pin."""
    state_dir = Path(state_dir)
    cfg_path = _config_path(state_dir)
    if cfg_path.exists():
        stored = json.loads(cfg_path.read_text(encoding="utf-8"))
        if stored.get("config_hash") != frozen_config_hash():
            raise RuntimeError(
                "forward state config hash mismatch: the stored forward ledger was accrued "
                f"under a different frozen configuration ({stored.get('config_hash')} != "
                f"{frozen_config_hash()}). Refusing to mix evidence; archive the state dir "
                "and start a new clock if the change is intentional."
            )
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {"config": FROZEN_FORWARD_CONFIG, "config_hash": frozen_config_hash()},
            indent=2,
        ),
        encoding="utf-8",
    )


def build_full_ledger(
    pieces: dict[str, ContinuousRebalanceComponents],
    hedge_returns: dict[int, float],
    hedge_funding: dict[int, float],
    hedge_returns_2: dict[int, float] | None = None,
    hedge_funding_2: dict[int, float] | None = None,
    hedge_intensity: dict[int, float] | None = None,
) -> pl.DataFrame:
    """Rebuild the full-history hedged ledger from component pieces (frozen params).

    The rebalance/hedge layer is path-dependent from inception (drawdown state,
    beta warm-up), so the canonical forward ledger is always recomputed over the
    FULL component history and then diffed against the stored ledger.

    The single-leg BTC hedge is the default (frozen ``btc_only`` object). Passing
    ``hedge_returns_2``/``hedge_funding_2`` routes the second leg through
    ``apply_rebalance_rule`` (the deployed BTC+ETH 2f object); callers must also
    promote ``FROZEN_FORWARD_CONFIG['hedge']`` to carry ``instrument2`` so the
    config hash and the stamped ``hedge_mode`` reflect the 2f object. Mixing a 2f
    ledger into a ``btc_only`` state dir is caught by the config-hash pin.

    ``hedge_intensity`` (default None -> byte-identical) is a causal
    per-day multiplier on the hedge leg only; the frozen control passes None.
    """
    combined = combine_continuous_components(pieces, FROZEN_FORWARD_CONFIG["weights"])
    return apply_rebalance_rule(
        combined,
        frozen_rebalance_rule(),
        frozen_hedge_rule(),
        hedge_returns,
        hedge_funding,
        hedge_returns_2,
        hedge_funding_2,
        hedge_intensity=hedge_intensity,
    )


def update_forward_ledger(
    state_dir: Path,
    venue: str,
    full_ledger: pl.DataFrame,
    *,
    end_day_ms: int | None = None,
    allow_history_revision: bool = False,
) -> ForwardUpdateResult:
    """Verify overlap against the stored ledger, then append only new days.

    ``full_ledger`` is the freshly rebuilt full-history ledger. Every stored day must
    re-verify (basket_return and equity within OVERLAP_ABS_TOL) — drift means the
    inputs or code changed and the forward evidence chain is broken (hard error).

    ``allow_history_revision`` is the opt-in self-heal path for a LEGITIMATE
    historical data revision (e.g. a late-arriving / backfilled funding or kline
    partition that the operator has confirmed is a real root refresh, not a code
    regression — see STATE.md "late top-ups" and research_summary.md late-arrival
    notes). It is threaded from the orchestrator's ``--allow-history-revision``
    flag and defaults to ``False`` so the default contract is UNCHANGED: any
    overlap drift is a same-code regression alarm and still raises a hard
    ``RuntimeError`` with nothing appended. When ``True``, an overlap day that
    drifts is RE-BASED to the rebuilt value (the rebuilt full-history ledger
    becomes authoritative for the overlap window) and the rewrite is logged at
    WARNING level for the audit trail, instead of raising. A stored day that is
    entirely missing from the rebuilt ledger is never auto-healed (that is a
    coverage loss, not a value revision) and always raises.
    """
    init_or_check_state(Path(state_dir))
    path = _ledger_path(Path(state_dir), venue)
    new = full_ledger if end_day_ms is None else full_ledger.filter(pl.col("ts_ms") <= end_day_ms)

    rebased = 0
    if path.exists():
        stored = pl.read_csv(path)
        # Fail loud on a duplicated/hand-edited ledger rather than silently mis-verifying:
        # a duplicate ts_ms would be compared against a single fresh row and pass (audit-iter6).
        if not stored.is_empty() and stored["ts_ms"].n_unique() != stored.height:
            raise RuntimeError(
                f"{venue}: stored forward ledger has duplicate ts_ms rows — corrupt/partial "
                f"ledger, refusing to verify."
            )
        new_by_day = {int(r["ts_ms"]): r for r in new.to_dicts()}
        drifted_days: list[tuple[int, str, Any, Any]] = []
        for row in stored.to_dicts():
            day = int(row["ts_ms"])
            fresh = new_by_day.get(day)
            if fresh is None:
                # A vanished stored day is a coverage loss, not a value revision,
                # so it is never auto-healed even under allow_history_revision.
                raise RuntimeError(f"{venue}: stored forward day {day} missing from the rebuilt ledger")
            day_drifted = False
            for col in ("basket_return", "equity"):
                rel_tol = EQUITY_REL_TOL if col == "equity" else 0.0
                if not math.isclose(
                    float(row[col]), float(fresh[col]), rel_tol=rel_tol, abs_tol=OVERLAP_ABS_TOL
                ):
                    if not allow_history_revision:
                        raise RuntimeError(
                            f"{venue}: forward-ledger drift on day {day} column {col}: "
                            f"stored {row[col]!r} vs rebuilt {fresh[col]!r}. Same-code regression "
                            "alarm — nothing appended."
                        )
                    drifted_days.append((day, col, row[col], fresh[col]))
                    day_drifted = True
            if day_drifted:
                rebased += 1
        last_stored = int(stored["ts_ms"].max())
        append = new.filter(pl.col("ts_ms") > last_stored)
        verified = stored.height - rebased
        if rebased:
            # Re-base: the rebuilt full-history ledger is authoritative for the
            # overlap window, so the canonical output IS the rebuilt ledger up to
            # the stored horizon plus the new appends — i.e. ``new`` restricted to
            # days <= last appended day. ``new`` already spans stored + appended
            # days, so it is exactly the re-based + extended ledger.
            for day, col, old_val, new_val in drifted_days:
                _logger.warning(
                    "%s: forward-ledger HISTORY REVISION re-base on day %d column %s: "
                    "stored %r -> rebuilt %r (allow_history_revision=True; rewriting stored ledger)",
                    venue, day, col, old_val, new_val,
                )
            _logger.warning(
                "%s: re-based %d stored forward day(s) to a revised data root; "
                "the forward evidence chain now reflects the revised inputs.",
                venue, rebased,
            )
            out = new
        else:
            out = pl.concat([stored.select(new.columns), append]) if append.height else stored.select(new.columns)
    else:
        append = new
        verified = 0
        out = new

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    out.write_csv(path)
    return ForwardUpdateResult(
        venue=venue,
        appended_days=append.height,
        verified_overlap_days=verified,
        total_days=out.height,
        last_day_ms=int(out["ts_ms"].max()) if out.height else None,
        rebased_days=rebased,
    )


def forward_readiness_summary(
    state_dir: Path,
    venue: str,
    *,
    forward_start_ms: int,
) -> dict[str, Any]:
    """Tier-3-facing summary of the accrued forward window (days >= forward_start_ms).

    The forward window starts when the clock was started (first accrual after the
    banking receipts) — days before that are in-sample history carried for state.
    """
    path = _ledger_path(Path(state_dir), venue)
    if not path.exists():
        return {"venue": venue, "forward_days": 0}
    df = pl.read_csv(path).filter(pl.col("ts_ms") >= forward_start_ms).sort("ts_ms")
    if df.is_empty():
        return {"venue": venue, "forward_days": 0}

    # The forward ledger has only OBSERVED rows (the continuous book has flat /
    # no-entry spells, research_summary.md:47), so calendar gaps are expected.
    # Compounding/vol/dd over observed rows alone would drop the zero-return
    # calendar days, collapsing the span out of the return numerator while the
    # year denominator stayed calendar — an inconsistent MAR and an inflated
    # Sharpe (forward-replay-2/6, metrics-3/6). Build a gap-filled calendar series
    # (same construction as the deployed-equity refresh reference,
    # scripts/continuous_deployed_equity_refresh.stats()) so total, dd and Sharpe share the
    # calendar basis. NOTE (audit2): the `years` denominator below deliberately uses
    # the RAW span (no +1), matching the _daily_pnl_metrics / _calendar_metrics
    # annualizer convention — NOT deployed_equity.stats()'s inclusive span+1 — so
    # forward_mar/annualized here differ from deployed_equity by one calendar day on
    # short windows. That divergence is intentional; do NOT "reconcile" it by adding
    # +1, which would shift a Tier-3-facing reported number (operator-gated).
    ts = df["ts_ms"].to_numpy()
    first_ms, last_ms = int(ts[0]), int(ts[-1])
    span_days = int((last_ms - first_ms) // MS_PER_DAY) + 1  # calendar span (inclusive)
    rets_obs = df["basket_return"].to_numpy()
    series = np.zeros(span_days, dtype=float)
    idx = ((ts - first_ms) // MS_PER_DAY).astype(int)
    series[idx] = rets_obs  # scatter observed returns onto the calendar grid

    eq = np.cumprod(1.0 + series)
    dd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
    total = float(eq[-1] - 1.0)
    # Calendar-span years WITHOUT the +1 the gate basis carries — aligns the
    # annualizer denominator with the sibling annualizers (continuous_events
    # ._daily_pnl_metrics, reconciliation._calendar_metrics), which use the raw
    # span and not span+1 (metrics-6).
    years = (last_ms - first_ms) / (ANN_DAYS * MS_PER_DAY) or (1.0 / ANN_DAYS)
    # No measurable drawdown (dd >= 0) -> MAR is a divide-by-~0 artifact, not
    # "infinitely good". Emit JSON null (None) rather than float("inf"), which
    # serialises to the non-strict "Infinity" token and breaks strict JSON
    # readers in the orchestrator/audit pipeline.
    mar = (total / years) / abs(dd) if dd < 0 else None
    # Sharpe on the SAME gap-filled calendar series, through the shared canonical
    # convention (ddof=1, sqrt(365.25)) so it matches the sibling reports
    # (forward-replay-6, metrics-3).
    sharpe = annualized_sharpe(series, ann_days=ANN_DAYS)
    ledger_days = df.height
    return {
        "venue": venue,
        "hedge_mode": frozen_hedge_mode(),
        "hedge_instruments": frozen_hedge_instruments(),
        "hedge_regime": frozen_hedge_regime(),
        # forward_days is the CALENDAR span (inclusive); ledger_days is the count
        # of OBSERVED rows. They differ whenever the book skips days.
        "forward_days": span_days,
        "ledger_days": ledger_days,
        "forward_return_pct": round(total * 100, 2),
        "forward_mar": (round(mar, 2) if mar is not None else None),
        "forward_sharpe": round(sharpe, 2),
        "forward_dd_pct": round(dd * 100, 2),
        # Gate on OBSERVED rows, not calendar span: a 30-day gate must require 30
        # actually-observed days, otherwise coverage gaps could satisfy it on
        # fewer real observations (forward-replay-2).
        "tier3_days_gate_30": ledger_days >= 30,
        # Named for what it tests: positive total return. sign(MAR) == sign(total)
        # when dd < 0, and a positive return with no/negative drawdown is
        # unambiguously MAR > 0, so this is also the Tier-3 "MAR > 0" sign gate;
        # kept return-based so a null MAR (no drawdown) still gates (metrics-5).
        "tier3_return_positive": total > 0,
        "tier3_dd_under_50pct": dd > -0.50,
    }
