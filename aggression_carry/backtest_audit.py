from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import polars as pl


@dataclass(frozen=True, slots=True)
class AuditGate:
    name: str
    status: str
    severity: str
    detail: str


HARD_FAIL = "hard"
SOFT_FAIL = "soft"
MS_PER_MINUTE = 60_000
MS_PER_HOUR = 60 * MS_PER_MINUTE
MS_PER_DAY = 24 * MS_PER_HOUR


def config_hash(config: Any) -> str:
    payload = asdict(config) if hasattr(config, "__dataclass_fields__") else config
    encoded = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def data_identity(data_root: str | Path, datasets: tuple[str, ...]) -> dict[str, Any]:
    root = Path(data_root).expanduser()
    rows: list[dict[str, Any]] = []
    for dataset in datasets:
        path = root / dataset
        files = sorted(path.glob("**/*.parquet")) if path.exists() else []
        row: dict[str, Any] = {
            "dataset": dataset,
            "path": str(path),
            "exists": path.exists(),
            "parquet_files": len(files),
            "bytes": sum(file.stat().st_size for file in files),
            "latest_mtime_ns": max((file.stat().st_mtime_ns for file in files), default=0),
        }
        if files:
            row.update(_parquet_identity_stats(files))
        rows.append(row)
    fingerprint_payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "data_root": str(root),
        "fingerprint": hashlib.sha256(fingerprint_payload).hexdigest()[:16],
        "datasets": rows,
    }


def gate(name: str, status: str, severity: str, detail: str) -> dict[str, str]:
    if status not in {"pass", "fail", "warn", "info"}:
        raise ValueError(f"Unsupported audit gate status: {status}")
    if severity not in {HARD_FAIL, SOFT_FAIL, "info"}:
        raise ValueError(f"Unsupported audit gate severity: {severity}")
    return asdict(AuditGate(name=name, status=status, severity=severity, detail=detail))


def audit_label(gates: list[dict[str, str]], *, biased: bool) -> str:
    hard_failed = any(item["status"] == "fail" and item["severity"] == HARD_FAIL for item in gates)
    if biased:
        return "biased_benchmark"
    if hard_failed:
        return "exploratory"
    return "candidate"


def promotion_allowed(gates: list[dict[str, str]], *, biased: bool) -> bool:
    if biased:
        return False
    return not any(item["status"] == "fail" for item in gates)


def summarize_gate_counts(gates: list[dict[str, str]]) -> dict[str, int]:
    return {
        "pass": sum(1 for item in gates if item["status"] == "pass"),
        "fail": sum(1 for item in gates if item["status"] == "fail"),
        "warn": sum(1 for item in gates if item["status"] == "warn"),
        "info": sum(1 for item in gates if item["status"] == "info"),
        "hard_fail": sum(1 for item in gates if item["status"] == "fail" and item["severity"] == HARD_FAIL),
        "soft_fail": sum(1 for item in gates if item["status"] == "fail" and item["severity"] == SOFT_FAIL),
    }


def close_fade_lifecycle(config: Any) -> dict[str, Any]:
    profit_delay = (
        config.profit_protection_delay_minutes
        if config.profit_protection_delay_minutes is not None
        else config.stop_delay_minutes
    )
    return {
        "decision_ts": f"UTC day at minute {config.signal_minute}",
        "data_available_ts": "same as decision_ts; features use only completed minute bars with minute_of_day < signal_minute",
        "order_submit_ts": f"decision_ts + {config.entry_delay_minutes} minutes",
        "fill_window": f"[order_submit_ts, order_submit_ts + {config.entry_twap_minutes} minutes)",
        "exit_activation_ts": {
            "hard_stop": f"first fill + {config.stop_delay_minutes} minutes",
            "adaptive_profit_protection": f"final entry fill + {profit_delay} minutes",
        },
        "state_initialization_ts": {
            "hard_stop": "first fill",
            "adaptive_profit_protection": "profit_protection_active_ts",
        },
    }


def volume_lifecycle(config: Any) -> dict[str, Any]:
    return {
        "decision_ts": "daily volume feature timestamp",
        "data_available_ts": "completed hourly/daily bars at signal timestamp",
        "order_submit_ts": f"decision_ts + {config.entry_delay_hours} hours",
        "fill_window": "synthetic entry at selected hourly close",
        "exit_activation_ts": "entry through max hold, stop, take profit, or rank exit",
        "state_initialization_ts": "entry bar",
    }


def close_fade_audit(
    *,
    config: Any,
    summary: dict[str, Any],
    rows: dict[str, int],
    cost_model: dict[str, Any],
    split_metrics: list[dict[str, Any]],
    data_root: str | Path,
) -> dict[str, Any]:
    identity = data_identity(
        data_root,
        (
            "klines_1m",
            "instruments",
            "archive_trade_manifest",
            "funding",
            "open_interest",
            "signed_flow_1h",
            "premium_index_1h",
            "mark_price_1h",
            "index_price_1h",
        ),
    )
    gates: list[dict[str, str]] = [
        gate("lifecycle_timestamps", "pass", HARD_FAIL, "Report declares decision, data, order, fill, exit, and state timing."),
        gate("causal_signal_features", "pass", HARD_FAIL, "Daily-close features are built from completed bars before the signal minute."),
        gate(
            "point_in_time_universe",
            "pass" if config.require_archive_membership else "fail",
            HARD_FAIL,
            "Archive membership is required." if config.require_archive_membership else "Current-listing/current-data universe benchmark; not promotion evidence.",
        ),
        gate(
            "fees_and_slippage",
            "pass" if float(cost_model.get("round_trip_cost_bps", 0.0)) > 0.0 else "fail",
            HARD_FAIL,
            "Round-trip cost is positive and recorded." if float(cost_model.get("round_trip_cost_bps", 0.0)) > 0.0 else "Round-trip cost is zero.",
        ),
        gate(
            "funding_carry",
            "pass" if summary.get("funding_mode") == "modeled" else "fail",
            HARD_FAIL,
            "Funding was modeled from funding events." if summary.get("funding_mode") == "modeled" else "Funding data missing or unused; carry is not proof-grade.",
        ),
        gate(
            "context_datasets",
            "pass" if _context_datasets_present(identity) else "fail",
            HARD_FAIL,
            "Open interest, signed flow, and premium context datasets are present."
            if _context_datasets_present(identity)
            else "One or more context datasets are missing: open_interest, signed_flow_1h, premium_index_1h or mark/index pair.",
        ),
        gate(
            "capacity_limits",
            "pass"
            if config.max_trade_notional_pct_of_day_turnover > 0.0
            or config.max_trade_notional_pct_of_baseline_turnover > 0.0
            else "fail",
            HARD_FAIL,
            "Turnover participation caps are enabled." if (
                config.max_trade_notional_pct_of_day_turnover > 0.0
                or config.max_trade_notional_pct_of_baseline_turnover > 0.0
            ) else "Turnover participation caps are disabled.",
        ),
        gate(
            "market_impact",
            "pass" if getattr(config, "market_impact_bps_per_1pct_turnover", 0.0) > 0.0 else "fail",
            HARD_FAIL,
            "Participation-based impact cost is enabled."
            if getattr(config, "market_impact_bps_per_1pct_turnover", 0.0) > 0.0
            else "Market impact cost is disabled.",
        ),
        gate("adaptive_state_tests", "pass", HARD_FAIL, "Profit-protection state starts at activation and has regression tests."),
        gate("trade_ledger", "pass" if rows.get("trades", 0) > 0 else "fail", HARD_FAIL, "Trade ledger rows exist."),
        gate("equity_and_drawdown", "pass", HARD_FAIL, "Equity and drawdown are computed from basket returns."),
        gate(
            "split_metrics",
            "pass" if _split_metrics_pass(split_metrics) else "fail",
            HARD_FAIL,
            "All chronological splits are positive with finite Sharpe." if _split_metrics_pass(split_metrics) else "Split stability is missing or failed.",
        ),
        gate(
            "synchronized_exit_anomaly",
            "fail" if _post_twap_cluster_rate(summary, "post_twap_exit_le16") > 0.50 else "pass",
            HARD_FAIL,
            "More than half of trades exit by post-TWAP minute 16." if _post_twap_cluster_rate(summary, "post_twap_exit_le16") > 0.50 else "No majority post-TWAP minute-16 exit cluster.",
        ),
        gate(
            "forward_twap_reconciliation",
            "fail" if config.entry_twap_minutes > 0 else "warn",
            SOFT_FAIL,
            "Forward/demo slice-level TWAP is not implemented yet." if config.entry_twap_minutes > 0 else "Single-fill lifecycle still needs forward reconciliation.",
        ),
    ]
    biased = not config.require_archive_membership
    label = audit_label(gates, biased=biased)
    return {
        "label": label,
        "can_support_promotion": promotion_allowed(gates, biased=biased),
        "gate_counts": summarize_gate_counts(gates),
        "gates": gates,
        "config_hash": config_hash(config),
        "lifecycle": close_fade_lifecycle(config),
        "data_identity": identity,
    }


def volume_backtest_audit(
    *,
    config: Any,
    summary: dict[str, Any],
    rows: dict[str, int],
    cost_model: dict[str, Any],
    split_metrics: list[dict[str, Any]],
    data_root: str | Path,
) -> dict[str, Any]:
    gates: list[dict[str, str]] = [
        gate("lifecycle_timestamps", "pass", HARD_FAIL, "Report declares decision, data, order, fill, exit, and state timing."),
        gate("causal_signal_features", "pass", HARD_FAIL, "Volume features are built from completed historical bars."),
        gate("point_in_time_universe", "fail", HARD_FAIL, "Volume backtest has no PIT universe gate yet; treat as current-universe benchmark."),
        gate(
            "fees_and_slippage",
            "pass" if float(cost_model.get("round_trip_cost_bps", 0.0)) > 0.0 else "fail",
            HARD_FAIL,
            "Round-trip cost is positive and recorded." if float(cost_model.get("round_trip_cost_bps", 0.0)) > 0.0 else "Round-trip cost is zero.",
        ),
        gate(
            "funding_carry",
            "pass" if summary.get("funding_mode") == "modeled" else "fail",
            HARD_FAIL,
            "Funding was modeled from funding events." if summary.get("funding_mode") == "modeled" else "Funding data missing or unused; carry is not proof-grade.",
        ),
        gate("ohlc_ordering", "pass", HARD_FAIL, "If stop and target touch in one 1h bar, stop wins."),
        gate("trade_ledger", "pass" if rows.get("trades", 0) > 0 else "fail", HARD_FAIL, "Trade ledger rows exist."),
        gate("equity_and_drawdown", "pass", HARD_FAIL, "Equity and drawdown are computed from basket returns."),
        gate(
            "split_metrics",
            "pass" if _split_metrics_pass(split_metrics) else "fail",
            HARD_FAIL,
            "All chronological splits are positive with finite Sharpe." if _split_metrics_pass(split_metrics) else "Split stability is missing or failed.",
        ),
        gate("forward_reconciliation", "fail", SOFT_FAIL, "No paper/demo lifecycle reconciliation for this backtest."),
    ]
    return {
        "label": audit_label(gates, biased=True),
        "can_support_promotion": False,
        "gate_counts": summarize_gate_counts(gates),
        "gates": gates,
        "config_hash": config_hash(config),
        "lifecycle": volume_lifecycle(config),
        "data_identity": data_identity(data_root, ("klines_1h", "instruments", "funding", "open_interest", "signed_flow_1h")),
    }


def _split_metrics_pass(split_metrics: list[dict[str, Any]]) -> bool:
    if not split_metrics:
        return False
    return all(
        int(item.get("basket_count", item.get("baskets", 0)) or 0) > 0
        and float(item.get("total_return", 0.0) or 0.0) > 0.0
        and _finite(float(item.get("sharpe_like", 0.0) or 0.0))
        for item in split_metrics
    )


def _context_datasets_present(identity: dict[str, Any]) -> bool:
    rows = {str(row["dataset"]): row for row in identity.get("datasets", [])}
    has_oi = _has_rows(rows.get("open_interest", {}))
    has_flow = _has_rows(rows.get("signed_flow_1h", {}))
    has_premium = _has_rows(rows.get("premium_index_1h", {}))
    has_mark_index = _has_rows(rows.get("mark_price_1h", {})) and _has_rows(rows.get("index_price_1h", {}))
    return has_oi and has_flow and (has_premium or has_mark_index)


def _has_rows(row: dict[str, Any]) -> bool:
    return int(row.get("parquet_files", 0) or 0) > 0 and int(row.get("row_count", 0) or 0) > 0


def _parquet_identity_stats(files: list[Path]) -> dict[str, Any]:
    try:
        scan = pl.scan_parquet([str(file) for file in files])
        columns = scan.collect_schema().names()
        expressions: list[pl.Expr] = [pl.len().alias("row_count")]
        if "symbol" in columns:
            expressions.append(pl.col("symbol").n_unique().alias("symbol_count"))
        if "ts_ms" in columns:
            expressions.extend(
                [
                    pl.col("ts_ms").min().alias("min_ts_ms"),
                    pl.col("ts_ms").max().alias("max_ts_ms"),
                    pl.col("ts_ms").n_unique().alias("timestamp_count"),
                    (pl.col("ts_ms").cast(pl.Int64) // MS_PER_MINUTE).n_unique().alias("minute_count"),
                    (pl.col("ts_ms").cast(pl.Int64) // MS_PER_HOUR).n_unique().alias("hour_count"),
                    (pl.col("ts_ms").cast(pl.Int64) // MS_PER_DAY).n_unique().alias("ts_date_count"),
                ]
            )
        if "date" in columns:
            expressions.extend(
                [
                    pl.col("date").min().alias("min_date"),
                    pl.col("date").max().alias("max_date"),
                    pl.col("date").n_unique().alias("date_count"),
                ]
            )
        stats = scan.select(expressions).collect().to_dicts()[0]
        return {key: _jsonable(value) for key, value in stats.items()}
    except Exception as exc:  # pragma: no cover - audit metadata should never crash a run
        return {"scan_error": str(exc)}


def _post_twap_cluster_rate(summary: dict[str, Any], key: str) -> float:
    count = float(summary.get(key, 0) or 0)
    total = float(summary.get("trade_count", 0) or 0)
    return count / total if total > 0.0 else 0.0


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


def _jsonable(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value
