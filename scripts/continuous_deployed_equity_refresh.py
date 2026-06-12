#!/usr/bin/env python3
"""Deployed continuous-ensemble equity on the June-extended PIT roots.

Re-runs the EXACT frozen winner_base component configs (loaded from the
2026-06-07 source receipts; only `end_date` is overridden) against the
refreshed per-venue full-PIT roots, then reproduces the deployed book —
winner_base 4-component ensemble @ w90/tv0.045/max4/ddh-0.04 + banked 2f
BTC+ETH hedge — and the official strategy-vs-BTC chart, exactly as
`continuous_deployed_equity.py` does for the frozen window.

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
import sys
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import continuous_deployed_equity as deployed  # noqa: E402
import continuous_ensemble_rebalance_scout as scout  # noqa: E402

from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    run_continuous_event_research,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
)
from liquidity_migration.volume_events_charts import _write_equity_benchmark_chart  # noqa: E402

SHARED = Path("C:/Users/user/SHARED_DATA")
PANEL_COLUMNS = ["symbol", "ts_ms", "date", "close", "turnover_quote"]


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
    component: str, venue: str, *, end_date: str, fallback_root: Path | None = None
) -> ContinuousEventConfig:
    spec = scout.SOURCES[component]
    report_path = spec.root / venue / spec.cell / "continuous_report.json"
    if not report_path.exists() and fallback_root is not None:
        # The 2026-06-07 one-off receipt dirs were consolidated away (STATE.md: git history
        # is the archive). A prior refresh's component report carries the same frozen config
        # verbatim (its runner overrode only end_date), so it is an equivalent source.
        report_path = fallback_root / "components" / venue / spec.cell / "continuous_report.json"
    if not report_path.exists():
        raise FileNotFoundError(f"missing frozen source report: {report_path}")
    return replace(config_from_report(report_path), end_date=end_date)


def load_extended_panel(venue: str, *, end_date: str, root: Path | None = None) -> pl.DataFrame:
    """Frozen feature-panel parquet + a kline-partition tail through end_date.

    Same construction as `continuous_rs_squeeze_probe.load_daily_panel`, but the
    tail is generic: both venues, from the frozen panel's last date + 1 through
    end_date (exclusive), reading whatever klines_1h partitions exist.
    """
    root = root if root is not None else SHARED / f"{venue}_full_pit"
    fp = pl.read_parquet(root / "feature_panel_2026-05-27.parquet", columns=PANEL_COLUMNS)
    n_before = fp.height
    fp = fp.sort("ts_ms").group_by(["symbol", "date"], maintain_order=True).last()
    dups = n_before - fp.height
    frames = [fp.select(["symbol", "date", "close", "turnover_quote"])]
    panel_end = dt.date.fromisoformat(str(fp["date"].max()))
    boundary = dt.date.fromisoformat(end_date)
    tail_frames = []
    d = panel_end + dt.timedelta(days=1)
    while d < boundary:
        part = root / "klines_1h" / f"date={d.isoformat()}"
        if part.exists():
            raw = pl.read_parquet(part, columns=["ts_ms", "symbol", "close", "turnover_quote"])
            agg = (
                raw.sort("ts_ms")
                .group_by("symbol", maintain_order=True)
                .agg(pl.col("close").last(), pl.col("turnover_quote").sum())
                .with_columns(pl.lit(d.isoformat()).alias("date"))
            )
            tail_frames.append(agg.select(["symbol", "date", "close", "turnover_quote"]))
        d += dt.timedelta(days=1)
    if tail_frames:
        frames.append(pl.concat(tail_frames))
    panel = pl.concat(frames).sort(["symbol", "date"])
    panel = panel.with_columns(pl.col("date").str.to_date().alias("d"))
    print(
        f"[{venue}] daily panel rows={panel.height} dup_ts_rows_collapsed={dups} "
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


def run_components(
    venue: str, *, output_root: Path, end_date: str, frozen_fallback: Path | None = None
) -> dict[str, Any]:
    data_root = SHARED / f"{venue}_full_pit"
    meta: dict[str, Any] = {}
    for component in deployed.WINNER_WEIGHTS:
        spec = scout.SOURCES[component]
        cell_dir = output_root / "components" / venue / spec.cell
        report_path = cell_dir / "continuous_report.json"
        if report_path.exists():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            resumed = True
        else:
            cfg = frozen_config(component, venue, end_date=end_date, fallback_root=frozen_fallback)
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
) -> None:
    for mult in (1.0, 4.0):
        tag = "" if mult == 1.0 else f"_{mult:g}x"
        mdf = df
        if mult != 1.0:
            import numpy as np

            rets = mdf["basket_return"].fill_null(0.0).to_numpy() * mult
            eq = np.cumprod(1.0 + rets)
            mdf = mdf.with_columns(pl.Series("basket_return", rets), pl.Series("equity", eq))
        equity = mdf.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().cast(pl.String).alias("date")
        )
        equity.write_csv(out_dir / f"continuous_equity{tag}.csv")
        name = "Continuous (deployed cfg)" if mult == 1.0 else f"Continuous (deployed cfg) {mult:g}x"
        sub = "IN-SAMPLE RESEARCH refresh to June 2026 data (window spent; forward demo is the arbiter) — not a promotion claim"
        if mult != 1.0:
            sub += f" | {mult:g}x = pure leverage on the same returns; margin/liquidation NOT modeled"
        meta = _write_equity_benchmark_chart(
            out_dir,
            root=out_dir,
            equity=equity,
            raw_klines=raw_klines,
            monthly=None,
            png_name=f"continuous_equity_btc{tag}.png",
            title=(
                f"CONTINUOUS deployed — winner_base ensemble + 2f hedge (max4)"
                f"{'' if mult == 1.0 else f', {mult:g}x levered'} [{venue}] — refreshed to {end_date}"
            ),
            subtitle=sub,
            strategy_name=name,
        )
        venue_summary[f"{mult:g}x"] = deployed.stats(mdf)
        print(f"[{venue}] {mult:g}x {json.dumps(venue_summary[f'{mult:g}x'])}", flush=True)
        print(f"[{venue}] png: {out_dir / f'continuous_equity_btc{tag}.png'} ({'ok' if meta else 'CHART FAILED'})", flush=True)


def run_venue(
    venue: str,
    *,
    output_root: Path,
    end_date: str,
    render_only: bool = False,
    frozen_fallback: Path | None = None,
) -> dict[str, Any]:
    out_dir = output_root / venue
    out_dir.mkdir(parents=True, exist_ok=True)
    if render_only:
        csv_path = out_dir / "continuous_equity.csv"
        if not csv_path.exists():
            raise FileNotFoundError(f"--render-only needs an existing {csv_path}")
        df = pl.read_csv(csv_path).select(["ts_ms", "basket_return", "equity"])
        panel = load_extended_panel(venue, end_date=end_date)
        venue_summary: dict[str, Any] = {}
    else:
        component_meta = run_components(
            venue, output_root=output_root, end_date=end_date, frozen_fallback=frozen_fallback
        )
        pieces = {}
        for component in deployed.WINNER_WEIGHTS:
            spec = scout.SOURCES[component]
            refreshed = scout.SourceSpec(output_root / "components", spec.cell)
            comp, _n, _cfg = scout._load_source(refreshed, venue)
            pieces[component] = comp
        combined = scout._combine_components(pieces, deployed.WINNER_WEIGHTS)
        panel = load_extended_panel(venue, end_date=end_date)
        btc_ret, btc_fund = deployed.instrument_inputs(venue, combined.days, "BTCUSDT", panel)
        eth_ret, eth_fund = deployed.instrument_inputs(venue, combined.days, "ETHUSDT", panel)
        df = apply_rebalance_rule(
            combined, deployed.winner_rule(), ContinuousHedgeRule(90, 60, 2.0, 5.0),
            btc_ret, btc_fund, eth_ret, eth_fund,
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
    )
    return venue_summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--end-date", default="2026-06-12", help="engine end boundary (exclusive at date midnight UTC)")
    parser.add_argument("--output-root", default=str(SHARED / "continuous_deployed_equity_refresh_2026-06-12"))
    parser.add_argument("--venues", nargs="+", default=["bybit", "binance"])
    parser.add_argument(
        "--render-only", action="store_true",
        help="re-render PNGs + stats from existing per-venue continuous_equity.csv (no engine runs)",
    )
    parser.add_argument(
        "--frozen-fallback",
        default=str(SHARED / "continuous_deployed_equity_refresh_2026-06-12"),
        help="components root whose continuous_report.json configs stand in for the consolidated 2026-06-07 receipts",
    )
    args = parser.parse_args()
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {"end_date": args.end_date}
    for venue in args.venues:
        summary[venue] = run_venue(
            venue, output_root=output_root, end_date=args.end_date,
            render_only=args.render_only, frozen_fallback=Path(args.frozen_fallback),
        )
    if not args.render_only:
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(f"summary: {output_root / 'summary.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
