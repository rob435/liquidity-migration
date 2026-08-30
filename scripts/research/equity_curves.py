#!/usr/bin/env python3
"""One command for standard equity curves.

LONG runs from ``long_native.long_v11a_profile``.
CARRY renders the deployed rule ``configs/lane2_carry_hold_v7.json``
through the same --research-config path (cross-venue panel, settlement-exact
scorer). That is the registered research shape, not a demo daemon replay.

    bash scripts/research/equity_curves.sh                      # LONG sleeve, last 3 years, bybit_full_pit
    bash scripts/research/equity_curves.sh --sleeves long,carry
    bash scripts/research/equity_curves.sh --root ~/SHARED_DATA/binance_full_pit

The strategy modules own their active configurations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from liquidity_migration.core.config import load_config  # noqa: E402
from liquidity_migration.core.symbol_codec import (  # noqa: E402
    SymbolIdentityError,
    decode_symbol_partition,
)

DEFAULT_ROOT = "~/SHARED_DATA/bybit_full_pit"
DEFAULT_CONFIG = "configs/volume_alpha.default.yaml"
DEFAULT_PANEL_ROOT = "~/SHARED_DATA/cross_venue_panel_v1"

#: Columns a registered financed-longs config needs from the cross-venue panel.
RESEARCH_PANEL_COLUMNS = (
    "symbol", "bar_ts_ms", "by_close", "by_turnover_quote", "by_funding",
    "by_funding_age_h", "bn_close", "bn_turnover_quote", "bn_funding", "bn_funding_age_h",
)
#: Present only on panels built with --metrics-root; kept when every shard has
#: them so v5 renders, while older panels still render v1..v4.
OPTIONAL_RESEARCH_PANEL_COLUMNS = ("bn_tt_ls", "bn_tt_ls_age_h")


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _shift_years(date: dt.date, years: int) -> dt.date:
    # Clamp Feb 29 to Feb 28 when the target year is not a leap year.
    try:
        return date.replace(year=date.year - years)
    except ValueError:
        return date.replace(year=date.year - years, day=28)


def _run_long(
    root: str,
    costs: Any,
    start: str,
    end: str,
    out: Path,
    pit_tol: float,
    long_notional: float | None = None,
    long_profile: str = "v12",
) -> dict[str, Any]:
    # LONG records its own PIT pass/taint label; pit_tol does not apply.
    del pit_tol
    from liquidity_migration.research.backtest.long_native import run_long_native_research
    from liquidity_migration.rules.long_contract import ConfigLayer, resolve_strategy_config
    from liquidity_migration.rules.long_native import long_v11a_profile, long_v12_profile

    profile = {"v11a": long_v11a_profile, "v12": long_v12_profile}[long_profile]
    cfg = replace(profile(), start_date=start, end_date=end)
    execution_values = {
        "round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier
    }
    if long_notional is not None:
        # Research convention is 1x; this option draws pure leverage on the same signal.
        execution_values["notional_multiplier"] = float(long_notional)
    effective = resolve_strategy_config(
        long_profile,
        rule=cfg,
        layers=(ConfigLayer(source="equity_curve_cli", values=execution_values),),
    )
    return run_long_native_research(
        root,
        config=cfg,
        cost_config=costs,
        report_dir=out,
        effective_config=effective,
    )


def _load_research_panel(panel_root: str | Path) -> Any:
    """Load the cross-venue panel columns every research-config render needs."""
    import polars as pl

    root = Path(panel_root).expanduser()
    shards = sorted(str(x) for x in root.glob("*/panel.parquet"))
    if not shards:
        raise RuntimeError(f"no cross-venue panel shards under {root}")
    scans = [pl.scan_parquet(s) for s in shards]
    optional = [
        c
        for c in OPTIONAL_RESEARCH_PANEL_COLUMNS
        if all(c in s.collect_schema().names() for s in scans)
    ]
    cols = list(RESEARCH_PANEL_COLUMNS) + optional
    return (
        pl.concat([s.select(cols) for s in scans])
        .collect()
        .sort(["symbol", "bar_ts_ms"])
    )


def _run_carry(
    panel_root: str,
    start: str,
    end: str,
    out: Path,
) -> dict[str, Any]:
    """Render the CARRY sleeve's registered research shape.

    The carry runtime (v7 profile) trades ``configs/lane2_carry_hold_v7.json``,
    so its standard curve is that same config through the --research-config
    path. It reads the cross-venue panel, not the demo cycle record; v7's
    pre-settle exit clock is execution-time and is not modeled here.
    """
    from liquidity_migration.strategy.carry_demo import CARRY_CONFIG_PATH
    from liquidity_migration.research.backtest.financed_longs import research_equity_chart

    panel = _load_research_panel(panel_root)
    return research_equity_chart(panel, CARRY_CONFIG_PATH, out, start=start, end=end)


#: Daily equity CSV produced by each sleeve runner under its output dir.
_COMBINED_EQUITY_CSVS = {
    "long": "long_native_equity.csv",
    "carry": "lane2_carry_hold_v7_daily_equity.csv",
}


def _combined_daily_returns(equity_csv: Path) -> list[tuple[str, float]]:
    """[date(str), ret] per day from a sleeve's daily equity CSV.

    LONG's CSV carries ``basket_return``; the carry research renderer writes
    ``date,equity`` only, so returns are derived by taking the last equity per
    date. A day with no row means the sleeve did not trade that day and
    contributes zero return (an absent book cannot lose or earn).
    """
    import polars as pl

    df = pl.read_csv(equity_csv)
    cols = {c.lower(): c for c in df.columns}
    dcol = next((cols[c] for c in ("date", "day", "timestamp") if c in cols), None)
    if dcol is None:
        raise SystemExit(f"{equity_csv.name}: no date column in {df.columns}")
    date_expr = pl.col(dcol).cast(pl.String).str.slice(0, 10)
    rcol = next((cols[c] for c in ("basket_return", "ret", "return") if c in cols), None)
    if rcol is not None:
        frame = df.select(date=date_expr, ret=pl.col(rcol).cast(pl.Float64))
    else:
        ecol = next((cols[c] for c in ("equity", "nav", "equity_usdt") if c in cols), None)
        if ecol is None:
            raise SystemExit(f"{equity_csv.name}: no return or equity column in {df.columns}")
        eq = df.select(date=date_expr, e=pl.col(ecol).cast(pl.Float64)).sort("date")
        eq = eq.group_by("date").agg(pl.col("e").last()).sort("date")
        frame = eq.with_columns(
            ret=(pl.col("e") / pl.col("e").shift(1) - 1.0).fill_null(0.0)
        ).select("date", "ret")
    return [
        (str(row["date"]), float(row["ret"]))
        for row in frame.group_by("date").agg(pl.col("ret").sum()).sort("date").to_dicts()
    ]


def _combined_equity_frame(
    out_root: Path,
    *,
    weight_carry: float | None,
    scale: float,
    long_multiplier: float,
    carry_multiplier: float,
    long_profile: str,
) -> pl.DataFrame:
    """Build the combined LONG+CARRY equity series.

    Each leg is first brought to its deployed size by multiplying its daily
    return by ``long_multiplier`` / ``carry_multiplier`` (the research render
    is at 1x native size; the deployed dials are LONG 6.0, CARRY 3.0). The two
    dial-scaled legs are then blended.

    ``weight_carry`` is the CARRY share of the blend. When it is None the blend
    is equal-risk (inverse full-window vol): each leg contributes proportional
    to 1/vol, so neither dominates. The two legs are aligned over the union of
    their trading days (a day where only one leg trades contributes that leg's
    return alone), then ``combined_ret = scale * weighted_blend``. ``scale`` is
    presentation leverage on the combined book and is NOT modelled cost — the
    two underlying returns already carry their own costs.
    """
    import polars as pl

    long_csv = out_root / "long" / _COMBINED_EQUITY_CSVS["long"]
    carry_csv = out_root / "carry" / _COMBINED_EQUITY_CSVS["carry"]
    long_rows = dict(_combined_daily_returns(long_csv))
    carry_rows = dict(_combined_daily_returns(carry_csv))
    all_days = sorted(set(long_rows) | set(carry_rows))
    if len(all_days) < 2:
        raise SystemExit(f"combined window too short: {len(all_days)} day(s)")

    scaled_long = {d: r * long_multiplier for d, r in long_rows.items()}
    scaled_carry = {d: r * carry_multiplier for d, r in carry_rows.items()}

    if weight_carry is None:
        long_vol = _ann_vol([scaled_long[d] for d in all_days if d in scaled_long])
        carry_vol = _ann_vol([scaled_carry[d] for d in all_days if d in scaled_carry])
        if long_vol <= 0 or carry_vol <= 0:
            raise SystemExit("equal-risk blend needs positive per-leg volatility")
        weight_carry = (1.0 / carry_vol) / (1.0 / long_vol + 1.0 / carry_vol)

    combined: list[tuple[str, float]] = []
    for day in all_days:
        lr = scaled_long.get(day, 0.0)
        cr = scaled_carry.get(day, 0.0)
        r = scale * ((1.0 - weight_carry) * lr + weight_carry * cr)
        combined.append((day, r))
    equity = 1.0
    rows = []
    for day, ret in combined:
        equity *= 1.0 + ret
        rows.append((day, equity))
    return pl.DataFrame({"date": [d for d, _ in rows], "equity": [e for _, e in rows]}), weight_carry


def _monthly_from_equity(equity: pl.DataFrame) -> pl.DataFrame:
    """Month-by-month strategy return from a daily equity frame (no trade counts).

    Runs last-equity-in-month / last-equity-in-previous-month - 1, so the first
    month (no prior month) reads 0.0 rather than a truncated entry month.
    """
    import polars as pl

    month_end = (
        equity.with_columns(month=pl.col("date").str.slice(0, 7))
        .sort("date")
        .group_by("month", maintain_order=True)
        .agg(pl.col("equity").last().alias("last_eq"))
        .sort("month")
        .with_columns(ret=pl.col("last_eq") / pl.col("last_eq").shift(1) - 1.0)
        .with_columns(ret=pl.col("ret").fill_null(0.0).cast(pl.Float64))
    )
    return month_end.select("month", pl.col("ret").alias("strategy_return"))


def _ann_vol(returns: list[float]) -> float:
    """Annualised volatility of a daily-return series, or 0.0 when undefined."""
    import math

    import polars as pl

    if len(returns) < 2:
        return 0.0
    s = pl.Series(returns).std(ddof=1)
    if s is None or float(s) == 0.0:
        return 0.0
    return float(s * math.sqrt(365))


def _btc_raw_klines(panel_root: str | Path, *, start: str, end: str) -> pl.DataFrame:
    """BTCUSDT close series in the renderer's ``symbol,date,ts_ms,close`` schema."""
    import polars as pl

    root = Path(panel_root).expanduser()
    shards = sorted(str(x) for x in root.glob("*/panel.parquet"))
    if not shards:
        return pl.DataFrame(schema={"symbol": pl.String, "date": pl.String, "ts_ms": pl.Int64, "close": pl.Float64})
    start_ts = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.UTC).timestamp() * 1000)
    end_ts = int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.UTC).timestamp() * 1000)
    return (
        pl.concat([pl.scan_parquet(s) for s in shards])
        .filter(
            (pl.col("symbol") == "BTCUSDT")
            & (pl.col("bar_ts_ms") >= start_ts)
            & (pl.col("bar_ts_ms") < end_ts)
            & pl.col("by_close").is_not_null()
        )
        .select(
            pl.col("symbol"),
            pl.from_epoch("bar_ts_ms", time_unit="ms").dt.date().cast(pl.String).alias("date"),
            pl.col("bar_ts_ms").alias("ts_ms"),
            pl.col("by_close").alias("close"),
        )
        .collect()
        .sort("ts_ms")
    )


def _run_combined(
    panel_root: str,
    start: str,
    end: str,
    out: Path,
    *,
    weight_carry: float | None,
    scale: float,
    long_multiplier: float,
    carry_multiplier: float,
    long_profile: str,
) -> dict[str, Any]:
    """Render the LONG+CARRY combined book through the standard chart.

    Reads the two sleeves' already-run daily equity CSVs (call the wrapper
    with ``--sleeves long,carry`` alongside ``--combined``), brings each leg
    to its deployed size, blends them (equal-risk by default), applies
    presentation-only ``scale`` leverage, and draws the same strategy-vs-BTC
    layout. Combined-book metrics are computed on the blended daily series.
    """
    import polars as pl

    out.mkdir(parents=True, exist_ok=True)
    from liquidity_migration.research.backtest.volume_events_charts import _write_equity_benchmark_chart

    equity, resolved_carry_weight = _combined_equity_frame(
        out.parent,
        weight_carry=weight_carry,
        scale=scale,
        long_multiplier=long_multiplier,
        carry_multiplier=carry_multiplier,
        long_profile=long_profile,
    )
    weight_carry = resolved_carry_weight
    equity.write_csv(out / "combined_equity.csv")

    vals = equity["equity"].to_list()
    returns = [
        (vals[i] / vals[i - 1] - 1.0) if i > 0 else 0.0
        for i in range(len(vals))
    ]
    days = equity["date"].to_list()
    years = max((dt.date.fromisoformat(days[-1]) - dt.date.fromisoformat(days[0])).days, 1) / 365.25
    total = float(vals[-1] - 1.0)
    annualized = float(vals[-1] ** (1.0 / years) - 1.0) if total > -1 else -1.0
    drawdown = float(
        min((v / max(vals[: i + 1]) - 1.0) for i, v in enumerate(vals))
    )
    deviation = float(pl.Series(returns).std(ddof=1)) if len(returns) > 1 else 0.0
    metrics = {
        "total_return_pct": total * 100.0,
        "annualized_pct": annualized * 100.0,
        "max_drawdown_pct": drawdown * 100.0,
        "worst_day_pct": min(returns) * 100.0,
        "sharpe_daily_ann": (
            float(pl.Series(returns).mean()) / deviation * (365.0 ** 0.5) if deviation > 0 else 0.0
        ),
        "mar": (annualized / abs(drawdown)) if drawdown < 0 else None,
        "years": years,
    }

    raw_klines = _btc_raw_klines(panel_root, start=start, end=end)
    combined_equity = equity.with_columns(
        pl.col("date").cast(pl.Date).cast(pl.Datetime("ms")).dt.epoch("ms").alias("ts_ms")
    ).select("ts_ms", "date", "equity")
    blend_note = (
        f"equal-risk blend (inverse-vol): {1.0 - weight_carry:.1%} LONG / {weight_carry:.1%} CARRY"
    )
    monthly = _monthly_from_equity(equity)
    chart = _write_equity_benchmark_chart(
        out,
        equity=combined_equity,
        raw_klines=raw_klines,
        monthly=monthly,
        png_name="combined_equity_btc.png",
        title="LONG + CARRY - combined research book",
        subtitle=(
            f"SIMULATION ON SEEN DATA - opinion, not evidence. Legs at deployed dials "
            f"(LONG x{long_multiplier:g}, CARRY x{carry_multiplier:g}); {blend_note}; "
            f"blend scaled x{scale:g} for presentation (not modelled cost). "
            f"Window {start} -> {end} (end exclusive). Long profile {long_profile}."
        ),
        step=False,
        strategy_name=f"Portfolio {scale:g}x {1.0-weight_carry:.2f} LONG / {weight_carry:.2f} CARRY",
        metrics=metrics,
    )
    return {
        "run_label": "combined_long_carry_research_seen_data",
        "summary": {
            "total_return": total,
            "max_drawdown": drawdown,
            "sharpe_like": metrics["sharpe_daily_ann"],
            "mar": metrics["mar"],
        },
        "metrics": metrics,
        "png": chart.get("png"),
        "equity": equity,
        "weight_carry": weight_carry,
    }


RUNNERS = {"long": _run_long, "carry": _run_carry}


def _find_png(out: Path) -> Path | None:
    hits = sorted(out.rglob("*equity*btc*.png")) or sorted(out.rglob("*equity*.png"))
    if not hits:
        return None

    def score(path: Path) -> tuple[int, int, int, int, str]:
        rel_parts = tuple(part.lower() for part in path.relative_to(out).parts)
        is_component = "components" in rel_parts
        is_levered = "_4x" in path.stem.lower()
        is_official_name = path.name == "long_native_equity_btc.png"
        return (
            1 if is_component else 0,
            1 if is_levered else 0,
            0 if is_official_name else 1,
            len(rel_parts),
            str(path),
        )

    return min(hits, key=score)


def _plot_equity_csv(out: Path, sleeve: str) -> Path | None:
    """Fallback: plot a cumulative-equity curve from the engine's equity CSV."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import polars as pl

    csvs = sorted(out.rglob("*equity*.csv"))
    if not csvs:
        return None
    primary = [p for p in csvs if "_4x" not in p.stem.lower()]
    df = pl.read_csv((primary or csvs)[-1])
    cols = {c.lower(): c for c in df.columns}
    eq = next((cols[c] for c in ("equity", "equity_usdt", "cum_return", "cumulative_return", "nav") if c in cols), None)
    xc = next((cols[c] for c in ("date", "ts_ms", "day", "timestamp") if c in cols), None)
    if eq is None:
        return None
    y = df[eq].to_list()
    x = list(range(len(y))) if xc is None else df[xc].to_list()
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(x, y, lw=1.3)
    ax.set_title(f"{sleeve} sleeve - equity ({eq})")
    ax.set_ylabel(eq)
    ax.grid(alpha=0.3)
    png = out / f"{sleeve}_equity.png"
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    plt.close(fig)
    return png


def _prepare_sleeve_output(out: Path, *, fresh: bool) -> None:
    """Create one sleeve directory, optionally discarding only derived output."""

    if fresh and (out.exists() or out.is_symlink()):
        if out.is_symlink() or not out.is_dir():
            raise RuntimeError(f"refusing to replace non-directory sleeve output: {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    # A kernel replay tape binds to the window that wrote it; a rerun with a
    # different window resumed onto it dies with "strategy event clock cannot
    # move backward". The replay state is derived scratch — always rebuilt.
    replay_state = out / "common_kernel_execution"
    if replay_state.is_dir():
        shutil.rmtree(replay_state)


def _label(payload: dict[str, Any]) -> str:
    return str(payload.get("run_label") or (payload.get("summary") or {}).get("run_label") or "-")


def _delisted_traded(out: Path, root: str) -> int | None:
    """Count traded symbols absent from the last 30d of klines.

    A value > 0 proves the run used a delisted-inclusive PIT universe. A
    current-universe survivorship-biased run would trade zero delisted names.
    """
    import glob
    import os

    import polars as pl

    tcsv = sorted(out.rglob("*best_trades.csv")) or sorted(out.rglob("*trades*.csv"))
    kroot = os.path.join(os.path.expanduser(root), "klines_1h")
    if not tcsv or not os.path.isdir(kroot):
        return None
    try:
        syms = set(pl.read_csv(tcsv[-1])["symbol"].unique().to_list())
    except Exception:
        return None
    recent: set[str] = set()
    for d in sorted(os.listdir(kroot))[-30:]:
        for s in glob.glob(os.path.join(kroot, d, "symbol=*")):
            try:
                recent.add(decode_symbol_partition(s.split("symbol=")[-1]))
            except SymbolIdentityError:
                continue
    return len(syms - recent)


def _pit_verdict(label: str, delisted: int | None) -> str:
    if "missing_manifest" in label:
        return "  [!] NOT clean full-PIT (manifest empty - do not cite)"
    if "current_universe" in label:
        if delisted and delisted > 0:
            return (
                f"  [OK] effectively full-PIT - {delisted} delisted names traded "
                "(no survivorship; label is conservative over a listing-boundary gap)"
            )
        return "  [!] current-universe (no delisted names traded - possible survivorship; treat as biased)"
    return ""


def _headline(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or payload.get("metrics") or {}
    bits = []
    for k, fmt in (
        ("total_return", "ret {:+.1%}"),
        ("max_drawdown", "DD {:.1%}"),
        ("sharpe_like", "Sharpe {:.2f}"),
        ("mar", "MAR {:.2f}"),
        ("trades", "{:.0f} trades"),
    ):
        if k in s and isinstance(s[k], (int, float)):
            bits.append(fmt.format(s[k]))
    return " | ".join(bits) if bits else "(see report)"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Standard equity curves for the active LONG profile and the registered CARRY config.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--sleeves",
        default="long",
        help=(
            "Comma list: long, carry. 'carry' renders the registered "
            "research config (lane2_carry_hold_v7, the file the deployed v7 "
            "profile trades) from the cross-venue panel — a research-shape "
            "simulation, not a daemon replay; the v7 pre-settle exit clock "
            "is execution-time and is not modeled here."
        ),
    )
    p.add_argument(
        "--long-notional-multiplier",
        type=float,
        default=None,
        help=(
            "Override the long sleeve's notional_multiplier. Research default is 1x; "
            "e.g. 5 draws pure leverage on the same signal."
        ),
    )
    p.add_argument(
        "--long-profile",
        choices=("v11a", "v12"),
        default="v12",
        help=(
            "Which LONG profile to render. v12 wide-stop is the deployed one "
            "(STATE.md change point 2026-08-03); v11a is its predecessor, kept "
            "for comparison."
        ),
    )
    # Default --years to a sentinel so an unset window preserves the active
    # profile's full history instead of forcing a rolling 3y override.
    p.add_argument(
        "--years",
        type=int,
        default=None,
        help="Window length in years (default 3; ignored if --start given).",
    )
    p.add_argument("--start", default=None, help="Window start YYYY-MM-DD (overrides --years).")
    p.add_argument("--end", default=None, help="Window end YYYY-MM-DD (exclusive; default tomorrow UTC).")
    p.add_argument("--root", default=DEFAULT_ROOT, help="Per-venue full-PIT data root.")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="Cost-model config.")
    p.add_argument("--out", default=None, help="Report dir (default <root>/reports/equity_curves).")
    p.add_argument(
        "--fresh-output",
        action="store_true",
        help=(
            "Remove each requested sleeve's derived report directory before running. "
            "Use for isolated research-run outputs; raw market data is never removed."
        ),
    )
    p.add_argument(
        "--research-config",
        action="append",
        default=None,
        metavar="CONFIG_JSON",
        help=(
            "Registered financed-longs config JSON (repeatable) to render through the "
            "SAME standard chart, labelled RESEARCH / simulation-on-seen-data. This is "
            "the supported way to put a Lane-2 research config in the standard format; "
            "never hand-build a lookalike chart. Reads the cross-venue panel."
        ),
    )
    p.add_argument(
        "--panel-root",
        default=DEFAULT_PANEL_ROOT,
        help="Cross-venue panel root for --research-config renders.",
    )
    p.add_argument(
        "--combined",
        action="store_true",
        help=(
            "Render the LONG+CARRY combined book through the standard chart, in "
            "addition to the requested sleeves. Requires --sleeves to include "
            "long and carry (it runs both so their daily equity CSVs exist). "
            "Writes under <out>/combined/. The combined book is the weighted sum "
            "of the two books' daily returns, then scaled by --combined-scale."
        ),
    )
    p.add_argument(
        "--combined-weight",
        type=float,
        default=None,
        help=(
            "CARRY share of the combined book (LONG gets 1 - this). Default None "
            "= equal-risk (inverse-vol) blend, so neither leg dominates. Setting a "
            "number forces a fixed return split."
        ),
    )
    p.add_argument(
        "--combined-scale",
        type=float,
        default=1.0,
        help="Presentation leverage applied to the combined daily return (not modelled cost). (default: 1.0)",
    )
    p.add_argument(
        "--combined-long-multiplier",
        type=float,
        default=6.0,
        help="LONG dial multiplier applied to the LONG leg before blending. Research render is 1x. (default: 6.0)",
    )
    p.add_argument(
        "--combined-carry-multiplier",
        type=float,
        default=3.0,
        help="CARRY dial multiplier applied to the CARRY leg before blending. Research render is 1x. (default: 3.0)",
    )
    p.add_argument(
        "--combined-long-profile",
        choices=("v11a", "v12"),
        default="v12",
        help="Which LONG profile was used for the right-hand LONG leg. (default: v12)",
    )
    args = p.parse_args()

    sleeves = [s.strip() for s in args.sleeves.split(",") if s.strip()]
    bad = [s for s in sleeves if s not in RUNNERS]
    if bad:
        raise SystemExit(f"unknown sleeve(s) {bad}; valid: {', '.join(RUNNERS)}")
    if args.combined:
        for need in ("long", "carry"):
            if need not in sleeves:
                print(f"[combined] adding sleeve {need!r} so its daily equity CSV is produced")
                sleeves.append(need)

    today = _today()
    end = args.end or (today + dt.timedelta(days=1)).isoformat()
    years = 3 if args.years is None else args.years
    start = args.start or _shift_years(today, years).isoformat()
    root = str(Path(args.root).expanduser())
    out_root = Path(args.out).expanduser() if args.out else Path(root) / "reports" / "equity_curves"
    costs = load_config(args.config).costs

    print(f"equity-curves - window {start} -> {end} | root {root} | sleeves {', '.join(sleeves)}\n")
    results: dict[str, dict[str, Any]] = {}
    for s in sleeves:
        out = out_root / s
        _prepare_sleeve_output(out, fresh=args.fresh_output)
        heading = {
            "long": "active LONG profile",
            "carry": "registered CARRY research config, simulation on seen data",
        }[s]
        print(f"=== {s.upper()} ({heading}) ===", flush=True)
        try:
            if s == "long":
                payload = _run_long(
                    root,
                    costs,
                    start,
                    end,
                    out,
                    0.0,
                    long_notional=args.long_notional_multiplier,
                    long_profile=args.long_profile,
                )
            else:
                payload = _run_carry(args.panel_root, start, end, out)
        except Exception as exc:  # noqa: BLE001 - report per-sleeve, keep going
            print(f"  [X] {s} failed: {type(exc).__name__}: {exc}\n", flush=True)
            results[s] = {"error": str(exc)}
            continue
        png = payload.get("png") or _find_png(out) or _plot_equity_csv(out, s)
        label = _label(payload)
        needs_pit_detail = "current_universe" in label or "missing_manifest" in label
        verdict = _pit_verdict(label, _delisted_traded(out, root) if needs_pit_detail else None)
        print(f"  run_label = {label}{verdict}")
        print(f"  {_headline(payload)}")
        print(f"  PNG: {png or '(none - no equity csv/png emitted)'}\n", flush=True)
        results[s] = {"png": str(png) if png else None, "run_label": label}

    panel = None
    if args.research_config:
        try:
            panel = _load_research_panel(args.panel_root)
        except Exception as exc:  # noqa: BLE001 - every research render fails together
            for raw_path in args.research_config:
                results[f"research:{Path(raw_path).stem}"] = {"error": str(exc)}
            print(f"  [X] research panel load failed: {type(exc).__name__}: {exc}\n", flush=True)
    research_paths = list(args.research_config or []) if panel is not None else []
    for raw_path in research_paths:
        cfg_path = Path(raw_path).expanduser()
        name = cfg_path.stem
        key = f"research:{name}"
        out = out_root / "research" / name
        _prepare_sleeve_output(out, fresh=args.fresh_output)
        print(f"=== RESEARCH ({name}) ===", flush=True)
        try:
            from liquidity_migration.research.backtest.financed_longs import research_equity_chart

            payload = research_equity_chart(panel, cfg_path, out, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 - report per-config, keep going
            print(f"  [X] {name} failed: {type(exc).__name__}: {exc}\n", flush=True)
            results[key] = {"error": str(exc)}
            continue
        print(f"  run_label = {payload['run_label']}")
        print(f"  {_headline(payload)}")
        print(f"  PNG: {payload.get('png') or '(none)'}\n", flush=True)
        results[key] = {"png": payload.get("png"), "run_label": payload["run_label"]}

    if args.combined:
        out = out_root / "combined"
        _prepare_sleeve_output(out, fresh=args.fresh_output)
        weight_desc = (
            f"carry weight {args.combined_weight:.2f}"
            if args.combined_weight is not None
            else "equal-risk (inverse-vol) blend"
        )
        print(
            f"=== COMBINED (LONG + CARRY; {weight_desc}, scale {args.combined_scale:g}, "
            f"dials LONG x{args.combined_long_multiplier:g} / CARRY x{args.combined_carry_multiplier:g}) ===",
            flush=True,
        )
        try:
            payload = _run_combined(
                args.panel_root,
                start,
                end,
                out,
                weight_carry=args.combined_weight,
                scale=args.combined_scale,
                long_multiplier=args.combined_long_multiplier,
                carry_multiplier=args.combined_carry_multiplier,
                long_profile=args.combined_long_profile,
            )
        except Exception as exc:  # noqa: BLE001 - report the combined book, keep going
            print(f"  [X] combined failed: {type(exc).__name__}: {exc}\n", flush=True)
            results["combined"] = {"error": str(exc)}
        else:
            wc = payload.get("weight_carry")
            if wc is not None:
                print(f"  blend weight: {1.0 - float(wc):.1%} LONG / {float(wc):.1%} CARRY (equal-risk)")
            print(f"  run_label = {payload['run_label']}")
            print(f"  {_headline(payload)}")
            print(f"  PNG: {payload.get('png') or '(none)'}\n", flush=True)
            results["combined"] = {"png": payload.get("png"), "run_label": payload["run_label"]}

    print("=" * 64)
    print("EQUITY CURVES - SUMMARY")
    entry_names = [
        s
        for s in [*sleeves, *(k for k in results if k.startswith("research:"))]
        if s != "combined"
    ]
    if "combined" in results:
        entry_names.append("combined")
    for s in entry_names:
        r = results.get(s, {})
        if r.get("error"):
            print(f"  {s:11} [X] {r['error'][:80]}")
        else:
            print(f"  {s:11} {r.get('run_label', '-'):42} {r.get('png') or '(no png)'}")
    # Keep going across sleeves, but exit non-zero so a driver cannot accept a
    # partial benchmark as complete.
    return 1 if any(result.get("error") for result in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
