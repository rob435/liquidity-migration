from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl

SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)


def run_portfolio_hedge_report(
    *,
    short_report_dir: str | Path,
    long_report_dirs: list[str | Path],
    hedge_weights: list[float],
    report_dir: str | Path,
) -> dict[str, Any]:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    short_daily = _daily_basket_returns(Path(short_report_dir), "short_return")
    rows: list[dict[str, Any]] = []
    for long_dir in long_report_dirs:
        long_path = Path(long_dir)
        long_daily = _daily_basket_returns(long_path, "long_return")
        joined = (
            short_daily.join(long_daily, on="exit_date", how="full", coalesce=True)
            .with_columns(
                [
                    pl.col("short_return").fill_null(0.0),
                    pl.col("long_return").fill_null(0.0),
                ]
            )
            .sort("exit_date")
        )
        common = short_daily.join(long_daily, on="exit_date", how="inner")
        correlation = float(common.select(pl.corr("short_return", "long_return")).item()) if common.height > 2 else 0.0
        short_bad_dates = _short_bad_dates(short_daily)
        long_bad_additive = float(joined.filter(pl.col("exit_date").is_in(short_bad_dates))["long_return"].sum())
        long_worst20_additive = float(
            joined.filter(pl.col("exit_date").is_in(_short_bad_dates(short_daily, count=20)))["long_return"].sum()
        )
        long_metrics = _path_metrics(long_daily.rename({"long_return": "portfolio_return"}))
        for weight in hedge_weights:
            combo = joined.select(
                [
                    "exit_date",
                    (pl.col("short_return") + weight * pl.col("long_return")).alias("portfolio_return"),
                ]
            )
            combo_metrics = _path_metrics(combo)
            rows.append(
                {
                    "long_name": long_path.name,
                    "hedge_weight": weight,
                    "long_total_return": long_metrics["total_return"],
                    "long_max_drawdown": long_metrics["max_drawdown"],
                    "common_day_correlation": correlation,
                    "long_return_on_short_bad_10pct": long_bad_additive,
                    "long_return_on_short_worst20": long_worst20_additive,
                    **{f"combo_{key}": value for key, value in combo_metrics.items() if key != "splits"},
                    **{f"combo_{name}_return": value for name, value in combo_metrics["splits"]},
                }
            )
    summary = pl.DataFrame(rows, infer_schema_length=None).sort(["combo_max_drawdown", "combo_total_return"], descending=[True, True])
    summary_path = target / "portfolio_hedge_summary.csv"
    report_path = target / "portfolio_hedge_report.md"
    summary.write_csv(summary_path)
    payload = {
        "short_report_dir": str(short_report_dir),
        "long_report_dirs": [str(item) for item in long_report_dirs],
        "hedge_weights": hedge_weights,
        "summary_path": str(summary_path),
        "report_path": str(report_path),
        "rows": summary.to_dicts(),
    }
    report_path.write_text(format_portfolio_hedge_report(payload), encoding="utf-8")
    return payload


def format_portfolio_hedge_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Portfolio Hedge Report",
        "",
        f"- Short report: `{payload['short_report_dir']}`",
        f"- Long reports: {len(payload['long_report_dirs'])}",
        f"- Hedge weights: {', '.join(str(item) for item in payload['hedge_weights'])}",
        "",
        "| Long | Weight | Combo Return | Combo Max DD | Combo Worst 90d | Train | Validation | OOS | Long Bad 10% | Corr |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["rows"]:
        lines.append(
            f"| `{row['long_name']}` | {row['hedge_weight']:.2f} | {_pct(row['combo_total_return'])} | "
            f"{_pct(row['combo_max_drawdown'])} | {_pct(row['combo_worst_90d_return'])} | "
            f"{_pct(row.get('combo_train_2023_2024_return'))} | {_pct(row.get('combo_validation_2024_2025_return'))} | "
            f"{_pct(row.get('combo_oos_2025_2026_return'))} | {_pct(row['long_return_on_short_bad_10pct'])} | "
            f"{float(row['common_day_correlation']):.3f} |"
        )
    lines.extend(
        [
            "",
            "This is a portfolio overlay diagnostic. A long leg is not promoted unless it improves the short book's bad periods without adding an unacceptable standalone drawdown or relying on missing stress evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def _daily_basket_returns(report_dir: Path, column_name: str) -> pl.DataFrame:
    baskets_path = report_dir / "volume_event_best_baskets.csv"
    if not baskets_path.exists():
        raise FileNotFoundError(f"Missing basket ledger: {baskets_path}")
    baskets = pl.read_csv(baskets_path)
    if baskets.is_empty():
        return pl.DataFrame({"exit_date": pl.Series([], dtype=pl.String), column_name: pl.Series([], dtype=pl.Float64)})
    return baskets.group_by("exit_date").agg(pl.col("basket_return").sum().alias(column_name)).sort("exit_date")


def _short_bad_dates(short_daily: pl.DataFrame, *, count: int | None = None) -> list[str]:
    if short_daily.is_empty():
        return []
    row_count = count if count is not None else max(10, int(short_daily.height * 0.10))
    return short_daily.sort("short_return").head(row_count)["exit_date"].to_list()


def _path_metrics(daily: pl.DataFrame) -> dict[str, Any]:
    returns = [float(item) for item in daily["portfolio_return"].to_list()]
    dates = [str(item) for item in daily["exit_date"].to_list()]
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    max_drawdown_date = ""
    for date, value in zip(dates, returns):
        equity *= 1.0 + value
        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        if drawdown < max_drawdown:
            max_drawdown = drawdown
            max_drawdown_date = date
    return {
        "total_return": equity - 1.0,
        "max_drawdown": max_drawdown,
        "max_drawdown_date": max_drawdown_date,
        "worst_30d_return": _worst_rolling_return(returns, 30),
        "worst_60d_return": _worst_rolling_return(returns, 60),
        "worst_90d_return": _worst_rolling_return(returns, 90),
        "splits": _split_returns(daily),
    }


def _worst_rolling_return(returns: list[float], window: int) -> float:
    if not returns:
        return 0.0
    worst = 0.0
    for index in range(len(returns)):
        equity = 1.0
        for value in returns[index : index + window]:
            equity *= 1.0 + value
        worst = min(worst, equity - 1.0)
    return worst


def _split_returns(daily: pl.DataFrame) -> list[tuple[str, float]]:
    rows = []
    for name, start, end in SPLITS:
        split = daily.filter((pl.col("exit_date") >= start) & (pl.col("exit_date") < end))
        equity = 1.0
        for value in split["portfolio_return"].to_list():
            equity *= 1.0 + float(value)
        rows.append((name, equity - 1.0))
    return rows


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number:.2%}"
