#!/usr/bin/env python3
"""Build the cached research panel for the idio-chart-versus-raw-chart screen.

The claim under test: chart signals recomputed on an idiosyncratic price path
yield higher Sharpe than the same signals on the raw path.

Deciding that needs four price sources, not two, because a naive raw-versus-idio
comparison is confounded twice over:

``raw``
    Chart features on the raw daily close through decision day ``D``.
``rawlag``
    The SAME features on the raw close through ``D-3``. This is the control
    that matters. An idio path is built from ``fit_factor_returns`` residuals,
    which describe the move completing at ``d+2`` and are readable at ``d+3``,
    so the idio arm is structurally three days stale. Scoring it against a raw
    arm that sees today's close measures information age, not idiosyncrasy.
``idio``
    Chart features on the cumulated residual path.
``iz``
    Chart features on the volatility-standardised residual path.

and two forward targets, because "higher Sharpe" is ambiguous about what is
actually harvestable:

``fwd_ret_1d``
    The raw executable forward return -- what a cross-sectional long/short on
    the underlying perp actually earns. Mode (a): no hedge legs held.
``resid_fwd``
    The residual forward return for the same day -- what the signal earns on
    the idiosyncratic component alone. Mode (b): only realisable by holding the
    factor hedge, whose cost this panel does NOT model.

A signal that wins on ``resid_fwd`` and loses on ``fwd_ret_1d`` has found
something real and untradeable at this cost basis. Reporting only one of the
two is how that distinction gets lost.

PRIMARY comparison: ``idio`` versus ``rawlag`` on ``fwd_ret_1d``. Every other
cell is reported as a diagnostic.

Dispatch:
    POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/data/build_idio_panel.py \\
        --root ~/SHARED_DATA/bybit_full_pit --start 2023-06-01 --end 2026-07-01 \\
        --out data/idio_panel/bybit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(line_buffering=True, encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration._common import MS_PER_DAY  # noqa: E402
from liquidity_migration.daily_feature_panel import (  # noqa: E402
    _aggregate_daily_klines,
    _autodetect_dataset_names,
    _date_str_to_ms,
    _read_window,
)
from liquidity_migration.idio_features import CHART_FEATURES, chart_features  # noqa: E402
from liquidity_migration.residual_price import (  # noqa: E402
    RESIDUAL_AVAILABILITY_SHIFT_DAYS,
    add_log_forward_return,
    build_idio_price,
)
from liquidity_migration.risk_model import (  # noqa: E402
    COMMON4_FACTOR_COLUMNS,
    build_factor_panel,
    build_factor_panel_from_daily,
    fit_factor_returns,
)
from liquidity_migration.storage import read_dataset_columns  # noqa: E402
from liquidity_migration.volume_events_pit import filter_klines_to_pit_membership  # noqa: E402

#: The six-factor set, for the factor-set-sensitivity arm. Residualising on
#: ``premium_index_z`` and ``funding_rate_z`` removes the premium/funding
#: anomalies themselves, so "idio" means something different here.
FULL6_FACTOR_COLUMNS = (
    "btc_beta",
    "xs_rank_ret_30d",
    "realized_vol_rank",
    "liquidity_rank",
    "funding_rate_z",
    "premium_index_z",
)

#: COMMON4 minus ``xs_rank_ret_30d``. The residual to COMMON4 is orthogonal to
#: 30-day cross-sectional momentum by construction, so a momentum feature on
#: that path measures momentum-shape with momentum regressed out. This set makes
#: the momentum arms a fair test under a deliberately different "idio".
NOMOM3_FACTOR_COLUMNS = (
    "btc_beta",
    "realized_vol_rank",
    "liquidity_rank",
)

PRICE_SOURCES = ("raw", "rawlag", "idio", "iz")


#: Warm-up for the rolling-60 BTC beta plus the 30d chart windows, matching the
#: 90-day pad ``risk_model.build_factor_panel`` applies internally.
PAD_BACK_DAYS = 90
#: ``_attach_forward_returns`` needs closes at D+2 for a 1-day forward return.
PAD_FORWARD_DAYS = 3


def _daily_klines(root: Path, *, start: str, end: str, require_pit: bool) -> pl.DataFrame:
    """Read the kline store once and return PIT-filtered daily bars.

    ``build_factor_panel`` reads five stores per call, and on a ~500k-file
    partitioned root that enumeration dominates runtime. COMMON4 and the
    ``first_bar_close`` forward return are all kline-derived, so read klines once
    and hand the daily bars to ``build_factor_panel_from_daily``. Funding and
    premium come back null, which COMMON4 does not use; the ``full6`` arm needs
    them and must use the full reader.
    """
    name = _autodetect_dataset_names(root)["klines_dataset"]
    klines = _read_window(
        root,
        name,
        start_ms=_date_str_to_ms(start) - PAD_BACK_DAYS * MS_PER_DAY,
        end_ms=_date_str_to_ms(end) + PAD_FORWARD_DAYS * MS_PER_DAY,
        columns=[
            "ts_ms", "symbol", "open", "high", "low", "close",
            "volume_base", "turnover_quote", "date",
        ],
    )
    if klines.is_empty():
        return klines
    if require_pit:
        manifest = read_dataset_columns(root, "archive_trade_manifest", columns=["date", "symbol"])
        klines, _receipt = filter_klines_to_pit_membership(klines, manifest)
    return _aggregate_daily_klines(klines)


def _lagged(features: pl.DataFrame, *, days: int) -> pl.DataFrame:
    """Re-index features forward by ``days`` so they are read that much later.

    Calendar offset, not a positional shift: a gap would stretch a positional
    shift and make the control arm's information age vary by symbol.
    """
    return features.with_columns(pl.col("ts_ms") + days * MS_PER_DAY)


def _residuals_for(
    panel: pl.DataFrame, factor_cols: tuple[str, ...], *, label: str,
    target: str = "fwd_logret_1d",
) -> pl.DataFrame:
    present = [c for c in factor_cols if c in panel.columns]
    missing = sorted(set(factor_cols) - set(present))
    if missing:
        print(f"  [{label}] WARNING: panel lacks factors {missing}; fitting on {present}")
    _factor_returns, residuals = fit_factor_returns(
        panel, factor_cols=present, target_col=target
    )
    print(f"  [{label}] residuals rows={residuals.height} days={residuals['ts_ms'].n_unique()}")
    return residuals


def build(
    root: Path,
    *,
    start: str,
    end: str,
    out: Path,
    require_pit: bool,
    factor_set: str,
) -> pl.DataFrame:
    factor_cols = {
        "common4": COMMON4_FACTOR_COLUMNS,
        "nomom3": NOMOM3_FACTOR_COLUMNS,
        "full6": FULL6_FACTOR_COLUMNS,
    }[factor_set]

    print(f"[1/6] daily klines {root.name} [{start}..{end}) pit={require_pit} (single read) ...")
    daily = _daily_klines(root, start=start, end=end, require_pit=require_pit)
    if daily.is_empty():
        raise SystemExit(f"empty daily klines for {root}")
    print(f"      rows={daily.height} symbols={daily['symbol'].n_unique()}")

    print("[2/6] factor panel from daily bars ...")
    if factor_set in ("common4", "nomom3"):
        panel = build_factor_panel_from_daily(daily, start=start, end=end)
    else:
        # full6 needs funding and premium, which the daily-bar owner cannot
        # supply; fall back to the full reader and pay for the extra stores.
        print("      full6 requires funding/premium -> full reader (slower)")
        panel = build_factor_panel(root, start=start, end=end, require_pit_membership=require_pit)
    if panel.is_empty():
        raise SystemExit(f"empty factor panel for {root}")
    print(f"      rows={panel.height} symbols={panel['symbol'].n_unique()} days={panel['ts_ms'].n_unique()}")

    print(f"[3/6] residuals on {factor_set} ({len(factor_cols)} factors) ...")
    panel = add_log_forward_return(panel)
    residuals = _residuals_for(panel, factor_cols, label=factor_set)
    if residuals.is_empty():
        raise SystemExit("no residuals fitted")

    # Two residual series, not interchangeable:
    #   log residual (above)    -> cumulated into the idio PATH; cumsum of simple
    #                              returns does not compound.
    #   simple residual (below) -> the mode-(b) P&L TARGET; a trader earns the
    #                              arithmetic return.
    # A log-return target imports the variance drag E[log(1+r)] ~ E[r] - Var(r)/2
    # (about -34.76 bp/day here, strongly cross-sectional), so any
    # volatility-correlated signal scores a large spurious spread against it.
    print("      simple-return residuals for the P&L target ...")
    residuals_simple = _residuals_for(panel, factor_cols, label=f"{factor_set}/simple", target="fwd_ret_1d")

    print("[4/6] idio price paths ...")
    idio = build_idio_price(residuals, end=end, residual_scale="log")
    print(f"      rows={idio.height} symbols={idio['symbol'].n_unique()} "
          f"filled={idio['is_filled'].mean():.4f}")

    print("[5/6] chart features on four price sources (reusing the single read) ...")
    raw_feats = chart_features(daily, price_col="close", prefix="raw_")
    rawlag_feats = _lagged(
        chart_features(daily, price_col="close", prefix="rawlag_"),
        days=RESIDUAL_AVAILABILITY_SHIFT_DAYS,
    )
    idio_feats = chart_features(idio, price_col="idio_close", prefix="idio_")
    # idio_zpath is a cumulative z-score and can go negative, so shift it positive
    # before the ratio-shaped features touch it. Range position and the MA ratio
    # keep their ordering under a constant offset; ret_Nd does not, which is why
    # this arm stays a diagnostic.
    iz = idio.with_columns((pl.col("idio_zpath") + 100.0).alias("_iz"))
    iz_feats = chart_features(iz, price_col="_iz", prefix="iz_")

    print("[6/6] assembling ...")
    turnover = daily.select("symbol", "ts_ms", "turnover_quote")
    out_panel = (
        panel.select(
            "symbol", "ts_ms", "date", "fwd_ret_1d", "fwd_logret_1d",
            # btc_beta is the only COMMON4 exposure with a tradable hedge
            # instrument, so mode (b) is in practice a BTC-beta hedge.
            *( ["btc_beta"] if "btc_beta" in panel.columns else [] ),
        )
        .join(residuals.rename({"residual_return": "resid_fwd_log"}), on=["symbol", "ts_ms"], how="left")
        .join(residuals_simple.rename({"residual_return": "resid_fwd"}), on=["symbol", "ts_ms"], how="left")
        .join(turnover, on=["symbol", "ts_ms"], how="left")
        .join(raw_feats, on=["symbol", "ts_ms"], how="left")
        .join(rawlag_feats, on=["symbol", "ts_ms"], how="left")
        .join(idio_feats, on=["symbol", "ts_ms"], how="left")
        .join(iz_feats, on=["symbol", "ts_ms"], how="left")
        .join(
            idio.select("symbol", "ts_ms", "coverage", "is_filled"),
            on=["symbol", "ts_ms"], how="left",
        )
        .filter(pl.col("ts_ms") >= _date_str_to_ms(start))
        .sort(["ts_ms", "symbol"])
    )

    out.mkdir(parents=True, exist_ok=True)
    residuals.write_parquet(out / f"residuals_{factor_set}.parquet")
    idio.write_parquet(out / f"idio_price_{factor_set}.parquet")
    out_panel.write_parquet(out / f"panel_{factor_set}.parquet")

    print(f"\nwrote {out}/panel_{factor_set}.parquet  rows={out_panel.height} "
          f"symbols={out_panel['symbol'].n_unique()} days={out_panel['ts_ms'].n_unique()}")
    for source in PRICE_SOURCES:
        col = f"{source}_{CHART_FEATURES[0]}"
        if col in out_panel.columns:
            n = out_panel[col].drop_nulls().len()
            print(f"  {source:7s} non-null {CHART_FEATURES[0]}: {n:>8d} "
                  f"({100.0 * n / out_panel.height:5.1f}%)")
    return out_panel


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True, help="exclusive")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--factor-set", choices=("common4", "nomom3", "full6"), default="common4")
    ap.add_argument(
        "--no-pit", action="store_true",
        help="skip archive-manifest PIT filtering (labels the run as non-decision-grade)",
    )
    args = ap.parse_args(argv)

    build(
        args.root.expanduser(),
        start=args.start,
        end=args.end,
        out=args.out.expanduser(),
        require_pit=not args.no_pit,
        factor_set=args.factor_set,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
