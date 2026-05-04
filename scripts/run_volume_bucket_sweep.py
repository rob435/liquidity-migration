from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aggression_carry.config import VolumeGridConfig, load_config
from aggression_carry.volume_backtest import run_volume_grid


DEFAULT_BUCKETS = "core:1-20,mid:21-80,tail:81-150"
DEFAULT_SCORES = "dollar_volume_rank,volume_change_1d,volume_change_3d,volume_persistence,volume_composite"


def main() -> int:
    args = parse_args()
    data_root = Path(args.data_root)
    config = load_config(args.config, data_root=data_root)
    grid = VolumeGridConfig(
        scores=_csv_str(args.scores),
        quantiles=_csv_float(args.quantiles),
        hold_days=_csv_int(args.hold_days),
        fixed_stop_loss_pcts=_csv_float(args.fixed_stops),
        vol_stop_multipliers=_csv_float(args.vol_stops),
        rank_exit_modes=_csv_bool(args.rank_exits),
        include_reverse_side=args.include_reverse,
        take_profit_pcts=_csv_float(args.take_profits),
        cost_multipliers=_csv_float(args.cost_multipliers),
    )
    summary_rows = []
    for name, rank_min, rank_max in _parse_buckets(args.buckets):
        base = replace(
            config.volume_backtest,
            universe_rank_min=rank_min,
            universe_rank_max=rank_max,
            stop_mode="none",
            stop_loss_pct=0.0,
        )
        report_dir = data_root / "reports" / f"bucket_{name}"
        payload = run_volume_grid(
            data_root,
            grid_config=grid,
            base_backtest_config=base,
            cost_config=config.costs,
            max_workers=args.workers,
            report_dir=report_dir,
        )
        best = payload.get("best_total_return", {})
        summary_rows.append(
            {
                "bucket": name,
                "rank_min": rank_min,
                "rank_max": rank_max,
                "rows": payload["rows"],
                "workers": payload["workers"],
                **best,
            }
        )
        print(
            f"{name}: rows={payload['rows']} workers={payload['workers']} "
            f"best_return={best.get('total_return', 0.0):.2%} "
            f"sharpe={best.get('sharpe_like', 0.0):.2f} "
            f"score={best.get('score')} side={best.get('side_mode')} hold={best.get('hold_days')} "
            f"q={best.get('quantile')} rank_exit={best.get('rank_exit_enabled')}"
        )

    summary = pl.DataFrame(summary_rows, infer_schema_length=None)
    output_dir = data_root / "reports"
    output_dir.mkdir(parents=True, exist_ok=True)
    summary.write_csv(output_dir / "volume_bucket_sweep_summary.csv")
    (output_dir / "volume_bucket_sweep_summary.md").write_text(_format_summary(summary_rows), encoding="utf-8")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run volume-alpha grids by daily liquidity-rank bucket.")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--config", default=None)
    parser.add_argument("--workers", type=int, default=0)
    parser.add_argument("--buckets", default=DEFAULT_BUCKETS, help="Comma-separated name:min-max specs.")
    parser.add_argument("--scores", default=DEFAULT_SCORES)
    parser.add_argument("--quantiles", default="0.2,0.3,0.5")
    parser.add_argument("--hold-days", default="3,7,14")
    parser.add_argument("--fixed-stops", default="0")
    parser.add_argument("--vol-stops", default="")
    parser.add_argument("--rank-exits", default="false,true")
    parser.add_argument("--take-profits", default="0")
    parser.add_argument("--cost-multipliers", default="1")
    parser.add_argument("--include-reverse", action="store_true")
    return parser.parse_args()


def _parse_buckets(value: str) -> list[tuple[str, int, int]]:
    buckets = []
    for item in value.split(","):
        if not item.strip():
            continue
        name, _, ranks = item.partition(":")
        start, _, end = ranks.partition("-")
        if not name or not start or not end:
            raise ValueError(f"Invalid bucket spec: {item!r}")
        buckets.append((name.strip(), int(start), int(end)))
    return buckets


def _csv_int(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv_str(value))


def _csv_float(value: str) -> tuple[float, ...]:
    return tuple(float(item) for item in _csv_str(value))


def _csv_bool(value: str) -> tuple[bool, ...]:
    return tuple(item.lower() in {"1", "true", "yes", "y", "on"} for item in _csv_str(value))


def _csv_str(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _format_summary(rows: list[dict]) -> str:
    lines = [
        "# Volume Bucket Sweep",
        "",
        "| Bucket | Ranks | Score | Return | Sharpe | Max DD | Hold | Quantile | Rank Exit | Cost | Side | Long | Short |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['bucket']} | {row['rank_min']}-{row['rank_max']} | "
            f"{row.get('score', '')} | "
            f"{row.get('total_return', 0.0):.2%} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{row.get('max_drawdown', 0.0):.2%} | {row.get('hold_days')}d | "
            f"{row.get('quantile', 0.0):.0%} | {row.get('rank_exit_enabled')} | "
            f"{row.get('cost_multiplier', 1.0):.1f}x | {row.get('side_mode')} | {row.get('long_return', 0.0):.2%} | "
            f"{row.get('short_return', 0.0):.2%} |"
        )
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
