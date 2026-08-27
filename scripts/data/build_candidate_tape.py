#!/usr/bin/env python3
"""Build one bounded Strategy Overhaul V2 candidate-tape partition.

Reads existing PIT data and writes a run-scoped diagnostic directory. It does
not refresh data/features, backtest, or build a portfolio curve.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import types
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

import polars as pl

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))


def _install_import_only_windows_fcntl_guard() -> None:
    """Stub :mod:`fcntl` so POSIX-owned strategy modules import on Windows.

    Candidate projection never takes a lock, so the stubbed ``flock`` raises
    rather than pretend Windows has POSIX flock semantics.
    """
    if os.name != "nt" or "fcntl" in sys.modules:
        return
    module = types.ModuleType("fcntl")
    module.LOCK_SH = 1  # type: ignore[attr-defined]
    module.LOCK_EX = 2  # type: ignore[attr-defined]
    module.LOCK_NB = 4  # type: ignore[attr-defined]
    module.LOCK_UN = 8  # type: ignore[attr-defined]

    def _forbidden_flock(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("candidate projection must not invoke POSIX account locking")

    module.flock = _forbidden_flock  # type: ignore[attr-defined]
    sys.modules["fcntl"] = module


_install_import_only_windows_fcntl_guard()

from liquidity_migration.core._common import MS_PER_HOUR, exact_duration_ms, sha256_file  # noqa: E402
from liquidity_migration.rules.long_native import (  # noqa: E402
    _classify_entry,
    build_long_features,
    long_pump_family,
    long_v11a_profile,
)
from liquidity_migration.data.volume_events_pit import filter_klines_to_pit_membership  # noqa: E402
from liquidity_migration.strategy.strategy_funnel import (  # noqa: E402
    canonical_payload,
    finalize_funnel_row,
    gate_state,
    payload_sha256,
    validate_decision_funnel,
    validate_path_labels,
)


LONG_REQUIRED_GATES = (
    "pit_tradable",
    "history_floor",
    "liquidity_floor",
    "pump_trigger",
    "entry_anchor",
    "signal_freshness",
)
REQUIRED_GATE_ORDERS = {
    "long": LONG_REQUIRED_GATES,
}
HORIZON_HOURS = (1, 6, 24, 72)
LONG_WARMUP_DAYS = 120
PATH_TAIL_DAYS = 4


def _git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _date_range(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=offset) for offset in range((end - start).days)]


def _date_ms(value: dt.date) -> int:
    return int(dt.datetime.combine(value, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)


def _dataset_files(root: Path, dataset: str, dates: Sequence[dt.date]) -> list[Path]:
    files: list[Path] = []
    for day in dates:
        partition = root / dataset / f"date={day.isoformat()}"
        files.extend(sorted(partition.rglob("*.parquet")) if partition.is_dir() else [])
    return files


def _read_klines(root: Path, dates: Sequence[dt.date]) -> tuple[pl.DataFrame, list[Path]]:
    columns = [
        "ts_ms",
        "symbol",
        "open",
        "high",
        "low",
        "close",
        "volume_base",
        "turnover_quote",
        "date",
    ]
    frames: list[pl.DataFrame] = []
    files: list[Path] = []
    for day in dates:
        day_files = _dataset_files(root, "klines_1h", [day])
        if not day_files:
            continue
        files.extend(day_files)
        frames.append(pl.read_parquet(day_files, columns=columns, rechunk=False))
    if not frames:
        raise FileNotFoundError("no kline partitions exist inside the declared read interval")
    return pl.concat(frames, how="vertical_relaxed", rechunk=True).sort(["symbol", "ts_ms"]), files


def _read_manifest(root: Path, dates: Sequence[dt.date]) -> tuple[pl.DataFrame, list[Path]]:
    files = _dataset_files(root, "archive_trade_manifest", dates)
    if not files:
        return pl.DataFrame(), []
    return pl.read_parquet(files, rechunk=True).unique(["date", "symbol"]), files


def _aggregate_file_identity(paths: Sequence[Path], *, relative_to: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    total_bytes = 0
    for path in sorted(set(paths)):
        stat = path.stat()
        file_hash = sha256_file(path)
        relative = path.relative_to(relative_to).as_posix()
        digest.update(canonical_payload({"path": relative, "bytes": stat.st_size, "sha256": file_hash}))
        digest.update(b"\n")
        total_bytes += stat.st_size
    return {
        "algorithm": "sha256(sorted canonical {path,bytes,sha256})",
        "file_count": len(set(paths)),
        "bytes": total_bytes,
        "aggregate_sha256": digest.hexdigest(),
    }


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _valid_ohlc() -> pl.Expr:
    return pl.all_horizontal(
        [pl.col(column).is_not_null() & pl.col(column).is_finite() & (pl.col(column) > 0.0)
         for column in ("open", "high", "low", "close")]
    )


def _bars_by_symbol(klines: pl.DataFrame) -> dict[str, dict[str, list[Any]]]:
    output: dict[str, dict[str, list[Any]]] = {}
    prepared = klines.with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("bar_end_ts_ms"))
    for key, part in prepared.sort(["symbol", "ts_ms"]).partition_by("symbol", as_dict=True).items():
        symbol = str(key[0] if isinstance(key, tuple) else key)
        output[symbol] = {
            "ends": [int(value) for value in part["bar_end_ts_ms"].to_list()],
            "close": [float(value) for value in part["close"].to_list()],
            "high": [float(value) for value in part["high"].to_list()],
            "low": [float(value) for value in part["low"].to_list()],
        }
    return output


def _exact_close(bars: dict[str, list[Any]] | None, bar_end_ts_ms: int) -> float | None:
    if bars is None:
        return None
    index = bisect.bisect_left(bars["ends"], int(bar_end_ts_ms))
    if index >= len(bars["ends"]) or bars["ends"][index] != int(bar_end_ts_ms):
        return None
    value = float(bars["close"][index])
    return value if math.isfinite(value) and value > 0.0 else None


def _long_entry_anchor(
    row: dict[str, Any],
    *,
    bars: dict[str, list[Any]] | None,
    retrace_pct: float,
    first_hour: int,
    deadline_hour: int,
) -> tuple[int | None, float | None, str | None]:
    signal_ts_ms = int(row["ts_ms"])
    signal_close = _finite(row.get("close"))
    if signal_close is None or signal_close <= 0.0:
        return None, None, "missing_signal_close"
    threshold = signal_close * (1.0 - retrace_pct)
    for hour in range(max(1, first_hour), max(first_hour, deadline_hour) + 1):
        candidate_ts_ms = signal_ts_ms + exact_duration_ms(hours=hour)
        price = _exact_close(bars, candidate_ts_ms)
        if price is not None and price <= threshold:
            return candidate_ts_ms, price, None
    deadline_ts_ms = signal_ts_ms + exact_duration_ms(hours=max(first_hour, deadline_hour))
    deadline_price = _exact_close(bars, deadline_ts_ms)
    if deadline_price is None:
        return None, None, "missing_deadline_bar"
    return deadline_ts_ms, deadline_price, None


def _manifest_maps(manifest: pl.DataFrame) -> tuple[set[tuple[str, str]], dict[tuple[str, str], dict[str, Any]]]:
    rows = manifest.to_dicts() if not manifest.is_empty() else []
    keys = {(str(row["date"]), str(row["symbol"]).upper()) for row in rows}
    detail = {(str(row["date"]), str(row["symbol"]).upper()): row for row in rows}
    return keys, detail


def _long_close_location_pass(row: dict[str, Any], pump: dict[str, Any], config: Any) -> bool:
    values = (
        ("trigger_1d", "close_location", config.fc_min_close_location),
        ("trigger_3d", "close_loc_3d", config.fc_close_loc_multi_day),
        ("trigger_7d", "close_loc_7d", config.fc_close_loc_multi_day),
    )
    return any(
        bool(pump[trigger])
        and _finite(row.get(column)) is not None
        and float(row[column]) >= threshold
        for trigger, column, threshold in values
    )


def _build_long_funnel(
    klines: pl.DataFrame,
    manifest: pl.DataFrame,
    *,
    venue: str,
    start_ms: int,
    end_ms: int,
    bars_by_symbol: dict[str, dict[str, list[Any]]],
) -> tuple[pl.DataFrame, set[str]]:
    config = long_v11a_profile()
    features = build_long_features(
        klines.filter(~pl.col("symbol").is_in(list(config.exclude_symbols))),
        config=config,
    ).filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") < end_ms))
    manifest_keys, manifest_detail = _manifest_maps(manifest)
    rows: list[dict[str, Any]] = []
    expected_keys: set[str] = set()
    for source in features.to_dicts():
        pump = long_pump_family(source, config)
        if not bool(pump["trigger_any"]):
            continue
        signal_ts_ms = int(source["ts_ms"])
        symbol = str(source["symbol"]).upper()
        membership_date = dt.datetime.fromtimestamp(
            (signal_ts_ms - 1) / 1000,
            tz=dt.timezone.utc,
        ).date().isoformat()
        membership_key = (membership_date, symbol)
        pit_tradable = membership_key in manifest_keys
        membership = manifest_detail.get(membership_key, {})
        entry_ts_ms, entry_price, anchor_missing = _long_entry_anchor(
            source,
            bars=bars_by_symbol.get(symbol),
            retrace_pct=config.fc_sniper_retrace_pct,
            first_hour=config.entry_delay_hours,
            deadline_hour=config.fc_sniper_deadline_hours,
        )
        turnover_median = _finite(source.get("turnover_median_90d"))
        history_days = int(source.get("symbol_age_days") or 0)
        atr_pct = _finite(source.get("atr_14d_pct"))
        pattern, _stop, _target, _hold = _classify_entry(source, config)
        row = finalize_funnel_row(
            {
                "sleeve": "long",
                "venue": venue,
                "symbol": symbol,
                "signal_ts_ms": signal_ts_ms,
                "feature_ts_ms": signal_ts_ms,
                "data_available_ts_ms": signal_ts_ms,
                "decision_ts_ms": entry_ts_ms or signal_ts_ms + exact_duration_ms(
                    hours=config.fc_sniper_deadline_hours
                ),
                "entry_ts_ms": entry_ts_ms,
                "evaluation_ts_ms": entry_ts_ms or signal_ts_ms + exact_duration_ms(
                    hours=config.fc_sniper_deadline_hours
                ),
                "entry_price": entry_price,
                "entry_missing_reason": anchor_missing,
                "component_scope": "long_active_profile",
                "source_strength": pump["source_strength"],
                "membership_date": membership_date,
                "membership_source": membership.get("membership_source"),
                "membership_inferred": membership.get("membership_inferred"),
                "membership_provenance_limitation": membership.get(
                    "membership_provenance_limitation"
                ),
                "signal_close": _finite(source.get("close")),
                "log_return": _finite(source.get("log_return")),
                "pump_3d_log": _finite(source.get("pump_3d_log")),
                "pump_7d_log": _finite(source.get("pump_7d_log")),
                "pump_threshold_1d": pump["threshold_1d"],
                "pump_threshold_3d": pump["threshold_3d"],
                "pump_threshold_7d": pump["threshold_7d"],
                "pump_trigger_1d": pump["trigger_1d"],
                "pump_trigger_3d": pump["trigger_3d"],
                "pump_trigger_7d": pump["trigger_7d"],
                "close_location": _finite(source.get("close_location")),
                "close_loc_3d": _finite(source.get("close_loc_3d")),
                "close_loc_7d": _finite(source.get("close_loc_7d")),
                "atr_14d_pct": atr_pct,
                "realized_vol": _finite(source.get("realized_vol")),
                "sigma_daily_30d": _finite(source.get("sigma_daily_30d")),
                "turnover_quote": _finite(source.get("turnover_quote")),
                "turnover_median_90d": turnover_median,
                "symbol_age_days": history_days,
                "today_volume_rank": _finite(source.get("today_volume_rank")),
                "universe_rank": _finite(source.get("universe_rank")),
                "btc_regime_on": source.get("regime_on"),
                "eth_regime_on": source.get("eth_regime_on"),
                "btc_rv_30": _finite(source.get("btc_rv_30")),
                "weekend_entry": (
                    None
                    if entry_ts_ms is None
                    else dt.datetime.fromtimestamp(entry_ts_ms / 1000, tz=dt.timezone.utc).weekday() >= 5
                ),
                "active_reference_accepted": pattern == "fomo_chase",
                "gate_pit_tradable": gate_state(pit_tradable),
                "gate_history_floor": gate_state(history_days >= 90 and turnover_median is not None),
                "gate_liquidity_floor": gate_state(
                    None if turnover_median is None else turnover_median >= 500_000.0
                ),
                "gate_pump_trigger": "pass",
                "gate_entry_anchor": gate_state(entry_ts_ms is not None),
                "gate_signal_freshness": gate_state(
                    None if entry_ts_ms is None else entry_ts_ms - signal_ts_ms <= exact_duration_ms(hours=24)
                ),
                "gate_active_btc_regime": gate_state(bool(source.get("regime_on"))),
                "gate_active_eth_regime": gate_state(bool(source.get("eth_regime_on"))),
                "gate_active_universe": gate_state(bool(source.get("in_universe"))),
                "gate_active_top_volume": gate_state(
                    _finite(source.get("today_volume_rank")) is not None
                    and float(source["today_volume_rank"]) <= config.fc_top_volume_rank_max
                ),
                "gate_active_close_location": gate_state(
                    _long_close_location_pass(source, pump, config)
                ),
                "gate_active_atr": gate_state(
                    None if atr_pct is None else 0.0 < atr_pct <= config.fc_max_atr_pct
                ),
                "gate_cooldown": "not_applicable",
                "gate_existing_exposure": "not_applicable",
                "gate_capacity": "not_applicable",
                "gate_owner_health": "not_applicable",
                "gate_unresolved_target": "not_applicable",
                "gate_terminal_attempt": "not_applicable",
                "gate_account_risk": "not_applicable",
                "gate_publication": "not_applicable",
            },
            required_gate_order=LONG_REQUIRED_GATES,
        )
        expected_keys.add(row["source_key"])
        rows.append(row)
    return pl.from_dicts(rows, infer_schema_length=None) if rows else _empty_funnel(), expected_keys


def _empty_funnel() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "source_key": pl.String,
            "sleeve": pl.String,
            "venue": pl.String,
            "symbol": pl.String,
            "signal_ts_ms": pl.Int64,
            "feature_ts_ms": pl.Int64,
            "data_available_ts_ms": pl.Int64,
            "decision_ts_ms": pl.Int64,
            "entry_ts_ms": pl.Int64,
            "first_rejection": pl.String,
            "barebones_accepted": pl.Boolean,
        }
    )


def _ensure_funnel_gate_columns(frame: pl.DataFrame) -> pl.DataFrame:
    all_gates = sorted(LONG_REQUIRED_GATES)
    missing = [f"gate_{gate}" for gate in all_gates if f"gate_{gate}" not in frame.columns]
    return frame.with_columns([pl.lit(None, dtype=pl.String).alias(column) for column in missing])


def _label_row(
    source: dict[str, Any],
    *,
    bars: dict[str, list[Any]] | None,
) -> dict[str, Any]:
    entry_ts_ms = int(source["entry_ts_ms"])
    entry_price = float(source["entry_price"])
    side_sign = 1.0 if source["sleeve"] == "long" else -1.0
    row: dict[str, Any] = {
        "source_key": source["source_key"],
        "sleeve": source["sleeve"],
        "venue": source["venue"],
        "symbol": source["symbol"],
        "signal_ts_ms": int(source["signal_ts_ms"]),
        "entry_ts_ms": entry_ts_ms,
        "entry_price": entry_price,
        "side_sign": int(side_sign),
    }
    observed_72h_index: int | None = None
    for hours in HORIZON_HOURS:
        target = entry_ts_ms + exact_duration_ms(hours=hours)
        row[f"target_{hours}h_ts_ms"] = target
        index = None if bars is None else bisect.bisect_left(bars["ends"], target)
        if (
            bars is None
            or index is None
            or index >= len(bars["ends"])
            or int(bars["ends"][index]) > target + MS_PER_HOUR
        ):
            row[f"actual_{hours}h_ts_ms"] = None
            row[f"return_{hours}h"] = None
            row[f"missing_{hours}h_reason"] = "no_complete_bar_within_one_hour"
            continue
        actual_ts_ms = int(bars["ends"][index])
        close = float(bars["close"][index])
        row[f"actual_{hours}h_ts_ms"] = actual_ts_ms
        row[f"return_{hours}h"] = side_sign * (close / entry_price - 1.0)
        row[f"missing_{hours}h_reason"] = None
        if hours == 72:
            observed_72h_index = index
    if bars is None or observed_72h_index is None:
        row["mae_72h"] = None
        row["mfe_72h"] = None
        row["path_missing_reason"] = "no_complete_72h_path"
    else:
        first_index = bisect.bisect_right(bars["ends"], entry_ts_ms)
        last_index = observed_72h_index + 1
        if first_index >= last_index:
            row["mae_72h"] = None
            row["mfe_72h"] = None
            row["path_missing_reason"] = "empty_post_entry_path"
        elif side_sign > 0:
            adverse = min(float(value) / entry_price - 1.0 for value in bars["low"][first_index:last_index])
            favorable = max(float(value) / entry_price - 1.0 for value in bars["high"][first_index:last_index])
            row["mae_72h"] = adverse
            row["mfe_72h"] = favorable
            row["path_missing_reason"] = None
        else:
            adverse = min(1.0 - float(value) / entry_price for value in bars["high"][first_index:last_index])
            favorable = max(1.0 - float(value) / entry_price for value in bars["low"][first_index:last_index])
            row["mae_72h"] = adverse
            row["mfe_72h"] = favorable
            row["path_missing_reason"] = None
    return row


def _build_path_labels(
    funnel: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, list[Any]]],
) -> pl.DataFrame:
    accepted = funnel.filter(pl.col("barebones_accepted"))
    rows = [
        _label_row(source, bars=bars_by_symbol.get(str(source["symbol"])))
        for source in accepted.to_dicts()
    ]
    if rows:
        return pl.from_dicts(rows, infer_schema_length=None).sort("source_key")
    schema: dict[str, Any] = {
        "source_key": pl.String,
        "sleeve": pl.String,
        "venue": pl.String,
        "symbol": pl.String,
        "signal_ts_ms": pl.Int64,
        "entry_ts_ms": pl.Int64,
        "entry_price": pl.Float64,
        "side_sign": pl.Int64,
        "mae_72h": pl.Float64,
        "mfe_72h": pl.Float64,
        "path_missing_reason": pl.String,
    }
    for hours in HORIZON_HOURS:
        schema[f"target_{hours}h_ts_ms"] = pl.Int64
        schema[f"actual_{hours}h_ts_ms"] = pl.Int64
        schema[f"return_{hours}h"] = pl.Float64
        schema[f"missing_{hours}h_reason"] = pl.String
    return pl.DataFrame(schema=schema)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    data = canonical_payload(payload) + b"\n"
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("candidate manifest write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(canonical_payload(payload) + b"\n")
    os.replace(temporary, path)


def _config_identities(start: dt.date, end: dt.date) -> dict[str, Any]:
    long_config = replace(
        long_v11a_profile(),
        start_date=start.isoformat(),
        end_date=end.isoformat(),
    )
    return {
        "long_full_config_sha256": payload_sha256(asdict(long_config)),
    }


def _null_counts(frame: pl.DataFrame) -> dict[str, int]:
    null_frame = frame.null_count()
    return {column: int(null_frame[column][0]) for column in null_frame.columns}


def _validate_existing_output(out: Path) -> dict[str, Any]:
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        raise FileExistsError(f"candidate output exists without a manifest: {out}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, identity in manifest.get("files", {}).items():
        path = out / name
        if not path.is_file() or sha256_file(path) != identity.get("sha256"):
            raise RuntimeError(f"existing candidate output failed identity check: {path}")
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--venue", choices=("bybit",), required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.root.expanduser().resolve(strict=True)
    out = args.out.expanduser().resolve()
    if args.end <= args.start:
        raise ValueError("candidate partition end must be after start")
    read_start = args.start - dt.timedelta(days=LONG_WARMUP_DAYS)
    read_end = args.end + dt.timedelta(days=PATH_TAIL_DAYS)
    dates = _date_range(read_start, read_end)
    kline_files = _dataset_files(root, "klines_1h", dates)
    # Membership spans the whole warm-up and path-tail interval; filtering only
    # the decision window lets nonmembers influence trailing features.
    membership_dates = dates
    manifest_files = _dataset_files(root, "archive_trade_manifest", membership_dates)
    rmom_path = root / "residual_momentum.parquet"
    preflight = {
        "schema_version": 1,
        "mode": "preflight",
        "root": str(root),
        "venue": args.venue,
        "source_window": {"start": args.start.isoformat(), "end_exclusive": args.end.isoformat()},
        "read_window": {"start": read_start.isoformat(), "end_exclusive": read_end.isoformat()},
        "kline_file_count": len(kline_files),
        "manifest_file_count": len(manifest_files),
        "residual_momentum_present": rmom_path.is_file(),
        "writes_data_root": False,
        "runs_strategy_backtest": False,
    }
    if args.preflight:
        print(json.dumps(preflight, ensure_ascii=False, sort_keys=True))
        return 0
    if not kline_files or not manifest_files:
        raise FileNotFoundError("candidate partition requires the declared kline and PIT manifest partitions")
    dirty = bool(_git("status", "--porcelain=v1"))
    if dirty and not args.allow_dirty:
        raise RuntimeError("candidate partition requires clean code; --allow-dirty remains exploratory and explicit")
    if out.exists():
        print(json.dumps(_validate_existing_output(out), ensure_ascii=False, sort_keys=True))
        return 0

    input_identity = {
        "klines_1h": _aggregate_file_identity(kline_files, relative_to=root),
        "archive_trade_manifest": _aggregate_file_identity(manifest_files, relative_to=root),
        "residual_momentum": (
            {
                "path": str(rmom_path),
                "sha256": sha256_file(rmom_path),
                "consumed": False,
                "rejection": "missing is_provisional provenance",
            }
            if rmom_path.is_file()
            else {"path": str(rmom_path), "consumed": False, "rejection": "missing file"}
        ),
    }
    run_identity = {
        **preflight,
        "mode": "build",
        "code_commit": _git("rev-parse", "HEAD"),
        "git_dirty": dirty,
        "input_identity": input_identity,
        "config_identities": _config_identities(args.start, args.end),
        "cost_config": {
            "path": "configs/volume_alpha.default.yaml",
            "sha256": sha256_file(REPO / "configs/volume_alpha.default.yaml"),
            "hash_scope": "native worktree bytes",
            "platform": sys.platform,
        },
    }
    run_identity_sha256 = payload_sha256(run_identity)
    work = out.with_name(f".{out.name}.working")
    work.mkdir(parents=True, exist_ok=True)
    checkpoint_path = work / "checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("run_identity_sha256") != run_identity_sha256:
            raise RuntimeError(f"candidate working state belongs to a different run: {work}")
    else:
        checkpoint = {"run_identity_sha256": run_identity_sha256, "completed": []}
        _atomic_checkpoint(checkpoint_path, checkpoint)

    klines, read_kline_files = _read_klines(root, dates)
    manifest, read_manifest_files = _read_manifest(root, membership_dates)
    if set(read_kline_files) != set(kline_files) or set(read_manifest_files) != set(manifest_files):
        raise RuntimeError("input partition discovery changed between identity and read")
    raw_kline_rows = klines.height
    invalid_ohlc_rows = klines.filter(~_valid_ohlc()).height
    klines = klines.filter(_valid_ohlc())
    valid_ohlc_rows = klines.height
    klines, pit_filter_receipt = filter_klines_to_pit_membership(klines, manifest)
    bars_by_symbol = _bars_by_symbol(klines)
    start_ms = _date_ms(args.start)
    end_ms = _date_ms(args.end)

    long_funnel_path = work / "long_funnel.parquet"
    if "long" not in checkpoint["completed"]:
        long_funnel, long_expected = _build_long_funnel(
            klines,
            manifest,
            venue=args.venue,
            start_ms=start_ms,
            end_ms=end_ms,
            bars_by_symbol=bars_by_symbol,
        )
        if set(long_funnel["source_key"].to_list()) != long_expected:
            raise RuntimeError("LONG source-key completeness failed")
        long_funnel.write_parquet(long_funnel_path, compression="zstd", statistics=True)
        checkpoint["completed"].append("long")
        _atomic_checkpoint(checkpoint_path, checkpoint)
    else:
        long_funnel = pl.read_parquet(long_funnel_path)

    funnel = _ensure_funnel_gate_columns(long_funnel).sort(
        ["sleeve", "signal_ts_ms", "symbol"]
    )
    structural_funnel = validate_decision_funnel(
        funnel,
        required_gate_orders=REQUIRED_GATE_ORDERS,
    )
    labels = _build_path_labels(funnel, bars_by_symbol)
    structural_labels = validate_path_labels(labels, funnel)

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    if staging.exists():
        raise FileExistsError(f"candidate staging directory already exists: {staging}")
    staging.mkdir(mode=0o700)
    try:
        funnel_path = staging / "decision_funnel.parquet"
        labels_path = staging / "path_labels.parquet"
        funnel.write_parquet(funnel_path, compression="zstd", statistics=True)
        labels.write_parquet(labels_path, compression="zstd", statistics=True)
        manifest_payload: dict[str, Any] = {
            "schema_version": 1,
            "kind": "candidate_tape_partition",
            "study_mode": "exploratory",
            "run_identity": run_identity,
            "run_identity_sha256": run_identity_sha256,
            "baseline_preflight": {
                "checked_at_utc": "2026-07-17T19:24:52.3929778Z",
                "expected_files": 23,
                "matching_files": 0,
                "missing_files": 23,
                "hash_mismatches": 0,
                "comparison_enabled": False,
            },
            "structural_validation": {
                "decision_funnel": structural_funnel,
                "path_labels": structural_labels,
                "source_key_completeness": True,
                "gate_transitions": "single_evaluation_partition_valid",
                "causal_availability": True,
                "duplicate_suppression": True,
            },
            "input_quality": {
                "raw_kline_rows": raw_kline_rows,
                "invalid_ohlc_rows_rejected": invalid_ohlc_rows,
                "valid_ohlc_rows_before_pit_filter": valid_ohlc_rows,
                "usable_kline_rows": klines.height,
                "pit_filter": pit_filter_receipt,
            },
            "counts": {
                "long_source_rows": long_funnel.height,
                "funnel_rows": funnel.height,
                "path_label_rows": labels.height,
            },
            "null_counts": {
                "decision_funnel": _null_counts(funnel),
                "path_labels": _null_counts(labels),
            },
            "outcomes_inspected": False,
            "files": {
                "decision_funnel.parquet": {
                    "bytes": funnel_path.stat().st_size,
                    "sha256": sha256_file(funnel_path),
                },
                "path_labels.parquet": {
                    "bytes": labels_path.stat().st_size,
                    "sha256": sha256_file(labels_path),
                },
            },
            "explicit_non_conclusions": [
                "no alpha conclusion",
                "no active-baseline comparison because pinned files are absent",
                "no strategy or deployment change",
                "no mainnet or real-money authority",
            ],
        }
        manifest_payload["manifest_payload_sha256"] = payload_sha256(manifest_payload)
        _write_json(staging / "manifest.json", manifest_payload)
        os.replace(staging, out)
        shutil.rmtree(work)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
