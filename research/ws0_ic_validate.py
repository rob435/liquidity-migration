"""WS-0b — validate the rebuilt IC tool on real data.

Not a research IC measurement (that is WS-2, reserved for IS-train). This only
reproduces the per-component ICs already published in docs/reversion_alpha_report.md
to confirm the tool is correct, and shows the composite — computed through the
SAME per-day path as the components — is a sane number, not the bug's +0.176.

Usage:  python -m research.ws0_ic_validate <window>   (default oos_binance)
"""
from __future__ import annotations

import sys

import polars as pl

from liquidity_migration.reversion_alpha import ReversionConfig, compute_reversion_score
from liquidity_migration.volume_events import _enriched_event_features, DEFAULT_EXCLUDED_SYMBOLS  # noqa: F401
from liquidity_migration.volume_features import build_volume_features
from liquidity_migration.storage import read_dataset
from liquidity_migration.ic_diagnostic import add_forward_short_returns, ic_table

WINDOWS = {
    "oos_binance": ("~/SHARED_DATA/binance_oos_pit", "2020-09-01", "2023-05-01"),
    "oos_bybit": ("~/SHARED_DATA/bybit_oos_pre2023", "2022-04-01", "2023-05-03"),
    "is_train": ("~/SHARED_DATA/bybit_fullpit_1h", "2023-09-01", "2024-09-01"),
}

# report per-component IC, docs/reversion_alpha_report.md (3d-forward short return)
REPORT_IC = {
    "oos_binance": {"z_rank_jump": 0.041, "z_residual_return": 0.026,
                    "z_turnover_ratio": 0.019, "z_close_location": 0.003},
    "is_train": {"z_rank_jump": 0.026, "z_residual_return": 0.029,
                 "z_turnover_ratio": 0.014, "z_close_location": 0.001},
}


def main() -> None:
    window = sys.argv[1] if len(sys.argv) > 1 else "oos_binance"
    root, start, end = WINDOWS[window]
    cfg = ReversionConfig(start=start, end=end)

    klines = read_dataset(root, "klines_1h")
    manifest = read_dataset(root, "archive_trade_manifest")
    features = _enriched_event_features(
        build_volume_features(klines), klines, manifest,
        funding=read_dataset(root, "funding"),
        open_interest=read_dataset(root, "open_interest"),
        signed_flow_1h=pl.DataFrame(),
        mark_price_1h=read_dataset(root, "mark_price_1h"),
        index_price_1h=read_dataset(root, "index_price_1h"),
        premium_index_1h=read_dataset(root, "premium_index_1h"),
    )
    if start:
        features = features.filter(pl.col("date") >= start)
    if end:
        features = features.filter(pl.col("date") < end)

    scored = compute_reversion_score(features, cfg)
    print(f"window={window}  scored rows={scored.height}  days={scored['date'].n_unique()}")

    panel = add_forward_short_returns(scored, klines, [3], entry_delay_hours=1)
    signals = ["z_rank_jump", "z_residual_return", "z_turnover_ratio",
               "z_close_location", "reversion_score"]
    tbl = ic_table(panel, signals, "fwd_short_return_3d", min_names=10)

    rep = REPORT_IC.get(window, {})
    print(f"\n{'signal':<20}{'mean_ic':>10}{'t_stat':>9}{'hit':>8}{'n_days':>8}{'report':>9}")
    for r in tbl.iter_rows(named=True):
        rv = rep.get(r["signal"])
        rep_s = f"{rv:+.3f}" if rv is not None else "  --"
        print(f"  {r['signal']:<18}{r['mean_ic']:>10.4f}{r['t_stat']:>9.2f}"
              f"{r['hit_rate']:>8.3f}{r['n_days']:>8}{rep_s:>9}")

    comp = next(r for r in tbl.iter_rows(named=True) if r["signal"] == "reversion_score")
    best = max(r["mean_ic"] for r in tbl.iter_rows(named=True) if r["signal"] != "reversion_score")
    print(f"\ncomposite IC = {comp['mean_ic']:+.4f}  | best component = {best:+.4f}"
          f"  | ratio = {comp['mean_ic']/best if best else float('nan'):.2f}x")
    print("bug check: composite must be within ~sqrt(4)=2x of the best component;"
          " the reported +0.176 was ~4x and impossible.")


if __name__ == "__main__":
    main()
