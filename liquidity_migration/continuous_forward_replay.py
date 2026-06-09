"""No-order signal-replay forward collector for the banked continuous object.

Design receipt: docs/preregistration/continuous-forward-clock-spec-2026-06-09.md
(R3, live-readiness program). This module accrues SIGNAL forward evidence for the
frozen winner+hedge configuration by re-running the same code path that produced the
banked receipts and appending only out-of-sample days to a persistent forward ledger.

Safety properties (all tested):
- the frozen configuration is hash-pinned: a state dir created under one config
  refuses to update under another;
- overlap days are re-verified against the stored ledger on every run (any drift is
  a same-code regression alarm -> hard error, nothing appended);
- appends are idempotent (re-running with the same end day adds nothing).

This collector emits NO orders. It is the STATE.md-compliant "no-order paper
evidence collector" for the continuous candidate.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from .continuous_rebalance import (
    ContinuousHedgeRule,
    ContinuousRebalanceComponents,
    ContinuousRebalanceRule,
    apply_rebalance_rule,
    combine_continuous_components,
)

MS_PER_DAY = 86_400_000

# The banked object, frozen. Receipts: continuous-winner-robustness-2026-06-09,
# continuous-hedge-{overlay,engine}-2026-06-09, continuous-walkforward-allocator-2026-06-09
# (frozen-weights policy). Changing ANY value here voids the accrued forward ledger.
FROZEN_FORWARD_CONFIG: dict[str, Any] = {
    "object": "continuous_winner_uptrend_ensemble_btc_hedged",
    "weights": {"turn3p3": 0.30, "turn4p3": 0.20, "turn4p5": 0.40, "age210tp14": 0.10},
    "rebalance": {
        "realized_vol_window_days": 90,
        "target_daily_vol": 0.045,
        "max_scale": 4.0,
        "drawdown_half_threshold": -0.04,
        "drawdown_zero_threshold": None,
        "resize_cost_bps": 10.0,
        "strategy_momentum_window_days": 0,
    },
    "hedge": {
        "instrument": "BTCUSDT",
        "beta_window_days": 90,
        "beta_min_obs": 60,
        "hedge_cap": 2.0,
        "cost_bps": 5.0,
    },
    "inception_day_ms": 1_680_307_200_000,  # 2023-04-01 (ledger history start)
}

OVERLAP_ABS_TOL = 1e-9


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
) -> pl.DataFrame:
    """Rebuild the full-history hedged ledger from component pieces (frozen params).

    The rebalance/hedge layer is path-dependent from inception (drawdown state,
    beta warm-up), so the canonical forward ledger is always recomputed over the
    FULL component history and then diffed against the stored ledger.
    """
    combined = combine_continuous_components(pieces, FROZEN_FORWARD_CONFIG["weights"])
    return apply_rebalance_rule(
        combined,
        frozen_rebalance_rule(),
        frozen_hedge_rule(),
        hedge_returns,
        hedge_funding,
    )


def update_forward_ledger(
    state_dir: Path,
    venue: str,
    full_ledger: pl.DataFrame,
    *,
    end_day_ms: int | None = None,
) -> ForwardUpdateResult:
    """Verify overlap against the stored ledger, then append only new days.

    ``full_ledger`` is the freshly rebuilt full-history ledger. Every stored day must
    re-verify (basket_return and equity within OVERLAP_ABS_TOL) — drift means the
    inputs or code changed and the forward evidence chain is broken (hard error).
    """
    init_or_check_state(Path(state_dir))
    path = _ledger_path(Path(state_dir), venue)
    new = full_ledger if end_day_ms is None else full_ledger.filter(pl.col("ts_ms") <= end_day_ms)

    if path.exists():
        stored = pl.read_csv(path)
        new_by_day = {int(r["ts_ms"]): r for r in new.to_dicts()}
        for row in stored.to_dicts():
            day = int(row["ts_ms"])
            fresh = new_by_day.get(day)
            if fresh is None:
                raise RuntimeError(f"{venue}: stored forward day {day} missing from the rebuilt ledger")
            for col in ("basket_return", "equity"):
                if not math.isclose(float(row[col]), float(fresh[col]), rel_tol=0.0, abs_tol=OVERLAP_ABS_TOL):
                    raise RuntimeError(
                        f"{venue}: forward-ledger drift on day {day} column {col}: "
                        f"stored {row[col]!r} vs rebuilt {fresh[col]!r}. Same-code regression "
                        "alarm — nothing appended."
                    )
        last_stored = int(stored["ts_ms"].max())
        append = new.filter(pl.col("ts_ms") > last_stored)
        verified = stored.height
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
    rets = df["basket_return"].to_numpy()
    eq = (1.0 + pl.Series(rets)).cum_prod().to_numpy()
    peak = pl.Series(eq).cum_max().to_numpy()
    dd = float(min(eq / peak - 1.0))
    total = float(eq[-1] - 1.0)
    span_days = int((int(df["ts_ms"].max()) - int(df["ts_ms"].min())) // MS_PER_DAY) + 1
    years = span_days / 365.25
    mar = (total / years) / abs(dd) if dd < 0 else float("inf")
    std = float(rets.std())
    sharpe = float(rets.mean() / std * (365.25**0.5)) if std > 0 else 0.0
    return {
        "venue": venue,
        "forward_days": span_days,
        "ledger_days": df.height,
        "forward_return_pct": round(total * 100, 2),
        "forward_mar": round(mar, 2),
        "forward_sharpe": round(sharpe, 2),
        "forward_dd_pct": round(dd * 100, 2),
        "tier3_days_gate_30": span_days >= 30,
        "tier3_mar_positive": total > 0,
        "tier3_dd_under_50pct": dd > -0.50,
    }
