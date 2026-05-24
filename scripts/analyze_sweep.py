"""Aggregate all universe/rank sweep results into one ranked table."""
from __future__ import annotations

from pathlib import Path
import polars as pl

ROOT = Path("/tmp/sweep_serial")
SOURCES = [
    ("baseline", Path("/tmp/sweep_baseline_check/sweep_summary.csv")),
    ("u100_ri60_solo", Path("/tmp/sweep_solo_test/sweep_summary.csv")),
    ("axis_universe", ROOT / "axis_universe" / "sweep_summary.csv"),
    ("axis_ri", ROOT / "axis_ri" / "sweep_summary.csv"),
    ("joint_relax", ROOT / "joint_relax" / "sweep_summary.csv"),
    ("joint_tight", ROOT / "joint_tight" / "sweep_summary.csv"),
    ("ri_fine", ROOT / "ri_fine" / "sweep_summary.csv"),
    ("wide_tight", ROOT / "wide_tight" / "sweep_summary.csv"),
    ("h2_baseline", ROOT / "h2_baseline" / "sweep_summary.csv"),
    ("h2_ri200", ROOT / "h2_ri200" / "sweep_summary.csv"),
    ("h2_wide_ri200", ROOT / "h2_wide_ri200" / "sweep_summary.csv"),
    ("maxact7_u200", ROOT / "maxact7_u200" / "sweep_summary.csv"),
    ("maxact10_u200", ROOT / "maxact10_u200" / "sweep_summary.csv"),
    ("maxact10_u260", ROOT / "maxact10_u260" / "sweep_summary.csv"),
    ("rmin51_u150", ROOT / "rmin51_u150" / "sweep_summary.csv"),
    ("rmin11_u150", ROOT / "rmin11_u150" / "sweep_summary.csv"),
    ("rmin51_u200", ROOT / "rmin51_u200" / "sweep_summary.csv"),
    ("h2_3pos", ROOT / "h2_3pos" / "sweep_summary.csv"),
    ("h3_3pos", ROOT / "h3_3pos" / "sweep_summary.csv"),
    ("quality_close050_base", ROOT / "quality_close050_base" / "sweep_summary.csv"),
    ("q_ri100_close050", ROOT / "q_ri100_close050" / "sweep_summary.csv"),
    ("q_ri80_close055", ROOT / "q_ri80_close055" / "sweep_summary.csv"),
    ("q_u200_close050", ROOT / "q_u200_close050" / "sweep_summary.csv"),
    ("q_u260_close055", ROOT / "q_u260_close055" / "sweep_summary.csv"),
    ("q_resid012_base", ROOT / "q_resid012_base" / "sweep_summary.csv"),
    ("q_tor10_base", ROOT / "q_tor10_base" / "sweep_summary.csv"),
    ("q_joint_relax_rescue", ROOT / "q_joint_relax_rescue" / "sweep_summary.csv"),
    ("q_u200_evrkf070", ROOT / "q_u200_evrkf070" / "sweep_summary.csv"),
    ("q_u180_close050", ROOT / "q_u180_close050" / "sweep_summary.csv"),
    ("q_u130_close050", ROOT / "q_u130_close050" / "sweep_summary.csv"),
    ("q_u220_close050", ROOT / "q_u220_close050" / "sweep_summary.csv"),
    ("q_ri120_close050", ROOT / "q_ri120_close050" / "sweep_summary.csv"),
    ("q_u180_close045", ROOT / "q_u180_close045" / "sweep_summary.csv"),
    ("q_u200_close040", ROOT / "q_u200_close040" / "sweep_summary.csv"),
    ("q_ri130_close050", ROOT / "q_ri130_close050" / "sweep_summary.csv"),
    ("L_u200_h5", ROOT / "L_u200_h5" / "sweep_summary.csv"),
    ("L_u200_h2_close050", ROOT / "L_u200_h2_close050" / "sweep_summary.csv"),
    ("L_u200_stop015", ROOT / "L_u200_stop015" / "sweep_summary.csv"),
    ("L_u170_ri130", ROOT / "L_u170_ri130" / "sweep_summary.csv"),
    ("L_u160", ROOT / "L_u160" / "sweep_summary.csv"),
    ("L_u160_ri140", ROOT / "L_u160_ri140" / "sweep_summary.csv"),
    ("L_max10_u200_h2", ROOT / "L_max10_u200_h2" / "sweep_summary.csv"),
    ("h2_close050_base", ROOT / "h2_close050_base" / "sweep_summary.csv"),
    ("L_umin1", ROOT / "L_umin1" / "sweep_summary.csv"),
]


def main() -> None:
    frames = []
    for tag, p in SOURCES:
        if not p.exists() or p.stat().st_size < 100:
            continue
        try:
            df = pl.read_csv(p).with_columns(pl.lit(tag).alias("source"))
            if df.height == 0:
                continue
        except Exception:
            continue
        frames.append(df)
    if not frames:
        print("No data yet")
        return
    df = pl.concat(frames)
    df = df.unique(subset=["universe_rank_max", "rank_improvement_min", "source"])
    print(f"Total cells: {df.height}")
    print()
    # Headline: rank by avg_split_sharpe
    print("=== Top by avg_split_sharpe ===")
    top = df.sort("avg_split_sharpe", descending=True).head(10)
    for r in top.iter_rows(named=True):
        print(
            f"  u={r['universe_rank_max']:3d}/ri={r['rank_improvement_min']:3d} | "
            f"trades={r['trades']:>3d} ret={r['total_return']*100:>5.0f}% "
            f"DD={r['max_drawdown']*100:>5.1f}% sharpe={r['avg_split_sharpe']:.2f} "
            f"train={r['train_2023_2024_sharpe']:.2f}/val={r['validation_2024_2025_sharpe']:.2f}/oos={r['oos_2025_2026_sharpe']:.2f} "
            f"pos={r['positive_splits']}/3 promote={r['promotion_gate_pass']} ({r['source']})"
        )
    print()
    # Rank by min validation+OOS sharpe (robustness)
    df = df.with_columns(
        ((pl.col("validation_2024_2025_sharpe") + pl.col("oos_2025_2026_sharpe")) / 2).alias("val_oos_avg_sharpe")
    )
    print("=== Top by (val + OOS)/2 sharpe (robustness) ===")
    top_rob = df.sort("val_oos_avg_sharpe", descending=True).head(10)
    for r in top_rob.iter_rows(named=True):
        print(
            f"  u={r['universe_rank_max']:3d}/ri={r['rank_improvement_min']:3d} | "
            f"trades={r['trades']:>3d} ret={r['total_return']*100:>5.0f}% "
            f"DD={r['max_drawdown']*100:>5.1f}% sharpe={r['avg_split_sharpe']:.2f} "
            f"val+oos/2={r['val_oos_avg_sharpe']:.2f} "
            f"pos={r['positive_splits']}/3 promote={r['promotion_gate_pass']} ({r['source']})"
        )
    print()
    # Best return
    print("=== Top by total return ===")
    top_ret = df.sort("total_return", descending=True).head(10)
    for r in top_ret.iter_rows(named=True):
        print(
            f"  u={r['universe_rank_max']:3d}/ri={r['rank_improvement_min']:3d} | "
            f"trades={r['trades']:>3d} ret={r['total_return']*100:>5.0f}% "
            f"DD={r['max_drawdown']*100:>5.1f}% sharpe={r['avg_split_sharpe']:.2f} "
            f"pos={r['positive_splits']}/3 promote={r['promotion_gate_pass']} ({r['source']})"
        )
    print()
    # Best drawdown
    print("=== Top by drawdown (least negative) ===")
    top_dd = df.sort("max_drawdown", descending=True).head(10)
    for r in top_dd.iter_rows(named=True):
        print(
            f"  u={r['universe_rank_max']:3d}/ri={r['rank_improvement_min']:3d} | "
            f"trades={r['trades']:>3d} ret={r['total_return']*100:>5.0f}% "
            f"DD={r['max_drawdown']*100:>5.1f}% sharpe={r['avg_split_sharpe']:.2f} "
            f"pos={r['positive_splits']}/3 promote={r['promotion_gate_pass']} ({r['source']})"
        )


if __name__ == "__main__":
    main()
