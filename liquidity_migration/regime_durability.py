"""B.2 — empirical regime-durability analyzer for the long-only v11a sleeve.

The v11a strategy gates entries on BTC and ETH being above their 30d SMA. What
happens to *held* positions when the regime flips mid-trade is not directly
measured. This module cohorts each trade into three buckets:

  * `fresh_regime`   — entry within `flip_window_days` *after* a regime-off →
                        regime-on flip (the regime has just turned bullish).
  * `held_through_flip` — entry pre-flip, exit post-flip (the trade lived
                        through a regime-on → regime-off transition).
  * `standard`       — neither of the above. The bulk of trades.

For each cohort we report N, mean / median net return, win rate, and a Welch
t-test against the `standard` cohort. The output is a markdown table written
to `<output_dir>/regime_durability_report.md`.

The module reads:
  * a long-native trade ledger (CSV) — same shape as
    `long_native_research/long_native_trades.csv`.
  * BTC and ETH 1h klines from the same root (or any path), reduced to daily
    bars on the close.

The CLI is `python -m liquidity_migration regime-durability ...`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY


# ---- pure helpers ----

@dataclass(frozen=True, slots=True)
class RegimeDurabilityConfig:
    btc_symbol: str = "BTCUSDT"
    eth_symbol: str = "ETHUSDT"
    sma_days: int = 30
    flip_window_days: int = 7


def daily_close_series(klines_1h: pl.DataFrame, *, symbol: str) -> pl.DataFrame:
    """Reduce a 1h kline frame to a daily-close series for a single symbol.

    Returns columns: date (yyyy-mm-dd), close, ts_ms (last-of-day bar end).
    """
    if klines_1h.is_empty():
        return pl.DataFrame(
            {
                "date": pl.Series([], dtype=pl.String),
                "ts_ms": pl.Series([], dtype=pl.Int64),
                "close": pl.Series([], dtype=pl.Float64),
            }
        )
    filt = klines_1h.filter(pl.col("symbol") == symbol)
    if "date" in filt.columns:
        daily = (
            filt.sort(["date", "ts_ms"])
            .group_by("date", maintain_order=True)
            .agg([pl.col("ts_ms").max().alias("ts_ms"), pl.col("close").last().alias("close")])
            .sort("date")
        )
    else:
        daily = (
            filt.sort("ts_ms")
            .with_columns(pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date"))
            .group_by("date", maintain_order=True)
            .agg([pl.col("ts_ms").max().alias("ts_ms"), pl.col("close").last().alias("close")])
            .sort("date")
        )
    return daily


def regime_flips(daily: pl.DataFrame, *, sma_days: int) -> list[dict[str, Any]]:
    """Identify regime-on/off SMA crossings in a daily-close series.

    Returns a list of dicts:
        {"date": "yyyy-mm-dd", "ts_ms": int, "direction": "on"|"off"}
    A row is emitted only when the previous day was on the opposite side of
    the SMA, so the first `sma_days` days are skipped.
    """
    if daily.is_empty() or daily.height < sma_days + 2:
        return []
    df = daily.sort("date").with_columns(
        pl.col("close")
        .rolling_mean(window_size=sma_days, min_samples=sma_days)
        .alias("sma")
    )
    df = df.with_columns(
        (pl.col("close") > pl.col("sma")).alias("above_sma"),
    )
    df = df.with_columns(pl.col("above_sma").shift(1).alias("prev_above"))
    flips = df.filter(
        pl.col("prev_above").is_not_null()
        & (pl.col("above_sma") != pl.col("prev_above"))
    )
    out: list[dict[str, Any]] = []
    for row in flips.iter_rows(named=True):
        out.append(
            {
                "date": row["date"],
                "ts_ms": int(row["ts_ms"]),
                "direction": "on" if row["above_sma"] else "off",
            }
        )
    return out


def cohort_for_trade(
    *,
    entry_ts_ms: int,
    exit_ts_ms: int,
    flips: list[dict[str, Any]],
    flip_window_days: int,
) -> str:
    """Classify a trade based on its lifetime relative to the flip list.

    Order of precedence:
      1. held_through_flip: an "off" flip falls strictly between entry+exit.
      2. fresh_regime: entered within flip_window_days *after* an "on" flip.
      3. standard.
    """
    window_ms = max(0, flip_window_days) * MS_PER_DAY
    for flip in flips:
        ts = flip["ts_ms"]
        if flip["direction"] == "off" and entry_ts_ms < ts <= exit_ts_ms:
            return "held_through_flip"
    if window_ms > 0:
        for flip in flips:
            ts = flip["ts_ms"]
            if flip["direction"] == "on" and ts <= entry_ts_ms <= ts + window_ms:
                return "fresh_regime"
    return "standard"


def _welch_t(a: np.ndarray, b: np.ndarray) -> tuple[float, int]:
    """Welch t-statistic and integer effective dof.

    Returns (0.0, 0) when either sample lacks ≥2 observations or both
    samples have zero variance.
    """
    if a.size < 2 or b.size < 2:
        return 0.0, 0
    mean_a = float(a.mean())
    mean_b = float(b.mean())
    var_a = float(a.var(ddof=1))
    var_b = float(b.var(ddof=1))
    n_a, n_b = int(a.size), int(b.size)
    denom = var_a / n_a + var_b / n_b
    if denom <= 1e-18:
        return 0.0, 0
    t = (mean_a - mean_b) / float(np.sqrt(denom))
    # Welch–Satterthwaite degrees of freedom (rounded to int for reporting only).
    num = (var_a / n_a + var_b / n_b) ** 2
    a_term = (var_a / n_a) ** 2 / max(n_a - 1, 1)
    b_term = (var_b / n_b) ** 2 / max(n_b - 1, 1)
    dof_d = a_term + b_term
    dof = int(round(num / dof_d)) if dof_d > 0 else max(n_a + n_b - 2, 1)
    return float(t), int(dof)


def summarize_cohort(trades: pl.DataFrame, *, cohort: str) -> dict[str, Any]:
    part = trades.filter(pl.col("regime_cohort") == cohort)
    if part.is_empty():
        return {
            "cohort": cohort,
            "trades": 0,
            "mean_net_return": 0.0,
            "median_net_return": 0.0,
            "win_rate": 0.0,
        }
    returns = part["net_return"].to_numpy().astype(float)
    return {
        "cohort": cohort,
        "trades": int(part.height),
        "mean_net_return": float(returns.mean()),
        "median_net_return": float(np.median(returns)),
        "win_rate": float((returns > 0.0).mean()),
    }


# ---- top-level orchestration ----

def run_regime_durability(
    *,
    trades: pl.DataFrame,
    klines_1h: pl.DataFrame,
    output_dir: Path,
    config: RegimeDurabilityConfig | None = None,
) -> dict[str, Any]:
    cfg = config or RegimeDurabilityConfig()
    output_dir.mkdir(parents=True, exist_ok=True)

    if trades.is_empty():
        payload = {
            "config": {
                "btc_symbol": cfg.btc_symbol,
                "eth_symbol": cfg.eth_symbol,
                "sma_days": cfg.sma_days,
                "flip_window_days": cfg.flip_window_days,
            },
            "cohorts": [],
            "tests": [],
            "flips": {"btc": [], "eth": []},
            "trade_count": 0,
        }
        (output_dir / "regime_durability_report.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        (output_dir / "regime_durability_report.md").write_text(
            "# Regime durability — no trades to analyze\n", encoding="utf-8"
        )
        return payload

    btc_daily = daily_close_series(klines_1h, symbol=cfg.btc_symbol)
    eth_daily = daily_close_series(klines_1h, symbol=cfg.eth_symbol)
    btc_flips = regime_flips(btc_daily, sma_days=cfg.sma_days)
    eth_flips = regime_flips(eth_daily, sma_days=cfg.sma_days)

    # Take the union of both venues' flips: a regime flip on either gates
    # the v11a strategy, so durability is sensitive to either.
    flips = sorted(btc_flips + eth_flips, key=lambda f: f["ts_ms"])

    cohorts = [
        cohort_for_trade(
            entry_ts_ms=int(row["entry_ts_ms"]),
            exit_ts_ms=int(row["exit_ts_ms"]),
            flips=flips,
            flip_window_days=cfg.flip_window_days,
        )
        for row in trades.iter_rows(named=True)
    ]
    trades = trades.with_columns(pl.Series("regime_cohort", cohorts))

    summaries = [
        summarize_cohort(trades, cohort=c)
        for c in ("fresh_regime", "held_through_flip", "standard")
    ]

    baseline = trades.filter(pl.col("regime_cohort") == "standard")["net_return"].to_numpy().astype(float)
    tests: list[dict[str, Any]] = []
    for cohort in ("fresh_regime", "held_through_flip"):
        sample = trades.filter(pl.col("regime_cohort") == cohort)["net_return"].to_numpy().astype(float)
        t_stat, dof = _welch_t(sample, baseline)
        tests.append(
            {
                "cohort": cohort,
                "n": int(sample.size),
                "n_baseline": int(baseline.size),
                "t_statistic": t_stat,
                "dof": dof,
            }
        )

    payload = {
        "config": {
            "btc_symbol": cfg.btc_symbol,
            "eth_symbol": cfg.eth_symbol,
            "sma_days": cfg.sma_days,
            "flip_window_days": cfg.flip_window_days,
        },
        "cohorts": summaries,
        "tests": tests,
        "flips": {"btc": btc_flips, "eth": eth_flips},
        "trade_count": int(trades.height),
    }

    (output_dir / "regime_durability_report.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    md_lines = [
        "# Regime durability",
        "",
        f"Trades analysed: {trades.height}",
        f"BTC flips: {len(btc_flips)}  ·  ETH flips: {len(eth_flips)}  "
        f"(SMA={cfg.sma_days}d, flip_window={cfg.flip_window_days}d)",
        "",
        "## Cohort outcomes (net return)",
        "",
        "| Cohort | Trades | Mean | Median | Win rate |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in summaries:
        md_lines.append(
            f"| {s['cohort']} | {s['trades']} | "
            f"{s['mean_net_return']*100:+.2f}% | {s['median_net_return']*100:+.2f}% | "
            f"{s['win_rate']*100:.0f}% |"
        )
    md_lines += [
        "",
        "## Welch t-statistics vs `standard` cohort",
        "",
        "| Cohort | N | N baseline | t-statistic | dof |",
        "|---|---:|---:|---:|---:|",
    ]
    for t in tests:
        md_lines.append(
            f"| {t['cohort']} | {t['n']} | {t['n_baseline']} | "
            f"{t['t_statistic']:+.2f} | {t['dof']} |"
        )
    md_lines.append("")
    (output_dir / "regime_durability_report.md").write_text("\n".join(md_lines), encoding="utf-8")
    return payload


def run_regime_durability_from_paths(
    *,
    trades_csv: str | Path,
    data_root: str | Path,
    output_dir: str | Path,
    config: RegimeDurabilityConfig | None = None,
) -> dict[str, Any]:
    from .storage import read_dataset_columns  # local to avoid import cycle

    trades_path = Path(trades_csv).expanduser()
    if not trades_path.exists():
        raise RuntimeError(f"trades CSV does not exist: {trades_path}")
    trades = pl.read_csv(trades_path)
    required = {"entry_ts_ms", "exit_ts_ms", "net_return"}
    missing = required - set(trades.columns)
    if missing:
        raise RuntimeError(f"trades CSV missing required columns: {sorted(missing)}")
    root = Path(data_root).expanduser()
    klines = read_dataset_columns(
        root, "klines_1h",
        columns=["ts_ms", "symbol", "date", "close"],
    )
    return run_regime_durability(
        trades=trades,
        klines_1h=klines,
        output_dir=Path(output_dir).expanduser(),
        config=config,
    )
