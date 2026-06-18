#!/usr/bin/env python3
"""Deployed continuous-ensemble equity on the June-extended PIT roots.

Re-runs the EXACT frozen continuous_ensemble_v2 component configs (loaded from the
2026-06-07 source receipts; only `end_date` is overridden) against the
refreshed per-venue full-PIT roots, then reproduces the deployed book —
continuous_ensemble_v2 3-component ensemble @ w90/tv0.045/max4/ddh-0.04 + banked 2f
BTC+ETH hedge — and the official strategy-vs-BTC chart.

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
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.continuous_component_sources import (  # noqa: E402
    CONTINUOUS_COMPONENT_SOURCES,
    ContinuousComponentSource,
    load_continuous_component_source,
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


def frozen_config(
    component: str,
    venue: str,
    *,
    end_date: str,
    start_date: str | None = None,
    fallback_root: Path | None = None,
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
    return cfg


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


def component_report_matches_window(payload: dict[str, Any], *, start_date: str | None, end_date: str) -> bool:
    cfg = payload.get("config") or {}
    if str(cfg.get("end_date")) != end_date:
        return False
    if start_date is not None and str(cfg.get("start_date")) != start_date:
        return False
    return True


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


def run_components(
    venue: str,
    *,
    output_root: Path,
    end_date: str,
    start_date: str | None = None,
    frozen_fallback: Path | None = None,
    data_root: Path | None = None,
) -> dict[str, Any]:
    data_root = data_root if data_root is not None else SHARED / f"{venue}_full_pit"
    meta: dict[str, Any] = {}
    for component in WINNER_WEIGHTS:
        spec = CONTINUOUS_COMPONENT_SOURCES[component]
        cell_dir = output_root / "components" / venue / spec.cell
        report_path = cell_dir / "continuous_report.json"
        if report_path.exists():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            resumed = component_report_matches_window(payload, start_date=start_date, end_date=end_date)
            if not resumed:
                cfg = frozen_config(
                    component,
                    venue,
                    end_date=end_date,
                    start_date=start_date,
                    fallback_root=frozen_fallback,
                )
                payload = run_continuous_event_research(data_root, config=cfg, report_dir=cell_dir)
        else:
            cfg = frozen_config(
                component,
                venue,
                end_date=end_date,
                start_date=start_date,
                fallback_root=frozen_fallback,
            )
            payload = run_continuous_event_research(data_root, config=cfg, report_dir=cell_dir)
            resumed = False
        meta[component] = {
            "config_hash": payload["config_hash"],
            "n_trades": payload["n_trades"],
            "funding_mode": payload["funding_mode"],
            "skips": payload.get("skips"),
            "resumed": resumed,
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
) -> None:
    for mult in chart_leverages(leverage):
        tag = "" if mult == 1.0 else f"_{mult:g}x"
        mdf = df
        if mult != 1.0:
            rets = mdf["basket_return"].fill_null(0.0).to_numpy() * mult
            eq = np.cumprod(1.0 + rets)
            mdf = mdf.with_columns(pl.Series("basket_return", rets), pl.Series("equity", eq))
        equity = mdf.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().cast(pl.String).alias("date")
        )
        equity.write_csv(out_dir / f"continuous_equity{tag}.csv")
        monthly = monthly_returns_with_trades(equity, monthly_trades)
        monthly.write_csv(out_dir / f"continuous_monthly{tag}.csv")
        name = "Continuous (deployed cfg)" if mult == 1.0 else f"Continuous (deployed cfg) {mult:g}x"
        sub = "IN-SAMPLE RESEARCH refresh to June 2026 data (window spent; forward demo is the arbiter) — not a promotion claim"
        if mult != 1.0:
            sub += f" | {mult:g}x = pure leverage on the same returns; margin/liquidation NOT modeled"
        meta = _write_equity_benchmark_chart(
            out_dir,
            root=out_dir,
            equity=equity,
            raw_klines=raw_klines,
            monthly=monthly,
            png_name=f"continuous_equity_btc{tag}.png",
            title=(
                f"CONTINUOUS deployed — continuous_ensemble_v2 ensemble + 2f hedge (max4)"
                f"{'' if mult == 1.0 else f', {mult:g}x levered'} [{venue}] — refreshed to {end_date}"
            ),
            subtitle=sub,
            strategy_name=name,
        )
        venue_summary[f"{mult:g}x"] = stats(mdf)
        print(f"[{venue}] {mult:g}x {json.dumps(venue_summary[f'{mult:g}x'])}", flush=True)
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
) -> dict[str, Any]:
    out_dir = output_root / venue
    out_dir.mkdir(parents=True, exist_ok=True)
    if render_only:
        csv_path = out_dir / "continuous_equity.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"--render-only needs an existing {csv_path}")
        df = pl.read_csv(csv_path).select(["ts_ms", "basket_return", "equity"])
        panel = load_extended_panel(venue, end_date=end_date, root=data_root)
        venue_summary: dict[str, Any] = {}
    else:
        component_meta = run_components(
            venue,
            output_root=output_root,
            end_date=end_date,
            start_date=start_date,
            frozen_fallback=frozen_fallback,
            data_root=data_root,
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
            combined, winner_rule(), ContinuousHedgeRule(90, 60, 2.0, 5.0),
            btc_ret, btc_fund, eth_ret, eth_fund,
            hedge_intensity=deployed_hedge_intensity(combined.days, btc_ret),
        )
        panel_last = dt.date.fromisoformat(str(panel["date"].max()))
        df = pad_flat_tail(df, through_date=panel_last)
        venue_summary = {"components": component_meta}
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
        )
    if not args.render_only:
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"summary: {output_root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
