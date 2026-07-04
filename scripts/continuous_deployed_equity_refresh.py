#!/usr/bin/env python3
"""Deployed continuous-ensemble equity on the June-extended PIT roots.

Re-runs the EXACT frozen continuous_ensemble_v2 component configs (loaded from the
2026-06-07 source receipts; only `end_date` is overridden) against the
refreshed per-venue full-PIT roots, then reproduces the deployed book:
continuous_ensemble_v2 three-component inverse-vol ensemble with daily rebalance
disabled, retained rebalance params w90/tv0.045/max4/ddh-0.04, and the banked 2f
BTC+ETH hedge. It also writes the official strategy-vs-BTC chart.

IN-SAMPLE RESEARCH refresh (data-boundary extension, zero parameter changes;
the window is spent; forward demo is the arbiter) — not a promotion claim.

    PYTHONIOENCODING=utf-8 POLARS_MAX_THREADS=6 .venv/bin/python \
        scripts/continuous_deployed_equity_refresh.py

`--render-only` re-renders the per-venue PNGs (and summary stats) from the
already-written `continuous_equity*.csv` without re-running components,
hedge, or funding loads — use after a chart-writer fix.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Callable

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.continuous_component_sources import (  # noqa: E402
    CONTINUOUS_COMPONENT_SOURCES,
    ContinuousComponentSource,
    load_continuous_component_source,
)
from liquidity_migration.continuous_btc_risk import (  # noqa: E402
    BTC_RISK_MIN_PRIOR,
    CTRL_BTC_RISK_70_90_35_ID,
    BtcRiskLiveSizer,
    btc_context_by_day,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    frozen_hedge_regime,
    frozen_rebalance_rule,
)
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_event_research,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
    combine_continuous_components,
)
from liquidity_migration.continuous_regime import btcvol_intensity_series  # noqa: E402
from liquidity_migration.storage import resolve_dataset_name  # noqa: E402
from liquidity_migration.volume_events_charts import _write_equity_benchmark_chart  # noqa: E402

SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA"))).expanduser()
PANEL_COLUMNS = ["symbol", "ts_ms", "date", "close", "turnover_quote"]
WINNER_WEIGHTS = dict(FROZEN_FORWARD_CONFIG["weights"])
ANN = 365.25
FUNDING_ROOT = {
    "bybit": SHARED / "bybit_full_pit" / "funding",
    "binance": SHARED / "binance_full_pit" / "binance_usdm_funding",
}
VALID_BTC_TREND_GATES = {"off", "uptrend", "downtrend"}


def winner_rule():
    return frozen_rebalance_rule()


def deployed_hedge_intensity(days: list[int], btc_ret: dict[int, float]) -> dict[int, float] | None:
    regime = frozen_hedge_regime()
    if not regime:
        return None
    return btcvol_intensity_series(days, btc_ret, regime["lam"], regime["vol_window"], regime["pct_window"])


def stats(df: pl.DataFrame) -> dict[str, Any]:
    dates = [dt.datetime.fromtimestamp(t / 1000, tz=dt.timezone.utc).date() for t in df["ts_ms"].to_list()]
    rets = df["basket_return"].to_numpy()
    ncal = (dates[-1] - dates[0]).days + 1
    series = np.zeros(ncal)
    for d, r in zip(dates, rets):
        series[(d - dates[0]).days] = r
    eq = np.cumprod(1.0 + series)
    dd = eq / np.maximum.accumulate(eq) - 1.0
    total = float(eq[-1] - 1.0)
    years = ncal / ANN
    max_dd = float(dd.min())
    mar = round((total / years) / abs(max_dd), 2) if abs(max_dd) > 1e-12 else None
    std = float(series.std(ddof=1)) if series.size > 1 else 0.0
    sharpe = round(float(series.mean()) / std * np.sqrt(ANN), 2) if std > 1e-12 else None
    return {
        "window": f"{dates[0]} -> {dates[-1]}",
        "years": round(years, 2),
        "total_return_pct": round(total * 100, 2),
        "annualized_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "mar": mar,
        "sharpe_daily_ann": sharpe,
        "worst_day_pct": round(float(series.min() * 100), 2),
    }


def config_from_report(path: Path) -> ContinuousEventConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))["config"]
    allowed = {f.name for f in fields(ContinuousEventConfig)}
    kwargs = {key: value for key, value in raw.items() if key in allowed}
    for tuple_key in ("feature_set", "exclude_symbols"):
        value = kwargs.get(tuple_key)
        if isinstance(value, list):
            kwargs[tuple_key] = tuple(value)
    return ContinuousEventConfig(**kwargs)


def _with_optional_component_overrides(
    cfg: ContinuousEventConfig,
    *,
    component_take_profit_pct: float | None = None,
    backtest_leverage: float = 1.0,
) -> ContinuousEventConfig:
    if backtest_leverage <= 0.0:
        raise ValueError("--backtest-leverage must be positive")
    updates: dict[str, Any] = {}
    if component_take_profit_pct is not None:
        updates["take_profit_pct"] = float(component_take_profit_pct)
    if abs(float(backtest_leverage) - 1.0) > 1e-12:
        updates["gross_exposure"] = float(cfg.gross_exposure) * float(backtest_leverage)
    return replace(cfg, **updates) if updates else cfg


def _with_btc_trend_gate(cfg: ContinuousEventConfig, btc_trend_gate: str | None) -> ContinuousEventConfig:
    if btc_trend_gate is None:
        return cfg
    if btc_trend_gate not in VALID_BTC_TREND_GATES:
        raise ValueError(
            f"--btc-trend-gate must be one of {', '.join(sorted(VALID_BTC_TREND_GATES))}; "
            f"got {btc_trend_gate!r}"
        )
    return replace(cfg, btc_trend_gate=btc_trend_gate)


def _combined_config_transform(
    config_transform: Callable[[ContinuousEventConfig], ContinuousEventConfig] | None,
    *,
    btc_trend_gate: str | None,
) -> Callable[[ContinuousEventConfig], ContinuousEventConfig] | None:
    if config_transform is None and btc_trend_gate is None:
        return None

    def transform(cfg: ContinuousEventConfig) -> ContinuousEventConfig:
        if config_transform is not None:
            cfg = config_transform(cfg)
        return _with_btc_trend_gate(cfg, btc_trend_gate)

    return transform


def frozen_config(
    component: str,
    venue: str,
    *,
    end_date: str,
    start_date: str | None = None,
    fallback_root: Path | None = None,
    component_take_profit_pct: float | None = None,
    backtest_leverage: float = 1.0,
) -> ContinuousEventConfig:
    spec = CONTINUOUS_COMPONENT_SOURCES[component]
    report_path = spec.root / venue / spec.cell / "continuous_report.json"
    if not report_path.exists() and fallback_root is not None:
        # The 2026-06-07 one-off receipt dirs were consolidated away (STATE.md: git history
        # is the archive). A prior refresh's component report carries the same frozen config
        # verbatim (its runner overrode only end_date), so it is an equivalent source.
        report_path = fallback_root / "components" / venue / spec.cell / "continuous_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"missing frozen source report: {report_path}")
    cfg = replace(config_from_report(report_path), end_date=end_date)
    if start_date is not None:
        cfg = replace(cfg, start_date=start_date)
    return _with_optional_component_overrides(
        cfg,
        component_take_profit_pct=component_take_profit_pct,
        backtest_leverage=backtest_leverage,
    )


# The frozen feature-panel filename is a CACHE key, not a hard dependency. It was
# only ever a cached daily rollup of the same klines_1h partitions, so when it is
# absent on a root we rebuild it on demand (and cache it back) instead of crashing
# with a FileNotFoundError — the brittleness that blocked both venues on 2026-06-15.
FROZEN_PANEL_NAME = "feature_panel_2026-05-27.parquet"
# Daily-panel build floor: ample warm-up for the 2023-04 continuous inception and
# the trailing-250d regime percentile of trailing-30d BTC vol.
PANEL_BUILD_START = "2022-01-01"


def _klines_daily_agg(root: Path, date_str: str) -> pl.DataFrame | None:
    """One day's (symbol -> close[last], turnover_quote[sum]) rollup from klines_1h."""
    part = root / "klines_1h" / f"date={date_str}"
    if not part.exists():
        return None
    raw = pl.read_parquet(part, columns=["ts_ms", "symbol", "close", "turnover_quote"])
    return (
        raw.sort("ts_ms")
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("close").last(), pl.col("turnover_quote").sum())
        .with_columns(pl.lit(date_str).alias("date"))
        .select(["symbol", "date", "close", "turnover_quote"])
    )


def _daily_panel_from_klines(root: Path, *, start_date: str, end_date: str) -> pl.DataFrame:
    """Build the daily close/turnover panel directly from klines_1h over [start, end).

    This is the on-demand replacement for the frozen feature-panel parquet: the
    frozen file was identical to this rollup (close=last, turnover_quote=sum per
    symbol/date), so building it removes the hard-coded-filename dependency.
    """
    d = dt.date.fromisoformat(start_date)
    boundary = dt.date.fromisoformat(end_date)
    frames = []
    while d < boundary:
        agg = _klines_daily_agg(root, d.isoformat())
        if agg is not None:
            frames.append(agg)
        d += dt.timedelta(days=1)
    if not frames:
        return pl.DataFrame(
            schema={"symbol": pl.String, "date": pl.String, "close": pl.Float64, "turnover_quote": pl.Float64}
        )
    return pl.concat(frames)


def load_extended_panel(venue: str, *, end_date: str, root: Path | None = None) -> pl.DataFrame:
    """Daily close/turnover panel through end_date, robust to a missing frozen file.

    If the cached frozen panel parquet is present, use it + a kline-partition tail
    from its last date + 1 through end_date (the fast path). If it is ABSENT, build
    the whole panel from the klines_1h partitions (the same rollup the frozen file
    held) and cache it back so later runs hit the fast path — no more hard
    FileNotFoundError on a root that simply never received the frozen snapshot.
    """
    root = root if root is not None else SHARED / f"{venue}_full_pit"
    boundary = dt.date.fromisoformat(end_date)
    frozen = root / FROZEN_PANEL_NAME
    dups = 0
    fp = pl.read_parquet(frozen, columns=PANEL_COLUMNS) if frozen.exists() else None
    if fp is not None:
        # Clip the cached panel to the requested end_date (exclusive): a frozen panel
        # built to a LATER date must not leak rows past --end-date (audit-iter3).
        fp = fp.filter(pl.col("date") < boundary.isoformat())
    if fp is not None and not fp.is_empty():
        n_before = fp.height
        fp = fp.sort("ts_ms").group_by(["symbol", "date"], maintain_order=True).last()
        dups = n_before - fp.height
        frames = [fp.select(["symbol", "date", "close", "turnover_quote"])]
        panel_end = dt.date.fromisoformat(str(fp["date"].max()))
        tail_frames = []
        d = panel_end + dt.timedelta(days=1)
        while d < boundary:
            agg = _klines_daily_agg(root, d.isoformat())
            if agg is not None:
                tail_frames.append(agg)
            d += dt.timedelta(days=1)
        if tail_frames:
            frames.append(pl.concat(tail_frames))
        panel = pl.concat(frames).sort(["symbol", "date"])
        source = "frozen+tail"
    else:
        panel = _daily_panel_from_klines(root, start_date=PANEL_BUILD_START, end_date=end_date).sort(
            ["symbol", "date"]
        )
        source = "klines"
        # Cache it as the frozen snapshot (close=last/turnover=sum, with a ts_ms per
        # row) so subsequent runs take the fast path. Best-effort: a read-only root
        # just keeps rebuilding.
        try:
            cache = panel.with_columns(
                (pl.col("date").str.to_date().cast(pl.Datetime("ms")).dt.timestamp("ms")).alias("ts_ms")
            ).select(PANEL_COLUMNS)
            cache.write_parquet(frozen)
        except Exception as exc:  # noqa: BLE001 - caching is best-effort, never fatal
            print(f"[{venue}] WARN: could not cache rebuilt panel to {frozen}: {exc}", flush=True)
    panel = panel.with_columns(pl.col("date").str.to_date().alias("d"))
    print(
        f"[{venue}] daily panel rows={panel.height} source={source} dup_ts_rows_collapsed={dups} "
        f"dates {panel['date'].min()}..{panel['date'].max()}",
        flush=True,
    )
    return panel


def pad_flat_tail(df: pl.DataFrame, *, through_date: dt.date) -> pl.DataFrame:
    """Extend the rebalance frame through `through_date` with flat (zero-return) days.

    This pad happens strictly AFTER `apply_rebalance_rule` — the right layer. The component
    mtm CSVs keep ledger-day rows on purpose (see `_portfolio_mtm_equity`: calendar-filling
    the rebalance INPUT dilutes the vol window and fabricates hedge PnL on flat days), so the
    book's closing flat spell is added here, where it can no longer affect sizing or hedging.
    A flat book IS a position state. Interior gaps are already zero-filled by `stats`.
    """
    if df.is_empty():
        return df
    ms_per_day = 86_400_000
    last_ts = int(df["ts_ms"].max())
    last_date = dt.datetime.fromtimestamp(last_ts / 1000, tz=dt.timezone.utc).date()
    n_days = (through_date - last_date).days
    if n_days <= 0:
        return df
    last_equity = float(df.sort("ts_ms")["equity"].tail(1)[0])
    pad = pl.DataFrame(
        {
            "ts_ms": [last_ts + (i + 1) * ms_per_day for i in range(n_days)],
            "basket_return": [0.0] * n_days,
            "equity": [last_equity] * n_days,
        }
    )
    return pl.concat([df, pad], how="diagonal").sort("ts_ms")


def funding_root(venue: str, data_root: Path | None = None) -> Path:
    if data_root is None:
        return FUNDING_ROOT[venue]
    return data_root / resolve_dataset_name(data_root, "funding")


def instrument_inputs(
    venue: str,
    days: list[int],
    symbol: str,
    panel: pl.DataFrame,
    *,
    data_root: Path | None = None,
) -> tuple[dict[int, float], dict[int, float]]:
    p = (
        panel.filter(pl.col("symbol") == symbol).sort("d")
        .with_columns(pl.col("close").shift(1).alias("prev"), pl.col("d").shift(1).alias("d_prev"))
        .filter(((pl.col("d") - pl.col("d_prev")).dt.total_days() == 1) & (pl.col("prev") > 0))
        .with_columns((pl.col("close") / pl.col("prev") - 1.0).alias("ret"))
    )
    rets = {
        int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc).timestamp() * 1000): float(r)
        for d, r in p.select(["d", "ret"]).iter_rows()
    }
    fund: dict[int, float] = {}
    root = funding_root(venue, data_root)
    missing = 0
    for day in days:
        date = dt.datetime.fromtimestamp(day / 1000, tz=dt.timezone.utc).date().isoformat()
        part = root / f"date={date}" / f"symbol={symbol}"
        if part.exists():
            fund[day] = float(pl.read_parquet(part, columns=["funding_rate"])["funding_rate"].sum())
        else:
            missing += 1
    # Surface funding-coverage gaps: a missing partition silently defaults to zero
    # funding for the hedge legs, which a wholly-missing/mis-resolved root would hide
    # (audit-iter3 backlog scripts). Diagnostic only — no numeric change.
    if missing:
        print(f"[{venue}] {symbol} funding coverage: {len(days) - missing}/{len(days)} days; "
              f"{missing} defaulted to zero", flush=True)
    return rets, fund


def component_report_matches_window(
    payload: dict[str, Any],
    *,
    start_date: str | None,
    end_date: str,
    component_take_profit_pct: float | None = None,
    expected_gross_exposure: float | None = None,
    expected_config_hash: str | None = None,
) -> bool:
    if expected_config_hash is not None and str(payload.get("config_hash")) != expected_config_hash:
        return False
    cfg = payload.get("config") or {}
    if str(cfg.get("end_date")) != end_date:
        return False
    if start_date is not None and str(cfg.get("start_date")) != start_date:
        return False
    if component_take_profit_pct is not None:
        if abs(float(cfg.get("take_profit_pct") or 0.0) - float(component_take_profit_pct)) > 1e-12:
            return False
    if expected_gross_exposure is not None:
        if abs(float(cfg.get("gross_exposure") or 0.0) - float(expected_gross_exposure)) > 1e-12:
            return False
    return True


def _btc_klines(root: Path) -> pl.DataFrame:
    return (
        pl.scan_parquet(str(root / "klines_1h" / "**" / "*.parquet"))
        .filter(pl.col("symbol") == "BTCUSDT")
        .select("ts_ms", "close")
        .collect()
        .sort("ts_ms")
    )


def _component_decision_rows(
    *,
    output_root: Path,
    venue: str,
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], int]]:
    component_count: dict[tuple[str, int], int] = defaultdict(int)
    rows_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for component in WINNER_WEIGHTS:
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        trades_path = output_root / "components" / venue / spec.cell / "continuous_trades.csv"
        if not trades_path.exists():
            continue
        trades = pl.read_csv(trades_path)
        if trades.is_empty():
            continue
        for row in trades.select("symbol", "entry_signal_ts_ms").iter_rows(named=True):
            symbol = str(row["symbol"])
            signal_ts = int(row["entry_signal_ts_ms"])
            key = (symbol, signal_ts)
            component_count[key] += 1
            rows_by_key.setdefault(key, {"symbol": symbol, "signal_ts_ms": signal_ts})
    rows = sorted(rows_by_key.values(), key=lambda r: (int(r["signal_ts_ms"]), str(r["symbol"])))
    return rows, component_count


def btc_risk_size_lookup(
    venue: str,
    *,
    decision_component_root: Path,
    output_root: Path,
    data_root: Path,
) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    """Score CTRL_BTC_RISK_70_90_35 chronologically across ensemble decisions."""
    decisions, component_count = _component_decision_rows(output_root=decision_component_root, venue=venue)
    context = btc_context_by_day(_btc_klines(data_root))
    sizer = BtcRiskLiveSizer(
        output_root / "btc_risk" / venue / "btc_risk_sizing_state.parquet",
        low=0.70,
        high=0.90,
        tail_mult=0.35,
        min_prior=BTC_RISK_MIN_PRIOR,
    )
    by_ts: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in decisions:
        by_ts[int(row["signal_ts_ms"])].append(row)

    rows: list[dict[str, Any]] = []
    lookup: dict[tuple[str, int], float] = {}
    stack_values: list[float] = []
    tail_selected = 0
    warmup = 0
    for signal_ts in sorted(by_ts):
        scored, _cycle = sizer.score_decisions(by_ts[signal_ts], btc_context=context)
        for (symbol, ts), score in sorted(scored.items(), key=lambda item: (item[0][1], item[0][0])):
            mult = float(score.get("stack_mult", 1.0))
            lookup[(symbol, int(ts))] = mult
            stack_values.append(mult)
            tail_selected += int(bool(score.get("tail_selected")))
            warmup += int(bool(score.get("score_warmup")))
            rows.append(
                {
                    "venue": venue,
                    "symbol": symbol,
                    "signal_ts_ms": int(ts),
                    "signal_date": dt.datetime.fromtimestamp(int(ts) / 1000, tz=dt.timezone.utc).date().isoformat(),
                    "component_count": component_count.get((symbol, int(ts)), 0),
                    "btc_risk_score": score.get("btc_risk_score"),
                    "score_warmup": bool(score.get("score_warmup")),
                    "tail_selected": bool(score.get("tail_selected")),
                    "stack_mult": mult,
                    "prior_decision_count": score.get("prior_decision_count"),
                }
            )
    sizer.save()
    out_csv = output_root / "btc_risk" / venue / "btc_risk_multipliers.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if rows:
        pl.DataFrame(rows).write_csv(out_csv)
    else:
        out_csv.write_text("", encoding="utf-8")
    return lookup, {
        "arm_id": CTRL_BTC_RISK_70_90_35_ID,
        "decision_keys": len(decisions),
        "tail_selected": tail_selected,
        "warmup": warmup,
        "min_stack_mult": float(min(stack_values)) if stack_values else 1.0,
        "mean_stack_mult": float(sum(stack_values) / len(stack_values)) if stack_values else 1.0,
        "multipliers_csv": str(out_csv),
    }


def load_btc_risk_size_lookup(root: Path, venue: str) -> tuple[dict[tuple[str, int], float], dict[str, Any]]:
    path = root / "btc_risk" / venue / "btc_risk_multipliers.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing BTC-risk multiplier cache: {path}")
    df = pl.read_csv(path)
    lookup = {
        (str(row["symbol"]), int(row["signal_ts_ms"])): float(row["stack_mult"])
        for row in df.to_dicts()
    }
    return lookup, {
        "arm_id": CTRL_BTC_RISK_70_90_35_ID,
        "decision_keys": len(lookup),
        "multipliers_csv": str(path),
        "reused_from": str(root),
    }


def monthly_trade_counts(*, output_root: Path, venue: str) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for component in WINNER_WEIGHTS:
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        trades_path = output_root / "components" / venue / spec.cell / "continuous_trades.csv"
        if not trades_path.exists():
            continue
        trades = pl.read_csv(trades_path)
        required = {"entry_ts_ms", "symbol", "side"}
        if trades.is_empty() or not required.issubset(set(trades.columns)):
            continue
        frames.append(
            trades.select(["entry_ts_ms", "symbol", "side"])
            .with_columns(pl.from_epoch(pl.col("entry_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
            .select(["month", "entry_ts_ms", "symbol", "side"])
        )
    if not frames:
        return pl.DataFrame({"month": pl.Series([], dtype=pl.String), "trades": pl.Series([], dtype=pl.Int64)})
    return (
        pl.concat(frames, how="vertical")
        .unique(subset=["entry_ts_ms", "symbol", "side"])
        .group_by("month")
        .agg(pl.len().alias("trades"))
        .sort("month")
    )


def monthly_returns_with_trades(equity: pl.DataFrame, trade_counts: pl.DataFrame) -> pl.DataFrame:
    empty = pl.DataFrame(
        {
            "month": pl.Series([], dtype=pl.String),
            "strategy_return": pl.Series([], dtype=pl.Float64),
            "trades": pl.Series([], dtype=pl.Int64),
        }
    )
    if equity.is_empty() or not {"date", "basket_return"}.issubset(set(equity.columns)):
        return empty
    monthly = (
        equity.with_columns(pl.col("date").cast(pl.Utf8).str.slice(0, 7).alias("month"))
        .group_by("month")
        .agg(((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"))
        .sort("month")
    )
    if trade_counts.is_empty():
        return monthly.with_columns(pl.lit(0, dtype=pl.Int64).alias("trades"))
    return (
        monthly.join(trade_counts, on="month", how="left")
        .with_columns(pl.col("trades").fill_null(0).cast(pl.Int64))
        .select(["month", "strategy_return", "trades"])
        .sort("month")
    )


def chart_leverages(extra_leverage: float | None) -> tuple[float, ...]:
    values = [1.0]
    if extra_leverage is not None and abs(float(extra_leverage) - 1.0) > 1e-12:
        if extra_leverage <= 0.0:
            raise ValueError("--chart-leverage must be positive")
        values.append(float(extra_leverage))
    return tuple(values)


def _component_report_payloads(output_root: Path, venue: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    components_root = output_root / "components" / venue
    for report_path in sorted(components_root.glob("*/continuous_report.json")):
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        payload["_component"] = report_path.parent.name
        payload["_report_path"] = str(report_path)
        payloads.append(payload)
    return payloads


def _component_report_rows(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for payload in payloads:
        cfg = payload.get("config") or {}
        metrics = ((payload.get("metrics") or {}).get("full") or {})
        rows.append(
            {
                "component": payload.get("_component"),
                "config_hash": payload.get("config_hash"),
                "trades": payload.get("n_trades"),
                "funding_mode": payload.get("funding_mode"),
                "take_profit_pct": cfg.get("take_profit_pct"),
                "btc_trend_gate": cfg.get("btc_trend_gate"),
                "gross_exposure": cfg.get("gross_exposure"),
                "total_return_pct": None
                if metrics.get("total_return") is None
                else round(float(metrics["total_return"]) * 100.0, 2),
                "max_drawdown_pct": None
                if metrics.get("max_drawdown") is None
                else round(float(metrics["max_drawdown"]) * 100.0, 2),
            }
        )
    return rows


def write_continuous_equity_report(
    *,
    venue: str,
    output_root: Path,
    out_dir: Path,
    data_root: Path,
    end_date: str,
    start_date: str | None,
    venue_summary: dict[str, Any],
    df: pl.DataFrame,
    chart_leverage: float | None,
    component_take_profit_pct: float | None,
    btc_risk_sizing: bool,
    backtest_leverage: float,
) -> None:
    payloads = _component_report_payloads(output_root, venue)
    component_rows = _component_report_rows(payloads)
    first_payload = payloads[0] if payloads else {}
    first_cfg = first_payload.get("config") or {}
    stats_1x = venue_summary.get("1x") or stats(df)
    inferred_tp = component_take_profit_pct
    if inferred_tp is None and first_cfg.get("take_profit_pct") is not None:
        inferred_tp = float(first_cfg["take_profit_pct"])
    btc_trend_gate = first_cfg.get("btc_trend_gate")
    funding_modes = sorted({str(row.get("funding_mode")) for row in component_rows if row.get("funding_mode")})
    final_equity = None if df.is_empty() else float(df["equity"][-1])
    summary = {
        "run_label": "exploratory",
        "strategy_run_label": "continuous_demo_paper_research_stage",
        "venue": venue,
        "data_root": str(data_root),
        "output_dir": str(out_dir),
        "start_date": start_date,
        "end_date": end_date,
        "window": stats_1x.get("window"),
        "component_take_profit_pct": inferred_tp,
        "btc_trend_gate": btc_trend_gate,
        "btc_risk_sizing": btc_risk_sizing,
        "backtest_leverage": backtest_leverage,
        "chart_leverage": chart_leverage,
        "final_equity": final_equity,
        "stats": stats_1x,
        "funding_modes": funding_modes,
        "cost_model": {
            "taker_fee_bps": first_cfg.get("taker_fee_bps"),
            "spread_bps": first_cfg.get("spread_bps"),
            "impact_coef_bps": first_cfg.get("impact_coef_bps"),
            "impact_exponent": first_cfg.get("impact_exponent"),
            "use_funding": first_cfg.get("use_funding"),
            "funding_modes": funding_modes,
        },
        "components": component_rows,
        "artifacts": {
            "equity_curve": str(out_dir / "continuous_equity.csv"),
            "monthly": str(out_dir / "continuous_monthly.csv"),
            "png": str(out_dir / "continuous_equity_btc.png"),
            "btc_risk_multipliers": str(output_root / "btc_risk" / venue / "btc_risk_multipliers.csv"),
        },
    }
    (out_dir / "continuous_equity_summary.json").write_text(
        json.dumps(summary, indent=2, default=str),
        encoding="utf-8",
    )

    cost = summary["cost_model"]
    rows_md = [
        "| Component | Trades | Funding | TP | Gross Exposure | Return | Max DD |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for row in component_rows:
        rows_md.append(
            "| {component} | {trades} | {funding_mode} | {tp} | {gross} | {ret} | {dd} |".format(
                component=row.get("component") or "-",
                trades=row.get("trades") if row.get("trades") is not None else "-",
                funding_mode=row.get("funding_mode") or "-",
                tp="-" if row.get("take_profit_pct") is None else f"{float(row['take_profit_pct']):.4g}",
                gross="-" if row.get("gross_exposure") is None else f"{float(row['gross_exposure']):.4g}",
                ret="-" if row.get("total_return_pct") is None else f"{float(row['total_return_pct']):.2f}%",
                dd="-" if row.get("max_drawdown_pct") is None else f"{float(row['max_drawdown_pct']):.2f}%",
            )
        )
    def fmt_stat(key: str, suffix: str = "") -> str:
        value = stats_1x.get(key)
        return "n/a" if value is None else f"{float(value):.2f}{suffix}"

    report = "\n".join(
        [
            "# Continuous Equity Report",
            "",
            "Run label: exploratory",
            "Strategy run label: continuous_demo_paper_research_stage",
            f"Venue: {venue}",
            f"Data root: {data_root}",
            f"Window: {stats_1x.get('window')}",
            f"End date: {end_date}",
            "",
            "## Method",
            "",
            (
                "Official equity-curve runner: scripts/equity_curves.sh -> "
                "scripts/equity_curves.py -> scripts/continuous_deployed_equity_refresh.py."
            ),
            f"Component TP: {inferred_tp}",
            f"BTC trend gate: {btc_trend_gate}",
            f"BTC-risk sizing: {btc_risk_sizing}",
            f"Modeled backtest leverage: {backtest_leverage:g}x",
            f"Chart leverage: {chart_leverage:g}x" if chart_leverage is not None else "Chart leverage: none",
            "",
            "## Metrics",
            "",
            f"Final equity: {final_equity:.10f}" if final_equity is not None else "Final equity: n/a",
            f"Total return: {fmt_stat('total_return_pct', '%')}",
            f"Annualized return: {fmt_stat('annualized_pct', '%')}",
            f"Max drawdown: {fmt_stat('max_drawdown_pct', '%')}",
            f"MAR: {fmt_stat('mar')}",
            f"Sharpe daily annualized: {fmt_stat('sharpe_daily_ann')}",
            f"Worst day: {fmt_stat('worst_day_pct', '%')}",
            "",
            "## Cost Model",
            "",
            f"Taker fee bps: {cost.get('taker_fee_bps')}",
            f"Spread bps: {cost.get('spread_bps')}",
            f"Impact coef bps: {cost.get('impact_coef_bps')}",
            f"Impact exponent: {cost.get('impact_exponent')}",
            f"Funding modeled: {cost.get('use_funding')}",
            f"Funding modes: {', '.join(funding_modes) if funding_modes else 'unknown'}",
            "",
            "## Split And OOS",
            "",
            (
                "Split metrics: component reports retain their configured split_date and full/early/recent "
                "metrics where available; this equity curve is a diagnostic portfolio render, not a new "
                "promotion split study."
            ),
            (
                "OOS window: none for this run. Continuous remains demo/paper research-stage; forward demo/paper "
                "is the decision surface."
            ),
            "",
            "## Components",
            "",
            *rows_md,
            "",
            "## Artifacts",
            "",
            f"Equity curve: {out_dir / 'continuous_equity.csv'}",
            f"Monthly: {out_dir / 'continuous_monthly.csv'}",
            f"PNG: {out_dir / 'continuous_equity_btc.png'}",
            f"Summary JSON: {out_dir / 'continuous_equity_summary.json'}",
            f"BTC-risk multipliers: {output_root / 'btc_risk' / venue / 'btc_risk_multipliers.csv'}",
            "",
        ]
    )
    (out_dir / "continuous_equity_report.md").write_text(report, encoding="utf-8")


def run_components(
    venue: str,
    *,
    output_root: Path,
    end_date: str,
    start_date: str | None = None,
    frozen_fallback: Path | None = None,
    data_root: Path | None = None,
    component_take_profit_pct: float | None = None,
    backtest_leverage: float = 1.0,
    size_mult_lookup: dict[tuple[str, int], float] | None = None,
    config_transform: Callable[[ContinuousEventConfig], ContinuousEventConfig] | None = None,
    write_candidate_tape: bool = False,
    write_symbol_events: bool = False,
) -> dict[str, Any]:
    data_root = data_root if data_root is not None else SHARED / f"{venue}_full_pit"
    meta: dict[str, Any] = {}
    for component in WINNER_WEIGHTS:
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        cell_dir = output_root / "components" / venue / spec.cell
        report_path = cell_dir / "continuous_report.json"
        cfg = frozen_config(
            component,
            venue,
            end_date=end_date,
            start_date=start_date,
            fallback_root=frozen_fallback,
            component_take_profit_pct=component_take_profit_pct,
            backtest_leverage=backtest_leverage,
        )
        if config_transform is not None:
            cfg = config_transform(cfg)
        candidate_tape_path = cell_dir / "candidate_tape.parquet" if write_candidate_tape else None
        symbol_event_path = cell_dir / "symbol_quarantine_events.csv" if write_symbol_events else None
        t0 = time.time()
        if report_path.exists() and size_mult_lookup is None:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            resumed = component_report_matches_window(
                payload,
                start_date=start_date,
                end_date=end_date,
                component_take_profit_pct=component_take_profit_pct,
                expected_gross_exposure=float(cfg.gross_exposure),
                expected_config_hash=cfg.config_hash(),
            )
            if candidate_tape_path is not None and not candidate_tape_path.exists():
                resumed = False
            if symbol_event_path is not None and not symbol_event_path.exists():
                resumed = False
            if not resumed:
                payload = run_continuous_event_research(
                    data_root,
                    config=cfg,
                    report_dir=cell_dir,
                    candidate_tape_path=candidate_tape_path,
                    symbol_event_path=symbol_event_path,
                )
        else:
            payload = run_continuous_event_research(
                data_root,
                config=cfg,
                report_dir=cell_dir,
                size_mult_lookup=size_mult_lookup,
                candidate_tape_path=candidate_tape_path,
                symbol_event_path=symbol_event_path,
            )
            resumed = False
        meta[component] = {
            "config_hash": payload["config_hash"],
            "n_trades": payload["n_trades"],
            "funding_mode": payload["funding_mode"],
            "skips": payload.get("skips"),
            "resumed": resumed,
            "seconds": round(time.time() - t0, 1),
        }
        print(
            f"[{venue}] component {component}: trades={payload['n_trades']} "
            f"funding={payload['funding_mode']} resumed={resumed}",
            flush=True,
        )
    return meta


def render_curves(
    venue: str,
    *,
    out_dir: Path,
    df: pl.DataFrame,
    raw_klines: pl.DataFrame,
    end_date: str,
    venue_summary: dict[str, Any],
    monthly_trades: pl.DataFrame,
    leverage: float | None = 4.0,
    backtest_leverage: float = 1.0,
    btc_risk_sizing: bool = False,
    btc_trend_gate: str | None = None,
) -> None:
    for mult in chart_leverages(leverage):
        tag = "" if mult == 1.0 else f"_{mult:g}x"
        mdf = df
        if mult != 1.0:
            rets = mdf["basket_return"].fill_null(0.0).to_numpy() * mult
            eq = np.cumprod(1.0 + rets)
            mdf = mdf.with_columns(pl.Series("basket_return", rets), pl.Series("equity", eq))
        chart_stats = stats(mdf)
        chart_metrics = {
            **chart_stats,
            "final_equity": None if mdf.is_empty() else float(mdf["equity"][-1]),
        }
        equity = mdf.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().cast(pl.String).alias("date")
        )
        equity.write_csv(out_dir / f"continuous_equity{tag}.csv")
        monthly = monthly_returns_with_trades(equity, monthly_trades)
        monthly.write_csv(out_dir / f"continuous_monthly{tag}.csv")
        if abs(float(backtest_leverage) - 1.0) > 1e-12:
            name = (
                f"Continuous {backtest_leverage:g}x"
                if mult == 1.0
                else f"Continuous {backtest_leverage:g}x / {mult:g}x chart"
            )
        else:
            name = "Continuous" if mult == 1.0 else f"Continuous {mult:g}x chart"
        sub = "IN-SAMPLE RESEARCH refresh to June 2026 data (window spent; forward demo is the arbiter) — not a promotion claim"
        if abs(float(backtest_leverage) - 1.0) > 1e-12:
            sub += f" | modeled backtest leverage {backtest_leverage:g}x: component notional/costs/funding and hedge cap scaled"
        if btc_trend_gate is not None:
            sub += f" | BTC trend gate {btc_trend_gate}"
        if mult != 1.0:
            sub += f" | {mult:g}x = pure leverage on the same returns; margin/liquidation NOT modeled"
        title_modeled = "" if abs(float(backtest_leverage) - 1.0) <= 1e-12 else f", modeled {backtest_leverage:g}x"
        title_chart = "" if mult == 1.0 else f", {mult:g}x chart"
        meta = _write_equity_benchmark_chart(
            out_dir,
            root=out_dir,
            equity=equity,
            raw_klines=raw_klines,
            monthly=monthly,
            png_name=f"continuous_equity_btc{tag}.png",
            title=(
                "CONTINUOUS deployed - continuous_ensemble_v2 ensemble + 2f hedge"
                f"{title_modeled}{title_chart} [{venue}] - refreshed to {end_date}"
            ),
            subtitle=sub,
            strategy_name=name,
            metrics=chart_metrics,
        )
        summary_key = f"{mult:g}x"
        venue_summary[summary_key] = chart_stats
        log_label = summary_key
        if abs(float(backtest_leverage) - 1.0) > 1e-12:
            log_label = f"modeled {backtest_leverage:g}x / chart {summary_key}"
        print(f"[{venue}] {log_label} {json.dumps(venue_summary[summary_key])}", flush=True)
        print(f"[{venue}] png: {out_dir / f'continuous_equity_btc{tag}.png'} ({'ok' if meta else 'CHART FAILED'})", flush=True)


def run_venue(
    venue: str,
    *,
    output_root: Path,
    end_date: str,
    start_date: str | None = None,
    render_only: bool = False,
    frozen_fallback: Path | None = None,
    data_root: Path | None = None,
    chart_leverage: float | None = 4.0,
    component_take_profit_pct: float | None = None,
    btc_risk_sizing: bool = False,
    backtest_leverage: float = 1.0,
    btc_trend_gate: str | None = None,
    config_transform: Callable[[ContinuousEventConfig], ContinuousEventConfig] | None = None,
    write_candidate_tape: bool = False,
    write_symbol_events: bool = False,
    btc_risk_lookup_root: Path | None = None,
) -> dict[str, Any]:
    out_dir = output_root / venue
    out_dir.mkdir(parents=True, exist_ok=True)
    effective_config_transform = _combined_config_transform(config_transform, btc_trend_gate=btc_trend_gate)
    if render_only:
        if btc_trend_gate is not None:
            raise ValueError("--render-only cannot apply --btc-trend-gate; rerun components")
        csv_path = out_dir / "continuous_equity.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"--render-only needs an existing {csv_path}")
        df = pl.read_csv(csv_path).select(["ts_ms", "basket_return", "equity"])
        panel = load_extended_panel(venue, end_date=end_date, root=data_root)
        venue_summary: dict[str, Any] = {}
    else:
        size_lookup = None
        btc_risk_meta = None
        if btc_risk_sizing:
            if btc_risk_lookup_root is not None:
                size_lookup, btc_risk_meta = load_btc_risk_size_lookup(btc_risk_lookup_root, venue)
            else:
                decision_root = output_root / "_btc_risk_decision_components"
                run_components(
                    venue,
                    output_root=decision_root,
                    end_date=end_date,
                    start_date=start_date,
                    frozen_fallback=frozen_fallback,
                    data_root=data_root,
                    component_take_profit_pct=component_take_profit_pct,
                    backtest_leverage=1.0,
                    config_transform=effective_config_transform,
                )
                root_for_context = data_root if data_root is not None else SHARED / f"{venue}_full_pit"
                size_lookup, btc_risk_meta = btc_risk_size_lookup(
                    venue,
                    decision_component_root=decision_root,
                    output_root=output_root,
                    data_root=root_for_context,
                )
        component_meta = run_components(
            venue,
            output_root=output_root,
            end_date=end_date,
            start_date=start_date,
            frozen_fallback=frozen_fallback,
            data_root=data_root,
            component_take_profit_pct=component_take_profit_pct,
            backtest_leverage=backtest_leverage,
            size_mult_lookup=size_lookup,
            config_transform=effective_config_transform,
            write_candidate_tape=write_candidate_tape,
            write_symbol_events=write_symbol_events,
        )
        pieces = {}
        for component in WINNER_WEIGHTS:
            spec = CONTINUOUS_COMPONENT_SOURCES[component]
            refreshed = ContinuousComponentSource(output_root / "components", spec.cell)
            comp, _n, _cfg = load_continuous_component_source(refreshed, venue)
            pieces[component] = comp
        combined = combine_continuous_components(pieces, WINNER_WEIGHTS)
        panel = load_extended_panel(venue, end_date=end_date, root=data_root)
        btc_ret, btc_fund = instrument_inputs(venue, combined.days, "BTCUSDT", panel, data_root=data_root)
        eth_ret, eth_fund = instrument_inputs(venue, combined.days, "ETHUSDT", panel, data_root=data_root)
        df = apply_rebalance_rule(
            combined, winner_rule(), ContinuousHedgeRule(90, 60, 2.0 * float(backtest_leverage), 5.0),
            btc_ret, btc_fund, eth_ret, eth_fund,
            hedge_intensity=deployed_hedge_intensity(combined.days, btc_ret),
        )
        panel_last = dt.date.fromisoformat(str(panel["date"].max()))
        df = pad_flat_tail(df, through_date=panel_last)
        venue_summary = {
            "components": component_meta,
            "component_take_profit_pct": component_take_profit_pct,
            "btc_risk_sizing": btc_risk_meta,
            "backtest_leverage": backtest_leverage,
        }
    raw_klines = (
        panel.filter(pl.col("symbol") == "BTCUSDT")
        .with_columns(
            pl.col("d").cast(pl.String).alias("date"),
            (pl.col("d").cast(pl.Datetime(time_unit="ms", time_zone="UTC")).dt.epoch("ms")).alias("ts_ms"),
        )
        .select(["symbol", "date", "ts_ms", "close"])
    )
    render_curves(
        venue, out_dir=out_dir, df=df, raw_klines=raw_klines,
        end_date=end_date, venue_summary=venue_summary,
        monthly_trades=monthly_trade_counts(output_root=output_root, venue=venue),
        leverage=chart_leverage,
        backtest_leverage=backtest_leverage,
        btc_risk_sizing=btc_risk_sizing,
        btc_trend_gate=btc_trend_gate,
    )
    write_continuous_equity_report(
        venue=venue,
        output_root=output_root,
        out_dir=out_dir,
        data_root=data_root if data_root is not None else SHARED / f"{venue}_full_pit",
        end_date=end_date,
        start_date=start_date,
        venue_summary=venue_summary,
        df=df,
        chart_leverage=chart_leverage,
        component_take_profit_pct=component_take_profit_pct,
        btc_risk_sizing=btc_risk_sizing,
        backtest_leverage=backtest_leverage,
    )
    return venue_summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", default="2026-06-12", help="engine end boundary (exclusive at date midnight UTC)")
    parser.add_argument(
        "--start-date",
        default=None,
        help="Optional engine start boundary. Omit to preserve each frozen component's original start.",
    )
    parser.add_argument("--output-root", default=str(SHARED / "continuous_deployed_equity_refresh_2026-06-12"))
    parser.add_argument("--venues", nargs="+", default=["bybit", "binance"])
    parser.add_argument(
        "--data-root",
        default=None,
        help="Optional per-venue full-PIT root override; requires exactly one --venues entry.",
    )
    parser.add_argument(
        "--render-only", action="store_true",
        help="re-render PNGs + stats from existing per-venue continuous_equity.csv (no engine runs)",
    )
    parser.add_argument(
        "--frozen-fallback",
        default=str(SHARED / "continuous_deployed_equity_refresh_2026-06-12"),
        help="components root whose continuous_report.json configs stand in for the consolidated 2026-06-07 receipts",
    )
    parser.add_argument(
        "--chart-leverage",
        type=float,
        default=4.0,
        help="Extra pure-leverage continuous chart to render alongside 1x. Use 1 to suppress the extra chart.",
    )
    parser.add_argument("--component-take-profit-pct", type=float, default=None)
    parser.add_argument(
        "--btc-risk-sizing",
        action="store_true",
        help=f"Apply {CTRL_BTC_RISK_70_90_35_ID} entry-size multipliers.",
    )
    parser.add_argument(
        "--backtest-leverage",
        type=float,
        default=1.0,
        help="Modeled backtest leverage: scales component gross exposure before costs/funding and scales hedge cap.",
    )
    parser.add_argument(
        "--btc-trend-gate",
        choices=sorted(VALID_BTC_TREND_GATES),
        default=None,
        help="Override component BTC_TREND_GATE; omit to preserve each frozen source config.",
    )
    args = parser.parse_args()
    data_root = Path(args.data_root).expanduser() if args.data_root else None
    if data_root is not None and len(args.venues) != 1:
        parser.error("--data-root requires exactly one --venues entry")
    output_root = Path(args.output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"start_date": args.start_date, "end_date": args.end_date}
    for venue in args.venues:
        summary[venue] = run_venue(
            venue, output_root=output_root, end_date=args.end_date,
            start_date=args.start_date,
            render_only=args.render_only, frozen_fallback=Path(args.frozen_fallback).expanduser(),
            data_root=data_root,
            chart_leverage=args.chart_leverage,
            component_take_profit_pct=args.component_take_profit_pct,
            btc_risk_sizing=args.btc_risk_sizing,
            backtest_leverage=args.backtest_leverage,
            btc_trend_gate=args.btc_trend_gate,
        )
    if not args.render_only:
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"summary: {output_root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
