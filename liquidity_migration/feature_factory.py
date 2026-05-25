from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import finite_float, pct


# Optional split layout for diagnostic edge stability. Callers pass `splits`
# via run_feature_factory_report; the default is `()` (whole-period only).


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    family: str
    causal_note: str


FEATURE_SPECS = (
    FeatureSpec("event_uniqueness_score", "event uniqueness", "same-day cross-section after the signal close"),
    FeatureSpec("liquidity_rank_improvement_1d", "rank migration", "today's rank minus shifted prior rank"),
    FeatureSpec("liquidity_rank_improvement_3d", "rank migration", "today's rank minus shifted prior rank"),
    FeatureSpec("liquidity_rank_improvement_7d", "rank migration", "today's rank minus shifted prior rank"),
    FeatureSpec("liquidity_rank_speed_3d", "rank migration", "3d rank migration divided by three"),
    FeatureSpec("intraday_range_expansion_7d", "local volatility", "signal-day range versus shifted prior 7d mean"),
    FeatureSpec("prior7_intraday_range_mean", "local volatility", "shifted prior 7d mean range"),
    FeatureSpec("mark_index_basis_last", "perp basis", "last hourly mark/index basis in the signal day"),
    FeatureSpec("mark_index_basis_1d_mean", "perp basis", "mean hourly mark/index basis in the signal day"),
    FeatureSpec("mark_index_basis_3d_mean", "perp basis", "rolling mean of daily basis means through signal day"),
    FeatureSpec("premium_index_last", "perp premium", "last hourly premium index in the signal day"),
    FeatureSpec("premium_index_1d_mean", "perp premium", "mean hourly premium index in the signal day"),
    FeatureSpec("premium_index_3d_mean", "perp premium", "rolling mean of daily premium means through signal day"),
    FeatureSpec("funding_rate_last", "funding", "last funding observation mapped to the signal day"),
    FeatureSpec("funding_rate_3d_sum", "funding", "rolling sum through the signal day"),
    FeatureSpec("funding_rate_7d_sum", "funding", "rolling sum through the signal day"),
    FeatureSpec("open_interest_return_3d", "open interest", "open-interest change through the signal day"),
    FeatureSpec("open_interest_return_7d", "open interest", "open-interest change through the signal day"),
    FeatureSpec("volume_to_open_interest_quote", "leverage/liquidity", "signal-day quote turnover divided by OI quote value"),
    FeatureSpec("taker_imbalance_1d", "flow", "signal-day signed taker flow"),
    FeatureSpec("taker_imbalance_3d", "flow", "rolling signed taker flow through the signal day"),
    FeatureSpec("residual_return_1d", "market residual", "signal-day return minus same-day market median"),
    FeatureSpec("market_pct_up_1d", "market regime", "same-day fraction of PIT symbols up"),
    FeatureSpec("signal_day_last6h_return", "event shape", "final six signal-day hours"),
    FeatureSpec("signal_day_last6h_turnover_share", "event shape", "final-six-hour turnover share"),
    FeatureSpec("prior30_max_daily_return", "failed pump memory", "shifted 30d max return before the signal day"),
    FeatureSpec("close_to_high_30d", "extension", "signal close versus trailing 30d high"),
)


def run_feature_factory_report(
    report_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    target_col: str = "net_return",
    min_rows: int = 12,
    shuffle_samples: int = 64,
    random_seed: int = 17,
    splits: tuple[tuple[str, str, str], ...] = (),
) -> dict[str, Any]:
    source = Path(report_dir).expanduser()
    trades_path = source / "volume_event_best_trades.csv"
    if not trades_path.exists():
        raise FileNotFoundError(f"Missing trade ledger: {trades_path}")
    target = Path(output_dir).expanduser() if output_dir else source / "feature_factory"
    target.mkdir(parents=True, exist_ok=True)
    trades = pl.read_csv(trades_path, infer_schema_length=None)
    coverage_rows = _coverage_rows(trades)
    edge_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    interaction_rows: list[dict[str, Any]] = []
    rng = np.random.default_rng(random_seed)
    for spec in FEATURE_SPECS:
        if spec.name not in trades.columns or target_col not in trades.columns:
            continue
        edge = _feature_edge(trades, spec.name, target_col, min_rows=min_rows, rng=rng, shuffle_samples=shuffle_samples, splits=splits)
        if edge:
            edge_rows.append({**edge, "family": spec.family, "causal_note": spec.causal_note})
        split_rows.extend(_split_edges(trades, spec.name, target_col, min_rows=max(6, min_rows // 2), splits=splits))
        interaction_rows.extend(_interaction_edges(trades, spec.name, target_col, min_rows=max(6, min_rows // 2)))

    coverage = pl.DataFrame(coverage_rows, infer_schema_length=None)
    edges = pl.DataFrame(edge_rows, infer_schema_length=None)
    split_edges = pl.DataFrame(split_rows, infer_schema_length=None)
    interactions = pl.DataFrame(interaction_rows, infer_schema_length=None)
    if not edges.is_empty():
        edges = edges.sort(["verdict_rank", "edge_over_shuffle_median_abs", "abs_high_low_edge"], descending=[True, True, True])
    coverage_path = target / "feature_factory_coverage.csv"
    edges_path = target / "feature_factory_edges.csv"
    split_path = target / "feature_factory_split_edges.csv"
    interaction_path = target / "feature_factory_interactions.csv"
    report_path = target / "feature_factory_report.md"
    json_path = target / "feature_factory_report.json"
    coverage.write_csv(coverage_path)
    edges.write_csv(edges_path)
    split_edges.write_csv(split_path)
    interactions.write_csv(interaction_path)
    payload = {
        "report_dir": str(source),
        "target_col": target_col,
        "rows": int(trades.height),
        "features_present": int(sum(1 for spec in FEATURE_SPECS if spec.name in trades.columns)),
        "features_with_coverage": int(sum(1 for row in coverage_rows if int(row["non_null_rows"]) > 0)),
        "features_expected": len(FEATURE_SPECS),
        "output_files": {
            "coverage": str(coverage_path),
            "edges": str(edges_path),
            "split_edges": str(split_path),
            "interactions": str(interaction_path),
            "markdown": str(report_path),
            "json": str(json_path),
        },
        "coverage": coverage.to_dicts(),
        "edges": edges.to_dicts(),
        "split_edges": split_edges.to_dicts(),
        "interactions": interactions.to_dicts(),
    }
    report_path.write_text(format_feature_factory_report(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def format_feature_factory_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Feature Factory Report",
        "",
        f"- Source report: `{payload['report_dir']}`",
        f"- Trade rows: {payload['rows']}",
        f"- Target column: `{payload['target_col']}`",
        f"- Present audited feature columns: {payload['features_present']}/{payload['features_expected']}",
        f"- Features with non-null coverage: {payload['features_with_coverage']}/{payload['features_expected']}",
        "",
        "This is a shadow research report. It does not promote a filter or change live trading. A feature becomes tradeable only after a separate Model Court run.",
        "",
        "## Leakage Guard",
        "",
        "All audited features are either signal-day close features, same-day cross-sectional ranks formed at the signal close, or shifted prior-window features. Basis and premium features are hourly aggregates mapped to the completed signal day.",
        "",
        "## Availability",
        "",
        "| Feature | Family | Coverage | Non-null | Causal note |",
        "|---|---|---:|---:|---|",
    ]
    coverage_by_name = {row["feature"]: row for row in payload["coverage"]}
    for spec in FEATURE_SPECS:
        row = coverage_by_name.get(spec.name, {})
        lines.append(
            f"| `{spec.name}` | {spec.family} | {_pct(row.get('coverage'))} | {int(row.get('non_null_rows') or 0)} | {spec.causal_note} |"
        )
    lines.extend(
        [
            "",
            "## Edge Screen",
            "",
            "| Feature | Verdict | Rows | High-Low Edge | Low Mean | High Mean | Shuffle Abs Median | Split Consistency | Crowded Edge |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["edges"][:20]:
        lines.append(
            f"| `{row['feature']}` | {row['verdict']} | {int(row['rows'])} | {_pct(row['high_low_edge'])} | "
            f"{_pct(row['low_mean_return'])} | {_pct(row['high_mean_return'])} | {_pct(row['shuffle_abs_edge_median'])} | "
            f"{int(row['split_consistency'])} | {_pct(row.get('crowded_high_low_edge'))} |"
        )
    lines.extend(
        [
            "",
            "## Readout",
            "",
            "- `PROMISING_SHADOW` means the feature has a high-vs-low bucket edge that beats its shuffled-feature control and has at least two split edges in the same direction.",
            "- `WATCH` means some edge exists but evidence is not stable enough for a parameter gate.",
            "- `NO_EDGE`, `THIN`, and `DATA_GAP` are rejections for now.",
            "",
        ]
    )
    return "\n".join(lines)


def _coverage_rows(trades: pl.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for spec in FEATURE_SPECS:
        if spec.name not in trades.columns:
            rows.append(
                {
                    "feature": spec.name,
                    "family": spec.family,
                    "present": False,
                    "rows": int(trades.height),
                    "non_null_rows": 0,
                    "coverage": 0.0,
                    "causal_note": spec.causal_note,
                }
            )
            continue
        values = [_finite_float(value) for value in trades[spec.name].to_list()]
        non_null = sum(value is not None for value in values)
        rows.append(
            {
                "feature": spec.name,
                "family": spec.family,
                "present": True,
                "rows": int(trades.height),
                "non_null_rows": int(non_null),
                "coverage": non_null / trades.height if trades.height else 0.0,
                "causal_note": spec.causal_note,
            }
        )
    return rows


def _feature_edge(
    trades: pl.DataFrame,
    feature: str,
    target_col: str,
    *,
    min_rows: int,
    rng: np.random.Generator,
    shuffle_samples: int,
    splits: tuple[tuple[str, str, str], ...] = (),
) -> dict[str, Any] | None:
    pairs = _finite_pairs(trades, feature, target_col)
    rows = len(pairs)
    if rows < min_rows:
        return _thin_edge_row(feature, rows)
    values = np.asarray([item[0] for item in pairs], dtype=float)
    targets = np.asarray([item[1] for item in pairs], dtype=float)
    edge = _tertile_edge(values, targets)
    if edge is None:
        return _thin_edge_row(feature, rows)
    shuffled_abs_edges = []
    for _ in range(max(0, shuffle_samples)):
        shuffled_abs_edges.append(abs(_tertile_edge_value(rng.permutation(values), targets)))
    shuffle_abs_median = float(np.median(shuffled_abs_edges)) if shuffled_abs_edges else 0.0
    split_edges = [
        row
        for row in _split_edges(trades, feature, target_col, min_rows=max(6, min_rows // 2), splits=splits)
        if row.get("high_low_edge") is not None
    ]
    split_consistency = _same_sign_count(edge["high_low_edge"], [float(row["high_low_edge"]) for row in split_edges])
    crowded_edge = _crowded_edge(trades, feature, target_col, min_rows=max(6, min_rows // 2))
    ratio = abs(edge["high_low_edge"]) / max(shuffle_abs_median, 1e-12)
    verdict, rank = _feature_verdict(rows=rows, ratio=ratio, split_consistency=split_consistency)
    return {
        "feature": feature,
        "rows": rows,
        **edge,
        "abs_high_low_edge": abs(edge["high_low_edge"]),
        "shuffle_abs_edge_median": shuffle_abs_median,
        "edge_over_shuffle_median_abs": ratio,
        "split_consistency": split_consistency,
        "crowded_high_low_edge": crowded_edge,
        "verdict": verdict,
        "verdict_rank": rank,
    }


def _split_edges(
    trades: pl.DataFrame,
    feature: str,
    target_col: str,
    *,
    min_rows: int,
    splits: tuple[tuple[str, str, str], ...] = (),
) -> list[dict[str, Any]]:
    if feature not in trades.columns or target_col not in trades.columns:
        return []
    rows = []
    for split_name, start, end in splits:
        split = _filter_split(trades, start, end)
        pairs = _finite_pairs(split, feature, target_col)
        if len(pairs) < min_rows:
            continue
        edge = _tertile_edge(np.asarray([item[0] for item in pairs]), np.asarray([item[1] for item in pairs]))
        if edge:
            rows.append({"feature": feature, "split": split_name, "rows": len(pairs), **edge})
    return rows


def _interaction_edges(trades: pl.DataFrame, feature: str, target_col: str, *, min_rows: int) -> list[dict[str, Any]]:
    if "crowding_entry_hour_signal_count" not in trades.columns:
        return []
    rows = []
    for label, predicate in (
        ("isolated_entry_hour", pl.col("crowding_entry_hour_signal_count") <= 1),
        ("crowded_entry_hour", pl.col("crowding_entry_hour_signal_count") >= 2),
    ):
        subset = trades.filter(predicate)
        pairs = _finite_pairs(subset, feature, target_col)
        if len(pairs) < min_rows:
            continue
        edge = _tertile_edge(np.asarray([item[0] for item in pairs]), np.asarray([item[1] for item in pairs]))
        if edge:
            rows.append({"feature": feature, "interaction": label, "rows": len(pairs), **edge})
    return rows


def _crowded_edge(trades: pl.DataFrame, feature: str, target_col: str, *, min_rows: int) -> float | None:
    rows = _interaction_edges(trades, feature, target_col, min_rows=min_rows)
    for row in rows:
        if row["interaction"] == "crowded_entry_hour":
            return float(row["high_low_edge"])
    return None


def _finite_pairs(frame: pl.DataFrame, feature: str, target_col: str) -> list[tuple[float, float]]:
    if frame.is_empty() or feature not in frame.columns or target_col not in frame.columns:
        return []
    pairs = []
    for feature_value, target_value in zip(frame[feature].to_list(), frame[target_col].to_list(), strict=False):
        feature_float = _finite_float(feature_value)
        target_float = _finite_float(target_value)
        if feature_float is not None and target_float is not None:
            pairs.append((feature_float, target_float))
    return pairs


def _tertile_edge(values: np.ndarray, targets: np.ndarray) -> dict[str, float] | None:
    low_cut = float(np.quantile(values, 1.0 / 3.0))
    high_cut = float(np.quantile(values, 2.0 / 3.0))
    low = targets[values <= low_cut]
    mid = targets[(values > low_cut) & (values < high_cut)]
    high = targets[values >= high_cut]
    if low.size == 0 or high.size == 0:
        return None
    low_mean = float(np.mean(low))
    high_mean = float(np.mean(high))
    return {
        "low_cut": low_cut,
        "high_cut": high_cut,
        "low_rows": int(low.size),
        "mid_rows": int(mid.size),
        "high_rows": int(high.size),
        "low_mean_return": low_mean,
        "mid_mean_return": float(np.mean(mid)) if mid.size else 0.0,
        "high_mean_return": high_mean,
        "high_low_edge": high_mean - low_mean,
    }


def _tertile_edge_value(values: np.ndarray, targets: np.ndarray) -> float:
    edge = _tertile_edge(values, targets)
    return float(edge["high_low_edge"]) if edge else 0.0


def _thin_edge_row(feature: str, rows: int) -> dict[str, Any]:
    verdict = "DATA_GAP" if rows == 0 else "THIN"
    return {
        "feature": feature,
        "rows": rows,
        "low_cut": None,
        "high_cut": None,
        "low_rows": 0,
        "mid_rows": 0,
        "high_rows": 0,
        "low_mean_return": None,
        "mid_mean_return": None,
        "high_mean_return": None,
        "high_low_edge": None,
        "abs_high_low_edge": 0.0,
        "shuffle_abs_edge_median": None,
        "edge_over_shuffle_median_abs": 0.0,
        "split_consistency": 0,
        "crowded_high_low_edge": None,
        "verdict": verdict,
        "verdict_rank": 0,
    }


def _feature_verdict(*, rows: int, ratio: float, split_consistency: int) -> tuple[str, int]:
    if rows <= 0:
        return "DATA_GAP", 0
    if rows < 12:
        return "THIN", 0
    if ratio >= 2.0 and split_consistency >= 2:
        return "PROMISING_SHADOW", 3
    if ratio >= 1.5 and split_consistency >= 1:
        return "WATCH", 2
    return "NO_EDGE", 1


def _same_sign_count(reference: float | None, values: list[float]) -> int:
    if reference is None or abs(reference) <= 1e-12:
        return 0
    sign = 1 if reference > 0 else -1
    return sum(1 for value in values if value * sign > 0.0)


def _filter_split(trades: pl.DataFrame, start: str, end: str) -> pl.DataFrame:
    if "entry_date" in trades.columns:
        return trades.filter((pl.col("entry_date") >= start) & (pl.col("entry_date") < end))
    if "exit_date" in trades.columns:
        return trades.filter((pl.col("exit_date") >= start) & (pl.col("exit_date") < end))
    if "entry_ts_ms" not in trades.columns:
        return trades.head(0)
    return trades.with_columns(_entry_date_expr().alias("_feature_entry_date")).filter(
        (pl.col("_feature_entry_date") >= start) & (pl.col("_feature_entry_date") < end)
    )


def _entry_date_expr() -> pl.Expr:
    return pl.from_epoch(pl.col("entry_ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d")


def _finite_float(value: Any) -> float | None:
    return finite_float(value)


def _pct(value: Any) -> str:
    return pct(value, invalid="n/a")
