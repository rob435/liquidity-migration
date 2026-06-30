#!/usr/bin/env python3
"""Continuous fade validation harness.

This is intentionally research-local.  It reuses the production/research engine
for fills and portfolio construction, then writes diagnostics summarized in
STATE.md, docs/research_summary.md, and docs/preregistration/INDEX.md.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import subprocess
import sys
from collections import Counter
from dataclasses import fields, replace
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np
import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
HOT_PATH_DOCS = (
    REPO_ROOT / "STATE.md",
    REPO_ROOT / "docs" / "research_summary.md",
    REPO_ROOT / "docs" / "preregistration" / "INDEX.md",
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import continuous_deployed_equity_refresh as deployed_refresh  # noqa: E402
from liquidity_migration.continuous_component_sources import (  # noqa: E402
    CONTINUOUS_COMPONENT_SOURCES,
)
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    _assert_funding_one_per_settlement,
    _portfolio_mtm_equity,
    _round_trip_bps,
    run_continuous_event_research,
)
from liquidity_migration.continuous_forward_replay import (  # noqa: E402
    FROZEN_FORWARD_CONFIG,
    frozen_config_hash,
)
from liquidity_migration.continuous_rebalance import (  # noqa: E402
    ContinuousHedgeRule,
    apply_rebalance_rule,
    combine_continuous_components,
    decompose_continuous_components,
)
from liquidity_migration.trade_lifecycle import (  # noqa: E402
    _funding_lookup,
    _perp_funding_return,
    derive_funding_interval_min,
)

MS_PER_HOUR = 3_600_000
MS_PER_DAY = 86_400_000
MS_PER_MINUTE = 60_000
MS_PER_5M = 5 * MS_PER_MINUTE
MS_PER_15M = 15 * MS_PER_MINUTE
ANN_DAYS = 365.25
RUN_NAME = "continuous_ensemble_v2_baseline_current"
COMPONENT_TP = 0.12
VENUES = ("bybit", "binance")
SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA"))).expanduser()
NORMAL = NormalDist()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _date_from_ms(ts_ms: int) -> dt.date:
    return dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc).date()


def _ms_from_date(date_value: dt.date) -> int:
    return int(dt.datetime(date_value.year, date_value.month, date_value.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _date_dirs(root: Path, dataset: str) -> list[dt.date]:
    base = root / dataset
    if not base.exists():
        return []
    out: list[dt.date] = []
    for path in base.iterdir():
        if path.is_dir() and path.name.startswith("date="):
            try:
                out.append(dt.date.fromisoformat(path.name.split("=", 1)[1]))
            except ValueError:
                continue
    return sorted(out)


def _end_boundary_from_root(root: Path) -> str:
    dates = _date_dirs(root, "klines_1h")
    if not dates:
        raise FileNotFoundError(f"no klines_1h/date=* partitions under {root}")
    return (dates[-1] + dt.timedelta(days=1)).isoformat()


def _json_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_git(args: list[str]) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return ""


def git_identity() -> dict[str, Any]:
    status = _run_git(["status", "--short"])
    return {
        "commit": _run_git(["rev-parse", "HEAD"]),
        "commit_short": _run_git(["rev-parse", "--short", "HEAD"]),
        "branch": _run_git(["branch", "--show-current"]),
        "dirty": bool(status),
        "status_short": status.splitlines(),
    }


def venue_root(venue: str) -> Path:
    return SHARED / f"{venue}_full_pit"


def _scan_parquet(path: Path, columns: list[str] | None = None) -> pl.DataFrame:
    files = [str(p) for p in path.rglob("*.parquet")]
    if not files:
        return pl.DataFrame()
    scan = pl.scan_parquet(files)
    if columns:
        present = set(scan.collect_schema().names())
        scan = scan.select([c for c in columns if c in present])
    return scan.collect()


def _partition_range(root: Path, dataset: str) -> dict[str, Any]:
    dates = _date_dirs(root, dataset)
    return {
        "dataset": dataset,
        "first_date": dates[0].isoformat() if dates else None,
        "last_date": dates[-1].isoformat() if dates else None,
        "partition_count": len(dates),
    }


def _manifest_identity(root: Path) -> dict[str, Any]:
    info = _partition_range(root, "archive_trade_manifest")
    if not info["last_date"]:
        return info | {"last_symbol_count": 0}
    latest = root / "archive_trade_manifest" / f"date={info['last_date']}"
    df = _scan_parquet(latest, ["symbol"])
    return info | {"last_symbol_count": int(df["symbol"].n_unique()) if "symbol" in df.columns and df.height else 0}


def _residual_identity(root: Path) -> dict[str, Any]:
    path = root / "residual_momentum.parquet"
    if not path.exists():
        return {"path": str(path), "exists": False}
    df = pl.read_parquet(path, columns=["symbol", "ts_ms"])
    return {
        "path": str(path),
        "exists": True,
        "sha256": _file_sha256(path),
        "rows": int(df.height),
        "symbols": int(df["symbol"].n_unique()) if df.height else 0,
        "first_ts_ms": int(df["ts_ms"].min()) if df.height else None,
        "last_ts_ms": int(df["ts_ms"].max()) if df.height else None,
        "first_date": _date_from_ms(int(df["ts_ms"].min())).isoformat() if df.height else None,
        "last_date": _date_from_ms(int(df["ts_ms"].max())).isoformat() if df.height else None,
    }


def data_root_identity(venue: str) -> dict[str, Any]:
    root = venue_root(venue)
    funding_dataset = "funding" if venue == "bybit" else "binance_usdm_funding"
    return {
        "venue": venue,
        "root": str(root),
        "klines_1h": _partition_range(root, "klines_1h"),
        "klines_5m": _partition_range(root, "klines_5m"),
        "manifest": _manifest_identity(root),
        "funding": _partition_range(root, funding_dataset),
        "residual_momentum": _residual_identity(root),
        "end_boundary_exclusive": _end_boundary_from_root(root),
    }


def _cfg_from_payload(payload: dict[str, Any]) -> ContinuousEventConfig:
    raw = dict(payload["config"])
    allowed = {f.name for f in fields(ContinuousEventConfig)}
    kwargs = {k: v for k, v in raw.items() if k in allowed}
    for key in ("feature_set", "exclude_symbols"):
        if isinstance(kwargs.get(key), list):
            kwargs[key] = tuple(kwargs[key])
    return ContinuousEventConfig(**kwargs)


def _component_cell(component: str) -> str:
    return CONTINUOUS_COMPONENT_SOURCES[component].cell


def _component_dir(output_root: Path, venue: str, component: str) -> Path:
    return output_root / "components" / venue / _component_cell(component)


def _component_report_path(output_root: Path, venue: str, component: str) -> Path:
    return _component_dir(output_root, venue, component) / "continuous_report.json"


def _load_component_payload(output_root: Path, venue: str, component: str) -> dict[str, Any]:
    path = _component_report_path(output_root, venue, component)
    return json.loads(path.read_text(encoding="utf-8"))


def _load_size_mult_lookup(output_root: Path, venue: str) -> dict[tuple[str, int], float] | None:
    path = output_root / "btc_risk" / venue / "btc_risk_multipliers.csv"
    if not path.exists():
        return None
    df = pl.read_csv(path)
    if df.is_empty():
        return None
    return {
        (str(row["symbol"]), int(row["signal_ts_ms"])): float(row["stack_mult"])
        for row in df.select("symbol", "signal_ts_ms", "stack_mult").iter_rows(named=True)
    }


def run_frozen_baseline(output_root: Path, venues: list[str], *, rerun: bool) -> dict[str, Any]:
    output_root.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "run_name": RUN_NAME,
        "created_at_utc": _utc_now(),
        "component_take_profit_pct": COMPONENT_TP,
        "btc_risk_sizing": True,
        "chart_leverage": 1.0,
        "backtest_leverage": 1.0,
        "venues": {},
    }
    fallback = SHARED / "continuous_deployed_equity_refresh_2026-06-12"
    for venue in venues:
        root = venue_root(venue)
        end_date = _end_boundary_from_root(root)
        if not rerun and (output_root / venue / "continuous_equity_summary.json").exists():
            venue_summary = json.loads((output_root / venue / "continuous_equity_summary.json").read_text(encoding="utf-8"))
        else:
            venue_summary = deployed_refresh.run_venue(
                venue,
                output_root=output_root,
                end_date=end_date,
                start_date=None,
                render_only=False,
                frozen_fallback=fallback,
                data_root=root,
                chart_leverage=1.0,
                component_take_profit_pct=COMPONENT_TP,
                btc_risk_sizing=True,
                backtest_leverage=1.0,
            )
        summary["venues"][venue] = {
            "data_root": str(root),
            "end_date_exclusive": end_date,
            "runner_summary": venue_summary,
        }
    (output_root / "baseline_run_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary


def write_candidate_tapes(output_root: Path, venues: list[str], *, rerun: bool) -> None:
    signal_root = output_root / "signals"
    for venue in venues:
        root = venue_root(venue)
        size_lookup = _load_size_mult_lookup(output_root, venue)
        for component in deployed_refresh.WINNER_WEIGHTS:
            tape_path = signal_root / venue / f"{component}_candidate_tape.parquet"
            if tape_path.exists() and not rerun:
                continue
            payload = _load_component_payload(output_root, venue, component)
            cfg = _cfg_from_payload(payload)
            run_continuous_event_research(
                root,
                config=cfg,
                report_dir=_component_dir(output_root, venue, component),
                candidate_tape_path=tape_path,
                size_mult_lookup=size_lookup,
            )


def _read_component_trades(output_root: Path, venues: list[str]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    commit = _run_git(["rev-parse", "HEAD"])
    profile_hash = frozen_config_hash()
    for venue in venues:
        for component, weight in deployed_refresh.WINNER_WEIGHTS.items():
            path = _component_dir(output_root, venue, component) / "continuous_trades.csv"
            if not path.exists():
                continue
            df = pl.read_csv(path)
            if df.is_empty():
                continue
            frames.append(
                df.with_columns(
                    pl.lit(venue).alias("venue"),
                    pl.lit(component).alias("component_id"),
                    pl.lit(_component_cell(component)).alias("component_cell"),
                    pl.lit(RUN_NAME).alias("profile_name"),
                    pl.lit(profile_hash).alias("profile_hash"),
                    pl.lit(commit).alias("git_commit"),
                    pl.lit(float(weight)).alias("component_weight"),
                    (pl.col("net_return") * float(weight)).alias("portfolio_net_return"),
                    (pl.col("gross_return") * float(weight)).alias("portfolio_gross_return"),
                    (pl.col("cost_return") * float(weight)).alias("portfolio_cost_return"),
                    (pl.col("funding_return") * float(weight)).alias("portfolio_funding_return"),
                    (-pl.col("mae")).alias("adverse_excursion_pct"),
                    pl.col("mfe").alias("favorable_excursion_pct"),
                    (
                        pl.lit(venue)
                        + pl.lit(":")
                        + pl.lit(component)
                        + pl.lit(":")
                        + pl.col("symbol")
                        + pl.lit(":")
                        + pl.col("entry_signal_ts_ms").cast(pl.Utf8)
                    ).alias("signal_id"),
                )
            )
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _read_candidate_tapes(output_root: Path, venues: list[str]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    commit = _run_git(["rev-parse", "HEAD"])
    profile_hash = frozen_config_hash()
    for venue in venues:
        for component in deployed_refresh.WINNER_WEIGHTS:
            path = output_root / "signals" / venue / f"{component}_candidate_tape.parquet"
            if not path.exists():
                continue
            df = pl.read_parquet(path)
            if df.is_empty():
                continue
            frames.append(
                df.with_columns(
                    pl.lit(venue).alias("venue"),
                    pl.lit(component).alias("component_id"),
                    pl.lit(_component_cell(component)).alias("component_cell"),
                    pl.lit(RUN_NAME).alias("profile_name"),
                    pl.lit(profile_hash).alias("profile_hash"),
                    pl.lit(commit).alias("git_commit"),
                    pl.col("composite").alias("component_score"),
                    pl.col("composite").alias("composite_score"),
                    (
                        pl.lit(venue)
                        + pl.lit(":")
                        + pl.lit(component)
                        + pl.lit(":")
                        + pl.col("symbol")
                        + pl.lit(":")
                        + pl.col("signal_ts_ms").cast(pl.Utf8)
                    ).alias("signal_id"),
                )
            )
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed")


def _date_part_paths(root: Path, dataset: str, start_ms: int, end_ms: int, symbols: list[str] | None = None) -> list[str]:
    start = _date_from_ms(start_ms) - dt.timedelta(days=1)
    end = _date_from_ms(end_ms) + dt.timedelta(days=1)
    out: list[str] = []
    d = start
    while d <= end:
        part = root / dataset / f"date={d.isoformat()}"
        if part.exists():
            if symbols:
                for symbol in symbols:
                    symbol_part = part / f"symbol={symbol}"
                    if symbol_part.exists():
                        out.extend(str(p) for p in symbol_part.rglob("*.parquet"))
            else:
                out.extend(str(p) for p in part.rglob("*.parquet"))
        d += dt.timedelta(days=1)
    return out


def _date_symbol_paths_for_rows(
    root: Path,
    dataset: str,
    rows: pl.DataFrame,
    *,
    start_col: str = "entry_ts_ms",
    end_col: str = "exit_ts_ms",
    pad_start_ms: int = 0,
    pad_end_ms: int = 0,
) -> list[str]:
    paths: set[str] = set()
    for row in rows.select(["symbol", start_col, end_col]).iter_rows(named=True):
        start_ms = int(row[start_col]) - pad_start_ms
        end_ms = int(row[end_col]) + pad_end_ms
        d = _date_from_ms(start_ms)
        end = _date_from_ms(end_ms)
        while d <= end:
            symbol_part = root / dataset / f"date={d.isoformat()}" / f"symbol={row['symbol']}"
            if symbol_part.exists():
                paths.update(str(p) for p in symbol_part.rglob("*.parquet"))
            d += dt.timedelta(days=1)
    return sorted(paths)


def _load_klines_for_rows(
    root: Path,
    rows: pl.DataFrame,
    *,
    end_col: str = "exit_ts_ms",
    dataset: str = "klines_1h",
    interval_ms: int = MS_PER_HOUR,
    sparse_windows: bool = False,
) -> dict[str, pl.DataFrame]:
    if rows.is_empty():
        return {}
    start_ms = int(rows["entry_ts_ms"].min()) - MS_PER_DAY
    end_ms = int(rows[end_col].max()) + MS_PER_DAY
    symbols = rows["symbol"].unique().to_list()
    if sparse_windows:
        paths = _date_symbol_paths_for_rows(
            root,
            dataset,
            rows,
            end_col=end_col,
            pad_start_ms=interval_ms,
            pad_end_ms=interval_ms,
        )
    else:
        paths = _date_part_paths(root, dataset, start_ms, end_ms, symbols=[str(s) for s in symbols])
        if not paths:
            paths = _date_part_paths(root, dataset, start_ms, end_ms)
    if not paths:
        return {}
    wanted = {"symbol", "ts_ms", "open", "high", "low", "close", "turnover_quote"}
    schema_names = set(pl.scan_parquet(paths).collect_schema().names())
    cols = [c for c in wanted if c in schema_names]
    df = (
        pl.scan_parquet(paths)
        .select(cols)
        .filter(pl.col("symbol").is_in(symbols))
        .with_columns((pl.col("ts_ms") + interval_ms).alias("bar_end_ts_ms"))
        .filter((pl.col("bar_end_ts_ms") >= start_ms) & (pl.col("bar_end_ts_ms") <= end_ms))
        .collect()
        .sort(["symbol", "bar_end_ts_ms"])
    )
    return {str(k[0] if isinstance(k, tuple) else k): part for k, part in df.partition_by("symbol", as_dict=True).items()}


def _load_klines_for_signal_rows(
    root: Path,
    rows: pl.DataFrame,
    *,
    max_forward_hours: int = 30,
    dataset: str = "klines_1h",
    interval_ms: int = MS_PER_HOUR,
    sparse_windows: bool = False,
) -> dict[str, pl.DataFrame]:
    if rows.is_empty():
        return {}
    entry_anchor = pl.coalesce([pl.col("entry_bar_end_ts_ms"), pl.col("order_submit_ts_ms")])
    tmp = rows.with_columns(
        entry_anchor.alias("entry_ts_ms"),
        (entry_anchor + max_forward_hours * MS_PER_HOUR).alias("exit_ts_ms"),
    )
    return _load_klines_for_rows(
        root,
        tmp,
        dataset=dataset,
        interval_ms=interval_ms,
        sparse_windows=sparse_windows,
    )


def _trade_path_row(row: dict[str, Any], bars_by_symbol: dict[str, pl.DataFrame]) -> dict[str, Any]:
    symbol = str(row["symbol"])
    bars = bars_by_symbol.get(symbol)
    out: dict[str, Any] = {
        "signal_id": row["signal_id"],
        "venue": row["venue"],
        "component_id": row["component_id"],
        "symbol": symbol,
        "entry_signal_ts_ms": int(row["entry_signal_ts_ms"]),
        "entry_ts_ms": int(row["entry_ts_ms"]),
        "exit_ts_ms": int(row["exit_ts_ms"]),
        "time_to_mae_hours": None,
        "time_to_mfe_hours": None,
        "time_to_first_profit_hours": None,
        "time_underwater_hours": None,
        "time_to_recovery_hours": None,
        "path_bars": 0,
    }
    if bars is None or bars.is_empty():
        return out
    entry_ts = int(row["entry_ts_ms"])
    exit_ts = int(row["exit_ts_ms"])
    entry_price = float(row["entry_price"])
    path = bars.filter((pl.col("bar_end_ts_ms") >= entry_ts) & (pl.col("bar_end_ts_ms") <= exit_ts))
    if path.is_empty() or entry_price <= 0:
        return out
    high = path["high"].to_numpy()
    low = path["low"].to_numpy()
    close = path["close"].to_numpy()
    ts = path["bar_end_ts_ms"].to_numpy()
    adverse = high / entry_price - 1.0
    favorable = 1.0 - low / entry_price
    close_ret = 1.0 - close / entry_price
    mae_idx = int(np.argmax(adverse))
    mfe_idx = int(np.argmax(favorable))
    out["path_bars"] = int(len(ts))
    out["time_to_mae_hours"] = float((int(ts[mae_idx]) - entry_ts) / MS_PER_HOUR)
    out["time_to_mfe_hours"] = float((int(ts[mfe_idx]) - entry_ts) / MS_PER_HOUR)
    prof = np.where(close_ret > 0.0)[0]
    if prof.size:
        out["time_to_first_profit_hours"] = float((int(ts[int(prof[0])]) - entry_ts) / MS_PER_HOUR)
    out["time_underwater_hours"] = float(np.sum(close_ret < 0.0))
    rec = np.where((np.arange(len(ts)) >= mae_idx) & (close_ret >= 0.0))[0]
    if rec.size:
        out["time_to_recovery_hours"] = float((int(ts[int(rec[0])]) - int(ts[mae_idx])) / MS_PER_HOUR)
    return out


def write_path_metrics(output_root: Path, trades: pl.DataFrame, venues: list[str]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for venue in venues:
        part = trades.filter(pl.col("venue") == venue)
        if part.is_empty():
            continue
        bars = _load_klines_for_rows(venue_root(venue), part)
        rows = [_trade_path_row(row, bars) for row in part.to_dicts()]
        frames.append(pl.DataFrame(rows))
    if not frames:
        return trades
    path = pl.concat(frames, how="diagonal_relaxed")
    path.write_parquet(output_root / "tables" / "trade_path_metrics.parquet")
    enriched = trades.join(path, on=["signal_id", "venue", "component_id", "symbol", "entry_signal_ts_ms", "entry_ts_ms", "exit_ts_ms"], how="left")
    enriched.write_parquet(output_root / "tables" / "trades_enriched.parquet")
    return enriched


def _series_sharpe(values: np.ndarray, periods_per_year: float) -> float | None:
    values = values[np.isfinite(values)]
    if values.size < 2:
        return None
    sd = float(values.std(ddof=1))
    if sd <= 1e-12:
        return None
    return float(values.mean() / sd * math.sqrt(periods_per_year))


def _sortino(values: np.ndarray, periods_per_year: float) -> float | None:
    values = values[np.isfinite(values)]
    downside = values[values < 0.0]
    if values.size < 2 or downside.size < 2:
        return None
    sd = float(downside.std(ddof=1))
    if sd <= 1e-12:
        return None
    return float(values.mean() / sd * math.sqrt(periods_per_year))


def _profit_factor(values: np.ndarray) -> float | None:
    pos = float(values[values > 0.0].sum())
    neg = float(values[values < 0.0].sum())
    if abs(neg) <= 1e-15:
        return None
    return pos / abs(neg)


def _safe_mean(values: pl.Series) -> float | None:
    vals = values.drop_nulls()
    return float(vals.mean()) if not vals.is_empty() else None


def _write_df(df: pl.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".parquet":
        df.write_parquet(path)
    else:
        df.write_csv(path)


def baseline_metrics(output_root: Path, trades: pl.DataFrame, candidates: pl.DataFrame, venues: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for venue in venues:
        v_trades = trades.filter(pl.col("venue") == venue)
        v_cands = candidates.filter(pl.col("venue") == venue)
        equity_path = output_root / venue / "continuous_equity.csv"
        equity = pl.read_csv(equity_path) if equity_path.exists() else pl.DataFrame()
        returns = equity["basket_return"].to_numpy() if not equity.is_empty() else np.array([], dtype=float)
        nonzero = returns[np.abs(returns) > 1e-15]
        years = None
        if not equity.is_empty():
            first = int(equity["ts_ms"].min())
            last = int(equity["ts_ms"].max())
            years = max((last - first) / (ANN_DAYS * MS_PER_DAY), 1.0 / ANN_DAYS)
        active_periods = (len(nonzero) / years) if years and years > 0 else ANN_DAYS
        values = v_trades["portfolio_net_return"].to_numpy() if not v_trades.is_empty() else np.array([], dtype=float)
        wins = values[values > 0.0]
        losses = values[values < 0.0]
        cluster = (
            v_trades.group_by("entry_signal_ts_ms").agg(pl.col("portfolio_net_return").sum().alias("cluster_return"))
            if not v_trades.is_empty()
            else pl.DataFrame({"cluster_return": []})
        )
        cluster_periods = (cluster.height / years) if years and years > 0 else ANN_DAYS
        summary_json = output_root / venue / "continuous_equity_summary.json"
        runner_summary = json.loads(summary_json.read_text(encoding="utf-8")) if summary_json.exists() else {}
        out[venue] = {
            "signals": int(v_cands.height),
            "selected_signals": int(v_cands.filter(pl.col("selected")).height) if not v_cands.is_empty() else 0,
            "trades": int(v_trades.height),
            "win_rate": float((values > 0.0).mean()) if values.size else None,
            "avg_win": float(wins.mean()) if wins.size else None,
            "avg_loss": float(losses.mean()) if losses.size else None,
            "payoff_ratio": float(wins.mean() / abs(losses.mean())) if wins.size and losses.size else None,
            "profit_factor": _profit_factor(values),
            "expectancy_per_trade": float(values.mean()) if values.size else None,
            "expectancy_per_signal": float(values.sum() / max(v_cands.height, 1)) if values.size else None,
            "median_pnl": float(np.median(values)) if values.size else None,
            "mean_pnl": float(values.mean()) if values.size else None,
            "net_pnl": float(values.sum()) if values.size else 0.0,
            "fees": float(v_trades["portfolio_cost_return"].sum()) if not v_trades.is_empty() else 0.0,
            "funding": float(v_trades["portfolio_funding_return"].sum()) if not v_trades.is_empty() else 0.0,
            "skew": float(pl.Series(values).skew()) if values.size >= 3 else None,
            "kurtosis": float(pl.Series(values).kurtosis()) if values.size >= 4 else None,
            "sharpe_calendar": _series_sharpe(returns, ANN_DAYS),
            "active_sharpe": _series_sharpe(nonzero, active_periods),
            "cluster_adjusted_sharpe": _series_sharpe(cluster["cluster_return"].to_numpy(), cluster_periods) if cluster.height else None,
            "sortino_calendar": _sortino(returns, ANN_DAYS),
            "runner_stats": runner_summary.get("stats", {}),
            "run_label": "exploratory",
            "label_reason": "Engine-grade PIT/costed research replay, but still internal in-sample research; forward demo/paper remains OOS arbiter.",
        }
    return out


def write_group_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    tables = output_root / "tables"
    by_component = (
        trades.group_by(["venue", "component_id"])
        .agg(
            pl.len().alias("trades"),
            pl.col("portfolio_net_return").sum().alias("net_pnl"),
            (pl.col("portfolio_net_return") > 0).mean().alias("win_rate"),
            pl.col("adverse_excursion_pct").mean().alias("avg_mae"),
            pl.col("favorable_excursion_pct").mean().alias("avg_mfe"),
            pl.col("portfolio_net_return").min().alias("worst_trade"),
        )
        .sort(["venue", "component_id"])
    )
    _write_df(by_component, tables / "pnl_by_component.csv")
    artifacts["pnl_by_component"] = str(tables / "pnl_by_component.csv")

    by_year = (
        trades.with_columns(pl.col("entry_date").cast(pl.Utf8).str.slice(0, 4).alias("year"))
        .group_by(["venue", "year"])
        .agg(
            pl.len().alias("trades"),
            pl.col("portfolio_net_return").sum().alias("net_pnl"),
            (pl.col("portfolio_net_return") > 0).mean().alias("win_rate"),
            pl.col("portfolio_net_return").min().alias("worst_trade"),
        )
        .sort(["venue", "year"])
    )
    _write_df(by_year, tables / "pnl_by_year.csv")
    artifacts["pnl_by_year"] = str(tables / "pnl_by_year.csv")

    by_symbol = (
        trades.group_by(["venue", "symbol"])
        .agg(
            pl.len().alias("trades"),
            pl.col("portfolio_net_return").sum().alias("net_pnl"),
            pl.col("portfolio_net_return").min().alias("worst_trade"),
        )
        .sort(["venue", "net_pnl"], descending=[False, False])
    )
    _write_df(by_symbol, tables / "pnl_by_symbol.csv")
    artifacts["pnl_by_symbol"] = str(tables / "pnl_by_symbol.csv")

    by_liq = (
        trades.with_columns(
            pl.when(pl.col("notional_weight") <= 0.01)
            .then(pl.lit("small_size"))
            .when(pl.col("notional_weight") <= 0.02)
            .then(pl.lit("mid_size"))
            .otherwise(pl.lit("large_size"))
            .alias("size_bucket")
        )
        .group_by(["venue", "size_bucket"])
        .agg(
            pl.len().alias("trades"),
            pl.col("portfolio_net_return").sum().alias("net_pnl"),
            (pl.col("portfolio_net_return") > 0).mean().alias("win_rate"),
            pl.col("adverse_excursion_pct").mean().alias("avg_mae"),
        )
        .sort(["venue", "size_bucket"])
    )
    _write_df(by_liq, tables / "pnl_by_size_bucket.csv")
    artifacts["pnl_by_size_bucket"] = str(tables / "pnl_by_size_bucket.csv")
    return artifacts


def write_mae_mfe_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    tables = output_root / "tables"
    buckets = [
        (0.00, 0.01, "0-1%"),
        (0.01, 0.02, "1-2%"),
        (0.02, 0.05, "2-5%"),
        (0.05, 0.10, "5-10%"),
        (0.10, 0.20, "10-20%"),
        (0.20, 0.40, "20-40%"),
        (0.40, 0.80, "40-80%"),
        (0.80, math.inf, "80%+"),
    ]
    rows: list[dict[str, Any]] = []
    for venue in sorted(trades["venue"].unique().to_list()):
        v = trades.filter(pl.col("venue") == venue)
        for lo, hi, label in buckets:
            part = v.filter((pl.col("adverse_excursion_pct") >= lo) & (pl.col("adverse_excursion_pct") < hi))
            vals = part["portfolio_net_return"].to_numpy() if not part.is_empty() else np.array([])
            rows.append(
                {
                    "venue": venue,
                    "mae_bucket": label,
                    "trades": int(part.height),
                    "win_rate": float((vals > 0).mean()) if vals.size else None,
                    "avg_net_pnl": float(vals.mean()) if vals.size else None,
                    "median_net_pnl": float(np.median(vals)) if vals.size else None,
                    "profit_factor": _profit_factor(vals),
                    "tail_5pct": float(np.quantile(vals, 0.05)) if vals.size else None,
                    "worst_trade": float(vals.min()) if vals.size else None,
                    "avg_recovery_time_hours": _safe_mean(part["time_to_recovery_hours"]) if "time_to_recovery_hours" in part.columns else None,
                    "p_profit_given_bucket": float((vals > 0).mean()) if vals.size else None,
                }
            )
    mae_bucket = pl.DataFrame(rows)
    _write_df(mae_bucket, tables / "mae_bucket_summary.csv")
    artifacts["mae_bucket_summary"] = str(tables / "mae_bucket_summary.csv")

    threshold_rows: list[dict[str, Any]] = []
    for venue in sorted(trades["venue"].unique().to_list()):
        v = trades.filter(pl.col("venue") == venue)
        total = max(v.height, 1)
        for threshold in (0.02, 0.05, 0.10, 0.20, 0.40):
            part = v.filter(pl.col("adverse_excursion_pct") >= threshold)
            vals = part["portfolio_net_return"].to_numpy() if not part.is_empty() else np.array([])
            threshold_rows.append(
                {
                    "venue": venue,
                    "mae_threshold_reached": threshold,
                    "trades": int(part.height),
                    "pct_of_trades": float(part.height / total),
                    "eventually_profitable": float((vals > 0).mean()) if vals.size else None,
                    "avg_final_pnl": float(vals.mean()) if vals.size else None,
                    "median_final_pnl": float(np.median(vals)) if vals.size else None,
                    "tail_5pct": float(np.quantile(vals, 0.05)) if vals.size else None,
                    "avg_recovery_time_hours": _safe_mean(part["time_to_recovery_hours"]) if "time_to_recovery_hours" in part.columns else None,
                }
            )
    threshold_df = pl.DataFrame(threshold_rows)
    _write_df(threshold_df, tables / "mae_conditional_recovery.csv")
    artifacts["mae_conditional_recovery"] = str(tables / "mae_conditional_recovery.csv")

    winner_loser = (
        trades.with_columns((pl.col("portfolio_net_return") > 0).alias("winner"))
        .group_by(["venue", "winner"])
        .agg(
            pl.len().alias("trades"),
            pl.col("adverse_excursion_pct").mean().alias("avg_mae"),
            pl.col("adverse_excursion_pct").median().alias("median_mae"),
            pl.col("favorable_excursion_pct").mean().alias("avg_mfe"),
            pl.col("favorable_excursion_pct").median().alias("median_mfe"),
            pl.col("time_to_mae_hours").mean().alias("avg_time_to_mae_hours"),
            pl.col("time_underwater_hours").mean().alias("avg_time_underwater_hours"),
        )
        .sort(["venue", "winner"])
    )
    _write_df(winner_loser, tables / "winner_loser_path_summary.csv")
    artifacts["winner_loser_path_summary"] = str(tables / "winner_loser_path_summary.csv")
    return artifacts


FORWARD_HORIZONS: tuple[tuple[str, int], ...] = (
    ("15m", 15 * MS_PER_MINUTE),
    ("30m", 30 * MS_PER_MINUTE),
    ("1h", MS_PER_HOUR),
    ("2h", 2 * MS_PER_HOUR),
    ("3h", 3 * MS_PER_HOUR),
    ("4h", 4 * MS_PER_HOUR),
    ("6h", 6 * MS_PER_HOUR),
    ("8h", 8 * MS_PER_HOUR),
    ("12h", 12 * MS_PER_HOUR),
    ("18h", 18 * MS_PER_HOUR),
    ("24h", 24 * MS_PER_HOUR),
    ("36h", 36 * MS_PER_HOUR),
    ("48h", 48 * MS_PER_HOUR),
)


def _forward_path_rows_for_symbol(rows: pl.DataFrame, bars: pl.DataFrame) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base_cols = [
        "signal_id",
        "venue",
        "component_id",
        "symbol",
        "signal_ts_ms",
        "entry_bar_end_ts_ms",
        "order_submit_ts_ms",
        "selected",
        "reason",
        "portfolio_net_return",
        "adverse_excursion_pct",
        "favorable_excursion_pct",
    ]
    if bars.is_empty():
        for row in rows.select([c for c in base_cols if c in rows.columns]).to_dicts():
            out.append(row | {"coverage_reason": "missing_bars", "entry_price": None})
        return out
    ts = bars["bar_end_ts_ms"].to_numpy()
    close = bars["close"].to_numpy()
    high = bars["high"].to_numpy()
    low = bars["low"].to_numpy()
    for row in rows.to_dicts():
        record = {c: row.get(c) for c in base_cols if c in row}
        entry_anchor = row.get("entry_bar_end_ts_ms")
        if entry_anchor is None:
            entry_anchor = row.get("order_submit_ts_ms")
        if entry_anchor is None:
            out.append(record | {"coverage_reason": "missing_entry_anchor", "entry_price": None})
            continue
        entry_ts = int(entry_anchor)
        record["forward_anchor_ts_ms"] = entry_ts
        idx0 = int(np.searchsorted(ts, entry_ts))
        if idx0 >= len(ts) or int(ts[idx0]) != entry_ts:
            out.append(record | {"coverage_reason": "missing_entry_bar", "entry_price": None})
            continue
        entry_price = float(close[idx0])
        record["entry_price"] = entry_price
        record["coverage_reason"] = "ok"
        max_complete = 0
        for label, horizon_ms in FORWARD_HORIZONS:
            target = entry_ts + horizon_ms
            idx = int(np.searchsorted(ts, target))
            ret_col = f"ret_{label}"
            up_col = f"max_up_{label}"
            down_col = f"max_down_{label}"
            complete_col = f"complete_{label}"
            if idx >= len(ts) or int(ts[idx]) != target or entry_price <= 0:
                record[ret_col] = None
                record[up_col] = None
                record[down_col] = None
                record[complete_col] = False
                continue
            expected = horizon_ms // MS_PER_5M + 1
            if idx - idx0 + 1 != expected:
                record[ret_col] = None
                record[up_col] = None
                record[down_col] = None
                record[complete_col] = False
                continue
            seg = slice(idx0, idx + 1)
            record[ret_col] = float(1.0 - close[idx] / entry_price)
            record[up_col] = float(np.max(high[seg]) / entry_price - 1.0)
            record[down_col] = float(1.0 - np.min(low[seg]) / entry_price)
            record[complete_col] = True
            max_complete = int(horizon_ms / MS_PER_MINUTE)
        record["max_complete_horizon_minutes"] = max_complete
        out.append(record)
    return out


def _append_forward_curve(rows: list[dict[str, Any]], segment: str, df: pl.DataFrame) -> None:
    if df.is_empty():
        return
    for label, horizon_ms in FORWARD_HORIZONS:
        ret_col = f"ret_{label}"
        if ret_col not in df.columns:
            continue
        vals = df[ret_col].drop_nulls().to_numpy()
        up_vals = df[f"max_up_{label}"].drop_nulls().to_numpy() if f"max_up_{label}" in df.columns else np.array([])
        down_vals = df[f"max_down_{label}"].drop_nulls().to_numpy() if f"max_down_{label}" in df.columns else np.array([])
        rows.append(
            {
                "venue": str(df["venue"][0]),
                "segment": segment,
                "horizon": label,
                "horizon_minutes": int(horizon_ms / MS_PER_MINUTE),
                "signals": int(df.height),
                "coverage": float(vals.size / max(df.height, 1)),
                "avg_short_return": float(vals.mean()) if vals.size else None,
                "median_short_return": float(np.median(vals)) if vals.size else None,
                "avg_max_up": float(up_vals.mean()) if up_vals.size else None,
                "avg_max_down": float(down_vals.mean()) if down_vals.size else None,
            }
        )


def write_forward_path_tables(output_root: Path, candidates: pl.DataFrame, trades: pl.DataFrame, venues: list[str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if candidates.is_empty():
        return artifacts
    tables = output_root / "tables"
    trade_outcomes = (
        trades.select("signal_id", "portfolio_net_return", "adverse_excursion_pct", "favorable_excursion_pct")
        if not trades.is_empty()
        else pl.DataFrame({"signal_id": [], "portfolio_net_return": [], "adverse_excursion_pct": [], "favorable_excursion_pct": []})
    )
    signals = candidates.join(trade_outcomes, on="signal_id", how="left")
    frames: list[pl.DataFrame] = []
    for venue in venues:
        part = signals.filter(pl.col("venue") == venue)
        if part.is_empty():
            continue
        rows: list[dict[str, Any]] = []
        for symbol_part in part.partition_by("symbol", maintain_order=True):
            bars = _load_klines_for_signal_rows(
                venue_root(venue),
                symbol_part,
                max_forward_hours=50,
                dataset="klines_5m",
                interval_ms=MS_PER_5M,
                sparse_windows=True,
            )
            symbol = str(symbol_part["symbol"][0])
            rows.extend(_forward_path_rows_for_symbol(symbol_part, bars.get(symbol, pl.DataFrame())))
        if rows:
            frames.append(pl.DataFrame(rows))
    if not frames:
        return artifacts
    by_signal = pl.concat(frames, how="diagonal_relaxed")
    _write_df(by_signal, tables / "forward_path_by_signal.parquet")
    artifacts["forward_path_by_signal"] = str(tables / "forward_path_by_signal.parquet")

    curve_rows: list[dict[str, Any]] = []
    for venue in venues:
        v = by_signal.filter(pl.col("venue") == venue)
        if v.is_empty():
            continue
        _append_forward_curve(curve_rows, "all_signals", v)
        _append_forward_curve(curve_rows, "selected_trades", v.filter(pl.col("selected")))
        _append_forward_curve(curve_rows, "winners", v.filter(pl.col("portfolio_net_return") > 0.0))
        _append_forward_curve(curve_rows, "losers", v.filter(pl.col("portfolio_net_return") <= 0.0))
        pnl = v["portfolio_net_return"].drop_nulls()
        if not pnl.is_empty():
            top = float(pnl.quantile(0.90))
            bottom = float(pnl.quantile(0.10))
            _append_forward_curve(curve_rows, "top_decile_winners", v.filter(pl.col("portfolio_net_return") >= top))
            _append_forward_curve(curve_rows, "bottom_decile_losers", v.filter(pl.col("portfolio_net_return") <= bottom))
        for component in sorted(v["component_id"].unique().to_list()):
            _append_forward_curve(curve_rows, f"component:{component}", v.filter(pl.col("component_id") == component))
    curves = pl.DataFrame(curve_rows)
    _write_df(curves, tables / "forward_path_curves.csv")
    artifacts["forward_path_curves"] = str(tables / "forward_path_curves.csv")
    return artifacts


def _label_trade_path(row: dict[str, Any]) -> str:
    pnl = float(row.get("portfolio_net_return") or 0.0)
    mae = float(row.get("adverse_excursion_pct") or 0.0)
    mfe = float(row.get("favorable_excursion_pct") or 0.0)
    first_profit = row.get("time_to_first_profit_hours")
    if mae >= 0.80:
        return "DISASTER"
    if pnl < 0.0 and mae >= 0.20:
        return "FAILED_FADE"
    if pnl > 0.0 and first_profit is not None and float(first_profit) <= 1.0:
        return "FAST_REVERT"
    if pnl > 0.0 and mae >= 0.10:
        return "SQUEEZE_REVERT"
    if pnl > 0.0:
        return "SLOW_REVERT"
    if mae < 0.05 and mfe < 0.05:
        return "CHOP"
    return "FAILED_FADE"


def write_path_label_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    tables = output_root / "tables"
    rows = []
    for row in trades.to_dicts():
        rows.append(
            {
                "signal_id": row["signal_id"],
                "venue": row["venue"],
                "component_id": row["component_id"],
                "symbol": row["symbol"],
                "portfolio_net_return": row["portfolio_net_return"],
                "adverse_excursion_pct": row["adverse_excursion_pct"],
                "favorable_excursion_pct": row["favorable_excursion_pct"],
                "time_to_first_profit_hours": row.get("time_to_first_profit_hours"),
                "path_label": _label_trade_path(row),
            }
        )
    labels = pl.DataFrame(rows)
    _write_df(labels, tables / "trade_path_labels.csv")
    artifacts["trade_path_labels"] = str(tables / "trade_path_labels.csv")
    summary = (
        labels.group_by(["venue", "path_label"])
        .agg(
            pl.len().alias("trades"),
            pl.col("portfolio_net_return").sum().alias("net_pnl"),
            pl.col("portfolio_net_return").mean().alias("avg_pnl"),
            pl.col("adverse_excursion_pct").mean().alias("avg_mae"),
            pl.col("portfolio_net_return").min().alias("worst_trade"),
        )
        .sort(["venue", "path_label"])
    )
    _write_df(summary, tables / "path_label_summary.csv")
    artifacts["path_label_summary"] = str(tables / "path_label_summary.csv")
    return artifacts


def write_component_ablation(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    rows: list[dict[str, Any]] = []
    for venue in sorted(trades["venue"].unique().to_list()):
        v = trades.filter(pl.col("venue") == venue)
        scenarios: list[tuple[str, pl.DataFrame, str]] = [("baseline_current_weights", v, "current weighted component ledger")]
        for component in sorted(v["component_id"].unique().to_list()):
            scenarios.append((f"remove_{component}", v.filter(pl.col("component_id") != component), "ledger recombination; not full portfolio replay"))
            only = v.filter(pl.col("component_id") == component).with_columns(pl.col("net_return").alias("scenario_return"))
            scenarios.append((f"only_{component}_unweighted", only, "single component raw ledger; not full portfolio replay"))
        scenarios.append(
            (
                "equal_weight_components",
                v.with_columns((pl.col("net_return") / len(deployed_refresh.WINNER_WEIGHTS)).alias("scenario_return")),
                "equal-weight raw component ledger recombination",
            )
        )
        for scenario, df, note in scenarios:
            if df.is_empty():
                vals = np.array([])
                cluster_vals = np.array([])
            else:
                ret_col = "scenario_return" if "scenario_return" in df.columns else "portfolio_net_return"
                vals = df[ret_col].to_numpy()
                cluster_vals = df.group_by("entry_signal_ts_ms").agg(pl.col(ret_col).sum().alias("ret"))["ret"].to_numpy()
            rows.append(
                {
                    "venue": venue,
                    "scenario": scenario,
                    "trades": int(df.height),
                    "net_pnl": float(vals.sum()) if vals.size else 0.0,
                    "win_rate": float((vals > 0).mean()) if vals.size else None,
                    "profit_factor": _profit_factor(vals),
                    "cluster_sharpe_like": _series_sharpe(cluster_vals, max(len(cluster_vals), 1)) if cluster_vals.size else None,
                    "worst_trade": float(vals.min()) if vals.size else None,
                    "note": note,
                }
            )
    out = pl.DataFrame(rows)
    _write_df(out, output_root / "tables" / "component_ablation_ledger_recombination.csv")
    artifacts["component_ablation_ledger_recombination"] = str(output_root / "tables" / "component_ablation_ledger_recombination.csv")
    return artifacts


def _scenario_values_for_tail(part: pl.DataFrame, scenario: str) -> np.ndarray:
    vals = part["portfolio_net_return"].to_numpy().copy()
    if vals.size == 0 or scenario == "baseline":
        return vals
    order = np.argsort(vals)
    if scenario.startswith("remove_best_"):
        count = int(scenario.removeprefix("remove_best_"))
        keep = np.ones(vals.size, dtype=bool)
        keep[np.argsort(vals)[-count:]] = False
        return vals[keep]
    if scenario.startswith("remove_worst_"):
        count = int(scenario.removeprefix("remove_worst_"))
        keep = np.ones(vals.size, dtype=bool)
        keep[order[:count]] = False
        return vals[keep]
    if scenario.startswith("double_worst_"):
        count = int(scenario.removeprefix("double_worst_"))
        vals[order[:count]] *= 2.0
        return vals
    if scenario.startswith("replace_worst_"):
        count = int(scenario.removeprefix("replace_worst_").split("_", 1)[0])
        worst_idx = order[:count]
        replacement = (
            -part["notional_weight"].to_numpy()[worst_idx] * part["component_weight"].to_numpy()[worst_idx]
            + part["portfolio_cost_return"].to_numpy()[worst_idx]
            + part["portfolio_funding_return"].to_numpy()[worst_idx]
        )
        vals[worst_idx] = replacement
    return vals


def write_worst_tail_and_heat_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    tables = output_root / "tables"
    scenarios = [
        "baseline",
        "remove_best_1",
        "remove_best_5",
        "remove_best_10",
        "remove_worst_1",
        "remove_worst_5",
        "remove_worst_10",
        "double_worst_1",
        "double_worst_5",
        "double_worst_10",
        "replace_worst_1_100pct_squeeze",
        "replace_worst_3_100pct_squeeze",
    ]
    rows: list[dict[str, Any]] = []
    for venue in sorted(trades["venue"].unique().to_list()):
        part = trades.filter(pl.col("venue") == venue)
        for scenario in scenarios:
            vals = _scenario_values_for_tail(part, scenario)
            rows.append(
                {
                    "venue": venue,
                    "scenario": scenario,
                    "trades": int(vals.size),
                    "net_pnl": float(vals.sum()) if vals.size else 0.0,
                    "profit_factor": _profit_factor(vals),
                    "worst_trade": float(vals.min()) if vals.size else None,
                    "es_95": float(vals[vals <= np.quantile(vals, 0.05)].mean()) if vals.size else None,
                    "note": "static trade-return shock; no margin/liquidation path",
                }
            )
    tail = pl.DataFrame(rows)
    _write_df(tail, tables / "worst_trade_dependency.csv")
    artifacts["worst_trade_dependency"] = str(tables / "worst_trade_dependency.csv")

    heat = trades.select(
        "venue",
        "component_id",
        "symbol",
        "entry_signal_ts_ms",
        "signal_id",
        (pl.col("notional_weight") * pl.col("component_weight")).alias("portfolio_notional"),
        (pl.col("notional_weight") * pl.col("component_weight") * 0.20).alias("loss_if_20pct_up"),
        (pl.col("notional_weight") * pl.col("component_weight") * 0.50).alias("loss_if_50pct_up"),
        (pl.col("notional_weight") * pl.col("component_weight") * 1.00).alias("loss_if_100pct_up"),
        (pl.col("notional_weight") * pl.col("component_weight") * 2.00).alias("loss_if_200pct_up"),
    )
    _write_df(heat, tables / "disaster_loss_by_trade.csv")
    artifacts["disaster_loss_by_trade"] = str(tables / "disaster_loss_by_trade.csv")
    cluster_heat = (
        heat.group_by(["venue", "entry_signal_ts_ms"])
        .agg(
            pl.len().alias("positions"),
            pl.col("portfolio_notional").sum().alias("portfolio_notional"),
            pl.col("loss_if_50pct_up").sum().alias("portfolio_heat_50pct"),
            pl.col("loss_if_100pct_up").sum().alias("portfolio_heat_100pct"),
            pl.col("loss_if_200pct_up").sum().alias("portfolio_heat_200pct"),
        )
        .sort(["venue", "portfolio_heat_100pct"], descending=[False, True])
    )
    _write_df(cluster_heat, tables / "portfolio_heat_by_entry_cluster.csv")
    artifacts["portfolio_heat_by_entry_cluster"] = str(tables / "portfolio_heat_by_entry_cluster.csv")
    return artifacts


DISASTER_SIZING_LOSS_BUDGETS = (0.0005, 0.0010, 0.0025)
DISASTER_SIZING_SCENARIOS: tuple[dict[str, Any], ...] = (
    {"scenario": "fixed_50pct", "fixed_floor_pct": 0.50, "empirical_move_source": "none"},
    {"scenario": "fixed_100pct", "fixed_floor_pct": 1.00, "empirical_move_source": "none"},
    {
        "scenario": "winner_mae_p95_floor_50pct",
        "fixed_floor_pct": 0.50,
        "empirical_move_source": "winner_mae_p95",
    },
    {
        "scenario": "all_mae_p99_floor_100pct",
        "fixed_floor_pct": 1.00,
        "empirical_move_source": "all_mae_p99",
    },
)


def _finite_quantile(series: pl.Series, q: float) -> float:
    vals = series.drop_nulls().to_numpy().astype(float)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 0.0
    return max(float(np.quantile(vals, q)), 0.0)


def _disaster_sizing_stats(part: pl.DataFrame) -> dict[str, float]:
    mae_col = "adverse_excursion_pct" if "adverse_excursion_pct" in part.columns else "mae"
    winners = part.filter(pl.col("portfolio_net_return") > 0.0) if "portfolio_net_return" in part.columns else pl.DataFrame()
    return {
        "winner_mae_p95": _finite_quantile(winners[mae_col], 0.95) if not winners.is_empty() else 0.0,
        "all_mae_p99": _finite_quantile(part[mae_col], 0.99),
    }


def _disaster_sizing_catastrophic_move(scenario: dict[str, Any], stats: dict[str, float]) -> tuple[float, float]:
    fixed = float(scenario["fixed_floor_pct"])
    source = str(scenario["empirical_move_source"])
    empirical = float(stats.get(source, 0.0)) if source != "none" else 0.0
    return max(fixed, empirical), empirical


def write_disaster_sizing_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty() or "notional_weight" not in trades.columns:
        return artifacts
    tables = output_root / "tables"
    frames: list[pl.DataFrame] = []
    summary_rows: list[dict[str, Any]] = []
    base_cols = [
        col
        for col in ("venue", "component_id", "symbol", "entry_signal_ts_ms", "signal_id", "portfolio_net_return", "adverse_excursion_pct")
        if col in trades.columns
    ]
    component_weight = pl.col("component_weight") if "component_weight" in trades.columns else pl.lit(1.0)
    prepared = trades.with_columns((pl.col("notional_weight") * component_weight).alias("current_notional_pct_equity"))
    for venue in sorted(prepared["venue"].unique().to_list()):
        part = prepared.filter(pl.col("venue") == venue)
        if part.is_empty():
            continue
        stats = _disaster_sizing_stats(part)
        base = part.select([*base_cols, "current_notional_pct_equity"])
        for scenario in DISASTER_SIZING_SCENARIOS:
            catastrophic_move, empirical_move = _disaster_sizing_catastrophic_move(scenario, stats)
            if catastrophic_move <= 0.0:
                continue
            for budget in DISASTER_SIZING_LOSS_BUDGETS:
                safe_notional = float(budget / catastrophic_move)
                frame = base.with_columns(
                    pl.lit(str(scenario["scenario"])).alias("scenario"),
                    pl.lit(str(scenario["empirical_move_source"])).alias("empirical_move_source"),
                    pl.lit(float(empirical_move)).alias("empirical_move_pct"),
                    pl.lit(float(scenario["fixed_floor_pct"])).alias("fixed_floor_pct"),
                    pl.lit(float(catastrophic_move)).alias("catastrophic_move_pct"),
                    pl.lit(float(budget)).alias("trade_loss_budget_pct_equity"),
                    pl.lit(float(safe_notional)).alias("safe_notional_pct_equity"),
                    (pl.col("current_notional_pct_equity") * catastrophic_move).alias(
                        "current_disaster_loss_pct_equity"
                    ),
                    (pl.col("current_notional_pct_equity") / safe_notional).alias("current_to_safe_notional"),
                    (pl.col("current_notional_pct_equity") > safe_notional).alias("over_budget"),
                )
                frames.append(frame)
                ratios = frame["current_to_safe_notional"].to_numpy().astype(float)
                losses = frame["current_disaster_loss_pct_equity"].to_numpy().astype(float)
                summary_rows.append(
                    {
                        "venue": str(venue),
                        "scenario": str(scenario["scenario"]),
                        "empirical_move_source": str(scenario["empirical_move_source"]),
                        "empirical_move_pct": empirical_move,
                        "fixed_floor_pct": float(scenario["fixed_floor_pct"]),
                        "catastrophic_move_pct": catastrophic_move,
                        "trade_loss_budget_pct_equity": float(budget),
                        "safe_notional_pct_equity": safe_notional,
                        "trades": int(frame.height),
                        "pct_trades_over_budget": float(np.mean(ratios > 1.0)) if ratios.size else None,
                        "median_current_to_safe_notional": float(np.median(ratios)) if ratios.size else None,
                        "p95_current_to_safe_notional": float(np.quantile(ratios, 0.95)) if ratios.size else None,
                        "max_current_to_safe_notional": float(np.max(ratios)) if ratios.size else None,
                        "median_current_disaster_loss_pct_equity": float(np.median(losses)) if losses.size else None,
                        "p95_current_disaster_loss_pct_equity": float(np.quantile(losses, 0.95)) if losses.size else None,
                        "max_current_disaster_loss_pct_equity": float(np.max(losses)) if losses.size else None,
                    }
                )
    if frames:
        by_trade = pl.concat(frames, how="diagonal_relaxed").sort(
            ["venue", "scenario", "trade_loss_budget_pct_equity", "current_to_safe_notional"],
            descending=[False, False, False, True],
        )
        _write_df(by_trade, tables / "disaster_sizing_by_trade.csv")
        artifacts["disaster_sizing_by_trade"] = str(tables / "disaster_sizing_by_trade.csv")
    if summary_rows:
        summary = pl.DataFrame(summary_rows).sort(["venue", "scenario", "trade_loss_budget_pct_equity"])
        _write_df(summary, tables / "disaster_sizing_summary.csv")
        artifacts["disaster_sizing_summary"] = str(tables / "disaster_sizing_summary.csv")
    return artifacts


def write_skip_logic_buckets(output_root: Path, candidates: pl.DataFrame, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if candidates.is_empty() or trades.is_empty():
        return artifacts
    tables = output_root / "tables"
    outcomes = trades.select("signal_id", "portfolio_net_return", "adverse_excursion_pct", "time_to_first_profit_hours")
    selected = candidates.filter(pl.col("selected")).join(outcomes, on="signal_id", how="inner")
    if selected.is_empty():
        return artifacts
    features = ["composite", "turnover_quote", "crowding_count", "active_count", "regime_size_mult", "notional_weight"]
    rows: list[dict[str, Any]] = []
    for venue in sorted(selected["venue"].unique().to_list()):
        v = selected.filter(pl.col("venue") == venue)
        bottom_cut = float(v["portfolio_net_return"].quantile(0.10))
        for feature in features:
            if feature not in v.columns:
                continue
            vals = v[feature].drop_nulls().to_numpy()
            if vals.size < 20:
                continue
            qs = np.unique(np.quantile(vals, [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]))
            if qs.size < 2:
                continue
            for idx in range(qs.size - 1):
                lo = float(qs[idx])
                hi = float(qs[idx + 1])
                if idx == qs.size - 2:
                    part = v.filter((pl.col(feature) >= lo) & (pl.col(feature) <= hi))
                else:
                    part = v.filter((pl.col(feature) >= lo) & (pl.col(feature) < hi))
                pnl = part["portfolio_net_return"].to_numpy() if not part.is_empty() else np.array([])
                rows.append(
                    {
                        "venue": venue,
                        "feature": feature,
                        "bucket": idx + 1,
                        "lo": lo,
                        "hi": hi,
                        "trades": int(part.height),
                        "net_pnl": float(pnl.sum()) if pnl.size else 0.0,
                        "avg_pnl": float(pnl.mean()) if pnl.size else None,
                        "win_rate": float((pnl > 0.0).mean()) if pnl.size else None,
                        "mae_ge20_rate": float((part["adverse_excursion_pct"] >= 0.20).mean()) if not part.is_empty() else None,
                        "bottom_decile_loss_rate": float((part["portfolio_net_return"] <= bottom_cut).mean()) if not part.is_empty() else None,
                    }
                )
    out = pl.DataFrame(rows)
    _write_df(out, tables / "skip_logic_feature_buckets.csv")
    artifacts["skip_logic_feature_buckets"] = str(tables / "skip_logic_feature_buckets.csv")
    return artifacts


def write_hedge_attribution(output_root: Path, venues: list[str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    for venue in venues:
        path = output_root / venue / "continuous_equity.csv"
        if not path.exists():
            continue
        equity = pl.read_csv(path)
        if equity.is_empty():
            continue
        hedge_total = float((equity["hedge_return"] + equity["hedge_funding_return"] + equity["hedge_cost_return"]).sum())
        rows.append(
            {
                "venue": venue,
                "periods": int(equity.height),
                "basket_return_sum": float(equity["basket_return"].sum()),
                "short_gross_return_sum": float(equity["gross_return"].sum()),
                "entry_cost_return_sum": float(equity["entry_cost_return"].sum()),
                "funding_return_sum": float(equity["funding_return"].sum()),
                "resize_cost_return_sum": float(equity["resize_cost_return"].sum()),
                "hedge_return_sum": float(equity["hedge_return"].sum()),
                "hedge_funding_return_sum": float(equity["hedge_funding_return"].sum()),
                "hedge_cost_return_sum": float(equity["hedge_cost_return"].sum()),
                "hedge_total_sum": hedge_total,
                "avg_abs_hedge_ratio": float(equity["hedge_ratio"].abs().mean()),
                "max_abs_hedge_ratio": float(equity["hedge_ratio"].abs().max()),
            }
        )
    if not rows:
        return artifacts
    out = pl.DataFrame(rows)
    _write_df(out, output_root / "tables" / "hedge_attribution.csv")
    artifacts["hedge_attribution"] = str(output_root / "tables" / "hedge_attribution.csv")
    return artifacts


def _simulate_trade_with_stop(row: dict[str, Any], bars_by_symbol: dict[str, pl.DataFrame], stop_pct: float | None) -> dict[str, Any]:
    entry_price = float(row["entry_price"])
    entry_ts = int(row["entry_ts_ms"])
    exit_ts = int(row["exit_ts_ms"])
    symbol = str(row["symbol"])
    bars = bars_by_symbol.get(symbol)
    base = {
        "venue": row["venue"],
        "component_id": row["component_id"],
        "symbol": symbol,
        "entry_signal_ts_ms": int(row["entry_signal_ts_ms"]),
        "stop_pct": stop_pct,
        "stop_hit": False,
        "post_stop_original_tp_hit": False,
        "gross_trade_return": float(row["gross_trade_return"]),
        "portfolio_net_return": float(row["portfolio_net_return"]),
    }
    if stop_pct is None or stop_pct <= 0.0 or bars is None or bars.is_empty() or entry_price <= 0:
        return base
    path = bars.filter((pl.col("bar_end_ts_ms") >= entry_ts) & (pl.col("bar_end_ts_ms") <= exit_ts))
    if path.is_empty():
        return base
    stop_price = entry_price * (1.0 + stop_pct)
    tp = float(row["take_profit_price"]) if row.get("take_profit_price") is not None else entry_price * (1.0 - COMPONENT_TP)
    stop_rows = path.filter(pl.col("high") >= stop_price)
    if stop_rows.is_empty():
        return base
    stop_ts = int(stop_rows["bar_end_ts_ms"][0])
    gross = 1.0 - stop_price / entry_price
    notional = float(row["notional_weight"]) * float(row["component_weight"])
    cost_funding = float(row["portfolio_cost_return"]) + float(row["portfolio_funding_return"])
    after = path.filter(pl.col("bar_end_ts_ms") >= stop_ts)
    return base | {
        "stop_hit": True,
        "post_stop_original_tp_hit": bool((after["low"] <= tp).any()) if not after.is_empty() else False,
        "gross_trade_return": gross,
        "portfolio_net_return": notional * gross + cost_funding,
    }


def write_stop_frontier(output_root: Path, trades: pl.DataFrame, venues: list[str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    tables = output_root / "tables"
    stops = [0.05, 0.075, 0.10, 0.125, 0.15, 0.20, 0.30, 0.40, 0.60, 0.80, None]
    rows: list[dict[str, Any]] = []
    for venue in venues:
        part = trades.filter(pl.col("venue") == venue)
        if part.is_empty():
            continue
        bars = _load_klines_for_rows(venue_root(venue), part)
        for stop in stops:
            sim_rows = [_simulate_trade_with_stop(row, bars, stop) for row in part.to_dicts()]
            df = pl.DataFrame(sim_rows)
            vals = df["portfolio_net_return"].to_numpy() if not df.is_empty() else np.array([])
            rows.append(
                {
                    "venue": venue,
                    "stop": "none" if stop is None else stop,
                    "stop_hits": int(df.filter(pl.col("stop_hit")).height) if "stop_hit" in df.columns else 0,
                    "post_stop_tp_hit_rate": (
                        float(df.filter(pl.col("stop_hit"))["post_stop_original_tp_hit"].mean())
                        if "stop_hit" in df.columns and df.filter(pl.col("stop_hit")).height
                        else None
                    ),
                    "net_pnl": float(vals.sum()) if vals.size else 0.0,
                    "profit_factor": _profit_factor(vals),
                    "win_rate": float((vals > 0).mean()) if vals.size else None,
                    "worst_trade": float(vals.min()) if vals.size else None,
                    "es_95": float(vals[vals <= np.quantile(vals, 0.05)].mean()) if vals.size else None,
                }
            )
    out = pl.DataFrame(rows)
    _write_df(out, tables / "stop_frontier.csv")
    artifacts["stop_frontier"] = str(tables / "stop_frontier.csv")
    return artifacts


SCALE_IN_TRIGGERS: tuple[float, ...] = (0.05, 0.10, 0.20, 0.40)
SCALE_IN_FRACTIONS: tuple[float, ...] = (0.25, 0.50)
SCALE_IN_ROUND_TRIP_COST_BPS = 15.0
SCALE_IN_PORTFOLIO_REPLAY_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "variant": "mae05_add25",
        "kind": "scale_in_overlay",
        "trigger_mae_pct": 0.05,
        "addon_fraction": 0.25,
        "note": "Add 25% child short after a 5% adverse move; child has separate TP12 and parent-exit clamp.",
    },
    {
        "variant": "mae05_add50",
        "kind": "scale_in_overlay",
        "trigger_mae_pct": 0.05,
        "addon_fraction": 0.50,
        "note": "Prior best diagnostic arm replayed through component MTM and the BTC/ETH hedge.",
    },
    {
        "variant": "mae10_add50",
        "kind": "scale_in_overlay",
        "trigger_mae_pct": 0.10,
        "addon_fraction": 0.50,
        "note": "More selective 10% adverse trigger with 50% child notional.",
    },
)
SCALE_IN_PORTFOLIO_REPLAY_VARIANT_NAMES = tuple(
    str(row["variant"]) for row in SCALE_IN_PORTFOLIO_REPLAY_VARIANTS
)
SIGNAL_INVALIDATION_RULES: tuple[dict[str, Any], ...] = (
    {
        "rule": "candidate_pressure_3h_score95",
        "description": "same-symbol candidate pressure >=95 score after 3h while the open short is losing",
        "candidate_reasons": ("selected", "cooldown", "crowding"),
        "min_trade_age_hours": 3.0,
        "min_component_score": 0.95,
        "min_volume_zscore": None,
    },
    {
        "rule": "candidate_pressure_3h_score99",
        "description": "same-symbol candidate pressure >=99 score after 3h while the open short is losing",
        "candidate_reasons": ("selected", "cooldown", "crowding"),
        "min_trade_age_hours": 3.0,
        "min_component_score": 0.99,
        "min_volume_zscore": None,
    },
    {
        "rule": "candidate_pressure_3h_score95_volume1",
        "description": "same-symbol candidate pressure >=95 score and volume z >=1 after 3h while losing",
        "candidate_reasons": ("selected", "cooldown", "crowding"),
        "min_trade_age_hours": 3.0,
        "min_component_score": 0.95,
        "min_volume_zscore": 1.0,
    },
    {
        "rule": "candidate_pressure_6h_score95",
        "description": "same-symbol candidate pressure >=95 score after 6h while the open short is losing",
        "candidate_reasons": ("selected", "cooldown", "crowding"),
        "min_trade_age_hours": 6.0,
        "min_component_score": 0.95,
        "min_volume_zscore": None,
    },
    {
        "rule": "btc_trend_reject_3h",
        "description": "future same-symbol candidate rejected by the BTC-trend gate after 3h while losing",
        "candidate_reasons": ("btc_trend",),
        "min_trade_age_hours": 3.0,
        "min_component_score": None,
        "min_volume_zscore": None,
    },
)


def _conditional_scale_in_trade_row(
    row: dict[str, Any],
    *,
    trigger_mae_pct: float,
    addon_fraction: float,
    round_trip_cost_bps: float = SCALE_IN_ROUND_TRIP_COST_BPS,
) -> dict[str, Any]:
    entry_price = float(row.get("entry_price") or 0.0)
    exit_price = float(row.get("exit_price") or 0.0)
    adverse = float(row.get("adverse_excursion_pct") or row.get("mae") or 0.0)
    component_weight = float(row.get("component_weight") or 1.0)
    primary_notional = float(row.get("notional_weight") or 0.0) * component_weight
    primary_return = float(row.get("portfolio_net_return") or 0.0)
    filled = (
        adverse >= trigger_mae_pct
        and entry_price > 0.0
        and exit_price > 0.0
        and primary_notional > 0.0
        and addon_fraction > 0.0
    )
    addon_notional = primary_notional * addon_fraction if filled else 0.0
    addon_entry_price = entry_price * (1.0 + trigger_mae_pct) if filled else None
    addon_gross = (1.0 - exit_price / addon_entry_price) if filled and addon_entry_price else 0.0
    addon_cost = -addon_notional * round_trip_cost_bps / 10_000.0 if filled else 0.0
    addon_net = addon_notional * addon_gross + addon_cost if filled else 0.0
    return {
        "venue": str(row.get("venue") or ""),
        "component_id": str(row.get("component_id") or ""),
        "signal_id": str(row.get("signal_id") or ""),
        "trade_id": str(row.get("trade_id") or ""),
        "symbol": str(row.get("symbol") or ""),
        "entry_signal_ts_ms": int(float(row.get("entry_signal_ts_ms") or 0.0)),
        "trigger_mae_pct": trigger_mae_pct,
        "addon_fraction_of_primary": addon_fraction,
        "round_trip_cost_bps": round_trip_cost_bps,
        "filled": bool(filled),
        "entry_price": entry_price,
        "addon_entry_price": addon_entry_price,
        "exit_price": exit_price,
        "primary_notional_pct_equity": primary_notional,
        "addon_notional_pct_equity": addon_notional,
        "primary_portfolio_net_return": primary_return,
        "addon_gross_return": addon_gross if filled else None,
        "addon_cost_return": addon_cost,
        "addon_net_return": addon_net,
        "combined_portfolio_net_return": primary_return + addon_net,
        "adverse_excursion_pct": adverse,
        "time_to_mae_hours": row.get("time_to_mae_hours"),
        "time_to_recovery_hours": row.get("time_to_recovery_hours"),
        "diagnostic_note": "Assumes add-on short fills exactly at the adverse threshold and exits with the original trade.",
    }


def write_conditional_scale_in_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty() or "portfolio_net_return" not in trades.columns:
        return artifacts
    tables = output_root / "tables"
    rows: list[dict[str, Any]] = []
    selected = trades.filter(pl.col("portfolio_net_return").is_not_null())
    for trade in selected.to_dicts():
        for trigger in SCALE_IN_TRIGGERS:
            for fraction in SCALE_IN_FRACTIONS:
                rows.append(
                    _conditional_scale_in_trade_row(
                        trade,
                        trigger_mae_pct=trigger,
                        addon_fraction=fraction,
                    )
                )
    if not rows:
        return artifacts
    by_trade = pl.DataFrame(rows, infer_schema_length=None).sort(
        ["venue", "trigger_mae_pct", "addon_fraction_of_primary", "entry_signal_ts_ms", "symbol"]
    )
    by_trade_path = tables / "conditional_scale_in_by_trade.csv"
    _write_df(by_trade, by_trade_path)
    artifacts["conditional_scale_in_by_trade"] = str(by_trade_path)

    summary_rows: list[dict[str, Any]] = []
    for keys, part in by_trade.group_by(["venue", "trigger_mae_pct", "addon_fraction_of_primary"], maintain_order=True):
        venue, trigger, fraction = keys
        primary = part["primary_portfolio_net_return"].to_numpy()
        combined = part["combined_portfolio_net_return"].to_numpy()
        addon = part["addon_net_return"].to_numpy()
        filled = part.filter(pl.col("filled"))
        summary_rows.append(
            {
                "venue": venue,
                "trigger_mae_pct": trigger,
                "addon_fraction_of_primary": fraction,
                "round_trip_cost_bps": SCALE_IN_ROUND_TRIP_COST_BPS,
                "trades": int(part.height),
                "fills": int(filled.height),
                "fill_rate": float(filled.height / part.height) if part.height else None,
                "primary_net_return": float(primary.sum()),
                "addon_net_return": float(addon.sum()),
                "combined_net_return": float(combined.sum()),
                "delta_net_return": float(addon.sum()),
                "primary_worst_trade": float(primary.min()) if primary.size else None,
                "combined_worst_trade": float(combined.min()) if combined.size else None,
                "primary_profit_factor": _profit_factor(primary),
                "combined_profit_factor": _profit_factor(combined),
                "avg_addon_net_when_filled": _safe_mean(filled["addon_net_return"]) if not filled.is_empty() else None,
                "note": "Diagnostic only; not a full component+hedge portfolio replay.",
            }
        )
    summary = pl.DataFrame(summary_rows, infer_schema_length=None).sort(
        ["venue", "trigger_mae_pct", "addon_fraction_of_primary"]
    )
    summary_path = tables / "conditional_scale_in_summary.csv"
    _write_df(summary, summary_path)
    artifacts["conditional_scale_in_summary"] = str(summary_path)
    return artifacts


def _bar_at_or_after(bars: pl.DataFrame, ts_ms: int) -> dict[str, Any] | None:
    if bars.is_empty():
        return None
    got = bars.filter(pl.col("bar_end_ts_ms") >= ts_ms).head(1)
    return got.to_dicts()[0] if not got.is_empty() else None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _position_gross_return(side: str, entry_price: float, exit_price: float) -> float:
    if side == "long":
        return exit_price / entry_price - 1.0
    return 1.0 - exit_price / entry_price


def _iso_date_ms(ts_ms: int) -> str:
    return _date_from_ms(int(ts_ms)).isoformat()


def _iso_month_ms(ts_ms: int) -> str:
    return _iso_date_ms(int(ts_ms))[:7]


def _parent_round_trip_bps(row: dict[str, Any]) -> float | None:
    weight = _float_or_none(row.get("notional_weight"))
    cost = _float_or_none(row.get("cost_return"))
    if weight is None or weight <= 0.0 or cost is None or cost >= 0.0:
        return None
    return max(-cost / weight * 10_000.0, 0.0)


def _scale_in_child_trade_row(
    parent: dict[str, Any],
    bars: pl.DataFrame,
    config: ContinuousEventConfig,
    *,
    variant: str,
    trigger_mae_pct: float,
    addon_fraction: float,
    funding_lookup: dict[str, dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Build one explicit child short for the portfolio-level scale-in overlay.

    The child fills when post-entry hourly high first touches the trigger. It
    cannot take profit on that same bar; exit checks start on the next bar and
    are clamped to the parent exit timestamp.
    """
    side = str(parent.get("side") or "short")
    if side != "short" or bars.is_empty():
        return None
    symbol = str(parent.get("symbol") or "")
    entry_ts = int(float(parent.get("entry_ts_ms") or 0.0))
    parent_exit_ts = int(float(parent.get("exit_ts_ms") or 0.0))
    parent_entry_price = _float_or_none(parent.get("entry_price"))
    parent_exit_price = _float_or_none(parent.get("exit_price"))
    parent_weight = _float_or_none(parent.get("notional_weight")) or 0.0
    child_weight = abs(parent_weight) * float(addon_fraction)
    if (
        not symbol
        or entry_ts <= 0
        or parent_exit_ts <= entry_ts
        or parent_entry_price is None
        or parent_entry_price <= 0.0
        or child_weight <= 0.0
    ):
        return None

    trigger_price = parent_entry_price * (1.0 + float(trigger_mae_pct))
    parent_window = bars.filter(
        (pl.col("bar_end_ts_ms") > entry_ts)
        & (pl.col("bar_end_ts_ms") <= parent_exit_ts)
    )
    if parent_window.is_empty():
        return None
    fills = parent_window.filter(pl.col("high") >= trigger_price).head(1)
    if fills.is_empty():
        return None
    fill = fills.row(0, named=True)
    fill_ts = int(fill["bar_end_ts_ms"])
    post_fill = parent_window.filter(pl.col("bar_end_ts_ms") > fill_ts)
    if post_fill.is_empty():
        return None

    take_profit_price = trigger_price * (1.0 - COMPONENT_TP)
    take_profit_rows = post_fill.filter(pl.col("low") <= take_profit_price).head(1)
    if not take_profit_rows.is_empty():
        exit_row = take_profit_rows.row(0, named=True)
        child_exit_ts = int(exit_row["bar_end_ts_ms"])
        child_exit_price = take_profit_price
        exit_reason = "scale_in_take_profit"
    else:
        child_exit_ts = parent_exit_ts
        if parent_exit_price is not None and parent_exit_price > 0.0:
            child_exit_price = parent_exit_price
        else:
            child_exit_price = float(post_fill.tail(1).row(0, named=True)["close"])
        exit_reason = "scale_in_parent_exit"

    path_to_exit = bars.filter(
        (pl.col("bar_end_ts_ms") >= fill_ts)
        & (pl.col("bar_end_ts_ms") <= child_exit_ts)
    )
    post_path_to_exit = path_to_exit.filter(pl.col("bar_end_ts_ms") > fill_ts)
    high = path_to_exit["high"].to_numpy() if "high" in path_to_exit.columns else np.array([], dtype=float)
    low = path_to_exit["low"].to_numpy() if "low" in path_to_exit.columns else np.array([], dtype=float)
    mae = float(np.min(1.0 - high / trigger_price)) if high.size else 0.0
    mfe = float(np.max(1.0 - low / trigger_price)) if low.size else 0.0
    turnover_quote = _float_or_none(fill.get("turnover_quote"))
    if turnover_quote is not None and turnover_quote > 0.0:
        round_trip_bps = _round_trip_bps(config, turnover_quote, notional_weight=child_weight)
        cost_source = "fill_bar_turnover"
    else:
        parent_bps = _parent_round_trip_bps(parent)
        if parent_bps is not None:
            round_trip_bps = parent_bps
            cost_source = "parent_cost_bps"
        else:
            round_trip_bps = _round_trip_bps(
                config,
                max(float(config.deploy_capital_usd), 1.0),
                notional_weight=child_weight,
            )
            cost_source = "config_turnover_fallback"

    gross_trade_return = _position_gross_return("short", trigger_price, child_exit_price)
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup,
        symbol=symbol,
        side="short",
        entry_ts_ms=fill_ts,
        exit_ts_ms=child_exit_ts,
    )
    gross_return = child_weight * gross_trade_return
    cost_return = -child_weight * float(round_trip_bps) / 10_000.0
    funding_return = child_weight * raw_funding_return
    net_return = gross_return + cost_return + funding_return
    parent_trade_id = str(parent.get("trade_id") or f"{_iso_date_ms(entry_ts)}-s-{symbol}")
    basket_id = str(parent.get("basket_id") or _iso_date_ms(int(parent.get("entry_signal_ts_ms") or entry_ts)))
    return {
        "trade_id": f"{parent_trade_id}-scalein-{variant}",
        "parent_trade_id": parent_trade_id,
        "basket_id": basket_id,
        "entry_signal_ts_ms": int(float(parent.get("entry_signal_ts_ms") or entry_ts)),
        "entry_ts_ms": fill_ts,
        "exit_ts_ms": child_exit_ts,
        "entry_date": _iso_date_ms(fill_ts),
        "exit_date": _iso_date_ms(child_exit_ts),
        "exit_month": _iso_month_ms(child_exit_ts),
        "symbol": symbol,
        "side": "short",
        "score": _float_or_none(parent.get("score")),
        "rank": parent.get("rank"),
        "entry_price": trigger_price,
        "exit_price": child_exit_price,
        "exit_reason": exit_reason,
        "planned_exit_ts_ms": parent_exit_ts,
        "stop_price": None,
        "take_profit_price": take_profit_price,
        "notional_weight": child_weight,
        "position_weight": 1.0,
        "gross_trade_return": gross_trade_return,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "funding_return": funding_return,
        "funding_mode": funding_mode,
        "funding_event_count": funding_event_count,
        "net_return": net_return,
        "mae": mae,
        "mfe": mfe,
        "bars_held": int(post_path_to_exit.height),
        "hold_hours": (child_exit_ts - fill_ts) / MS_PER_HOUR,
        "scale_in_variant": variant,
        "scale_in_trigger_mae_pct": float(trigger_mae_pct),
        "scale_in_addon_fraction": float(addon_fraction),
        "scale_in_trigger_price": trigger_price,
        "scale_in_round_trip_bps": float(round_trip_bps),
        "scale_in_cost_source": cost_source,
        "scale_in_note": "Research-only child short; no same-bar TP after trigger and no queue/liquidation model.",
    }


def _signal_invalidation_trade_row(
    row: dict[str, Any],
    future_signals: list[dict[str, Any]],
    bars_by_symbol: dict[str, pl.DataFrame],
    rule: dict[str, Any],
) -> dict[str, Any]:
    entry_price = float(row.get("entry_price") or 0.0)
    entry_ts = int(float(row.get("entry_ts_ms") or 0.0))
    exit_ts = int(float(row.get("exit_ts_ms") or 0.0))
    entry_signal_ts = int(float(row.get("entry_signal_ts_ms") or 0.0))
    side = str(row.get("side") or "short")
    symbol = str(row.get("symbol") or "")
    component_weight = float(row.get("component_weight") or 1.0)
    primary_notional = float(row.get("notional_weight") or 0.0) * component_weight
    original_net = float(row.get("portfolio_net_return") or 0.0)
    original_gross = float(row.get("portfolio_gross_return") or 0.0)
    original_cost = float(row.get("portfolio_cost_return") or 0.0)
    original_funding = float(row.get("portfolio_funding_return") or 0.0)
    original_hold_hours = float(row.get("hold_hours") or 0.0)
    if original_hold_hours <= 0.0 and exit_ts > entry_ts:
        original_hold_hours = (exit_ts - entry_ts) / MS_PER_HOUR
    base = {
        "venue": str(row.get("venue") or ""),
        "component_id": str(row.get("component_id") or ""),
        "signal_id": str(row.get("signal_id") or ""),
        "trade_id": str(row.get("trade_id") or ""),
        "symbol": symbol,
        "rule": str(rule["rule"]),
        "rule_description": str(rule["description"]),
        "entry_signal_ts_ms": entry_signal_ts,
        "entry_ts_ms": entry_ts,
        "original_exit_ts_ms": exit_ts,
        "entry_price": entry_price,
        "original_exit_price": _float_or_none(row.get("exit_price")),
        "primary_notional_pct_equity": primary_notional,
        "original_portfolio_gross_return": original_gross,
        "original_cost_return": original_cost,
        "original_funding_return": original_funding,
        "original_portfolio_net_return": original_net,
        "invalidated": False,
        "invalidation_reason": None,
        "invalidation_signal_ts_ms": None,
        "invalidation_action_ts_ms": None,
        "invalidation_fill_ts_ms": None,
        "invalidation_price": None,
        "invalidation_age_hours": None,
        "invalidation_unrealized_return": None,
        "invalidation_component_score": None,
        "invalidation_volume_zscore": None,
        "future_candidate_rows_in_window": 0,
        "rule_candidate_rows_considered": 0,
        "coverage_miss_count": 0,
        "scenario_hold_hours": original_hold_hours,
        "scenario_portfolio_gross_return": original_gross,
        "scenario_cost_return": original_cost,
        "scenario_funding_return": original_funding,
        "scenario_portfolio_net_return": original_net,
        "delta_net_return": 0.0,
        "diagnostic_note": (
            "Candidate-tape diagnostic only: exits use explicit future same-symbol candidate rows, "
            "original round-trip cost, and prorated original funding."
        ),
    }
    if entry_price <= 0.0 or primary_notional <= 0.0 or exit_ts <= entry_ts:
        return base | {"coverage_miss_count": len(future_signals)}
    bars = bars_by_symbol.get(symbol, pl.DataFrame())
    reasons = set(rule["candidate_reasons"])
    min_age_hours = float(rule["min_trade_age_hours"])
    min_score = _float_or_none(rule.get("min_component_score"))
    min_volume_zscore = _float_or_none(rule.get("min_volume_zscore"))
    future_rows_in_window = 0
    considered = 0
    coverage_misses = 0
    for signal in future_signals:
        signal_ts = int(float(signal.get("signal_ts_ms") or 0.0))
        action_ts = int(float(signal.get("order_submit_ts_ms") or signal_ts))
        if signal_ts <= entry_signal_ts or action_ts <= entry_ts or action_ts > exit_ts:
            continue
        future_rows_in_window += 1
        age_hours = (action_ts - entry_ts) / MS_PER_HOUR
        if age_hours < min_age_hours:
            continue
        reason = str(signal.get("reason") or "")
        if reason not in reasons:
            continue
        score = _float_or_none(signal.get("component_score"))
        if min_score is not None and (score is None or score < min_score):
            continue
        volume_zscore = _float_or_none(signal.get("volume_zscore"))
        if min_volume_zscore is not None and (volume_zscore is None or volume_zscore < min_volume_zscore):
            continue
        considered += 1
        bar = _bar_at_or_after(bars, action_ts)
        if bar is None or int(bar.get("bar_end_ts_ms") or 0) > exit_ts:
            coverage_misses += 1
            continue
        invalidation_ts = int(bar["bar_end_ts_ms"])
        invalidation_price = float(bar["close"])
        unrealized = _position_gross_return(side, entry_price, invalidation_price)
        if unrealized >= 0.0:
            continue
        scenario_hold_hours = max((invalidation_ts - entry_ts) / MS_PER_HOUR, 0.0)
        funding_ratio = min(max(scenario_hold_hours / original_hold_hours, 0.0), 1.0) if original_hold_hours > 0.0 else 1.0
        scenario_gross = primary_notional * unrealized
        scenario_funding = original_funding * funding_ratio
        scenario_net = scenario_gross + original_cost + scenario_funding
        return base | {
            "invalidated": True,
            "invalidation_reason": reason,
            "invalidation_signal_ts_ms": signal_ts,
            "invalidation_action_ts_ms": action_ts,
            "invalidation_fill_ts_ms": invalidation_ts,
            "invalidation_price": invalidation_price,
            "invalidation_age_hours": age_hours,
            "invalidation_unrealized_return": unrealized,
            "invalidation_component_score": score,
            "invalidation_volume_zscore": volume_zscore,
            "future_candidate_rows_in_window": future_rows_in_window,
            "rule_candidate_rows_considered": considered,
            "coverage_miss_count": coverage_misses,
            "scenario_hold_hours": scenario_hold_hours,
            "scenario_portfolio_gross_return": scenario_gross,
            "scenario_funding_return": scenario_funding,
            "scenario_portfolio_net_return": scenario_net,
            "delta_net_return": scenario_net - original_net,
        }
    return base | {
        "future_candidate_rows_in_window": future_rows_in_window,
        "rule_candidate_rows_considered": considered,
        "coverage_miss_count": coverage_misses,
    }


def write_signal_invalidation_tables(output_root: Path, trades: pl.DataFrame, candidates: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    required_trade_cols = {"venue", "component_id", "symbol", "entry_ts_ms", "exit_ts_ms", "entry_price", "portfolio_net_return"}
    required_signal_cols = {"venue", "component_id", "symbol", "signal_ts_ms", "order_submit_ts_ms", "reason"}
    if trades.is_empty() or candidates.is_empty() or not required_trade_cols <= set(trades.columns):
        return artifacts
    if not required_signal_cols <= set(candidates.columns):
        return artifacts
    tables = output_root / "tables"
    signal_cols = [
        "venue",
        "component_id",
        "symbol",
        "signal_ts_ms",
        "order_submit_ts_ms",
        "reason",
        "selected",
        "component_score",
        "residual_momentum_rank",
        "volume_zscore",
        "btc_trend_gate",
        "regime_trend",
        "regime_size_mult",
    ]
    present_signal_cols = [col for col in signal_cols if col in candidates.columns]
    lookup: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    signals = candidates.select(present_signal_cols).sort(["venue", "component_id", "symbol", "signal_ts_ms"])
    for signal in signals.to_dicts():
        key = (str(signal["venue"]), str(signal["component_id"]), str(signal["symbol"]))
        lookup.setdefault(key, []).append(signal)

    selected = trades.filter(pl.col("portfolio_net_return").is_not_null())
    rows: list[dict[str, Any]] = []
    for venue in sorted(str(v) for v in selected["venue"].unique().to_list()):
        venue_trades = selected.filter(pl.col("venue") == venue)
        bars_by_symbol = _load_klines_for_rows(venue_root(venue), venue_trades, sparse_windows=True)
        for trade in venue_trades.to_dicts():
            key = (str(trade.get("venue") or ""), str(trade.get("component_id") or ""), str(trade.get("symbol") or ""))
            future_signals = lookup.get(key, [])
            for rule in SIGNAL_INVALIDATION_RULES:
                rows.append(_signal_invalidation_trade_row(trade, future_signals, bars_by_symbol, rule))
    if not rows:
        return artifacts
    by_trade = pl.DataFrame(rows, infer_schema_length=None).sort(["venue", "rule", "entry_signal_ts_ms", "symbol"])
    by_trade_path = tables / "signal_invalidation_by_trade.csv"
    _write_df(by_trade, by_trade_path)
    artifacts["signal_invalidation_by_trade"] = str(by_trade_path)

    summary_rows: list[dict[str, Any]] = []
    for keys, part in by_trade.group_by(["venue", "rule"], maintain_order=True):
        venue, rule_name = keys
        original = part["original_portfolio_net_return"].to_numpy()
        scenario = part["scenario_portfolio_net_return"].to_numpy()
        invalidated = part.filter(pl.col("invalidated"))
        first = part.row(0, named=True)
        summary_rows.append(
            {
                "venue": venue,
                "rule": rule_name,
                "rule_description": first["rule_description"],
                "trades": int(part.height),
                "invalidations": int(invalidated.height),
                "invalidation_rate": float(invalidated.height / part.height) if part.height else None,
                "original_net_return": float(original.sum()) if original.size else 0.0,
                "scenario_net_return": float(scenario.sum()) if scenario.size else 0.0,
                "delta_net_return": float(scenario.sum() - original.sum()) if original.size else 0.0,
                "original_worst_trade": float(original.min()) if original.size else None,
                "scenario_worst_trade": float(scenario.min()) if scenario.size else None,
                "original_profit_factor": _profit_factor(original),
                "scenario_profit_factor": _profit_factor(scenario),
                "avg_invalidation_age_hours": (
                    _safe_mean(invalidated["invalidation_age_hours"]) if not invalidated.is_empty() else None
                ),
                "coverage_miss_count": int(part["coverage_miss_count"].sum()),
                "note": "Candidate-tape diagnostic only; not a full component+hedge portfolio replay.",
            }
        )
    summary = pl.DataFrame(summary_rows, infer_schema_length=None).sort(["venue", "rule"])
    summary_path = tables / "signal_invalidation_summary.csv"
    _write_df(summary, summary_path)
    artifacts["signal_invalidation_summary"] = str(summary_path)
    return artifacts


def _continuous_state_dataset_name(root: Path, canonical: str) -> str:
    if canonical == "open_interest":
        for name in ("open_interest", "binance_usdm_open_interest"):
            if (root / name).exists():
                return name
    if canonical == "funding":
        for name in ("funding", "binance_usdm_funding"):
            if (root / name).exists():
                return name
    return canonical


def _hourly_trade_state_grid(trades: pl.DataFrame) -> pl.DataFrame:
    required = {"venue", "component_id", "symbol", "entry_ts_ms", "exit_ts_ms", "entry_price"}
    if trades.is_empty() or not required <= set(trades.columns):
        return pl.DataFrame()
    rows: list[dict[str, Any]] = []
    optional_cols = [
        "signal_id",
        "trade_id",
        "side",
        "entry_signal_ts_ms",
        "portfolio_net_return",
        "component_weight",
        "notional_weight",
    ]
    for trade in trades.to_dicts():
        entry_ts = int(float(trade.get("entry_ts_ms") or 0.0))
        exit_ts = int(float(trade.get("exit_ts_ms") or 0.0))
        if entry_ts <= 0 or exit_ts <= entry_ts:
            continue
        first_state = entry_ts + MS_PER_HOUR
        for state_ts in range(first_state, exit_ts + 1, MS_PER_HOUR):
            row = {
                "venue": str(trade.get("venue") or ""),
                "component_id": str(trade.get("component_id") or ""),
                "symbol": str(trade.get("symbol") or ""),
                "entry_ts_ms": entry_ts,
                "exit_ts_ms": exit_ts,
                "state_ts_ms": state_ts,
                "state_age_hours": (state_ts - entry_ts) / MS_PER_HOUR,
                "entry_price": _float_or_none(trade.get("entry_price")),
            }
            for col in optional_cols:
                if col in trade:
                    row[col] = trade.get(col)
            rows.append(row)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None).sort(
        ["venue", "component_id", "symbol", "entry_ts_ms", "state_ts_ms"]
    )


def _bars_dict_to_state_frame(bars_by_symbol: dict[str, pl.DataFrame]) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for symbol, bars in bars_by_symbol.items():
        if bars.is_empty() or "bar_end_ts_ms" not in bars.columns:
            continue
        cols = [col for col in ("bar_end_ts_ms", "close", "high", "low", "turnover_quote") if col in bars.columns]
        frames.append(
            bars.select(cols)
            .rename({"bar_end_ts_ms": "state_ts_ms"})
            .with_columns(pl.lit(str(symbol)).alias("symbol"))
        )
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed").sort(["symbol", "state_ts_ms"])


def _load_state_dataset_for_rows(
    root: Path,
    dataset: str,
    rows: pl.DataFrame,
    *,
    value_cols: list[str],
    pad_start_ms: int = 0,
    pad_end_ms: int = 0,
) -> pl.DataFrame:
    dataset = _continuous_state_dataset_name(root, dataset)
    if rows.is_empty() or not (root / dataset).exists():
        return pl.DataFrame()
    paths = _date_symbol_paths_for_rows(
        root,
        dataset,
        rows,
        pad_start_ms=pad_start_ms,
        pad_end_ms=pad_end_ms,
    )
    if not paths:
        return pl.DataFrame()
    schema_names = set(pl.scan_parquet(paths).collect_schema().names())
    cols = [col for col in ["symbol", "ts_ms", *value_cols] if col in schema_names]
    if "symbol" not in cols or "ts_ms" not in cols:
        return pl.DataFrame()
    symbols = rows["symbol"].unique().to_list()
    start_ms = int(rows["entry_ts_ms"].min()) - pad_start_ms
    end_ms = int(rows["exit_ts_ms"].max()) + pad_end_ms
    return (
        pl.scan_parquet(paths)
        .select(cols)
        .filter(pl.col("symbol").is_in(symbols))
        .filter((pl.col("ts_ms") >= start_ms) & (pl.col("ts_ms") <= end_ms))
        .collect()
        .sort(["symbol", "ts_ms"])
    )


def _load_btc_state_for_rows(root: Path, rows: pl.DataFrame) -> pl.DataFrame:
    if rows.is_empty():
        return pl.DataFrame()
    start_ms = int(rows["entry_ts_ms"].min()) - MS_PER_DAY
    end_ms = int(rows["exit_ts_ms"].max()) + MS_PER_DAY
    paths = _date_part_paths(root, "klines_1h", start_ms, end_ms, symbols=["BTCUSDT"])
    if not paths:
        return pl.DataFrame()
    schema_names = set(pl.scan_parquet(paths).collect_schema().names())
    cols = [col for col in ("symbol", "ts_ms", "close") if col in schema_names]
    if set(cols) != {"symbol", "ts_ms", "close"}:
        return pl.DataFrame()
    return (
        pl.scan_parquet(paths)
        .select(cols)
        .filter(pl.col("symbol") == "BTCUSDT")
        .with_columns((pl.col("ts_ms") + MS_PER_HOUR).alias("state_ts_ms"))
        .select(["state_ts_ms", pl.col("close").alias("btc_close")])
        .collect()
        .sort("state_ts_ms")
    )


def _join_asof_state(
    panel: pl.DataFrame,
    state: pl.DataFrame,
    *,
    source_ts_col: str,
    prefix: str,
    tolerance_ms: int,
    by_symbol: bool = True,
) -> pl.DataFrame:
    if panel.is_empty() or state.is_empty() or source_ts_col not in state.columns:
        return panel
    rename = {source_ts_col: f"{prefix}_source_ts_ms"}
    for col in state.columns:
        if col not in {"symbol", source_ts_col}:
            rename[col] = f"{prefix}_{col}"
    state = state.rename(rename)
    left_sort = ["symbol", "state_ts_ms"] if by_symbol else ["state_ts_ms"]
    right_sort = ["symbol", f"{prefix}_source_ts_ms"] if by_symbol else [f"{prefix}_source_ts_ms"]
    kwargs = {"by": "symbol"} if by_symbol else {}
    return panel.sort(left_sort).join_asof(
        state.sort(right_sort),
        left_on="state_ts_ms",
        right_on=f"{prefix}_source_ts_ms",
        strategy="backward",
        tolerance=tolerance_ms,
        check_sortedness=False,
        **kwargs,
    )


def _build_signal_invalidation_state_panel_for_venue(
    trades: pl.DataFrame,
    candidates: pl.DataFrame,
    *,
    price_state: pl.DataFrame,
    open_interest_state: pl.DataFrame | None = None,
    funding_state: pl.DataFrame | None = None,
    btc_state: pl.DataFrame | None = None,
) -> pl.DataFrame:
    panel = _hourly_trade_state_grid(trades)
    if panel.is_empty():
        return panel
    if not price_state.is_empty():
        price_cols = [
            col
            for col in ["symbol", "state_ts_ms", "close", "high", "low", "turnover_quote"]
            if col in price_state.columns
        ]
        panel = panel.join(
            price_state.select(price_cols).rename(
                {col: f"price_{col}" for col in price_cols if col not in {"symbol", "state_ts_ms"}}
            ),
            on=["symbol", "state_ts_ms"],
            how="left",
        )
    if not candidates.is_empty():
        cand_cols = [
            col
            for col in [
                "venue",
                "component_id",
                "symbol",
                "signal_ts_ms",
                "selected",
                "reason",
                "component_score",
                "residual_momentum_rank",
                "volume_zscore",
                "btc_trend_gate",
                "regime_trend",
                "regime_size_mult",
            ]
            if col in candidates.columns
        ]
        cand = (
            candidates.select(cand_cols)
            .rename({"signal_ts_ms": "state_ts_ms"})
            .unique(subset=["venue", "component_id", "symbol", "state_ts_ms"], keep="last")
        )
        rename = {
            col: f"candidate_{col}"
            for col in cand.columns
            if col not in {"venue", "component_id", "symbol", "state_ts_ms"}
        }
        panel = panel.join(cand.rename(rename), on=["venue", "component_id", "symbol", "state_ts_ms"], how="left")
    if open_interest_state is not None and not open_interest_state.is_empty():
        panel = _join_asof_state(
            panel,
            open_interest_state,
            source_ts_col="ts_ms",
            prefix="oi",
            tolerance_ms=2 * MS_PER_HOUR,
        )
    if funding_state is not None and not funding_state.is_empty():
        panel = _join_asof_state(
            panel,
            funding_state,
            source_ts_col="ts_ms",
            prefix="funding",
            tolerance_ms=9 * MS_PER_HOUR,
        )
    if btc_state is not None and not btc_state.is_empty():
        panel = _join_asof_state(
            panel,
            btc_state,
            source_ts_col="state_ts_ms",
            prefix="btc",
            tolerance_ms=2 * MS_PER_HOUR,
            by_symbol=False,
        )

    exprs = [
        pl.col("price_close").is_not_null().alias("price_available")
        if "price_close" in panel.columns
        else pl.lit(False).alias("price_available"),
        pl.col("candidate_reason").is_not_null().alias("candidate_state_available")
        if "candidate_reason" in panel.columns
        else pl.lit(False).alias("candidate_state_available"),
        pl.col("oi_open_interest").is_not_null().alias("open_interest_available")
        if "oi_open_interest" in panel.columns
        else pl.lit(False).alias("open_interest_available"),
        pl.col("funding_funding_rate").is_not_null().alias("funding_available")
        if "funding_funding_rate" in panel.columns
        else pl.lit(False).alias("funding_available"),
        pl.col("btc_btc_close").is_not_null().alias("btc_state_available")
        if "btc_btc_close" in panel.columns
        else pl.lit(False).alias("btc_state_available"),
        pl.lit(False).alias("spread_depth_available"),
        pl.lit(False).alias("sector_proxy_available"),
    ]
    panel = panel.with_columns(exprs)
    if "price_close" in panel.columns:
        panel = panel.with_columns(
            pl.when((pl.col("entry_price") > 0.0) & pl.col("price_close").is_not_null())
            .then(1.0 - pl.col("price_close") / pl.col("entry_price"))
            .otherwise(None)
            .alias("unrealized_return")
        )
    else:
        panel = panel.with_columns(pl.lit(None, dtype=pl.Float64).alias("unrealized_return"))
    return panel.sort(["venue", "component_id", "symbol", "entry_ts_ms", "state_ts_ms"])


def write_signal_invalidation_state_panel(
    output_root: Path, trades: pl.DataFrame, candidates: pl.DataFrame, venues: list[str]
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    required_trade_cols = {"venue", "component_id", "symbol", "entry_ts_ms", "exit_ts_ms", "entry_price"}
    if trades.is_empty() or not required_trade_cols <= set(trades.columns):
        return artifacts
    tables = output_root / "tables"
    panels: list[pl.DataFrame] = []
    for venue in venues:
        venue_trades = trades.filter(pl.col("venue") == venue)
        if venue_trades.is_empty():
            continue
        root = venue_root(venue)
        bars_by_symbol = _load_klines_for_rows(root, venue_trades, sparse_windows=True)
        price_state = _bars_dict_to_state_frame(bars_by_symbol)
        oi = _load_state_dataset_for_rows(
            root,
            "open_interest",
            venue_trades,
            value_cols=["open_interest", "open_interest_value"],
            pad_start_ms=2 * MS_PER_HOUR,
            pad_end_ms=2 * MS_PER_HOUR,
        )
        funding = _load_state_dataset_for_rows(
            root,
            "funding",
            venue_trades,
            value_cols=["funding_rate", "funding_rate_8h_equiv"],
            pad_start_ms=9 * MS_PER_HOUR,
            pad_end_ms=MS_PER_HOUR,
        )
        btc = _load_btc_state_for_rows(root, venue_trades)
        venue_candidates = candidates.filter(pl.col("venue") == venue) if "venue" in candidates.columns else candidates
        panels.append(
            _build_signal_invalidation_state_panel_for_venue(
                venue_trades,
                venue_candidates,
                price_state=price_state,
                open_interest_state=oi,
                funding_state=funding,
                btc_state=btc,
            )
        )
    non_empty_panels = [p for p in panels if not p.is_empty()]
    if not non_empty_panels:
        return artifacts
    panel = pl.concat(non_empty_panels, how="diagonal_relaxed")
    if panel.is_empty():
        return artifacts
    panel_path = tables / "signal_invalidation_hourly_state_panel.parquet"
    _write_df(panel, panel_path)
    artifacts["signal_invalidation_hourly_state_panel"] = str(panel_path)

    summary_rows: list[dict[str, Any]] = []
    for keys, part in panel.group_by(["venue"], maintain_order=True):
        venue = keys[0] if isinstance(keys, tuple) else keys
        trades_count = part.select(["venue", "component_id", "symbol", "entry_ts_ms"]).unique().height
        rows_count = part.height
        candidate_available = int(part.filter(pl.col("candidate_state_available")).height)
        losing = part.filter(pl.col("unrealized_return") < 0.0)
        required_flags = [
            "price_available",
            "open_interest_available",
            "funding_available",
            "btc_state_available",
            "spread_depth_available",
            "sector_proxy_available",
        ]
        full_panel_ready = all(part.filter(~pl.col(flag)).is_empty() for flag in required_flags)
        summary_rows.append(
            {
                "venue": venue,
                "trades": trades_count,
                "state_rows": rows_count,
                "candidate_state_rows": candidate_available,
                "candidate_state_coverage": candidate_available / rows_count if rows_count else None,
                "candidate_state_rows_while_losing": int(losing.filter(pl.col("candidate_state_available")).height),
                "losing_state_rows": int(losing.height),
                "price_coverage": float(part["price_available"].mean()) if rows_count else None,
                "open_interest_coverage": float(part["open_interest_available"].mean()) if rows_count else None,
                "funding_coverage": float(part["funding_available"].mean()) if rows_count else None,
                "btc_state_coverage": float(part["btc_state_available"].mean()) if rows_count else None,
                "spread_depth_coverage": 0.0,
                "sector_proxy_coverage": 0.0,
                "full_hourly_state_panel_ready": bool(full_panel_ready),
                "note": (
                    "Coverage audit only: candidate rows remain sparse; absence of a row is not invalidation. "
                    "Spread/depth and sector proxy state are unavailable in the frozen tape."
                ),
            }
        )
    summary = pl.DataFrame(summary_rows, infer_schema_length=None).sort("venue")
    summary_path = tables / "signal_invalidation_state_panel_summary.csv"
    _write_df(summary, summary_path)
    artifacts["signal_invalidation_state_panel_summary"] = str(summary_path)
    return artifacts


def _path_after(bars: pl.DataFrame, start_ms: int, end_ms: int) -> pl.DataFrame:
    if bars.is_empty():
        return bars
    return bars.filter((pl.col("bar_end_ts_ms") >= start_ms) & (pl.col("bar_end_ts_ms") <= end_ms))


def _path_coverage_issue(
    bars: pl.DataFrame,
    start_ms: int,
    end_ms: int,
    *,
    interval_ms: int,
) -> tuple[str | None, int, int | None]:
    path = _path_after(bars, start_ms, end_ms)
    if path.is_empty():
        return "missing_path", 0, None
    ts = path["bar_end_ts_ms"].to_numpy()
    if int(ts[0]) > start_ms:
        return "incomplete_path_start", int(len(ts)), None
    if int(ts[-1]) < end_ms:
        return "incomplete_path_tail", int(len(ts)), None
    if len(ts) > 1:
        max_gap = int(np.max(np.diff(ts)))
        if max_gap > interval_ms:
            return "incomplete_path_gap", int(len(ts)), max_gap
    return None, int(len(ts)), None


def _simulate_timing_entry(
    bars: pl.DataFrame,
    *,
    entry_ts_ms: int,
    entry_price: float,
    interval_ms: int,
    require_complete_path: bool,
) -> dict[str, Any] | None:
    if require_complete_path:
        end_ms = entry_ts_ms + 24 * MS_PER_HOUR
        issue, path_bars, max_gap = _path_coverage_issue(bars, entry_ts_ms, end_ms, interval_ms=interval_ms)
        if issue is not None:
            return {
                "filled": False,
                "unit_return": 0.0,
                "reason": issue,
                "path_bars": path_bars,
                "max_path_gap_ms": max_gap,
            }
    return _simulate_unit_short_from_entry(bars, entry_ts_ms=entry_ts_ms, entry_price=entry_price)


def _simulate_unit_short_from_entry(
    bars: pl.DataFrame,
    *,
    entry_ts_ms: int,
    entry_price: float,
    hold_hours: int = 24,
    take_profit_pct: float = COMPONENT_TP,
) -> dict[str, Any] | None:
    if entry_price <= 0 or bars.is_empty():
        return None
    exit_boundary = entry_ts_ms + hold_hours * MS_PER_HOUR
    path = _path_after(bars, entry_ts_ms, exit_boundary)
    if path.is_empty():
        return None
    tp = entry_price * (1.0 - take_profit_pct)
    tp_rows = path.filter(pl.col("low") <= tp)
    if not tp_rows.is_empty():
        exit_price = tp
        exit_ts = int(tp_rows["bar_end_ts_ms"][0])
        reason = "take_profit"
        path_to_exit = _path_after(path, entry_ts_ms, exit_ts)
    else:
        last = path.tail(1).to_dicts()[0]
        exit_price = float(last["close"])
        exit_ts = int(last["bar_end_ts_ms"])
        reason = "max_hold"
        path_to_exit = path
    high = path_to_exit["high"].to_numpy()
    low = path_to_exit["low"].to_numpy()
    close = path_to_exit["close"].to_numpy()
    gross = 1.0 - exit_price / entry_price
    adverse = float(np.max(high / entry_price - 1.0)) if high.size else None
    favorable = float(np.max(1.0 - low / entry_price)) if low.size else None
    prof_idx = np.where((1.0 - close / entry_price) > 0.0)[0]
    first_profit = None
    if prof_idx.size:
        ts = int(path_to_exit["bar_end_ts_ms"][int(prof_idx[0])])
        first_profit = (ts - entry_ts_ms) / MS_PER_HOUR
    return {
        "filled": True,
        "entry_ts_ms": entry_ts_ms,
        "exit_ts_ms": exit_ts,
        "exit_reason": reason,
        "unit_return": gross,
        "mae": adverse,
        "mfe": favorable,
        "time_to_first_profit_hours": first_profit,
        "path_bars": int(path_to_exit.height),
    }


def _atr_pct_before(bars: pl.DataFrame, entry_ts_ms: int, lookback: int = 24) -> float | None:
    prev = bars.filter(pl.col("bar_end_ts_ms") < entry_ts_ms).tail(lookback)
    if prev.height < max(4, lookback // 4):
        return None
    close = prev["close"].to_numpy()
    rng = (prev["high"].to_numpy() - prev["low"].to_numpy()) / np.maximum(close, 1e-12)
    return float(np.nanmean(rng))


def _parse_delay_ms(method: str) -> int | None:
    raw = method.split("_", 1)[1]
    if raw.endswith("h"):
        return int(raw[:-1]) * MS_PER_HOUR
    if raw.endswith("m"):
        return int(raw[:-1]) * MS_PER_MINUTE
    return None


def _parse_interval_ms(raw: str) -> int | None:
    if raw.endswith("h"):
        return int(raw[:-1]) * MS_PER_HOUR
    if raw.endswith("m"):
        return int(raw[:-1]) * MS_PER_MINUTE
    return None


def _aggregate_bars(bars: pl.DataFrame, *, interval_ms: int, source_interval_ms: int) -> pl.DataFrame:
    if bars.is_empty():
        return bars
    required = max(1, interval_ms // source_interval_ms)
    return (
        bars.sort("bar_end_ts_ms")
        .with_columns((((pl.col("bar_end_ts_ms") - 1) // interval_ms + 1) * interval_ms).alias("agg_end_ts_ms"))
        .group_by("agg_end_ts_ms", maintain_order=True)
        .agg(
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.col("bar_end_ts_ms").min().alias("first_source_bar_end_ts_ms"),
            pl.col("bar_end_ts_ms").max().alias("last_source_bar_end_ts_ms"),
            pl.len().alias("source_bars"),
        )
        .filter(pl.col("source_bars") >= required)
        .rename({"agg_end_ts_ms": "bar_end_ts_ms"})
        .sort("bar_end_ts_ms")
    )


def _next_red_entry_bar(
    bars: pl.DataFrame,
    *,
    entry_base_ts: int,
    red_interval_ms: int,
    source_interval_ms: int,
) -> dict[str, Any] | None:
    window = _path_after(bars, entry_base_ts + source_interval_ms, entry_base_ts + 24 * MS_PER_HOUR)
    if window.is_empty():
        return None
    red_bars = _aggregate_bars(window, interval_ms=red_interval_ms, source_interval_ms=source_interval_ms)
    if red_bars.is_empty():
        return None
    fills = red_bars.filter((pl.col("bar_end_ts_ms") > entry_base_ts) & (pl.col("close") < pl.col("open"))).head(1)
    return fills.to_dicts()[0] if not fills.is_empty() else None


def _simulate_timing_candidate(
    row: dict[str, Any],
    bars_by_symbol: dict[str, pl.DataFrame],
    method: str,
    *,
    dataset: str = "klines_1h",
    interval_ms: int = MS_PER_HOUR,
    require_complete_path: bool = False,
) -> dict[str, Any]:
    symbol = str(row["symbol"])
    bars = bars_by_symbol.get(symbol)
    base = {
        "venue": row["venue"],
        "component_id": row["component_id"],
        "method": method,
        "symbol": symbol,
        "signal_ts_ms": int(row["signal_ts_ms"]),
        "filled": False,
        "unit_return": 0.0,
        "mae": None,
        "mfe": None,
        "time_to_first_profit_hours": None,
        "source_dataset": dataset,
        "source_interval_minutes": interval_ms / MS_PER_MINUTE,
        "path_bars": None,
        "max_path_gap_ms": None,
    }
    if bars is None or bars.is_empty():
        return base | {"reason": "missing_bars"}
    entry_anchor = row.get("entry_bar_end_ts_ms")
    if entry_anchor is None:
        entry_anchor = row.get("order_submit_ts_ms")
    if entry_anchor is None:
        return base | {"reason": "missing_entry_anchor"}
    entry_base_ts = int(entry_anchor)
    first = _bar_at_or_after(bars, entry_base_ts)
    if first is None:
        return base | {"reason": "missing_entry_bar"}
    if method.startswith("delay_"):
        delay_ms = _parse_delay_ms(method)
        if delay_ms is None:
            return base | {"reason": "bad_method"}
        entry_bar = _bar_at_or_after(bars, entry_base_ts + delay_ms)
        if entry_bar is None:
            return base | {"reason": "missing_delay_bar"}
        sim = _simulate_timing_entry(
            bars,
            entry_ts_ms=int(entry_bar["bar_end_ts_ms"]),
            entry_price=float(entry_bar["close"]),
            interval_ms=interval_ms,
            require_complete_path=require_complete_path,
        )
        return base | (sim or {"reason": "missing_path"})
    if method.startswith("next_red_"):
        red_interval_ms = _parse_interval_ms(method.split("_", 2)[2])
        if red_interval_ms is None:
            return base | {"reason": "bad_method"}
        entry_bar = _next_red_entry_bar(
            bars,
            entry_base_ts=entry_base_ts,
            red_interval_ms=red_interval_ms,
            source_interval_ms=interval_ms,
        )
        if entry_bar is None:
            return base | {"reason": "unfilled"}
        sim = _simulate_timing_entry(
            bars,
            entry_ts_ms=int(entry_bar["bar_end_ts_ms"]),
            entry_price=float(entry_bar["close"]),
            interval_ms=interval_ms,
            require_complete_path=require_complete_path,
        )
        return base | (sim or {"reason": "missing_path"})
    if method.startswith("adverse_"):
        raw = method.split("_", 1)[1]
        if raw.endswith("pct"):
            threshold = float(raw[:-3]) / 100.0
        elif raw.endswith("atr"):
            atr = _atr_pct_before(bars, entry_base_ts)
            if atr is None:
                return base | {"reason": "missing_atr"}
            threshold = float(raw[:-3]) * atr
        else:
            return base | {"reason": "bad_method"}
        signal_price = float(first["close"])
        limit_price = signal_price * (1.0 + threshold)
        window = _path_after(bars, entry_base_ts, entry_base_ts + 24 * MS_PER_HOUR)
        fills = window.filter(pl.col("high") >= limit_price)
        if fills.is_empty():
            return base | {"reason": "unfilled"}
        fill_ts = int(fills["bar_end_ts_ms"][0])
        sim = _simulate_timing_entry(
            bars,
            entry_ts_ms=fill_ts,
            entry_price=limit_price,
            interval_ms=interval_ms,
            require_complete_path=require_complete_path,
        )
        return base | (sim or {"reason": "missing_path"})
    if method == "immediate":
        sim = _simulate_timing_entry(
            bars,
            entry_ts_ms=int(first["bar_end_ts_ms"]),
            entry_price=float(first["close"]),
            interval_ms=interval_ms,
            require_complete_path=require_complete_path,
        )
        return base | (sim or {"reason": "missing_path"})
    return base | {"reason": "unsupported"}


def write_timing_table(output_root: Path, candidates: pl.DataFrame, venues: list[str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if candidates.is_empty():
        return artifacts
    one_hour_methods = [
        "immediate",
        "delay_1h",
        "delay_2h",
        "delay_3h",
        "delay_4h",
        "delay_6h",
        "adverse_0.5pct",
        "adverse_1pct",
        "adverse_1.5pct",
        "adverse_2pct",
        "adverse_0.5atr",
        "adverse_1atr",
        "adverse_1.5atr",
    ]
    five_minute_methods = [
        "delay_15m",
        "delay_30m",
        "next_red_15m",
    ]
    methods = [*one_hour_methods, *five_minute_methods]
    rows: list[dict[str, Any]] = []
    for venue in venues:
        part = candidates.filter(pl.col("venue") == venue)
        if part.is_empty():
            continue
        bars_by_symbol = _load_klines_for_signal_rows(venue_root(venue), part, max_forward_hours=36)
        immediate_by_signal: dict[str, float] = {}
        method_rows: dict[str, list[dict[str, Any]]] = {m: [] for m in methods}
        for row in part.to_dicts():
            signal_key = f"{row['venue']}:{row['component_id']}:{row['symbol']}:{row['signal_ts_ms']}"
            for method in one_hour_methods:
                sim = _simulate_timing_candidate(row, bars_by_symbol, method)
                sim["signal_key"] = signal_key
                method_rows[method].append(sim)
                if method == "immediate":
                    immediate_by_signal[signal_key] = float(sim.get("unit_return") or 0.0)
        for symbol_part in part.partition_by("symbol", maintain_order=True):
            bars_5m = _load_klines_for_signal_rows(
                venue_root(venue),
                symbol_part,
                max_forward_hours=50,
                dataset="klines_5m",
                interval_ms=MS_PER_5M,
                sparse_windows=True,
            )
            for row in symbol_part.to_dicts():
                signal_key = f"{row['venue']}:{row['component_id']}:{row['symbol']}:{row['signal_ts_ms']}"
                for method in five_minute_methods:
                    sim = _simulate_timing_candidate(
                        row,
                        bars_5m,
                        method,
                        dataset="klines_5m",
                        interval_ms=MS_PER_5M,
                        require_complete_path=True,
                    )
                    sim["signal_key"] = signal_key
                    method_rows[method].append(sim)
        total_signals = max(part.height, 1)
        for method, sims in method_rows.items():
            df = pl.DataFrame(sims)
            filled = df.filter(pl.col("filled")) if "filled" in df.columns else pl.DataFrame()
            vals = filled["unit_return"].to_numpy() if not filled.is_empty() else np.array([])
            all_vals = df["unit_return"].to_numpy() if not df.is_empty() else np.array([])
            source_dataset = str(sims[0].get("source_dataset", "unknown")) if sims else "unknown"
            source_interval = float(sims[0].get("source_interval_minutes", 0.0) or 0.0) if sims else 0.0
            reason_counts = Counter(str(sim.get("reason", "filled")) for sim in sims)
            missed = 0
            if method != "immediate" and not df.is_empty():
                missed = sum(
                    1
                    for sim in sims
                    if immediate_by_signal.get(str(sim.get("signal_key")), 0.0) > 0.0
                    and (not bool(sim.get("filled")) or float(sim.get("unit_return") or 0.0) <= 0.0)
                )
            rows.append(
                {
                    "venue": venue,
                    "method": method,
                    "signals": int(total_signals),
                    "fills": int(filled.height),
                    "fill_rate": float(filled.height / total_signals),
                    "source_dataset": source_dataset,
                    "source_interval_minutes": source_interval,
                    "coverage_excluded": int(
                        sum(
                            count
                            for reason, count in reason_counts.items()
                            if reason.startswith("missing_") or reason.startswith("incomplete_")
                        )
                    ),
                    "unit_pnl_per_signal": float(all_vals.sum() / total_signals) if all_vals.size else 0.0,
                    "unit_pnl_per_fill": float(vals.mean()) if vals.size else None,
                    "median_unit_pnl_per_fill": float(np.median(vals)) if vals.size else None,
                    "trade_sharpe_like": _series_sharpe(vals, max(len(vals), 1)) if vals.size else None,
                    "worst_trade": float(vals.min()) if vals.size else None,
                    "avg_mae": _safe_mean(filled["mae"]) if not filled.is_empty() and "mae" in filled.columns else None,
                    "avg_mfe": _safe_mean(filled["mfe"]) if not filled.is_empty() and "mfe" in filled.columns else None,
                    "median_time_to_profit_hours": float(filled["time_to_first_profit_hours"].median())
                    if not filled.is_empty() and "time_to_first_profit_hours" in filled.columns
                    else None,
                    "missed_winners": int(missed),
                    "reason_counts_json": json.dumps(dict(sorted(reason_counts.items())), sort_keys=True),
                    "note": (
                        "signal-level gross unit simulation; not a full portfolio replay. "
                        "5m methods require a complete 24h post-entry 5m path."
                    ),
                }
            )
    out = pl.DataFrame(rows)
    _write_df(out, output_root / "tables" / "timing_by_original_signal.csv")
    artifacts["timing_by_original_signal"] = str(output_root / "tables" / "timing_by_original_signal.csv")
    unsupported = pl.DataFrame(
        [
            {"method": "next_red_1h", "status": "not_run", "reason": "not yet implemented as a portfolio replay"},
            {"method": "failed_high", "status": "not_run", "reason": "not yet implemented as a portfolio replay"},
            {"method": "lower_high", "status": "not_run", "reason": "not yet implemented as a portfolio replay"},
        ]
    )
    _write_df(unsupported, output_root / "tables" / "timing_not_run.csv")
    artifacts["timing_not_run"] = str(output_root / "tables" / "timing_not_run.csv")
    return artifacts


TIMING_PORTFOLIO_REPLAY_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "variant": "baseline_current",
        "kind": "baseline",
        "entry_delay_hours": 1,
        "adverse_limit_pct": 0.0,
        "adverse_limit_wait_hours": 0,
        "note": "Frozen current target; one post-decision bar delay.",
    },
    {
        "variant": "delay_plus_1h",
        "kind": "delay",
        "entry_delay_hours": 2,
        "adverse_limit_pct": 0.0,
        "adverse_limit_wait_hours": 0,
        "note": "Full engine replay with one extra hour of entry delay versus baseline.",
    },
    {
        "variant": "delay_plus_2h",
        "kind": "delay",
        "entry_delay_hours": 3,
        "adverse_limit_pct": 0.0,
        "adverse_limit_wait_hours": 0,
        "note": "Full engine replay with two extra hours of entry delay versus baseline.",
    },
    {
        "variant": "adverse_limit_1pct",
        "kind": "adverse_limit",
        "entry_delay_hours": 1,
        "adverse_limit_pct": 0.01,
        "adverse_limit_wait_hours": 24,
        "note": "Full engine replay: submit after baseline entry close, fill only if later bars touch +1%.",
    },
)


STOP_PORTFOLIO_REPLAY_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "variant": "baseline_current",
        "kind": "baseline",
        "stop_loss_pct": 0.0,
        "note": "Frozen current target; no fixed price stop.",
    },
    {
        "variant": "fixed_stop_20pct",
        "kind": "fixed_stop",
        "stop_loss_pct": 0.20,
        "note": "Full engine replay with a fixed 20% adverse price stop.",
    },
    {
        "variant": "fixed_stop_40pct",
        "kind": "fixed_stop",
        "stop_loss_pct": 0.40,
        "note": "Full engine replay with a fixed 40% catastrophic-width stop.",
    },
    {
        "variant": "fixed_stop_80pct",
        "kind": "fixed_stop",
        "stop_loss_pct": 0.80,
        "note": "Full engine replay with a fixed 80% catastrophic-width stop.",
    },
)


REGIME_PORTFOLIO_REPLAY_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "variant": "baseline_current",
        "kind": "baseline",
        "btc_trend_gate": "uptrend",
        "btc_trend_lookback_days": 30,
        "note": "Frozen current target; prior-30d BTC uptrend gate.",
    },
    {
        "variant": "btc_gate_off",
        "kind": "gate_off",
        "btc_trend_gate": "off",
        "btc_trend_lookback_days": 30,
        "note": "Full engine replay with the BTC trend gate disabled.",
    },
    *(
        {
            "variant": f"btc_uptrend_{lookback}d",
            "kind": "lookback",
            "btc_trend_gate": "uptrend",
            "btc_trend_lookback_days": lookback,
            "note": f"Full engine replay with prior-{lookback}d BTC uptrend gate.",
        }
        for lookback in (10, 15, 20, 25, 35, 40, 50, 60, 90)
    ),
)
REGIME_PORTFOLIO_REPLAY_VARIANT_NAMES = tuple(
    str(replay["variant"]) for replay in REGIME_PORTFOLIO_REPLAY_VARIANTS
)


SKIP_PORTFOLIO_REPLAY_VARIANTS: tuple[dict[str, Any], ...] = (
    {
        "variant": "baseline_current",
        "kind": "baseline",
        "entry_skip_external_size_multiplier_lte": 0.0,
        "note": "Frozen current target; BTC-risk tail state downsizes entries to 35%.",
    },
    {
        "variant": "skip_btc_tail_035",
        "kind": "external_size_multiplier",
        "entry_skip_external_size_multiplier_lte": 0.35,
        "note": "Full engine replay that skips entries the BTC-risk hook would size at <=35%.",
    },
    {
        "variant": "skip_btc_tail_035_btc_gate_off",
        "kind": "external_size_multiplier_gate_off",
        "entry_skip_external_size_multiplier_lte": 0.35,
        "btc_trend_gate": "off",
        "btc_trend_lookback_days": 30,
        "note": "Full engine replay that skips BTC-risk <=35% entries with the BTC trend gate disabled.",
    },
)
SKIP_PORTFOLIO_REPLAY_VARIANT_NAMES = tuple(
    str(replay["variant"]) for replay in SKIP_PORTFOLIO_REPLAY_VARIANTS
)


def _regime_replay_complete(replay: pl.DataFrame, venues: list[str]) -> bool:
    if replay.is_empty():
        return False
    expected = set(REGIME_PORTFOLIO_REPLAY_VARIANT_NAMES)
    for venue in venues:
        actual = set(
            replay.filter(pl.col("venue") == venue)
            .select("variant")
            .to_series()
            .cast(pl.Utf8)
            .to_list()
        )
        if not expected <= actual:
            return False
    return True


def _skip_replay_complete(replay: pl.DataFrame, venues: list[str]) -> bool:
    if replay.is_empty():
        return False
    expected = set(SKIP_PORTFOLIO_REPLAY_VARIANT_NAMES)
    for venue in venues:
        actual = set(
            replay.filter(pl.col("venue") == venue)
            .select("variant")
            .to_series()
            .cast(pl.Utf8)
            .to_list()
        )
        if not expected <= actual:
            return False
    return True


def _timing_replay_transform(entry_delay_hours: int, adverse_limit_pct: float, adverse_limit_wait_hours: int):
    def _transform(config: ContinuousEventConfig) -> ContinuousEventConfig:
        return replace(
            config,
            entry_delay_hours=entry_delay_hours,
            entry_adverse_limit_pct=adverse_limit_pct,
            entry_adverse_limit_wait_hours=adverse_limit_wait_hours,
        )

    return _transform


def _stop_replay_transform(stop_loss_pct: float):
    def _transform(config: ContinuousEventConfig) -> ContinuousEventConfig:
        return replace(
            config,
            stop_loss_pct=stop_loss_pct,
            stop_approach_frac=0.0,
            stop_vol_mult=0.0,
        )

    return _transform


def _regime_replay_transform(btc_trend_gate: str, btc_trend_lookback_days: int):
    def _transform(config: ContinuousEventConfig) -> ContinuousEventConfig:
        return replace(
            config,
            btc_trend_gate=btc_trend_gate,
            btc_trend_lookback_days=btc_trend_lookback_days,
        )

    return _transform


def _skip_replay_transform(
    entry_skip_external_size_multiplier_lte: float,
    btc_trend_gate: str | None = None,
    btc_trend_lookback_days: int | None = None,
):
    def _transform(config: ContinuousEventConfig) -> ContinuousEventConfig:
        kwargs: dict[str, Any] = {
            "entry_skip_external_size_multiplier_lte": entry_skip_external_size_multiplier_lte,
        }
        if btc_trend_gate is not None:
            kwargs["btc_trend_gate"] = btc_trend_gate
        if btc_trend_lookback_days is not None:
            kwargs["btc_trend_lookback_days"] = btc_trend_lookback_days
        return replace(
            config,
            **kwargs,
        )

    return _transform


def _summary_row_from_equity_summary(
    *,
    variant: str,
    variant_kind: str,
    entry_delay_hours: int,
    adverse_limit_pct: float,
    adverse_limit_wait_hours: int,
    stop_loss_pct: float = 0.0,
    stop_vol_mult: float = 0.0,
    stop_approach_frac: float = 0.0,
    btc_trend_gate: str | None = None,
    btc_trend_lookback_days: int | None = None,
    entry_skip_external_size_multiplier_lte: float = 0.0,
    venue: str,
    summary_path: Path,
    root: Path,
    note: str,
) -> dict[str, Any]:
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    stats = summary.get("stats") or {}
    component_rows = summary.get("components") or []
    return {
        "variant": variant,
        "variant_kind": variant_kind,
        "entry_delay_hours": entry_delay_hours,
        "added_delay_vs_baseline_hours": entry_delay_hours - 1,
        "entry_adverse_limit_pct": adverse_limit_pct,
        "entry_adverse_limit_wait_hours": adverse_limit_wait_hours,
        "stop_loss_pct": stop_loss_pct,
        "stop_vol_mult": stop_vol_mult,
        "stop_approach_frac": stop_approach_frac,
        "btc_trend_gate": btc_trend_gate,
        "btc_trend_lookback_days": btc_trend_lookback_days,
        "entry_skip_external_size_multiplier_lte": entry_skip_external_size_multiplier_lte,
        "venue": venue,
        "window": summary.get("window"),
        "component_take_profit_pct": summary.get("component_take_profit_pct"),
        "btc_risk_sizing": bool(summary.get("btc_risk_sizing")),
        "component_trades": int(sum(int(row.get("trades") or 0) for row in component_rows)),
        "full_return_pct": stats.get("total_return_pct"),
        "annualized_pct": stats.get("annualized_pct"),
        "max_drawdown_pct": stats.get("max_drawdown_pct"),
        "mar": stats.get("mar"),
        "sharpe_daily_ann": stats.get("sharpe_daily_ann"),
        "worst_day_pct": stats.get("worst_day_pct"),
        "summary_path": str(summary_path),
        "artifact_root": str(root),
        "note": note,
    }


def write_timing_portfolio_replay(
    output_root: Path,
    venues: list[str],
    *,
    run_replays: bool,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    tables = output_root / "tables"
    out_path = tables / "timing_portfolio_replay.csv"
    replay_root = output_root / "portfolio_replays" / "timing_delay_grid"
    fallback = SHARED / "continuous_deployed_equity_refresh_2026-06-12"
    rows: list[dict[str, Any]] = []

    for replay in TIMING_PORTFOLIO_REPLAY_VARIANTS:
        variant = str(replay["variant"])
        kind = str(replay["kind"])
        entry_delay = int(replay["entry_delay_hours"])
        adverse_limit_pct = float(replay["adverse_limit_pct"])
        adverse_limit_wait_hours = int(replay["adverse_limit_wait_hours"])
        note = str(replay["note"])
        for venue in venues:
            root = output_root if variant == "baseline_current" else replay_root / variant
            summary_path = root / venue / "continuous_equity_summary.json"
            if variant != "baseline_current" and run_replays and not summary_path.exists():
                deployed_refresh.run_venue(
                    venue,
                    output_root=root,
                    end_date=_end_boundary_from_root(venue_root(venue)),
                    start_date=None,
                    render_only=False,
                    frozen_fallback=fallback,
                    data_root=venue_root(venue),
                    chart_leverage=1.0,
                    component_take_profit_pct=COMPONENT_TP,
                    btc_risk_sizing=True,
                    backtest_leverage=1.0,
                    config_transform=_timing_replay_transform(
                        entry_delay,
                        adverse_limit_pct,
                        adverse_limit_wait_hours,
                    ),
                )
            if summary_path.exists():
                rows.append(
                    _summary_row_from_equity_summary(
                        variant=variant,
                        variant_kind=kind,
                        entry_delay_hours=entry_delay,
                        adverse_limit_pct=adverse_limit_pct,
                        adverse_limit_wait_hours=adverse_limit_wait_hours,
                        venue=venue,
                        summary_path=summary_path,
                        root=root,
                        note=note,
                    )
                )

    if rows:
        out = pl.DataFrame(rows).sort(["venue", "entry_delay_hours", "variant"])
        _write_df(out, out_path)
    elif out_path.exists():
        out = pl.read_csv(out_path)
    else:
        out = pl.DataFrame()

    if not out.is_empty():
        artifacts["timing_portfolio_replay"] = str(out_path)
    return artifacts


def write_stop_portfolio_replay(
    output_root: Path,
    venues: list[str],
    *,
    run_replays: bool,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    tables = output_root / "tables"
    out_path = tables / "stop_portfolio_replay.csv"
    replay_root = output_root / "portfolio_replays" / "stop_grid"
    fallback = SHARED / "continuous_deployed_equity_refresh_2026-06-12"
    rows: list[dict[str, Any]] = []

    for replay in STOP_PORTFOLIO_REPLAY_VARIANTS:
        variant = str(replay["variant"])
        kind = str(replay["kind"])
        stop_loss_pct = float(replay["stop_loss_pct"])
        note = str(replay["note"])
        for venue in venues:
            root = output_root if variant == "baseline_current" else replay_root / variant
            summary_path = root / venue / "continuous_equity_summary.json"
            if variant != "baseline_current" and run_replays and not summary_path.exists():
                deployed_refresh.run_venue(
                    venue,
                    output_root=root,
                    end_date=_end_boundary_from_root(venue_root(venue)),
                    start_date=None,
                    render_only=False,
                    frozen_fallback=fallback,
                    data_root=venue_root(venue),
                    chart_leverage=1.0,
                    component_take_profit_pct=COMPONENT_TP,
                    btc_risk_sizing=True,
                    backtest_leverage=1.0,
                    config_transform=_stop_replay_transform(stop_loss_pct),
                )
            if summary_path.exists():
                rows.append(
                    _summary_row_from_equity_summary(
                        variant=variant,
                        variant_kind=kind,
                        entry_delay_hours=1,
                        adverse_limit_pct=0.0,
                        adverse_limit_wait_hours=0,
                        stop_loss_pct=stop_loss_pct,
                        venue=venue,
                        summary_path=summary_path,
                        root=root,
                        note=note,
                    )
                )

    if rows:
        out = pl.DataFrame(rows).sort(["venue", "stop_loss_pct", "variant"])
        _write_df(out, out_path)
    elif out_path.exists():
        out = pl.read_csv(out_path)
    else:
        out = pl.DataFrame()

    if not out.is_empty():
        artifacts["stop_portfolio_replay"] = str(out_path)
    return artifacts


def write_regime_portfolio_replay(
    output_root: Path,
    venues: list[str],
    *,
    run_replays: bool,
    variant_names: list[str] | None = None,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    tables = output_root / "tables"
    out_path = tables / "regime_portfolio_replay.csv"
    replay_root = output_root / "portfolio_replays" / "btc_regime_grid"
    fallback = SHARED / "continuous_deployed_equity_refresh_2026-06-12"
    rows: list[dict[str, Any]] = []
    variant_filter = set(variant_names or [])
    unknown = variant_filter - set(REGIME_PORTFOLIO_REPLAY_VARIANT_NAMES)
    if unknown:
        raise ValueError(f"unknown regime replay variant(s): {', '.join(sorted(unknown))}")

    for order, replay in enumerate(REGIME_PORTFOLIO_REPLAY_VARIANTS):
        variant = str(replay["variant"])
        kind = str(replay["kind"])
        btc_trend_gate = str(replay["btc_trend_gate"])
        btc_trend_lookback_days = int(replay["btc_trend_lookback_days"])
        note = str(replay["note"])
        for venue in venues:
            root = output_root if variant == "baseline_current" else replay_root / variant
            summary_path = root / venue / "continuous_equity_summary.json"
            should_run = not variant_filter or variant in variant_filter
            if variant != "baseline_current" and run_replays and should_run and not summary_path.exists():
                deployed_refresh.run_venue(
                    venue,
                    output_root=root,
                    end_date=_end_boundary_from_root(venue_root(venue)),
                    start_date=None,
                    render_only=False,
                    frozen_fallback=fallback,
                    data_root=venue_root(venue),
                    chart_leverage=1.0,
                    component_take_profit_pct=COMPONENT_TP,
                    btc_risk_sizing=True,
                    backtest_leverage=1.0,
                    config_transform=_regime_replay_transform(
                        btc_trend_gate,
                        btc_trend_lookback_days,
                    ),
                )
            if summary_path.exists():
                row = _summary_row_from_equity_summary(
                    variant=variant,
                    variant_kind=kind,
                    entry_delay_hours=1,
                    adverse_limit_pct=0.0,
                    adverse_limit_wait_hours=0,
                    stop_loss_pct=0.0,
                    btc_trend_gate=btc_trend_gate,
                    btc_trend_lookback_days=btc_trend_lookback_days,
                    venue=venue,
                    summary_path=summary_path,
                    root=root,
                    note=note,
                )
                row["variant_order"] = order
                rows.append(row)

    if rows:
        out = pl.DataFrame(rows).sort(["venue", "variant_order"])
        _write_df(out, out_path)
    elif out_path.exists():
        out = pl.read_csv(out_path)
    else:
        out = pl.DataFrame()

    if not out.is_empty():
        artifacts["regime_portfolio_replay"] = str(out_path)
    return artifacts


def write_skip_portfolio_replay(
    output_root: Path,
    venues: list[str],
    *,
    run_replays: bool,
    variant_names: list[str] | None = None,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    tables = output_root / "tables"
    out_path = tables / "skip_portfolio_replay.csv"
    replay_root = output_root / "portfolio_replays" / "skip_grid"
    fallback = SHARED / "continuous_deployed_equity_refresh_2026-06-12"
    rows: list[dict[str, Any]] = []
    variant_filter = set(variant_names or [])
    unknown = variant_filter - set(SKIP_PORTFOLIO_REPLAY_VARIANT_NAMES)
    if unknown:
        raise ValueError(f"unknown skip replay variant(s): {', '.join(sorted(unknown))}")

    for order, replay in enumerate(SKIP_PORTFOLIO_REPLAY_VARIANTS):
        variant = str(replay["variant"])
        kind = str(replay["kind"])
        threshold = float(replay["entry_skip_external_size_multiplier_lte"])
        btc_trend_gate = replay.get("btc_trend_gate")
        if btc_trend_gate is not None:
            btc_trend_gate = str(btc_trend_gate)
        btc_trend_lookback_days = replay.get("btc_trend_lookback_days")
        if btc_trend_lookback_days is not None:
            btc_trend_lookback_days = int(btc_trend_lookback_days)
        note = str(replay["note"])
        for venue in venues:
            root = output_root if variant == "baseline_current" else replay_root / variant
            summary_path = root / venue / "continuous_equity_summary.json"
            should_run = not variant_filter or variant in variant_filter
            if variant != "baseline_current" and run_replays and should_run and not summary_path.exists():
                deployed_refresh.run_venue(
                    venue,
                    output_root=root,
                    end_date=_end_boundary_from_root(venue_root(venue)),
                    start_date=None,
                    render_only=False,
                    frozen_fallback=fallback,
                    data_root=venue_root(venue),
                    chart_leverage=1.0,
                    component_take_profit_pct=COMPONENT_TP,
                    btc_risk_sizing=True,
                    backtest_leverage=1.0,
                    config_transform=_skip_replay_transform(
                        threshold,
                        btc_trend_gate=btc_trend_gate,
                        btc_trend_lookback_days=btc_trend_lookback_days,
                    ),
                )
            if summary_path.exists():
                row = _summary_row_from_equity_summary(
                    variant=variant,
                    variant_kind=kind,
                    entry_delay_hours=1,
                    adverse_limit_pct=0.0,
                    adverse_limit_wait_hours=0,
                    stop_loss_pct=0.0,
                    btc_trend_gate=btc_trend_gate,
                    btc_trend_lookback_days=btc_trend_lookback_days,
                    entry_skip_external_size_multiplier_lte=threshold,
                    venue=venue,
                    summary_path=summary_path,
                    root=root,
                    note=note,
                )
                row["variant_order"] = order
                rows.append(row)

    if rows:
        out = pl.DataFrame(rows).sort(["venue", "variant_order"])
        _write_df(out, out_path)
    elif out_path.exists():
        out = pl.read_csv(out_path)
    else:
        out = pl.DataFrame()

    if not out.is_empty():
        artifacts["skip_portfolio_replay"] = str(out_path)
    return artifacts


def _scale_in_funding_lookup(
    root: Path,
    venue: str,
    trades: pl.DataFrame,
    config: ContinuousEventConfig,
) -> dict[str, dict[str, Any]] | None:
    if trades.is_empty() or not bool(config.use_funding):
        return None
    start_ms = int(trades["entry_ts_ms"].min()) - 10 * MS_PER_DAY
    end_ms = int(trades["exit_ts_ms"].max()) + MS_PER_DAY
    symbols = [str(symbol) for symbol in trades["symbol"].unique().to_list()]
    dataset = deployed_refresh.funding_root(venue, root).name
    paths = _date_part_paths(root, dataset, start_ms, end_ms, symbols=symbols)
    if not paths:
        return None
    funding = pl.read_parquet(paths)
    if funding.is_empty():
        return None
    funding_intervals = derive_funding_interval_min(funding)
    _assert_funding_one_per_settlement(funding, root=root, interval_by_symbol=funding_intervals)
    return _funding_lookup(funding, interval_by_symbol=funding_intervals)


def _empty_scale_in_child_trades() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "trade_id": pl.String,
            "parent_trade_id": pl.String,
            "symbol": pl.String,
            "entry_ts_ms": pl.Int64,
            "exit_ts_ms": pl.Int64,
            "notional_weight": pl.Float64,
            "net_return": pl.Float64,
            "scale_in_variant": pl.String,
        }
    )


def _scale_in_component_overlay(
    *,
    output_root: Path,
    replay_root: Path,
    venue: str,
    component: str,
    replay: dict[str, Any],
) -> tuple[Any, dict[str, Any]]:
    base_dir = _component_dir(output_root, venue, component)
    base_trades_path = base_dir / "continuous_trades.csv"
    if not base_trades_path.exists():
        raise FileNotFoundError(f"missing baseline component trades: {base_trades_path}")
    base_trades = pl.read_csv(base_trades_path)
    payload = _load_component_payload(output_root, venue, component)
    config = _cfg_from_payload(payload)
    data_root = venue_root(venue)
    bars_by_symbol = _load_klines_for_rows(data_root, base_trades, sparse_windows=True)
    funding_lookup = _scale_in_funding_lookup(data_root, venue, base_trades, config)
    variant = str(replay["variant"])
    trigger = float(replay["trigger_mae_pct"])
    fraction = float(replay["addon_fraction"])
    child_rows: list[dict[str, Any]] = []
    for parent in base_trades.to_dicts():
        child = _scale_in_child_trade_row(
            parent,
            bars_by_symbol.get(str(parent.get("symbol") or ""), pl.DataFrame()),
            config,
            variant=variant,
            trigger_mae_pct=trigger,
            addon_fraction=fraction,
            funding_lookup=funding_lookup,
        )
        if child is not None:
            child_rows.append(child)

    child_trades = (
        pl.DataFrame(child_rows, infer_schema_length=None)
        if child_rows
        else _empty_scale_in_child_trades()
    )
    combined_trades = (
        pl.concat([base_trades, child_trades], how="diagonal_relaxed")
        if not child_trades.is_empty()
        else base_trades
    ).sort(["entry_ts_ms", "trade_id"])
    kline_frames = [
        part.select(["symbol", "ts_ms", "close"])
        for part in bars_by_symbol.values()
        if not part.is_empty() and {"symbol", "ts_ms", "close"} <= set(part.columns)
    ]
    klines = (
        pl.concat(kline_frames, how="diagonal_relaxed").unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
        if kline_frames
        else pl.DataFrame()
    )
    mtm = _portfolio_mtm_equity(combined_trades, klines)
    piece = decompose_continuous_components(combined_trades, mtm, payload["config"])

    component_out = replay_root / "components" / venue / _component_cell(component)
    component_out.mkdir(parents=True, exist_ok=True)
    _write_df(combined_trades, component_out / "continuous_trades.csv")
    _write_df(child_trades, component_out / "scale_in_child_trades.csv")
    _write_df(mtm, component_out / "continuous_mtm_equity.csv")
    child_net = float(child_trades["net_return"].sum()) if not child_trades.is_empty() else 0.0
    child_notional = float(child_trades["notional_weight"].sum()) if not child_trades.is_empty() else 0.0
    meta = {
        "component": component,
        "component_cell": _component_cell(component),
        "config_hash": payload.get("config_hash"),
        "trades": int(combined_trades.height),
        "parent_trades": int(base_trades.height),
        "child_trades": int(child_trades.height),
        "child_fill_rate": float(child_trades.height / base_trades.height) if base_trades.height else 0.0,
        "child_notional_weight_sum": child_notional,
        "child_net_return": child_net,
        "funding_modes": sorted(
            str(mode)
            for mode in child_trades["funding_mode"].drop_nulls().unique().to_list()
        )
        if not child_trades.is_empty() and "funding_mode" in child_trades.columns
        else [],
        "combined_trades_csv": str(component_out / "continuous_trades.csv"),
        "child_trades_csv": str(component_out / "scale_in_child_trades.csv"),
        "mtm_csv": str(component_out / "continuous_mtm_equity.csv"),
    }
    (component_out / "scale_in_component_summary.json").write_text(
        json.dumps(meta, indent=2, default=str),
        encoding="utf-8",
    )
    return piece, meta


def _write_scale_in_variant_venue(
    output_root: Path,
    venue: str,
    replay: dict[str, Any],
) -> Path:
    variant = str(replay["variant"])
    replay_root = output_root / "portfolio_replays" / "scale_in_grid" / variant
    out_dir = replay_root / venue
    out_dir.mkdir(parents=True, exist_ok=True)
    pieces: dict[str, Any] = {}
    component_rows: list[dict[str, Any]] = []
    for component in deployed_refresh.WINNER_WEIGHTS:
        piece, meta = _scale_in_component_overlay(
            output_root=output_root,
            replay_root=replay_root,
            venue=venue,
            component=component,
            replay=replay,
        )
        pieces[component] = piece
        component_rows.append(meta)
    combined = combine_continuous_components(pieces, deployed_refresh.WINNER_WEIGHTS)
    data_root = venue_root(venue)
    end_date = _end_boundary_from_root(data_root)
    panel = deployed_refresh.load_extended_panel(venue, end_date=end_date, root=data_root)
    btc_ret, btc_fund = deployed_refresh.instrument_inputs(venue, combined.days, "BTCUSDT", panel, data_root=data_root)
    eth_ret, eth_fund = deployed_refresh.instrument_inputs(venue, combined.days, "ETHUSDT", panel, data_root=data_root)
    df = apply_rebalance_rule(
        combined,
        deployed_refresh.winner_rule(),
        ContinuousHedgeRule(90, 60, 2.0, 5.0),
        btc_ret,
        btc_fund,
        eth_ret,
        eth_fund,
        hedge_intensity=deployed_refresh.deployed_hedge_intensity(combined.days, btc_ret),
    )
    panel_last = dt.date.fromisoformat(str(panel["date"].max()))
    df = deployed_refresh.pad_flat_tail(df, through_date=panel_last)
    equity = df.with_columns(
        pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.date().cast(pl.String).alias("date")
    )
    equity_path = out_dir / "continuous_equity.csv"
    equity.write_csv(equity_path)
    summary = {
        "run_label": "exploratory",
        "strategy_run_label": "continuous_demo_paper_research_stage",
        "venue": venue,
        "variant": variant,
        "variant_kind": str(replay["kind"]),
        "data_root": str(data_root),
        "output_dir": str(out_dir),
        "end_date": end_date,
        "window": deployed_refresh.stats(df).get("window"),
        "component_take_profit_pct": COMPONENT_TP,
        "btc_risk_sizing": True,
        "backtest_leverage": 1.0,
        "scale_in_replay": {
            "trigger_mae_pct": float(replay["trigger_mae_pct"]),
            "addon_fraction_of_primary": float(replay["addon_fraction"]),
            "method": "explicit child shorts, separate TP12, parent-exit clamp, component MTM and BTC/ETH hedge recomputed",
            "limitations": "No order-book queue, liquidation mechanics, margin coupling, or intrabar trigger ordering beyond no same-bar TP.",
        },
        "stats": deployed_refresh.stats(df),
        "components": component_rows,
        "artifacts": {
            "equity_curve": str(equity_path),
            "components_root": str(replay_root / "components" / venue),
        },
    }
    summary_path = out_dir / "continuous_equity_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary_path


def write_scale_in_portfolio_replay(
    output_root: Path,
    venues: list[str],
    *,
    run_replays: bool,
    variant_names: list[str] | None = None,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    tables = output_root / "tables"
    out_path = tables / "scale_in_portfolio_replay.csv"
    variant_filter = set(variant_names or [])
    unknown = variant_filter - set(SCALE_IN_PORTFOLIO_REPLAY_VARIANT_NAMES)
    if unknown:
        raise ValueError(f"unknown scale-in replay variant(s): {', '.join(sorted(unknown))}")

    rows: list[dict[str, Any]] = []
    baseline_rows: dict[str, dict[str, Any]] = {}
    for venue in venues:
        summary_path = output_root / venue / "continuous_equity_summary.json"
        if not summary_path.exists():
            continue
        base = _summary_row_from_equity_summary(
            variant="baseline_current",
            variant_kind="baseline",
            entry_delay_hours=1,
            adverse_limit_pct=0.0,
            adverse_limit_wait_hours=0,
            venue=venue,
            summary_path=summary_path,
            root=output_root,
            note="Frozen continuous_ensemble_v2 baseline; no scale-in overlay.",
        )
        base.update(
            {
                "variant_order": 0,
                "trigger_mae_pct": 0.0,
                "addon_fraction_of_primary": 0.0,
                "child_trades": 0,
                "child_fill_rate": 0.0,
                "child_net_return": 0.0,
                "return_delta_pct": 0.0,
                "mar_delta": 0.0,
                "drawdown_delta_pct": 0.0,
            }
        )
        baseline_rows[venue] = base
        rows.append(base)

    for order, replay in enumerate(SCALE_IN_PORTFOLIO_REPLAY_VARIANTS, start=1):
        variant = str(replay["variant"])
        should_include = not variant_filter or variant in variant_filter
        if not should_include:
            continue
        for venue in venues:
            replay_root = output_root / "portfolio_replays" / "scale_in_grid" / variant
            summary_path = replay_root / venue / "continuous_equity_summary.json"
            if run_replays and not summary_path.exists():
                summary_path = _write_scale_in_variant_venue(output_root, venue, replay)
            if not summary_path.exists():
                continue
            row = _summary_row_from_equity_summary(
                variant=variant,
                variant_kind=str(replay["kind"]),
                entry_delay_hours=1,
                adverse_limit_pct=0.0,
                adverse_limit_wait_hours=0,
                venue=venue,
                summary_path=summary_path,
                root=replay_root,
                note=str(replay["note"]),
            )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            component_rows = summary.get("components") or []
            child_trades = int(sum(int(part.get("child_trades") or 0) for part in component_rows))
            parent_trades = int(sum(int(part.get("parent_trades") or 0) for part in component_rows))
            child_net = float(sum(float(part.get("child_net_return") or 0.0) for part in component_rows))
            base = baseline_rows.get(venue)
            row.update(
                {
                    "variant_order": order,
                    "trigger_mae_pct": float(replay["trigger_mae_pct"]),
                    "addon_fraction_of_primary": float(replay["addon_fraction"]),
                    "parent_trades": parent_trades,
                    "child_trades": child_trades,
                    "child_fill_rate": float(child_trades / parent_trades) if parent_trades else 0.0,
                    "child_net_return": child_net,
                    "return_delta_pct": (
                        float(row["full_return_pct"]) - float(base["full_return_pct"])
                        if base and row.get("full_return_pct") is not None and base.get("full_return_pct") is not None
                        else None
                    ),
                    "mar_delta": (
                        float(row["mar"]) - float(base["mar"])
                        if base and row.get("mar") is not None and base.get("mar") is not None
                        else None
                    ),
                    "drawdown_delta_pct": (
                        float(row["max_drawdown_pct"]) - float(base["max_drawdown_pct"])
                        if base and row.get("max_drawdown_pct") is not None and base.get("max_drawdown_pct") is not None
                        else None
                    ),
                }
            )
            rows.append(row)

    if rows:
        out = pl.DataFrame(rows, infer_schema_length=None).sort(["venue", "variant_order"])
        _write_df(out, out_path)
    elif out_path.exists():
        out = pl.read_csv(out_path)
    else:
        out = pl.DataFrame()
    if not out.is_empty():
        artifacts["scale_in_portfolio_replay"] = str(out_path)
    return artifacts


def _btc_daily_returns(root: Path, lookbacks: list[int]) -> pl.DataFrame:
    paths = [
        str(p)
        for date_part in (root / "klines_1h").glob("date=*")
        for p in (date_part / "symbol=BTCUSDT").rglob("*.parquet")
    ]
    if not paths:
        paths = [str(p) for p in (root / "klines_1h").rglob("*.parquet")]
    if not paths:
        return pl.DataFrame()
    btc = (
        pl.scan_parquet(paths)
        .filter(pl.col("symbol") == "BTCUSDT")
        .select("ts_ms", "close")
        .with_columns(((pl.col("ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts"))
        .group_by("day_ts")
        .agg(pl.col("close").last())
        .sort("day_ts")
        .collect()
    )
    if btc.is_empty():
        return btc
    exprs = []
    for lb in lookbacks:
        exprs.append((pl.col("close").shift(1) / pl.col("close").shift(lb + 1) - 1.0).alias(f"btc_ret_{lb}d"))
    return btc.with_columns(exprs).select(["day_ts", *[f"btc_ret_{lb}d" for lb in lookbacks]])


def write_regime_tables(output_root: Path, trades: pl.DataFrame, venues: list[str]) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    lookbacks = [10, 15, 20, 25, 30, 35, 40, 50, 60, 90]
    rows: list[dict[str, Any]] = []
    for venue in venues:
        part = trades.filter(pl.col("venue") == venue)
        if part.is_empty():
            continue
        btc = _btc_daily_returns(venue_root(venue), lookbacks)
        if btc.is_empty():
            continue
        joined = part.with_columns(((pl.col("entry_signal_ts_ms") // MS_PER_DAY) * MS_PER_DAY).alias("day_ts")).join(
            btc, on="day_ts", how="left"
        )
        for lb in lookbacks:
            col = f"btc_ret_{lb}d"
            for state, subset in (
                ("on", joined.filter(pl.col(col) > 0.0)),
                ("off", joined.filter(pl.col(col) <= 0.0)),
            ):
                vals = subset["portfolio_net_return"].to_numpy() if not subset.is_empty() else np.array([])
                rows.append(
                    {
                        "venue": venue,
                        "btc_filter": f"{lb}d_{state}",
                        "trades": int(subset.height),
                        "net_pnl": float(vals.sum()) if vals.size else 0.0,
                        "win_rate": float((vals > 0).mean()) if vals.size else None,
                        "profit_factor": _profit_factor(vals),
                        "worst_trade": float(vals.min()) if vals.size else None,
                    }
                )
    out = pl.DataFrame(rows)
    _write_df(out, output_root / "tables" / "btc_regime_trade_robustness.csv")
    artifacts["btc_regime_trade_robustness"] = str(output_root / "tables" / "btc_regime_trade_robustness.csv")
    return artifacts


def write_cost_tail_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    rows: list[dict[str, Any]] = []
    scenarios = [
        ("baseline", 0.0, 1.0),
        ("plus_2bps", -2.0, 1.0),
        ("plus_5bps", -5.0, 1.0),
        ("plus_10bps", -10.0, 1.0),
        ("funding_2x", 0.0, 2.0),
        ("funding_excluded", 0.0, 0.0),
    ]
    for venue in sorted(trades["venue"].unique().to_list()):
        part = trades.filter(pl.col("venue") == venue)
        notional = (part["notional_weight"] * part["component_weight"]).to_numpy()
        for name, extra_bps, funding_mult in scenarios:
            vals = (
                part["portfolio_gross_return"].to_numpy()
                + part["portfolio_cost_return"].to_numpy()
                + part["portfolio_funding_return"].to_numpy() * funding_mult
                + notional * extra_bps / 10_000.0
            )
            rows.append(
                {
                    "venue": venue,
                    "scenario": name,
                    "trades": int(part.height),
                    "net_pnl": float(vals.sum()),
                    "win_rate": float((vals > 0.0).mean()),
                    "profit_factor": _profit_factor(vals),
                    "worst_trade": float(vals.min()),
                    "es_95": float(vals[vals <= np.quantile(vals, 0.05)].mean()),
                }
            )
    costs = pl.DataFrame(rows)
    _write_df(costs, output_root / "tables" / "cost_funding_scenarios.csv")
    artifacts["cost_funding_scenarios"] = str(output_root / "tables" / "cost_funding_scenarios.csv")

    shock_rows: list[dict[str, Any]] = []
    shocks = [
        ("one_coin_30pct", 1, 0.30),
        ("one_coin_50pct", 1, 0.50),
        ("one_coin_100pct", 1, 1.00),
        ("one_coin_200pct", 1, 2.00),
        ("three_coins_30pct", 3, 0.30),
        ("three_coins_50pct", 3, 0.50),
        ("five_coins_30pct", 5, 0.30),
    ]
    for venue in sorted(trades["venue"].unique().to_list()):
        part = trades.filter(pl.col("venue") == venue)
        for name, n, shock in shocks:
            exposures = (
                part.with_columns((pl.col("notional_weight") * pl.col("component_weight") * shock).alias("shock_loss"))
                .sort("shock_loss", descending=True)
                .head(n)
            )
            shock_rows.append(
                {
                    "venue": venue,
                    "scenario": name,
                    "positions_hit": int(min(n, part.height)),
                    "shock_loss_pct_equity": float(exposures["shock_loss"].sum()) if not exposures.is_empty() else 0.0,
                    "largest_symbol": str(exposures["symbol"][0]) if not exposures.is_empty() else None,
                    "strategy_survives_assuming_no_margin_call": bool(float(exposures["shock_loss"].sum()) < 0.50)
                    if not exposures.is_empty()
                    else True,
                }
            )
    tail = pl.DataFrame(shock_rows)
    _write_df(tail, output_root / "tables" / "synthetic_squeeze_static_heat.csv")
    artifacts["synthetic_squeeze_static_heat"] = str(output_root / "tables" / "synthetic_squeeze_static_heat.csv")
    return artifacts


SYNTHETIC_SQUEEZE_SURVIVAL_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "scenario": "one_coin_50pct",
        "positions_hit": 1,
        "symbol_shock_pct": 0.50,
        "all_active": False,
        "btc_shock_pct": 0.0,
        "eth_shock_pct": 0.0,
        "extra_slippage_pct": 0.0,
        "outage_minutes": 0,
        "note": "Instant single-name squeeze against the largest active symbol exposure.",
    },
    {
        "scenario": "one_coin_100pct",
        "positions_hit": 1,
        "symbol_shock_pct": 1.00,
        "all_active": False,
        "btc_shock_pct": 0.0,
        "eth_shock_pct": 0.0,
        "extra_slippage_pct": 0.0,
        "outage_minutes": 0,
        "note": "Instant single-name +100% squeeze against the largest active symbol exposure.",
    },
    {
        "scenario": "three_coins_50pct",
        "positions_hit": 3,
        "symbol_shock_pct": 0.50,
        "all_active": False,
        "btc_shock_pct": 0.0,
        "eth_shock_pct": 0.0,
        "extra_slippage_pct": 0.0,
        "outage_minutes": 0,
        "note": "Instant +50% squeeze against the three largest active symbol exposures.",
    },
    {
        "scenario": "five_coins_30pct",
        "positions_hit": 5,
        "symbol_shock_pct": 0.30,
        "all_active": False,
        "btc_shock_pct": 0.0,
        "eth_shock_pct": 0.0,
        "extra_slippage_pct": 0.0,
        "outage_minutes": 0,
        "note": "Instant +30% squeeze against the five largest active symbol exposures.",
    },
    {
        "scenario": "btc10_alts30",
        "positions_hit": None,
        "symbol_shock_pct": 0.30,
        "all_active": True,
        "btc_shock_pct": 0.10,
        "eth_shock_pct": 0.10,
        "extra_slippage_pct": 0.0,
        "outage_minutes": 0,
        "note": "All active shorts gap +30% while BTC/ETH hedge legs are credited with +10%.",
    },
    {
        "scenario": "exchange_down_1h_one_coin_100pct",
        "positions_hit": 1,
        "symbol_shock_pct": 1.00,
        "all_active": False,
        "btc_shock_pct": 0.0,
        "eth_shock_pct": 0.0,
        "extra_slippage_pct": 0.10,
        "outage_minutes": 60,
        "note": "Single-name +100% squeeze with one-hour exchange outage and 10% extra taker/slippage on hit notional.",
    },
    {
        "scenario": "risk_daemon_down_one_coin_100pct",
        "positions_hit": 1,
        "symbol_shock_pct": 1.00,
        "all_active": False,
        "btc_shock_pct": 0.0,
        "eth_shock_pct": 0.0,
        "extra_slippage_pct": 0.20,
        "outage_minutes": 120,
        "note": "Single-name +100% squeeze with synthetic risk-daemon failure and 20% extra exit damage on hit notional.",
    },
)


def _active_trade_snapshots(trades: pl.DataFrame) -> list[tuple[int, list[dict[str, Any]]]]:
    rows = trades.to_dicts()
    events: list[tuple[int, int, int]] = []
    for idx, row in enumerate(rows):
        entry_ts = row.get("entry_ts_ms")
        exit_ts = row.get("exit_ts_ms")
        if entry_ts is None or exit_ts is None:
            continue
        events.append((int(entry_ts), 1, idx))
        events.append((int(exit_ts), -1, idx))
    events.sort(key=lambda item: (item[0], item[1]))
    active: set[int] = set()
    snapshots: list[tuple[int, list[dict[str, Any]]]] = []
    pos = 0
    while pos < len(events):
        ts = events[pos][0]
        exits: list[int] = []
        entries: list[int] = []
        while pos < len(events) and events[pos][0] == ts:
            _, action, idx = events[pos]
            if action < 0:
                exits.append(idx)
            else:
                entries.append(idx)
            pos += 1
        for idx in exits:
            active.discard(idx)
        for idx in entries:
            active.add(idx)
        if active:
            snapshots.append((ts, [rows[idx] for idx in sorted(active)]))
    return snapshots


def _equity_context_at(equity: pl.DataFrame, ts_ms: int) -> dict[str, float] | None:
    if equity.is_empty() or "ts_ms" not in equity.columns or "equity" not in equity.columns:
        return None
    ordered = equity.sort("ts_ms")
    ts = ordered["ts_ms"].to_numpy()
    idx = int(np.searchsorted(ts, ts_ms, side="right") - 1)
    if idx < 0:
        idx = 0
    values = ordered["equity"].to_numpy()
    peak = float(np.max(values[: idx + 1]))
    hedge_leg1 = float(ordered["hedge_ratio_leg1"][idx]) if "hedge_ratio_leg1" in ordered.columns else 0.0
    hedge_leg2 = float(ordered["hedge_ratio_leg2"][idx]) if "hedge_ratio_leg2" in ordered.columns else 0.0
    return {
        "idx": float(idx),
        "equity": float(values[idx]),
        "peak_equity": peak,
        "baseline_drawdown": float(values[idx] / peak - 1.0) if peak else 0.0,
        "final_equity": float(values[-1]),
        "hedge_ratio_leg1": hedge_leg1,
        "hedge_ratio_leg2": hedge_leg2,
    }


def _days_to_recover_after_loss(equity: pl.DataFrame, ts_ms: int, peak_equity: float, net_loss: float) -> float | None:
    if equity.is_empty() or peak_equity <= 0.0:
        return None
    ordered = equity.sort("ts_ms")
    ts = ordered["ts_ms"].to_numpy()
    values = ordered["equity"].to_numpy()
    idx = int(np.searchsorted(ts, ts_ms, side="left"))
    if idx < 0:
        idx = 0
    adjusted = values[idx:] - net_loss
    hits = np.flatnonzero(adjusted >= peak_equity)
    if hits.size == 0:
        return None
    recover_ts = int(ts[idx + int(hits[0])])
    return float(max(recover_ts - ts_ms, 0) / MS_PER_DAY)


def _symbol_exposures(active: list[dict[str, Any]]) -> list[tuple[str, float, int]]:
    by_symbol: dict[str, tuple[float, int]] = {}
    for row in active:
        symbol = str(row.get("symbol") or "")
        notional = float(row.get("notional_weight") or 0.0) * float(row.get("component_weight") or 0.0)
        prev_notional, prev_count = by_symbol.get(symbol, (0.0, 0))
        by_symbol[symbol] = (prev_notional + max(notional, 0.0), prev_count + 1)
    return sorted(
        ((symbol, exposure, count) for symbol, (exposure, count) in by_symbol.items() if exposure > 0.0),
        key=lambda item: item[1],
        reverse=True,
    )


def _squeeze_snapshot_row(
    *,
    venue: str,
    scenario: dict[str, Any],
    placement: str,
    ts_ms: int,
    active: list[dict[str, Any]],
    equity: pl.DataFrame,
) -> dict[str, Any] | None:
    ctx = _equity_context_at(equity, ts_ms)
    exposures = _symbol_exposures(active)
    if ctx is None or not exposures:
        return None
    active_notional = float(sum(exposure for _, exposure, _ in exposures))
    all_active = bool(scenario["all_active"])
    hit = exposures if all_active else exposures[: int(scenario["positions_hit"])]
    hit_exposure = float(sum(exposure for _, exposure, _ in hit))
    if hit_exposure <= 0.0:
        return None
    symbol_shock = float(scenario["symbol_shock_pct"])
    extra_slippage = float(scenario["extra_slippage_pct"])
    short_loss = hit_exposure * symbol_shock
    execution_loss = hit_exposure * extra_slippage
    hedge_offset = (
        float(ctx["hedge_ratio_leg1"]) * float(scenario["btc_shock_pct"])
        + float(ctx["hedge_ratio_leg2"]) * float(scenario["eth_shock_pct"])
    )
    net_loss = short_loss + execution_loss - hedge_offset
    equity_before = float(ctx["equity"])
    peak_equity = float(ctx["peak_equity"])
    post_equity = equity_before - net_loss
    maintenance_margin = active_notional * 0.005
    post_drawdown = post_equity / peak_equity - 1.0 if peak_equity else None
    remaining_to_zero = max(post_equity - maintenance_margin, 0.0) / hit_exposure if hit_exposure else None
    pre_liq_move = max(equity_before - maintenance_margin, 0.0) / hit_exposure if hit_exposure else None
    days_to_recovery = _days_to_recover_after_loss(equity, ts_ms, peak_equity, net_loss)
    return {
        "venue": venue,
        "scenario": str(scenario["scenario"]),
        "placement": placement,
        "event_ts_ms": ts_ms,
        "event_date": _date_from_ms(ts_ms).isoformat(),
        "active_positions": int(sum(count for _, _, count in exposures)),
        "active_symbols": int(len(exposures)),
        "symbols_hit": int(len(hit)),
        "hit_symbols": ",".join(symbol for symbol, _, _ in hit[:10]),
        "active_notional_pct_equity": active_notional,
        "hit_notional_pct_equity": hit_exposure,
        "symbol_shock_pct": symbol_shock,
        "extra_slippage_pct": extra_slippage,
        "outage_minutes": int(scenario["outage_minutes"]),
        "short_loss_pct_equity": short_loss,
        "execution_loss_pct_equity": execution_loss,
        "hedge_offset_pct_equity": hedge_offset,
        "net_loss_pct_equity": net_loss,
        "equity_before": equity_before,
        "post_event_equity": post_equity,
        "post_event_drawdown_pct": post_drawdown,
        "drawdown_increment_pct": (post_drawdown - float(ctx["baseline_drawdown"])) if post_drawdown is not None else None,
        "margin_peak_pct_equity": active_notional / max(post_equity, 1e-12) if post_equity > 0.0 else None,
        "maintenance_margin_assumption_pct_notional": 0.005,
        "pre_shock_liquidation_move_pct": pre_liq_move,
        "liquidation_distance_after_shock_pct": remaining_to_zero,
        "positions_liquidated_account_level": int(sum(count for _, _, count in hit)) if post_equity <= maintenance_margin else 0,
        "days_to_recovery": days_to_recovery,
        "post_event_final_return_pct": (float(ctx["final_equity"]) - net_loss - 1.0) * 100.0,
        "survives_equity_positive": bool(post_equity > 0.0),
        "survives_50pct_ruin_bar": bool(net_loss < 0.50 and post_equity > 0.0),
        "note": str(scenario["note"]) + " Diagnostic: instant shock on active baseline book, not an exchange liquidation engine.",
    }


def _select_squeeze_placements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    ordered = sorted(
        rows,
        key=lambda row: (
            float(row["net_loss_pct_equity"]),
            -float(row["post_event_drawdown_pct"]) if row.get("post_event_drawdown_pct") is not None else 0.0,
        ),
    )
    indexes = {
        "median_active": int(round((len(ordered) - 1) * 0.50)),
        "p95_active": int(round((len(ordered) - 1) * 0.95)),
        "worst_active": len(ordered) - 1,
    }
    selected: list[dict[str, Any]] = []
    for placement, idx in indexes.items():
        row = dict(ordered[idx])
        row["placement"] = placement
        selected.append(row)
    return selected


def write_synthetic_squeeze_survival_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty():
        return artifacts
    rows: list[dict[str, Any]] = []
    for venue in sorted(trades["venue"].unique().to_list()):
        part = trades.filter(pl.col("venue") == venue)
        equity_path = output_root / str(venue) / "continuous_equity.csv"
        if part.is_empty() or not equity_path.exists():
            continue
        equity = pl.read_csv(equity_path)
        snapshots = _active_trade_snapshots(part)
        for scenario in SYNTHETIC_SQUEEZE_SURVIVAL_SCENARIOS:
            scenario_rows = [
                row
                for ts_ms, active in snapshots
                if (
                    row := _squeeze_snapshot_row(
                        venue=str(venue),
                        scenario=scenario,
                        placement="all_active",
                        ts_ms=ts_ms,
                        active=active,
                        equity=equity,
                    )
                )
                is not None
            ]
            rows.extend(_select_squeeze_placements(scenario_rows))
    if rows:
        out = pl.DataFrame(rows).sort(["venue", "scenario", "placement"])
        out_path = output_root / "tables" / "synthetic_squeeze_survival.csv"
        _write_df(out, out_path)
        artifacts["synthetic_squeeze_survival"] = str(out_path)
    return artifacts


CLUSTER_RISK_BOOTSTRAP_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "scenario": "cluster_bootstrap",
        "method": "cluster",
        "block_len": 1,
        "worst_weight_mult": 1.0,
        "tail_scenario": None,
        "tail_placement": None,
        "tail_count": 0,
        "note": "Same-signal cluster bootstrap with replacement.",
    },
    {
        "scenario": "block_bootstrap_20_clusters",
        "method": "block",
        "block_len": 20,
        "worst_weight_mult": 1.0,
        "tail_scenario": None,
        "tail_placement": None,
        "tail_count": 0,
        "note": "Consecutive 20-cluster block bootstrap with replacement.",
    },
    {
        "scenario": "worst_cluster_overweighted_3x",
        "method": "cluster",
        "block_len": 1,
        "worst_weight_mult": 3.0,
        "tail_scenario": None,
        "tail_placement": None,
        "tail_count": 0,
        "note": "Same-signal cluster bootstrap with worst 5% clusters sampled at 3x weight.",
    },
    {
        "scenario": "tail_injected_one_worst_100pct",
        "method": "cluster",
        "block_len": 1,
        "worst_weight_mult": 1.0,
        "tail_scenario": "one_coin_100pct",
        "tail_placement": "worst_active",
        "tail_count": 1,
        "note": "Cluster bootstrap plus one worst active-book one-coin +100% squeeze loss.",
    },
    {
        "scenario": "tail_injected_one_worst_outage_100pct",
        "method": "cluster",
        "block_len": 1,
        "worst_weight_mult": 1.0,
        "tail_scenario": "exchange_down_1h_one_coin_100pct",
        "tail_placement": "worst_active",
        "tail_count": 1,
        "note": "Cluster bootstrap plus one worst active-book +100% squeeze with outage/extra exit damage.",
    },
    {
        "scenario": "tail_injected_three_p95_100pct",
        "method": "cluster",
        "block_len": 1,
        "worst_weight_mult": 1.0,
        "tail_scenario": "one_coin_100pct",
        "tail_placement": "p95_active",
        "tail_count": 3,
        "note": "Cluster bootstrap plus three p95 active-book one-coin +100% squeeze losses.",
    },
)


def _stable_seed_offset(*parts: str) -> int:
    raw = "|".join(parts).encode("utf-8")
    return int(hashlib.sha256(raw).hexdigest()[:8], 16)


def _cluster_bootstrap_paths(
    cluster_returns: np.ndarray,
    *,
    scenario: dict[str, Any],
    trials: int,
    rng: np.random.Generator,
) -> np.ndarray:
    n = int(cluster_returns.size)
    if n <= 0:
        return np.empty((0, 0), dtype=float)
    method = str(scenario["method"])
    if method == "block":
        block_len = max(int(scenario["block_len"]), 1)
        block_count = int(math.ceil(n / block_len))
        starts = rng.integers(0, n, size=(trials, block_count))
        offsets = np.arange(block_len)
        indexes = (starts[:, :, None] + offsets[None, None, :]) % n
        indexes = indexes.reshape(trials, block_count * block_len)[:, :n]
        return cluster_returns[indexes]
    weights = None
    worst_weight_mult = float(scenario.get("worst_weight_mult") or 1.0)
    if worst_weight_mult > 1.0 and n >= 20:
        cutoff = float(np.quantile(cluster_returns, 0.05))
        weights = np.ones(n, dtype=float)
        weights[cluster_returns <= cutoff] *= worst_weight_mult
        weights /= weights.sum()
    indexes = rng.choice(n, size=(trials, n), replace=True, p=weights)
    return cluster_returns[indexes]


def _tail_loss_from_survival(
    survival: pl.DataFrame,
    *,
    venue: str,
    scenario: dict[str, Any],
) -> float | None:
    tail_scenario = scenario.get("tail_scenario")
    if not tail_scenario or survival.is_empty():
        return None
    part = survival.filter(
        (pl.col("venue") == venue)
        & (pl.col("scenario") == str(tail_scenario))
        & (pl.col("placement") == str(scenario["tail_placement"]))
    )
    if part.is_empty() or "net_loss_pct_equity" not in part.columns:
        return None
    return float(part["net_loss_pct_equity"][0])


def _inject_tail_losses(paths: np.ndarray, *, loss: float, count: int, rng: np.random.Generator) -> None:
    if paths.size == 0 or loss <= 0.0 or count <= 0:
        return
    trials, width = paths.shape
    rows = np.repeat(np.arange(trials), count)
    cols = rng.integers(0, width, size=trials * count)
    paths[rows, cols] -= float(loss)


def _longest_underwater_runs(equity: np.ndarray) -> np.ndarray:
    if equity.size == 0:
        return np.array([], dtype=float)
    out = np.zeros(equity.shape[0], dtype=float)
    peaks = np.maximum.accumulate(equity, axis=1)
    underwater = equity < (peaks - 1e-12)
    for row_idx, row in enumerate(underwater):
        best = cur = 0
        for flag in row:
            if bool(flag):
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        out[row_idx] = float(best)
    return out


def _cluster_risk_summary_row(
    *,
    venue: str,
    scenario: dict[str, Any],
    paths: np.ndarray,
    trials: int,
    seed: int,
    horizon_years: float,
    observed_clusters: int,
    observed_cluster_return_sum: float,
    observed_cluster_compound_return: float,
    observed_worst_cluster_return: float,
    tail_loss: float | None,
) -> dict[str, Any]:
    factors = 1.0 + paths
    equity = np.cumprod(factors, axis=1) if paths.size else np.empty((0, 0), dtype=float)
    final_return = equity[:, -1] - 1.0 if equity.size else np.array([], dtype=float)
    peaks = np.maximum.accumulate(equity, axis=1) if equity.size else np.empty((0, 0), dtype=float)
    drawdowns = equity / np.maximum(peaks, 1e-12) - 1.0 if equity.size else np.empty((0, 0), dtype=float)
    max_drawdown = drawdowns.min(axis=1) if drawdowns.size else np.array([], dtype=float)
    min_equity = equity.min(axis=1) if equity.size else np.array([], dtype=float)
    with np.errstate(invalid="ignore"):
        annualized = np.where(final_return > -1.0, np.power(1.0 + final_return, 1.0 / horizon_years) - 1.0, -1.0)
    q05 = float(np.quantile(final_return, 0.05)) if final_return.size else 0.0
    es05 = float(final_return[final_return <= q05].mean()) if final_return.size and np.any(final_return <= q05) else None
    longest = _longest_underwater_runs(equity)
    return {
        "venue": venue,
        "scenario": str(scenario["scenario"]),
        "method": str(scenario["method"]),
        "trials": int(trials),
        "seed": int(seed),
        "clusters_per_path": int(paths.shape[1]) if paths.size else 0,
        "observed_clusters": int(observed_clusters),
        "horizon_years": float(horizon_years),
        "observed_cluster_return_sum": float(observed_cluster_return_sum),
        "observed_cluster_compound_return": float(observed_cluster_compound_return),
        "observed_worst_cluster_return": float(observed_worst_cluster_return),
        "tail_scenario": scenario.get("tail_scenario"),
        "tail_placement": scenario.get("tail_placement"),
        "tail_count": int(scenario.get("tail_count") or 0),
        "tail_loss_pct_equity": tail_loss,
        "final_return_median": float(np.median(final_return)) if final_return.size else None,
        "final_return_p05": q05 if final_return.size else None,
        "final_return_p01": float(np.quantile(final_return, 0.01)) if final_return.size else None,
        "annual_return_median": float(np.median(annualized)) if annualized.size else None,
        "annual_return_p05": float(np.quantile(annualized, 0.05)) if annualized.size else None,
        "annual_return_p01": float(np.quantile(annualized, 0.01)) if annualized.size else None,
        "expected_shortfall_5pct": es05,
        "prob_final_return_negative": float(np.mean(final_return < 0.0)) if final_return.size else None,
        "max_drawdown_median": float(np.median(max_drawdown)) if max_drawdown.size else None,
        "max_drawdown_p05": float(np.quantile(max_drawdown, 0.05)) if max_drawdown.size else None,
        "worst_simulated_drawdown": float(max_drawdown.min()) if max_drawdown.size else None,
        "prob_drawdown_5pct": float(np.mean(max_drawdown <= -0.05)) if max_drawdown.size else None,
        "prob_drawdown_10pct": float(np.mean(max_drawdown <= -0.10)) if max_drawdown.size else None,
        "prob_drawdown_20pct": float(np.mean(max_drawdown <= -0.20)) if max_drawdown.size else None,
        "prob_drawdown_50pct": float(np.mean(max_drawdown <= -0.50)) if max_drawdown.size else None,
        "prob_account_impairment_50pct_equity": float(np.mean(min_equity <= 0.50)) if min_equity.size else None,
        "prob_account_ruin_equity_nonpositive": float(np.mean(min_equity <= 0.0)) if min_equity.size else None,
        "longest_drawdown_clusters_p95": float(np.quantile(longest, 0.95)) if longest.size else None,
        "longest_drawdown_clusters_max": float(longest.max()) if longest.size else None,
        "note": str(scenario["note"]) + " Diagnostic cluster return bootstrap, not a margin/liquidation engine.",
    }


def write_cluster_risk_of_ruin_tables(
    output_root: Path,
    trades: pl.DataFrame,
    *,
    trials: int = 10_000,
    seed: int = 20_260_628,
) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if trades.is_empty() or "portfolio_net_return" not in trades.columns:
        return artifacts
    survival_path = output_root / "tables" / "synthetic_squeeze_survival.csv"
    survival = pl.read_csv(survival_path) if survival_path.exists() else pl.DataFrame()
    rows: list[dict[str, Any]] = []
    for venue in sorted(trades["venue"].unique().to_list()):
        part = trades.filter(pl.col("venue") == venue)
        clusters = (
            part.group_by("entry_signal_ts_ms")
            .agg(pl.col("portfolio_net_return").sum().alias("cluster_return"))
            .sort("entry_signal_ts_ms")
        )
        if clusters.is_empty():
            continue
        values = clusters["cluster_return"].to_numpy().astype(float)
        ts_values = clusters["entry_signal_ts_ms"].to_numpy()
        horizon_days = max((int(ts_values.max()) - int(ts_values.min())) / MS_PER_DAY, 1.0)
        horizon_years = max(horizon_days / ANN_DAYS, 1.0 / ANN_DAYS)
        observed_compound = float(np.prod(1.0 + values) - 1.0)
        for scenario in CLUSTER_RISK_BOOTSTRAP_SCENARIOS:
            tail_loss = _tail_loss_from_survival(survival, venue=str(venue), scenario=scenario)
            if scenario.get("tail_scenario") and tail_loss is None:
                continue
            scenario_seed = int(seed + _stable_seed_offset(str(venue), str(scenario["scenario"])) % 1_000_000_000)
            rng = np.random.default_rng(scenario_seed)
            paths = _cluster_bootstrap_paths(values, scenario=scenario, trials=int(trials), rng=rng)
            _inject_tail_losses(paths, loss=float(tail_loss or 0.0), count=int(scenario.get("tail_count") or 0), rng=rng)
            rows.append(
                _cluster_risk_summary_row(
                    venue=str(venue),
                    scenario=scenario,
                    paths=paths,
                    trials=int(trials),
                    seed=scenario_seed,
                    horizon_years=horizon_years,
                    observed_clusters=int(values.size),
                    observed_cluster_return_sum=float(values.sum()),
                    observed_cluster_compound_return=observed_compound,
                    observed_worst_cluster_return=float(values.min()),
                    tail_loss=tail_loss,
                )
            )
    if rows:
        out = pl.DataFrame(rows).sort(["venue", "scenario"])
        out_path = output_root / "tables" / "cluster_risk_of_ruin.csv"
        _write_df(out, out_path)
        artifacts["cluster_risk_of_ruin"] = str(out_path)
    return artifacts


def _squeeze_scenario_by_name() -> dict[str, dict[str, Any]]:
    return {str(row["scenario"]): dict(row) for row in SYNTHETIC_SQUEEZE_SURVIVAL_SCENARIOS}


def _snapshot_lookup(trades: pl.DataFrame) -> dict[int, list[dict[str, Any]]]:
    return {int(ts): active for ts, active in _active_trade_snapshots(trades)}


def _hit_exposures_for_survival_row(
    row: dict[str, Any],
    *,
    snapshots_by_ts: dict[int, list[dict[str, Any]]],
    scenario: dict[str, Any],
) -> list[tuple[str, float, int]]:
    active = snapshots_by_ts.get(int(row["event_ts_ms"]))
    if not active:
        return []
    exposures = _symbol_exposures(active)
    if bool(scenario.get("all_active")):
        return exposures
    positions_hit = int(scenario.get("positions_hit") or 0)
    return exposures[:positions_hit]


def _reference_close_at_event(bars: pl.DataFrame, event_ts_ms: int) -> float | None:
    if bars.is_empty():
        return None
    prev = bars.filter(pl.col("bar_end_ts_ms") <= event_ts_ms).tail(1)
    if not prev.is_empty():
        return float(prev["close"][0])
    nxt = bars.filter(pl.col("bar_end_ts_ms") > event_ts_ms).head(1)
    return float(nxt["close"][0]) if not nxt.is_empty() else None


def _dynamic_symbol_overlay(
    *,
    symbol: str,
    exposure: float,
    bars: pl.DataFrame | None,
    event_ts_ms: int,
    delay_ms: int,
    symbol_shock_pct: float,
) -> dict[str, Any]:
    start_ms = event_ts_ms + MS_PER_5M
    flatten_ts_ms = event_ts_ms + max(delay_ms, MS_PER_5M)
    if bars is None or bars.is_empty():
        return {
            "symbol": symbol,
            "exposure": exposure,
            "coverage_status": "missing_bars",
            "path_bars": 0,
            "max_path_gap_ms": None,
            "peak_move_pct": symbol_shock_pct,
            "flatten_move_pct": symbol_shock_pct,
        }
    ref = _reference_close_at_event(bars, event_ts_ms)
    if ref is None or ref <= 0.0:
        return {
            "symbol": symbol,
            "exposure": exposure,
            "coverage_status": "missing_reference",
            "path_bars": 0,
            "max_path_gap_ms": None,
            "peak_move_pct": symbol_shock_pct,
            "flatten_move_pct": symbol_shock_pct,
        }
    issue, path_bars, max_gap = _path_coverage_issue(
        bars,
        start_ms,
        flatten_ts_ms,
        interval_ms=MS_PER_5M,
    )
    path = _path_after(bars, start_ms, flatten_ts_ms)
    if path.is_empty():
        return {
            "symbol": symbol,
            "exposure": exposure,
            "coverage_status": issue or "missing_path",
            "path_bars": path_bars,
            "max_path_gap_ms": max_gap,
            "peak_move_pct": symbol_shock_pct,
            "flatten_move_pct": symbol_shock_pct,
        }
    high_ratio = max(float(path["high"].max()) / ref, 1.0)
    flatten_price = float(path["close"].tail(1)[0])
    flatten_ratio = flatten_price / ref
    peak_move = max((1.0 + symbol_shock_pct) * high_ratio - 1.0, symbol_shock_pct)
    flatten_move = (1.0 + symbol_shock_pct) * flatten_ratio - 1.0
    return {
        "symbol": symbol,
        "exposure": exposure,
        "coverage_status": issue or "ok",
        "path_bars": path_bars,
        "max_path_gap_ms": max_gap,
        "reference_price": ref,
        "flatten_price": flatten_price,
        "peak_move_pct": peak_move,
        "flatten_move_pct": flatten_move,
    }


def _dynamic_tail_request_rows(
    survival: pl.DataFrame,
    trades: pl.DataFrame,
    venue: str,
) -> pl.DataFrame:
    scenario_map = _squeeze_scenario_by_name()
    snapshots_by_ts = _snapshot_lookup(trades.filter(pl.col("venue") == venue))
    rows: list[dict[str, Any]] = []
    for survival_row in survival.filter(pl.col("venue") == venue).to_dicts():
        scenario = scenario_map.get(str(survival_row["scenario"]))
        if scenario is None:
            continue
        delay_ms = max(int(scenario.get("outage_minutes") or 0) * MS_PER_MINUTE, MS_PER_5M)
        for symbol, _exposure, _count in _hit_exposures_for_survival_row(
            survival_row,
            snapshots_by_ts=snapshots_by_ts,
            scenario=scenario,
        ):
            rows.append(
                {
                    "symbol": symbol,
                    "entry_ts_ms": int(survival_row["event_ts_ms"]),
                    "exit_ts_ms": int(survival_row["event_ts_ms"]) + delay_ms + MS_PER_5M,
                }
            )
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).unique(["symbol", "entry_ts_ms", "exit_ts_ms"])


def _dynamic_tail_row(
    *,
    survival_row: dict[str, Any],
    scenario: dict[str, Any],
    hit_exposures: list[tuple[str, float, int]],
    bars_by_symbol: dict[str, pl.DataFrame],
    equity: pl.DataFrame,
) -> dict[str, Any] | None:
    if not hit_exposures:
        return None
    event_ts = int(survival_row["event_ts_ms"])
    ctx = _equity_context_at(equity, event_ts)
    if ctx is None:
        return None
    delay_ms = max(int(scenario.get("outage_minutes") or 0) * MS_PER_MINUTE, MS_PER_5M)
    symbol_shock = float(survival_row["symbol_shock_pct"])
    extra_slippage = float(survival_row["extra_slippage_pct"])
    symbol_rows = [
        _dynamic_symbol_overlay(
            symbol=symbol,
            exposure=float(exposure),
            bars=bars_by_symbol.get(symbol),
            event_ts_ms=event_ts,
            delay_ms=delay_ms,
            symbol_shock_pct=symbol_shock,
        )
        for symbol, exposure, _count in hit_exposures
    ]
    hit_exposure = float(sum(float(row["exposure"]) for row in symbol_rows))
    if hit_exposure <= 0.0:
        return None
    active_notional = float(survival_row["active_notional_pct_equity"])
    hedge_offset = float(survival_row["hedge_offset_pct_equity"])
    peak_short_loss = float(sum(float(row["exposure"]) * float(row["peak_move_pct"]) for row in symbol_rows))
    flatten_short_loss = float(sum(float(row["exposure"]) * float(row["flatten_move_pct"]) for row in symbol_rows))
    execution_loss = hit_exposure * extra_slippage
    peak_net_loss = peak_short_loss - hedge_offset
    flatten_net_loss = flatten_short_loss + execution_loss - hedge_offset
    equity_before = float(ctx["equity"])
    peak_equity = float(ctx["peak_equity"])
    maintenance_margin = active_notional * 0.005
    post_peak_equity = equity_before - peak_net_loss
    post_flatten_equity = equity_before - flatten_net_loss
    peak_drawdown = post_peak_equity / peak_equity - 1.0 if peak_equity else None
    flatten_drawdown = post_flatten_equity / peak_equity - 1.0 if peak_equity else None
    missing = [str(row["symbol"]) for row in symbol_rows if row["coverage_status"] != "ok"]
    covered_exposure = float(sum(float(row["exposure"]) for row in symbol_rows if row["coverage_status"] == "ok"))
    liquidated = post_peak_equity <= maintenance_margin
    return {
        "venue": str(survival_row["venue"]),
        "scenario": str(survival_row["scenario"]),
        "placement": str(survival_row["placement"]),
        "event_ts_ms": event_ts,
        "event_date": str(survival_row["event_date"]),
        "active_positions": int(survival_row["active_positions"]),
        "active_symbols": int(survival_row["active_symbols"]),
        "symbols_hit": int(len(hit_exposures)),
        "hit_symbols": ",".join(symbol for symbol, _exposure, _count in hit_exposures[:20]),
        "path_delay_minutes": float(delay_ms / MS_PER_MINUTE),
        "outage_minutes": int(scenario.get("outage_minutes") or 0),
        "symbol_shock_pct": symbol_shock,
        "extra_slippage_pct": extra_slippage,
        "active_notional_pct_equity": active_notional,
        "hit_notional_pct_equity": hit_exposure,
        "covered_hit_notional_pct_equity": covered_exposure,
        "coverage_status": "ok" if not missing else "partial_or_missing_5m",
        "missing_symbols": ",".join(missing),
        "min_path_bars": int(min(int(row["path_bars"]) for row in symbol_rows)),
        "max_path_gap_ms": max(
            [int(row["max_path_gap_ms"]) for row in symbol_rows if row.get("max_path_gap_ms") is not None],
            default=None,
        ),
        "static_net_loss_pct_equity": float(survival_row["net_loss_pct_equity"]),
        "dynamic_peak_short_loss_pct_equity": peak_short_loss,
        "dynamic_flatten_short_loss_pct_equity": flatten_short_loss,
        "dynamic_execution_loss_pct_equity": execution_loss,
        "hedge_offset_pct_equity": hedge_offset,
        "dynamic_peak_net_loss_pct_equity": peak_net_loss,
        "dynamic_flatten_net_loss_pct_equity": flatten_net_loss,
        "dynamic_peak_drawdown_pct": peak_drawdown,
        "dynamic_flatten_drawdown_pct": flatten_drawdown,
        "dynamic_peak_loss_increment_vs_static_pct_equity": peak_net_loss - float(survival_row["net_loss_pct_equity"]),
        "dynamic_flatten_loss_increment_vs_static_pct_equity": flatten_net_loss - float(survival_row["net_loss_pct_equity"]),
        "maintenance_margin_pct_equity": maintenance_margin,
        "margin_peak_pct_equity": active_notional / max(post_peak_equity, 1e-12) if post_peak_equity > 0 else None,
        "liquidation_distance_after_peak_pct": max(post_peak_equity - maintenance_margin, 0.0) / hit_exposure,
        "positions_liquidated_account_level": int(survival_row["active_positions"]) if liquidated else 0,
        "survives_equity_positive": bool(post_peak_equity > 0.0),
        "survives_account_maintenance_proxy": bool(not liquidated),
        "max_symbol_peak_move_pct": float(max(float(row["peak_move_pct"]) for row in symbol_rows)),
        "max_symbol_flatten_move_pct": float(max(float(row["flatten_move_pct"]) for row in symbol_rows)),
        "note": "5m path overlay on synthetic active-book shock; simple account maintenance proxy, not an exchange liquidation engine.",
    }


def write_dynamic_liquidation_outage_tables(output_root: Path, trades: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    survival_path = output_root / "tables" / "synthetic_squeeze_survival.csv"
    if trades.is_empty() or not survival_path.exists():
        return artifacts
    survival = pl.read_csv(survival_path)
    if survival.is_empty():
        return artifacts
    scenario_map = _squeeze_scenario_by_name()
    rows: list[dict[str, Any]] = []
    for venue in sorted(survival["venue"].unique().to_list()):
        venue = str(venue)
        venue_trades = trades.filter(pl.col("venue") == venue)
        if venue_trades.is_empty():
            continue
        requests = _dynamic_tail_request_rows(survival, trades, venue)
        bars_by_symbol = (
            _load_klines_for_rows(
                venue_root(venue),
                requests,
                dataset="klines_5m",
                interval_ms=MS_PER_5M,
                sparse_windows=True,
            )
            if not requests.is_empty()
            else {}
        )
        equity_path = output_root / venue / "continuous_equity.csv"
        if not equity_path.exists():
            continue
        equity = pl.read_csv(equity_path)
        snapshots_by_ts = _snapshot_lookup(venue_trades)
        for survival_row in survival.filter(pl.col("venue") == venue).to_dicts():
            scenario = scenario_map.get(str(survival_row["scenario"]))
            if scenario is None:
                continue
            hit_exposures = _hit_exposures_for_survival_row(
                survival_row,
                snapshots_by_ts=snapshots_by_ts,
                scenario=scenario,
            )
            row = _dynamic_tail_row(
                survival_row=survival_row,
                scenario=scenario,
                hit_exposures=hit_exposures,
                bars_by_symbol=bars_by_symbol,
                equity=equity,
            )
            if row is not None:
                rows.append(row)
    if rows:
        out = pl.DataFrame(rows).sort(["venue", "scenario", "placement"])
        out_path = output_root / "tables" / "dynamic_liquidation_outage.csv"
        _write_df(out, out_path)
        artifacts["dynamic_liquidation_outage"] = str(out_path)
    return artifacts


OVERFIT_REPLAY_TABLES = (
    "timing_portfolio_replay.csv",
    "stop_portfolio_replay.csv",
    "regime_portfolio_replay.csv",
    "skip_portfolio_replay.csv",
    "scale_in_portfolio_replay.csv",
)


def _resolve_existing_artifact_path(raw: Any, output_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text)
    candidates = [path] if path.is_absolute() else [REPO_ROOT / path, output_root / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else None


def _equity_path_from_summary_row(row: dict[str, Any], output_root: Path) -> Path | None:
    summary_path = _resolve_existing_artifact_path(row.get("summary_path"), output_root)
    if summary_path is not None:
        candidate = summary_path.parent / "continuous_equity.csv"
        if candidate.exists():
            return candidate
    artifact_root = _resolve_existing_artifact_path(row.get("artifact_root"), output_root)
    venue = str(row.get("venue") or "").strip()
    if artifact_root is not None and venue:
        for candidate in (artifact_root / venue / "continuous_equity.csv", artifact_root / "continuous_equity.csv"):
            if candidate.exists():
                return candidate
    return None


def _read_equity_returns(path: Path) -> pl.DataFrame:
    if not path.exists():
        return pl.DataFrame()
    frame = pl.read_csv(path)
    if "basket_return" not in frame.columns:
        return pl.DataFrame()
    if "date" not in frame.columns and "ts_ms" in frame.columns:
        frame = frame.with_columns(
            pl.from_epoch(pl.col("ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%d").alias("date")
        )
    if "date" not in frame.columns:
        return pl.DataFrame()
    return (
        frame.select(["date", pl.col("basket_return").cast(pl.Float64)])
        .filter(pl.col("basket_return").is_not_null())
        .sort("date")
    )


def _annualized_sharpe(values: np.ndarray) -> float | None:
    vals = values[np.isfinite(values)]
    if vals.size < 2:
        return None
    std = float(vals.std(ddof=1))
    if std <= 1e-15:
        return None
    return float(vals.mean() / std * math.sqrt(ANN_DAYS))


def _return_moments(values: np.ndarray) -> tuple[int, float, float]:
    vals = values[np.isfinite(values)]
    if vals.size < 2:
        return int(vals.size), 0.0, 3.0
    centered = vals - float(vals.mean())
    std = float(vals.std(ddof=0))
    if std <= 1e-15:
        return int(vals.size), 0.0, 3.0
    z = centered / std
    return int(vals.size), float(np.mean(z**3)), float(np.mean(z**4))


def _overfit_float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        out = float(value)
        return out if math.isfinite(out) else 0.0
    except Exception:
        return 0.0


def _overfit_int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(float(value))
    except Exception:
        return 0


def _expected_max_sharpe_threshold(sharpes: list[float]) -> float:
    finite = np.asarray([value for value in sharpes if math.isfinite(value)], dtype=float)
    if finite.size <= 1:
        return 0.0
    std = float(finite.std(ddof=1))
    if std <= 1e-15:
        return float(finite.mean())
    trials = int(finite.size)
    euler_gamma = 0.5772156649015329
    q1 = NORMAL.inv_cdf(1.0 - 1.0 / trials)
    q2 = NORMAL.inv_cdf(1.0 - 1.0 / (trials * math.e))
    return float(finite.mean() + std * ((1.0 - euler_gamma) * q1 + euler_gamma * q2))


def _deflated_sharpe_probability(
    *,
    sharpe_ann: float,
    threshold_ann: float,
    observations: int,
    skew: float,
    kurtosis: float,
) -> float | None:
    if observations < 3 or not math.isfinite(sharpe_ann) or not math.isfinite(threshold_ann):
        return None
    sr = sharpe_ann / math.sqrt(ANN_DAYS)
    threshold = threshold_ann / math.sqrt(ANN_DAYS)
    denominator = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr
    if denominator <= 1e-15:
        return None
    z = (sr - threshold) * math.sqrt(observations - 1.0) / math.sqrt(denominator)
    return float(NORMAL.cdf(z))


def _load_overfit_variant_universe(output_root: Path, venues: list[str]) -> pl.DataFrame:
    tables = output_root / "tables"
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    allowed_venues = set(venues)
    for table_name in OVERFIT_REPLAY_TABLES:
        path = tables / table_name
        if not path.exists():
            continue
        frame = pl.read_csv(path)
        required = {"venue", "variant", "sharpe_daily_ann", "summary_path", "artifact_root"}
        if not required <= set(frame.columns):
            continue
        family = table_name.removesuffix(".csv")
        for row in frame.to_dicts():
            venue = str(row.get("venue") or "")
            variant = str(row.get("variant") or "")
            if not venue or venue not in allowed_venues or not variant:
                continue
            equity_path = _equity_path_from_summary_row(row, output_root)
            if equity_path is None or not equity_path.exists():
                continue
            key = (venue, variant)
            if key in rows_by_key:
                continue
            returns = _read_equity_returns(equity_path)
            if returns.is_empty():
                continue
            values = returns["basket_return"].to_numpy()
            observations, skew, kurtosis = _return_moments(values)
            sharpe_ann = _overfit_float(row.get("sharpe_daily_ann"))
            if sharpe_ann == 0.0:
                computed = _annualized_sharpe(values)
                sharpe_ann = float(computed) if computed is not None else 0.0
            rows_by_key[key] = {
                "venue": venue,
                "variant": variant,
                "family": "baseline" if variant == "baseline_current" else family,
                "variant_kind": str(row.get("variant_kind") or ""),
                "component_trades": _overfit_int(row.get("component_trades")),
                "full_return_pct": _overfit_float(row.get("full_return_pct")),
                "max_drawdown_pct": _overfit_float(row.get("max_drawdown_pct")),
                "mar": _overfit_float(row.get("mar")),
                "sharpe_daily_ann": sharpe_ann,
                "worst_day_pct": _overfit_float(row.get("worst_day_pct")),
                "observations": observations,
                "skew": skew,
                "kurtosis": kurtosis,
                "equity_path": str(equity_path),
                "summary_path": str(_resolve_existing_artifact_path(row.get("summary_path"), output_root) or ""),
            }
    if not rows_by_key:
        return pl.DataFrame()
    return pl.DataFrame(list(rows_by_key.values()), infer_schema_length=None).sort(["venue", "variant"])


def _returns_by_variant(variant_rows: pl.DataFrame) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for row in variant_rows.to_dicts():
        returns = _read_equity_returns(Path(str(row["equity_path"])))
        out[str(row["variant"])] = {
            str(item["date"]): float(item["basket_return"])
            for item in returns.to_dicts()
            if item.get("date") is not None
        }
    return out


def _empty_pbo_summary(venue: str, variants: int, observations: int, partitions: int) -> dict[str, Any]:
    return {
        "venue": venue,
        "variants": variants,
        "common_observations": observations,
        "partitions": partitions,
        "splits": 0,
        "pbo": None,
        "median_logit_rank": None,
        "median_oos_rank_pct": None,
        "median_selected_train_sharpe": None,
        "median_selected_test_sharpe": None,
        "most_selected_variant": "",
        "most_selected_count": 0,
    }


def _pbo_cscv_for_venue(
    venue_rows: pl.DataFrame,
    *,
    partitions: int = 8,
) -> tuple[dict[str, Any], pl.DataFrame]:
    venue = str(venue_rows["venue"][0]) if not venue_rows.is_empty() else ""
    variants = [str(value) for value in venue_rows["variant"].to_list()]
    series = _returns_by_variant(venue_rows)
    if len(variants) < 2 or not series:
        return _empty_pbo_summary(venue, len(variants), 0, partitions), pl.DataFrame()
    common_dates = set.intersection(*(set(values) for values in series.values()))
    dates = sorted(common_dates)
    if len(dates) < partitions * 2:
        return _empty_pbo_summary(venue, len(variants), len(dates), partitions), pl.DataFrame()
    matrix = np.asarray([[series[variant][date] for variant in variants] for date in dates], dtype=float)
    partition_indices = np.array_split(np.arange(len(dates)), partitions)
    split_rows: list[dict[str, Any]] = []
    split_id = 0
    for train_parts in itertools.combinations(range(partitions), partitions // 2):
        train_set = set(train_parts)
        train_idx = np.concatenate([partition_indices[idx] for idx in train_parts])
        test_idx = np.concatenate([partition_indices[idx] for idx in range(partitions) if idx not in train_set])
        train_sharpes = [_annualized_sharpe(matrix[train_idx, col]) for col in range(len(variants))]
        test_sharpes = [_annualized_sharpe(matrix[test_idx, col]) for col in range(len(variants))]
        finite_train = [value if value is not None else -math.inf for value in train_sharpes]
        if all(not math.isfinite(value) for value in finite_train):
            continue
        selected_idx = int(np.argmax(np.asarray(finite_train, dtype=float)))
        selected_test = test_sharpes[selected_idx]
        finite_test = [value for value in test_sharpes if value is not None and math.isfinite(value)]
        if selected_test is None or not math.isfinite(selected_test) or not finite_test:
            rank_pct = None
            logit_rank = None
        else:
            below_or_equal = sum(1 for value in finite_test if value <= selected_test)
            rank_pct = min(max((below_or_equal - 0.5) / len(finite_test), 1e-6), 1.0 - 1e-6)
            logit_rank = math.log(rank_pct / (1.0 - rank_pct))
        split_rows.append(
            {
                "venue": venue,
                "split_id": split_id,
                "train_partitions": ",".join(str(idx) for idx in train_parts),
                "test_partitions": ",".join(str(idx) for idx in range(partitions) if idx not in train_set),
                "selected_variant": variants[selected_idx],
                "selected_train_sharpe": train_sharpes[selected_idx],
                "selected_test_sharpe": selected_test,
                "selected_test_rank_pct": rank_pct,
                "logit_rank": logit_rank,
            }
        )
        split_id += 1
    splits = pl.DataFrame(split_rows, infer_schema_length=None)
    if splits.is_empty():
        return _empty_pbo_summary(venue, len(variants), len(dates), partitions), splits
    valid = splits.filter(pl.col("selected_test_rank_pct").is_not_null())
    selected_counts = Counter(str(value) for value in splits["selected_variant"].to_list())
    most_selected_variant, most_selected_count = selected_counts.most_common(1)[0]
    summary = {
        "venue": venue,
        "variants": len(variants),
        "common_observations": len(dates),
        "partitions": partitions,
        "splits": splits.height,
        "pbo": (
            float((valid["selected_test_rank_pct"] < 0.5).sum() / valid.height)
            if not valid.is_empty()
            else None
        ),
        "median_logit_rank": float(valid["logit_rank"].median()) if not valid.is_empty() else None,
        "median_oos_rank_pct": (
            float(valid["selected_test_rank_pct"].median()) if not valid.is_empty() else None
        ),
        "median_selected_train_sharpe": (
            float(valid["selected_train_sharpe"].median()) if not valid.is_empty() else None
        ),
        "median_selected_test_sharpe": (
            float(valid["selected_test_sharpe"].median()) if not valid.is_empty() else None
        ),
        "most_selected_variant": most_selected_variant,
        "most_selected_count": int(most_selected_count),
    }
    return summary, splits


def write_overfit_diagnostics(output_root: Path, venues: list[str]) -> dict[str, str]:
    tables = output_root / "tables"
    artifacts: dict[str, str] = {}
    variants = _load_overfit_variant_universe(output_root, venues)
    if variants.is_empty():
        return artifacts
    variant_path = tables / "overfit_variant_universe.csv"
    _write_df(variants, variant_path)
    artifacts["overfit_variant_universe"] = str(variant_path)

    dsr_rows: list[dict[str, Any]] = []
    pbo_rows: list[dict[str, Any]] = []
    split_frames: list[pl.DataFrame] = []
    for venue in venues:
        venue_rows = variants.filter(pl.col("venue") == venue)
        if venue_rows.is_empty():
            continue
        sharpe_daily_trials = [
            float(value) / math.sqrt(ANN_DAYS)
            for value in venue_rows["sharpe_daily_ann"].to_list()
            if value is not None and math.isfinite(float(value))
        ]
        threshold_daily = _expected_max_sharpe_threshold(sharpe_daily_trials)
        threshold_ann = threshold_daily * math.sqrt(ANN_DAYS)
        best_sharpe = float(venue_rows["sharpe_daily_ann"].max())
        for row in venue_rows.to_dicts():
            prob = _deflated_sharpe_probability(
                sharpe_ann=float(row["sharpe_daily_ann"]),
                threshold_ann=threshold_ann,
                observations=int(row["observations"]),
                skew=float(row["skew"]),
                kurtosis=float(row["kurtosis"]),
            )
            dsr_rows.append(
                {
                    **row,
                    "trial_count": len(sharpe_daily_trials),
                    "trial_sharpe_daily_mean": float(np.mean(sharpe_daily_trials)),
                    "trial_sharpe_daily_std": (
                        float(np.std(sharpe_daily_trials, ddof=1)) if len(sharpe_daily_trials) > 1 else 0.0
                    ),
                    "dsr_threshold_daily": threshold_daily,
                    "dsr_threshold_ann": threshold_ann,
                    "dsr_probability": prob,
                    "dsr_pass_95": bool(prob is not None and prob >= 0.95),
                    "is_baseline": row["variant"] == "baseline_current",
                    "is_best_sharpe": abs(float(row["sharpe_daily_ann"]) - best_sharpe) <= 1e-12,
                }
            )
        pbo_summary, splits = _pbo_cscv_for_venue(venue_rows)
        pbo_rows.append(pbo_summary)
        if not splits.is_empty():
            split_frames.append(splits)

    dsr = pl.DataFrame(dsr_rows, infer_schema_length=None).sort(["venue", "variant"])
    dsr_path = tables / "deflated_sharpe.csv"
    _write_df(dsr, dsr_path)
    artifacts["deflated_sharpe"] = str(dsr_path)

    pbo = pl.DataFrame(pbo_rows, infer_schema_length=None).sort("venue")
    summary_rows: list[dict[str, Any]] = []
    for row in pbo.to_dicts():
        venue = str(row["venue"])
        venue_dsr = dsr.filter(pl.col("venue") == venue)
        baseline = _first_row(venue_dsr, variant="baseline_current")
        best = venue_dsr.sort("sharpe_daily_ann", descending=True).row(0, named=True)
        pbo_value = row.get("pbo")
        baseline_prob = baseline.get("dsr_probability") if baseline else None
        verdict = "incomplete"
        if pbo_value is not None and baseline_prob is not None:
            if float(pbo_value) >= 0.20 or float(baseline_prob) < 0.95:
                verdict = "fragile_internal_surface"
            else:
                verdict = "no_obvious_dsr_pbo_failure"
        summary_rows.append(
            {
                **row,
                "baseline_sharpe_ann": baseline.get("sharpe_daily_ann") if baseline else None,
                "baseline_dsr_probability": baseline_prob,
                "best_sharpe_variant": best["variant"],
                "best_sharpe_ann": best["sharpe_daily_ann"],
                "best_dsr_probability": best["dsr_probability"],
                "verdict": verdict,
                "note": (
                    "Frozen full-replay artifact diagnostic only; not a new sweep, "
                    "not OOS, and not promotion evidence."
                ),
            }
        )
    pbo_summary = pl.DataFrame(summary_rows, infer_schema_length=None).sort("venue")
    pbo_path = tables / "pbo_cscv_summary.csv"
    _write_df(pbo_summary, pbo_path)
    artifacts["pbo_cscv_summary"] = str(pbo_path)

    if split_frames:
        splits_path = tables / "pbo_cscv_splits.csv"
        _write_df(pl.concat(split_frames, how="diagonal_relaxed").sort(["venue", "split_id"]), splits_path)
        artifacts["pbo_cscv_splits"] = str(splits_path)
    return artifacts


def refresh_report_from_existing_artifacts(
    output_root: Path,
    venues: list[str],
    extra_artifacts: dict[str, str] | None = None,
) -> Path:
    tables = output_root / "tables"
    metadata = build_metadata(output_root, venues)
    metrics = json.loads((tables / "baseline_metrics.json").read_text(encoding="utf-8"))
    leakage = json.loads((tables / "leakage_audit.json").read_text(encoding="utf-8"))
    artifact_path = tables / "artifact_index.json"
    artifacts = json.loads(artifact_path.read_text(encoding="utf-8")) if artifact_path.exists() else {}
    if extra_artifacts:
        artifacts.update(extra_artifacts)
    artifact_path.write_text(json.dumps(artifacts, indent=2, default=str), encoding="utf-8")
    write_report(output_root, metadata, metrics, artifacts, leakage)
    return output_root / "reports" / "final_research_report.md"


def _first_row(df: pl.DataFrame, **filters: Any) -> dict[str, Any] | None:
    part = df
    for col, value in filters.items():
        part = part.filter(pl.col(col) == value)
    return part.to_dicts()[0] if not part.is_empty() else None


def report_findings(output_root: Path, venues: list[str]) -> list[str]:
    tables = output_root / "tables"
    findings: list[str] = []
    mae_path = tables / "mae_conditional_recovery.csv"
    if mae_path.exists():
        mae = pl.read_csv(mae_path)
        for venue in venues:
            row20 = _first_row(mae, venue=venue, mae_threshold_reached=0.20)
            row40 = _first_row(mae, venue=venue, mae_threshold_reached=0.40)
            if row20:
                findings.append(
                    f"{venue}: {int(row20['trades'])} trades reached >=20% MAE; "
                    f"{_fmt_pct(row20['eventually_profitable'])} still finished profitable "
                    f"(5% tail {_fmt_pct(row20['tail_5pct'])})."
                )
            if row40:
                findings.append(
                    f"{venue}: {int(row40['trades'])} trades reached >=40% MAE; "
                    f"{_fmt_pct(row40['eventually_profitable'])} still finished profitable."
                )
    stop_path = tables / "stop_frontier.csv"
    if stop_path.exists():
        stops = pl.read_csv(stop_path)
        for venue in venues:
            none = _first_row(stops, venue=venue, stop="none")
            stop20 = _first_row(stops, venue=venue, stop="0.2")
            if none and stop20:
                findings.append(
                    f"{venue}: a 20% stop changes diagnostic component net from "
                    f"{_fmt_pct(none['net_pnl'])} to {_fmt_pct(stop20['net_pnl'])}; "
                    f"post-stop original-TP hit rate {_fmt_pct(stop20['post_stop_tp_hit_rate'])}."
                )
    stop_replay_path = tables / "stop_portfolio_replay.csv"
    if stop_replay_path.exists():
        replay = pl.read_csv(stop_replay_path)
        for venue in venues:
            base = _first_row(replay, venue=venue, variant="baseline_current")
            if not base:
                continue
            stop_rows = replay.filter((pl.col("venue") == venue) & (pl.col("variant_kind") == "fixed_stop"))
            parts = []
            for row in stop_rows.sort("stop_loss_pct").iter_rows(named=True):
                parts.append(
                    f"{float(row['stop_loss_pct']) * 100:.0f}% {float(row['full_return_pct']):.2f}%/"
                    f"MAR {float(row['mar']):.2f}, DD {float(row['max_drawdown_pct']):.2f}%"
                )
            if parts:
                findings.append(
                    f"{venue}: full portfolio fixed-stop replays versus baseline "
                    f"{float(base['full_return_pct']):.2f}%/MAR {float(base['mar']):.2f}, "
                    f"DD {float(base['max_drawdown_pct']):.2f}%: "
                    + "; ".join(parts)
                    + "."
                )
    scale_in_path = tables / "conditional_scale_in_summary.csv"
    if scale_in_path.exists():
        scale = pl.read_csv(scale_in_path)
        for venue in venues:
            venue_rows = scale.filter(pl.col("venue") == venue)
            if venue_rows.is_empty():
                continue
            best = venue_rows.sort("combined_net_return", descending=True).row(0, named=True)
            findings.append(
                f"{venue}: best diagnostic conditional scale-in arm "
                f"trigger {float(best['trigger_mae_pct']) * 100:.0f}% / "
                f"{float(best['addon_fraction_of_primary']) * 100:.0f}% add-on changes component net "
                f"from {_fmt_pct(best['primary_net_return'])} to {_fmt_pct(best['combined_net_return'])}; "
                f"fill rate {_fmt_pct(best['fill_rate'])}. This is not a full portfolio replay."
            )
    scale_in_replay_path = tables / "scale_in_portfolio_replay.csv"
    if scale_in_replay_path.exists():
        replay = pl.read_csv(scale_in_replay_path)
        for venue in venues:
            base = _first_row(replay, venue=venue, variant="baseline_current")
            venue_rows = replay.filter((pl.col("venue") == venue) & (pl.col("variant") != "baseline_current"))
            if not base or venue_rows.is_empty():
                continue
            best_mar = venue_rows.sort("mar", descending=True).row(0, named=True)
            best_return = venue_rows.sort("full_return_pct", descending=True).row(0, named=True)
            failing = venue_rows.filter((pl.col("mar_delta") <= 0.0) | (pl.col("drawdown_delta_pct") < 0.0))
            verdict = "no replay arm clears MAR+drawdown improvement" if failing.height == venue_rows.height else "at least one arm clears MAR+drawdown improvement"
            findings.append(
                f"{venue}: scale-in portfolio replay baseline "
                f"{float(base['full_return_pct']):.2f}%/MAR {float(base['mar']):.2f}, "
                f"DD {float(base['max_drawdown_pct']):.2f}%; best MAR arm `{best_mar['variant']}` "
                f"{float(best_mar['full_return_pct']):.2f}%/MAR {float(best_mar['mar']):.2f}, "
                f"DD {float(best_mar['max_drawdown_pct']):.2f}% with "
                f"{int(best_mar['child_trades'])} child trades; best return arm `{best_return['variant']}` "
                f"{float(best_return['full_return_pct']):.2f}%/MAR {float(best_return['mar']):.2f}. "
                f"{verdict}; still exploratory."
            )
    invalidation_path = tables / "signal_invalidation_summary.csv"
    if invalidation_path.exists():
        invalidation = pl.read_csv(invalidation_path)
        for venue in venues:
            venue_rows = invalidation.filter(pl.col("venue") == venue)
            if venue_rows.is_empty():
                continue
            active_rows = venue_rows.filter(pl.col("invalidations") > 0)
            zero_hit = venue_rows.filter(pl.col("invalidations") == 0)
            parts = []
            if not active_rows.is_empty():
                best_active = active_rows.sort("scenario_net_return", descending=True).row(0, named=True)
                max_delta = float(active_rows["delta_net_return"].max())
                verdict = "all active arms hurt component net" if max_delta < 0.0 else "at least one active arm improved component net"
                parts.append(
                    f"best active arm `{best_active['rule']}` changes component net from "
                    f"{_fmt_pct(best_active['original_net_return'])} to {_fmt_pct(best_active['scenario_net_return'])} "
                    f"with invalidation rate {_fmt_pct(best_active['invalidation_rate'])}; {verdict}"
                )
            if not zero_hit.is_empty():
                zero_rules = ", ".join(str(rule) for rule in zero_hit["rule"].to_list())
                parts.append(f"zero-hit arm(s): {zero_rules}")
            if parts:
                findings.append(f"{venue}: candidate-tape signal-invalidation diagnostic: {'; '.join(parts)}. This is not a full portfolio replay.")
    state_panel_path = tables / "signal_invalidation_state_panel_summary.csv"
    if state_panel_path.exists():
        state_panel = pl.read_csv(state_panel_path)
        for venue in venues:
            row = _first_row(state_panel, venue=venue)
            if not row:
                continue
            findings.append(
                f"{venue}: signal-invalidation hourly state coverage: "
                f"price {_fmt_pct(row.get('price_coverage'))}, "
                f"open interest {_fmt_pct(row.get('open_interest_coverage'))}, "
                f"funding {_fmt_pct(row.get('funding_coverage'))}, "
                f"BTC state {_fmt_pct(row.get('btc_state_coverage'))}, "
                f"candidate-state {_fmt_pct(row.get('candidate_state_coverage'))} over "
                f"{int(row.get('state_rows', 0))} state rows / {int(row.get('trades', 0))} trades; "
                f"spread/depth {_fmt_pct(row.get('spread_depth_coverage'))}, "
                f"sector proxy {_fmt_pct(row.get('sector_proxy_coverage'))}; "
                f"full panel ready={bool(row.get('full_hourly_state_panel_ready'))}. "
                "Coverage audit only; not live invalidation-exit evidence."
            )
    pbo_path = tables / "pbo_cscv_summary.csv"
    if pbo_path.exists():
        pbo = pl.read_csv(pbo_path)
        for venue in venues:
            row = _first_row(pbo, venue=venue)
            if not row:
                continue
            findings.append(
                f"{venue}: DSR/PBO frozen-artifact diagnostic across "
                f"{int(row.get('variants', 0))} full-replay variants: PBO "
                f"{_fmt_pct(row.get('pbo'))}, median OOS rank "
                f"{_fmt_pct(row.get('median_oos_rank_pct'))}, baseline DSR "
                f"{_fmt_pct(row.get('baseline_dsr_probability'))}, best Sharpe variant "
                f"`{row.get('best_sharpe_variant')}` "
                f"({float(row.get('best_sharpe_ann') or 0.0):.2f} ann Sharpe); "
                f"verdict `{row.get('verdict')}`. This is internal inference-risk telemetry, not OOS."
            )
    timing_path = tables / "timing_by_original_signal.csv"
    if timing_path.exists():
        timing = pl.read_csv(timing_path)
        for venue in venues:
            imm = _first_row(timing, venue=venue, method="immediate")
            delay15 = _first_row(timing, venue=venue, method="delay_15m")
            delay30 = _first_row(timing, venue=venue, method="delay_30m")
            delay1 = _first_row(timing, venue=venue, method="delay_1h")
            red15 = _first_row(timing, venue=venue, method="next_red_15m")
            adv1 = _first_row(timing, venue=venue, method="adverse_1pct")
            if imm and delay15 and delay30:
                findings.append(
                    f"{venue}: 5m delay diagnostics PnL/signal are "
                    f"15m {_fmt_pct(delay15['unit_pnl_per_signal'])} and "
                    f"30m {_fmt_pct(delay30['unit_pnl_per_signal'])} versus immediate "
                    f"{_fmt_pct(imm['unit_pnl_per_signal'])}; complete-path exclusions "
                    f"{int(delay15['coverage_excluded'])}/{int(delay30['coverage_excluded'])}."
                )
            if imm and delay1:
                findings.append(
                    f"{venue}: immediate unit PnL/signal {_fmt_pct(imm['unit_pnl_per_signal'])}; "
                    f"1h delay {_fmt_pct(delay1['unit_pnl_per_signal'])} with "
                    f"{int(delay1['missed_winners'])} missed immediate winners."
                )
            if imm and red15:
                findings.append(
                    f"{venue}: next-red 15m diagnostic fill rate {_fmt_pct(red15['fill_rate'])}, "
                    f"unit PnL/signal {_fmt_pct(red15['unit_pnl_per_signal'])}; "
                    f"{int(red15['coverage_excluded'])} rows excluded for incomplete 5m paths."
                )
            if imm and adv1:
                findings.append(
                    f"{venue}: +1% adverse-limit fill rate {_fmt_pct(adv1['fill_rate'])}, "
                    f"unit PnL/signal {_fmt_pct(adv1['unit_pnl_per_signal'])} versus immediate "
                    f"{_fmt_pct(imm['unit_pnl_per_signal'])}."
                )
    timing_replay_path = tables / "timing_portfolio_replay.csv"
    if timing_replay_path.exists():
        replay = pl.read_csv(timing_replay_path)
        for venue in venues:
            base = _first_row(replay, venue=venue, variant="baseline_current")
            if not base:
                continue
            variants = replay.filter(pl.col("venue") == venue)
            delay_variants = variants.filter(pl.col("variant").str.starts_with("delay_"))
            parts = []
            for row in delay_variants.sort("entry_delay_hours").iter_rows(named=True):
                parts.append(
                    f"+{int(row['added_delay_vs_baseline_hours'])}h {float(row['full_return_pct']):.2f}%/"
                    f"MAR {float(row['mar']):.2f}"
                )
            if parts:
                findings.append(
                    f"{venue}: full portfolio delay replays trail baseline "
                    f"{float(base['full_return_pct']):.2f}%/MAR {float(base['mar']):.2f}: "
                    + "; ".join(parts)
                    + "."
                )
            adverse = _first_row(replay, venue=venue, variant="adverse_limit_1pct")
            if adverse:
                findings.append(
                    f"{venue}: adverse-limit +1% portfolio replay changes baseline "
                    f"{float(base['full_return_pct']):.2f}%/MAR {float(base['mar']):.2f} to "
                    f"{float(adverse['full_return_pct']):.2f}%/MAR {float(adverse['mar']):.2f}; "
                    f"component trades {int(adverse['component_trades'])} versus baseline "
                    f"{int(base['component_trades'])}."
                )
    regime_replay_path = tables / "regime_portfolio_replay.csv"
    if regime_replay_path.exists():
        replay = pl.read_csv(regime_replay_path)
        for venue in venues:
            base = _first_row(replay, venue=venue, variant="baseline_current")
            if not base:
                continue
            off = _first_row(replay, venue=venue, variant="btc_gate_off")
            if off:
                findings.append(
                    f"{venue}: BTC gate-off portfolio replay changes baseline "
                    f"{float(base['full_return_pct']):.2f}%/MAR {float(base['mar']):.2f} to "
                    f"{float(off['full_return_pct']):.2f}%/MAR {float(off['mar']):.2f}; "
                    f"component trades {int(off['component_trades'])} versus baseline "
                    f"{int(base['component_trades'])}."
                )
            lookbacks = replay.filter((pl.col("venue") == venue) & (pl.col("variant_kind") == "lookback"))
            if not lookbacks.is_empty():
                best = lookbacks.sort("mar", descending=True).to_dicts()[0]
                worst = lookbacks.sort("mar").to_dicts()[0]
                rows = lookbacks.select("btc_trend_lookback_days", "full_return_pct", "mar", "max_drawdown_pct")
                mar_values = rows["mar"].drop_nulls()
                if mar_values.is_empty():
                    mar_range = "n/a"
                else:
                    mar_range = f"{float(mar_values.min()):.2f}-{float(mar_values.max()):.2f}"
                findings.append(
                    f"{venue}: BTC lookback portfolio grid spans {lookbacks.height} non-30d arms; "
                    f"MAR range {mar_range}. Best {int(best['btc_trend_lookback_days'])}d "
                    f"{float(best['full_return_pct']):.2f}%/MAR {float(best['mar']):.2f}; "
                    f"worst {int(worst['btc_trend_lookback_days'])}d "
                    f"{float(worst['full_return_pct']):.2f}%/MAR {float(worst['mar']):.2f}."
                )
    skip_replay_path = tables / "skip_portfolio_replay.csv"
    if skip_replay_path.exists():
        replay = pl.read_csv(skip_replay_path)
        for venue in venues:
            base = _first_row(replay, venue=venue, variant="baseline_current")
            skip = _first_row(replay, venue=venue, variant="skip_btc_tail_035")
            if base and skip:
                base_trades = int(base["component_trades"])
                skip_trades = int(skip["component_trades"])
                removed = max(base_trades - skip_trades, 0)
                removed_frac = removed / base_trades if base_trades > 0 else 0.0
                findings.append(
                    f"{venue}: BTC-risk <=35% size skip portfolio replay changes baseline "
                    f"{float(base['full_return_pct']):.2f}%/MAR {float(base['mar']):.2f}, "
                    f"DD {float(base['max_drawdown_pct']):.2f}% to "
                    f"{float(skip['full_return_pct']):.2f}%/MAR {float(skip['mar']):.2f}, "
                    f"DD {float(skip['max_drawdown_pct']):.2f}%; component trades "
                    f"{skip_trades} versus {base_trades} ({_fmt_pct(removed_frac)} removed)."
                )
    cost_path = tables / "cost_funding_scenarios.csv"
    if cost_path.exists():
        costs = pl.read_csv(cost_path)
        for venue in venues:
            base = _first_row(costs, venue=venue, scenario="baseline")
            plus10 = _first_row(costs, venue=venue, scenario="plus_10bps")
            fund2 = _first_row(costs, venue=venue, scenario="funding_2x")
            if base and plus10:
                findings.append(
                    f"{venue}: +10 bps extra execution cost cuts diagnostic component net from "
                    f"{_fmt_pct(base['net_pnl'])} to {_fmt_pct(plus10['net_pnl'])}."
                )
            if base and fund2:
                findings.append(
                    f"{venue}: 2x funding stress cuts diagnostic component net to {_fmt_pct(fund2['net_pnl'])}."
                )
    tail_path = tables / "synthetic_squeeze_static_heat.csv"
    if tail_path.exists():
        tail = pl.read_csv(tail_path)
        for venue in venues:
            row = _first_row(tail, venue=venue, scenario="one_coin_100pct")
            if row:
                findings.append(
                    f"{venue}: static current-size one-coin +100% shock loss is "
                    f"{_fmt_pct(row['shock_loss_pct_equity'])} of equity on the largest sampled exposure."
                )
    survival_path = tables / "synthetic_squeeze_survival.csv"
    if survival_path.exists():
        survival = pl.read_csv(survival_path)
        for venue in venues:
            row = _first_row(survival, venue=venue, scenario="one_coin_100pct", placement="worst_active")
            outage = _first_row(
                survival,
                venue=venue,
                scenario="exchange_down_1h_one_coin_100pct",
                placement="worst_active",
            )
            if row:
                findings.append(
                    f"{venue}: worst active one-coin +100% squeeze diagnostic loses "
                    f"{_fmt_pct(row['net_loss_pct_equity'])}, post-event DD "
                    f"{_fmt_pct(row['post_event_drawdown_pct'])}, remaining liquidation distance "
                    f"{_fmt_pct(row['liquidation_distance_after_shock_pct'])}."
                )
            if outage:
                findings.append(
                    f"{venue}: adding 1h exchange outage/10% exit damage to the worst active +100% squeeze lifts "
                    f"diagnostic loss to {_fmt_pct(outage['net_loss_pct_equity'])}."
                )
    cluster_risk_path = tables / "cluster_risk_of_ruin.csv"
    if cluster_risk_path.exists():
        risk = pl.read_csv(cluster_risk_path)
        for venue in venues:
            base = _first_row(risk, venue=venue, scenario="cluster_bootstrap")
            outage = _first_row(risk, venue=venue, scenario="tail_injected_one_worst_outage_100pct")
            overweight = _first_row(risk, venue=venue, scenario="worst_cluster_overweighted_3x")
            if base:
                findings.append(
                    f"{venue}: 10k cluster bootstrap risk has p(DD>=5%) "
                    f"{_fmt_pct(base['prob_drawdown_5pct'])}, p(DD>=10%) "
                    f"{_fmt_pct(base['prob_drawdown_10pct'])}, p(DD>=20%) "
                    f"{_fmt_pct(base['prob_drawdown_20pct'])}; annual-return p1 "
                    f"{_fmt_pct(base['annual_return_p01'])}."
                )
            if outage:
                findings.append(
                    f"{venue}: adding one worst active +100% outage shock to each cluster-bootstrap path gives "
                    f"p(DD>=5%) {_fmt_pct(outage['prob_drawdown_5pct'])}, p(DD>=10%) "
                    f"{_fmt_pct(outage['prob_drawdown_10pct'])}, account-impairment p "
                    f"{_fmt_pct(outage['prob_account_impairment_50pct_equity'])}."
                )
            if overweight:
                findings.append(
                    f"{venue}: worst-5% cluster 3x overweight stress gives p(DD>=10%) "
                    f"{_fmt_pct(overweight['prob_drawdown_10pct'])}, p(DD>=20%) "
                    f"{_fmt_pct(overweight['prob_drawdown_20pct'])}, annual-return p1 "
                    f"{_fmt_pct(overweight['annual_return_p01'])}; this is fragility evidence, not a sizing pass."
                )
    dynamic_tail_path = tables / "dynamic_liquidation_outage.csv"
    if dynamic_tail_path.exists():
        dynamic = pl.read_csv(dynamic_tail_path)
        for venue in venues:
            venue_rows = dynamic.filter(pl.col("venue") == venue)
            if venue_rows.is_empty():
                continue
            worst = venue_rows.sort("dynamic_peak_drawdown_pct").row(0, named=True)
            worst_flatten = venue_rows.sort("dynamic_flatten_net_loss_pct_equity", descending=True).row(0, named=True)
            liquidation_rows = venue_rows.filter(~pl.col("survives_account_maintenance_proxy")).height
            partial_rows = venue_rows.filter(pl.col("coverage_status") != "ok").height
            coverage = (
                "all rows have complete 5m coverage"
                if partial_rows == 0
                else f"{partial_rows}/{venue_rows.height} rows have partial/missing 5m coverage"
            )
            findings.append(
                f"{venue}: dynamic 5m outage overlay worst case is {worst['scenario']}/{worst['placement']} "
                f"with peak net loss {_fmt_pct(worst['dynamic_peak_net_loss_pct_equity'])}, "
                f"peak DD {_fmt_pct(worst['dynamic_peak_drawdown_pct'])}; max flatten net loss is "
                f"{_fmt_pct(worst_flatten['dynamic_flatten_net_loss_pct_equity'])} in "
                f"{worst_flatten['scenario']}/{worst_flatten['placement']}; maintenance-proxy liquidation rows "
                f"{liquidation_rows}/{venue_rows.height}, {coverage}."
            )
    labels_path = tables / "path_label_summary.csv"
    if labels_path.exists():
        labels = pl.read_csv(labels_path)
        for venue in venues:
            failed = _first_row(labels, venue=venue, path_label="FAILED_FADE")
            disaster = _first_row(labels, venue=venue, path_label="DISASTER")
            if failed:
                detail = f"{venue}: path labels mark {int(failed['trades'])} FAILED_FADE trades with net {_fmt_pct(failed['net_pnl'])}"
                if disaster:
                    detail += f"; DISASTER labels {int(disaster['trades'])} trades."
                findings.append(detail)
    dep_path = tables / "worst_trade_dependency.csv"
    if dep_path.exists():
        dep = pl.read_csv(dep_path)
        for venue in venues:
            base = _first_row(dep, venue=venue, scenario="baseline")
            dbl5 = _first_row(dep, venue=venue, scenario="double_worst_5")
            sq3 = _first_row(dep, venue=venue, scenario="replace_worst_3_100pct_squeeze")
            if base and dbl5:
                findings.append(
                    f"{venue}: doubling the worst 5 trade returns cuts static net from "
                    f"{_fmt_pct(base['net_pnl'])} to {_fmt_pct(dbl5['net_pnl'])}."
                )
            if base and sq3:
                findings.append(
                    f"{venue}: replacing the worst 3 trades with +100% adverse squeeze losses leaves static net "
                    f"{_fmt_pct(sq3['net_pnl'])}."
                )
    sizing_path = tables / "disaster_sizing_summary.csv"
    if sizing_path.exists():
        sizing = pl.read_csv(sizing_path)
        for venue in venues:
            row = _first_row(
                sizing,
                venue=venue,
                scenario="fixed_100pct",
                trade_loss_budget_pct_equity=0.001,
            )
            if row:
                findings.append(
                    f"{venue}: disaster sizing at fixed +100% and 0.10% trade-loss budget flags "
                    f"{_fmt_pct(row['pct_trades_over_budget'])} of component trades over budget; "
                    f"median current/safe notional {_fmt_num(row['median_current_to_safe_notional'])}, "
                    f"p95 {_fmt_num(row['p95_current_to_safe_notional'])}."
                )
    hedge_path = tables / "hedge_attribution.csv"
    if hedge_path.exists():
        hedge = pl.read_csv(hedge_path)
        for venue in venues:
            row = _first_row(hedge, venue=venue)
            if row:
                findings.append(
                    f"{venue}: hedge attribution sums to {_fmt_pct(row['hedge_total_sum'])} "
                    f"versus short gross {_fmt_pct(row['short_gross_return_sum'])}."
                )
    return findings


def write_signal_tables(output_root: Path, candidates: pl.DataFrame) -> dict[str, str]:
    artifacts: dict[str, str] = {}
    if candidates.is_empty():
        return artifacts
    tables = output_root / "tables"
    entry_anchor = pl.coalesce([pl.col("entry_bar_end_ts_ms"), pl.col("order_submit_ts_ms")])
    signals = candidates.with_columns(
        pl.from_epoch(pl.col("signal_ts_ms"), time_unit="ms").dt.strftime("%Y-%m-%dT%H:%M:%SZ").alias("signal_ts"),
        pl.from_epoch(entry_anchor, time_unit="ms").dt.strftime("%Y-%m-%dT%H:%M:%SZ").alias("entry_eligible_ts"),
        (entry_anchor >= pl.col("signal_ts_ms")).alias("entry_after_signal_bar"),
    )
    _write_df(signals, tables / "signal_table.parquet")
    artifacts["signal_table"] = str(tables / "signal_table.parquet")
    reason = (
        signals.group_by(["venue", "component_id", "reason", "selected"])
        .agg(pl.len().alias("signals"))
        .sort(["venue", "component_id", "selected", "reason"])
    )
    _write_df(reason, tables / "signal_reason_counts.csv")
    artifacts["signal_reason_counts"] = str(tables / "signal_reason_counts.csv")
    return artifacts


def write_leakage_audit(output_root: Path, candidates: pl.DataFrame, metadata: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if candidates.is_empty():
        checks.append({"check": "candidate_tape_present", "status": "fail", "detail": "no candidate rows loaded"})
    else:
        entry_anchor = pl.coalesce([pl.col("entry_bar_end_ts_ms"), pl.col("order_submit_ts_ms")])
        bad_entry = candidates.filter(entry_anchor.is_null() | (entry_anchor < pl.col("signal_ts_ms")))
        checks.append(
            {
                "check": "entry anchor >= signal_ts_ms",
                "status": "pass" if bad_entry.is_empty() else "fail",
                "detail": f"{bad_entry.height} violating rows",
            }
        )
        selected = candidates.filter(pl.col("selected"))
        bad_exit = selected.filter(pl.col("exit_ts_ms") < pl.col("entry_bar_end_ts_ms"))
        checks.append(
            {
                "check": "exit_ts_ms >= entry_eligible_ts for selected rows",
                "status": "pass" if bad_exit.is_empty() else "fail",
                "detail": f"{bad_exit.height} violating rows",
            }
        )
        timestamp_cols = [
            "signal_bar_close_ts_ms",
            "decision_ts_ms",
            "feature_ts_ms",
            "data_available_ts_ms",
            "rmom_data_available_ts_ms",
            "order_submit_ts_ms",
            "fill_window_start_ts_ms",
            "fill_window_end_ts_ms",
        ]
        missing_cols = sorted(set(timestamp_cols) - set(candidates.columns))
        null_counts = (
            {}
            if missing_cols
            else dict(
                zip(
                    timestamp_cols,
                    candidates.select([pl.col(c).is_null().sum().alias(c) for c in timestamp_cols]).row(0),
                )
            )
        )
        checks.append(
            {
                "check": "candidate tape carries direct timing provenance",
                "status": "pass" if not missing_cols and not any(int(v) > 0 for v in null_counts.values()) else "limited",
                "detail": {
                    "missing_columns": missing_cols,
                    "null_counts": null_counts,
                },
            }
        )
        if not missing_cols:
            if any(int(v) > 0 for v in null_counts.values()):
                checks.append(
                    {
                        "check": "direct timing provenance has no nulls",
                        "status": "fail",
                        "detail": null_counts,
                    }
                )
            checks.append(
                {
                    "check": "signal_bar_close_ts_ms == decision_ts_ms",
                    "status": "pass"
                    if candidates.filter(
                        pl.col("signal_bar_close_ts_ms").is_null()
                        | pl.col("decision_ts_ms").is_null()
                        | (pl.col("signal_bar_close_ts_ms") != pl.col("decision_ts_ms"))
                    ).is_empty()
                    else "fail",
                    "detail": (
                        f"{candidates.filter(pl.col('signal_bar_close_ts_ms').is_null() | pl.col('decision_ts_ms').is_null() | (pl.col('signal_bar_close_ts_ms') != pl.col('decision_ts_ms'))).height} "
                        "violating rows"
                    ),
                }
            )
            for col in ("feature_ts_ms", "data_available_ts_ms", "rmom_data_available_ts_ms"):
                bad = candidates.filter(pl.col(col).is_null() | pl.col("decision_ts_ms").is_null() | (pl.col(col) > pl.col("decision_ts_ms")))
                checks.append(
                    {
                        "check": f"{col} <= decision_ts_ms",
                        "status": "pass" if bad.is_empty() else "fail",
                        "detail": f"{bad.height} violating rows",
                    }
                )
            btc_cols = {"btc_trend_source_end_ts_ms", "btc_trend_data_available_ts_ms"} & set(candidates.columns)
            for col in sorted(btc_cols):
                bad = candidates.filter(pl.col(col).is_not_null() & (pl.col(col) > pl.col("decision_ts_ms")))
                checks.append(
                    {
                        "check": f"{col} <= decision_ts_ms when present",
                        "status": "pass" if bad.is_empty() else "fail",
                        "detail": f"{bad.height} violating rows",
                    }
                )
            bad_order = candidates.filter(
                pl.col("order_submit_ts_ms").is_null()
                | pl.col("decision_ts_ms").is_null()
                | (pl.col("order_submit_ts_ms") < pl.col("decision_ts_ms"))
            )
            checks.append(
                {
                    "check": "order_submit_ts_ms >= decision_ts_ms",
                    "status": "pass" if bad_order.is_empty() else "fail",
                    "detail": f"{bad_order.height} violating rows",
                }
            )
            bad_fill = candidates.filter(
                pl.col("fill_window_start_ts_ms").is_null()
                | pl.col("fill_window_end_ts_ms").is_null()
                | pl.col("order_submit_ts_ms").is_null()
                | (pl.col("fill_window_start_ts_ms") < pl.col("order_submit_ts_ms"))
                | (pl.col("fill_window_end_ts_ms") < pl.col("fill_window_start_ts_ms"))
            )
            checks.append(
                {
                    "check": "fill window is causal and ordered",
                    "status": "pass" if bad_fill.is_empty() else "fail",
                    "detail": f"{bad_fill.height} violating rows",
                }
            )
    for venue, ident in metadata["data_roots"].items():
        manifest = ident.get("manifest", {})
        klines_5m = ident.get("klines_5m", {})
        resid = ident.get("residual_momentum", {})
        checks.append(
            {
                "check": f"{venue} PIT archive manifest present",
                "status": "pass" if manifest.get("partition_count", 0) > 0 and manifest.get("last_symbol_count", 0) > 0 else "fail",
                "detail": manifest,
            }
        )
        checks.append(
            {
                "check": f"{venue} 5m klines present for sub-hour timing diagnostics",
                "status": "pass" if klines_5m.get("partition_count", 0) > 0 else "fail",
                "detail": klines_5m,
            }
        )
        checks.append(
            {
                "check": f"{venue} residual momentum spans run window",
                "status": "pass"
                if resid.get("exists") and resid.get("first_date") and resid.get("last_date")
                else "fail",
                "detail": resid,
            }
        )
    if not candidates.is_empty() and "feature_ts_ms" in candidates.columns and "decision_ts_ms" in candidates.columns:
        checks.append(
            {
                "check": "feature/data timestamp direct assertion",
                "status": "pass",
                "detail": "Per-row feature_ts_ms, data_available_ts_ms, rmom_data_available_ts_ms, and decision_ts_ms are persisted and checked above.",
            }
        )
    else:
        checks.append(
            {
                "check": "feature/data timestamp direct assertion",
                "status": "limited",
                "detail": "Candidate tape lacks direct per-row feature/data timestamp columns.",
            }
        )
    payload = {
        "created_at_utc": _utc_now(),
        "checks": checks,
        "verdict": "limited" if any(c["status"] == "limited" for c in checks) else (
            "fail" if any(c["status"] == "fail" for c in checks) else "pass"
        ),
    }
    path = output_root / "tables" / "leakage_audit.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def build_metadata(output_root: Path, venues: list[str]) -> dict[str, Any]:
    data_roots = {venue: data_root_identity(venue) for venue in venues}
    frozen_profile = {
        "object": FROZEN_FORWARD_CONFIG["object"],
        "profile_hash": frozen_config_hash(),
        "weights": FROZEN_FORWARD_CONFIG["weights"],
        "entry_sizing": FROZEN_FORWARD_CONFIG["entry_sizing"],
        "rebalance": FROZEN_FORWARD_CONFIG["rebalance"],
        "hedge": FROZEN_FORWARD_CONFIG["hedge"],
        "component_take_profit_pct": COMPONENT_TP,
        "btc_risk_sizing": "CTRL_BTC_RISK_70_90_35",
    }
    metadata = {
        "run_name": RUN_NAME,
        "created_at_utc": _utc_now(),
        "git": git_identity(),
        "profile": frozen_profile,
        "profile_hash": frozen_profile["profile_hash"],
        "run_config_hash": _json_hash(frozen_profile | {"data_roots": data_roots}),
        "plan": {
            "path": str(REPO_ROOT / "docs" / "preregistration" / "INDEX.md"),
            "sha256": _file_sha256(REPO_ROOT / "docs" / "preregistration" / "INDEX.md"),
            "note": "Hot-path preregistration index; detailed historical receipts live in git history and run artifacts.",
        },
        "source_docs": [
            {"path": str(path), "sha256": _file_sha256(path)}
            for path in HOT_PATH_DOCS
        ],
        "data_roots": data_roots,
        "methodology": {
            "decision_ts": "closed 1h signal bar; candidate signal_ts_ms from engine tape",
            "data_available_ts": "closed-bar trailing features plus PIT residual_momentum day-floor join",
            "order_submit_ts": "entry_eligible_ts / entry_bar_end_ts_ms, one bar after decision for current configs",
            "fill_window": "engine fill at entry bar close; exits evaluate 1h OHLC path with stop-before-TP ordering",
            "exit_activation_ts": "entry_ts_ms; TP12 and 24h max-hold active from entry; no tactical stop in baseline",
            "state_initialization_ts": "component replay starts at configured start_date; BTC-risk sizing scores decisions chronologically",
            "cost_model": "2*taker fee + 2*spread + impact; funding modeled where root coverage exists",
            "sub_hour_timing": "15m/30m/next-red diagnostics use manifest-gated klines_5m and exclude rows without a complete 24h 5m post-entry path",
            "run_label": "exploratory",
        },
        "safety": {
            "orders": "none",
            "real_money": False,
            "paths": "offline full-PIT research roots only",
        },
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "run_metadata.json").write_text(json.dumps(metadata, indent=2, default=str), encoding="utf-8")
    return metadata


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return "n/a"


def _fmt_num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "n/a"


def write_report(
    output_root: Path,
    metadata: dict[str, Any],
    metrics: dict[str, Any],
    artifacts: dict[str, str],
    leakage: dict[str, Any],
) -> None:
    adverse_limit_replayed = False
    timing_replay_path = output_root / "tables" / "timing_portfolio_replay.csv"
    if timing_replay_path.exists():
        timing_replay = pl.read_csv(timing_replay_path)
        adverse_limit_replayed = not timing_replay.filter(pl.col("variant") == "adverse_limit_1pct").is_empty()
    fixed_stop_replayed = False
    stop_replay_path = output_root / "tables" / "stop_portfolio_replay.csv"
    if stop_replay_path.exists():
        stop_replay = pl.read_csv(stop_replay_path)
        fixed_stop_replayed = not stop_replay.filter(pl.col("variant_kind") == "fixed_stop").is_empty()
    regime_replayed = False
    regime_replay_path = output_root / "tables" / "regime_portfolio_replay.csv"
    if regime_replay_path.exists():
        regime_replay = pl.read_csv(regime_replay_path)
        regime_venues = list((metadata.get("data_roots") or {}).keys()) or list(VENUES)
        regime_replayed = _regime_replay_complete(regime_replay, regime_venues)
    skip_replayed = False
    skip_replay_path = output_root / "tables" / "skip_portfolio_replay.csv"
    if skip_replay_path.exists():
        skip_replay = pl.read_csv(skip_replay_path)
        skip_venues = list((metadata.get("data_roots") or {}).keys()) or list(VENUES)
        skip_replayed = _skip_replay_complete(skip_replay, skip_venues)
    scale_in_replay_done = (output_root / "tables" / "scale_in_portfolio_replay.csv").exists()
    cluster_risk_done = (output_root / "tables" / "cluster_risk_of_ruin.csv").exists()
    dynamic_tail_done = (output_root / "tables" / "dynamic_liquidation_outage.csv").exists()
    disaster_sizing_done = (output_root / "tables" / "disaster_sizing_summary.csv").exists()
    remaining_items = []
    if not fixed_stop_replayed:
        remaining_items.append("stop")
    if not regime_replayed:
        remaining_items.append("regime")
    if not skip_replayed:
        remaining_items.append("skip")
    if not dynamic_tail_done:
        remaining_items.append("tail")
    if not remaining_items:
        remaining_lifecycle = "no listed replay/tail bucket"
    elif len(remaining_items) == 1:
        remaining_lifecycle = remaining_items[0]
    elif len(remaining_items) == 2:
        remaining_lifecycle = f"{remaining_items[0]} and {remaining_items[1]}"
    else:
        remaining_lifecycle = f"{', '.join(remaining_items[:-1])}, and {remaining_items[-1]}"
    replay_remaining_items = ["Next-red", *remaining_items]
    if len(replay_remaining_items) == 1:
        replay_remaining = replay_remaining_items[0]
    elif len(replay_remaining_items) == 2:
        replay_remaining = f"{replay_remaining_items[0]} and {replay_remaining_items[1]}"
    else:
        replay_remaining = f"{', '.join(replay_remaining_items[:-1])}, and {replay_remaining_items[-1]}"
    adverse_limit_verdict = (
        "Adverse-limit +1% now has a full portfolio replay in `timing_portfolio_replay.csv`; "
        f"{remaining_lifecycle} variants still need full lifecycle replays."
        if adverse_limit_replayed and remaining_items
        else "Adverse-limit +1% now has a full portfolio replay in `timing_portfolio_replay.csv`; no listed replay/tail bucket remains open."
        if adverse_limit_replayed
        else f"Adverse-limit, {remaining_lifecycle} variants still need full lifecycle replays."
    )
    replay_notes = [
        "engine-supported +1h/+2h added entry delays and +1% adverse-limit are full component+hedge portfolio replays in `timing_portfolio_replay.csv`",
    ]
    if fixed_stop_replayed:
        replay_notes.append(
            "staged fixed 20%/40%/80% stop arms are full component+hedge portfolio replays in `stop_portfolio_replay.csv`"
        )
    if regime_replayed:
        replay_notes.append(
            "BTC gate-off and simple-return lookback arms are full component+hedge portfolio replays in `regime_portfolio_replay.csv`"
        )
    if skip_replayed:
        replay_notes.append(
            "BTC-risk <=35% external-size skip is a full component+hedge portfolio replay in `skip_portfolio_replay.csv`"
        )
    if scale_in_replay_done:
        replay_notes.append(
            "pre-registered conditional scale-in arms are full component+hedge overlay replays in `scale_in_portfolio_replay.csv`"
        )
    replay_limitations = (
        "- Completed "
        + "; ".join(replay_notes)
        + f"; +4h timed out with partial artifacts and is omitted. {replay_remaining} variants remain diagnostics where not listed as full replays."
    )
    keep_rejected = [
        "added-delay timing",
    ]
    if adverse_limit_replayed:
        keep_rejected.append("+1% adverse-limit")
    if fixed_stop_replayed:
        keep_rejected.append("staged fixed-stop arms")
    if regime_replayed:
        keep_rejected.append("failed BTC-regime arms")
    rejected_clause = ", ".join(keep_rejected)
    rejected_clause = rejected_clause[:1].upper() + rejected_clause[1:]
    if remaining_items:
        next_research_step = (
            f"{rejected_clause} should stay rejected unless forward OOS evidence contradicts these replays; "
            f"{remaining_lifecycle} mechanisms still need full portfolio lifecycle tests where not already replayed."
        )
    else:
        next_research_step = (
            f"{rejected_clause} should stay rejected unless forward OOS evidence contradicts these replays; "
            "listed replay/tail diagnostics are complete, so forward demo/paper remains the arbiter."
        )
    if cluster_risk_done and remaining_items == ["tail"] and not dynamic_tail_done:
        next_research_step = (
            f"{rejected_clause} should stay rejected unless forward OOS evidence contradicts these replays; "
            "remaining tail work is dynamic liquidation/outage modeling, not a live-size approval gate."
        )
    if skip_replayed:
        next_research_step += " The BTC-risk skip arm remains exploratory and needs forward demo/paper arbitration before any sizing change."
    if cluster_risk_done and not dynamic_tail_done and remaining_items != ["tail"]:
        next_research_step += " Cluster risk-of-ruin is now a diagnostic artifact; remaining tail work is dynamic liquidation/outage modeling."
    if cluster_risk_done and dynamic_tail_done:
        next_research_step += " Cluster risk-of-ruin and dynamic outage overlays are diagnostics, not live-size approval gates."
    if disaster_sizing_done:
        next_research_step += (
            " Disaster-loss sizing flags strict per-trade budgets, so no size increase should be considered "
            "without explicit loss-at-disaster caps."
        )
    if scale_in_replay_done:
        next_research_step += " Scale-in overlay replay remains exploratory and cannot change live/paper behavior without forward demo/paper validation."
    lines: list[str] = [
        "# Continuous Fade Research Report",
        "",
        f"Run: `{RUN_NAME}`",
        f"Generated UTC: `{metadata['created_at_utc']}`",
        f"Git commit: `{metadata['git']['commit_short']}`",
        f"Profile hash: `{metadata['profile_hash']}`",
        f"Run config hash: `{metadata['run_config_hash']}`",
        "Run label: `exploratory`",
        "",
        "## Verdict",
        "",
        (
            "Not live-ready from this evidence alone. The baseline is costed and PIT-rooted and the "
            "candidate tape now carries direct per-row timing provenance. Added-delay timing has an "
            f"engine-level replay when `timing_portfolio_replay.csv` is present; {adverse_limit_verdict} Forward demo/paper remains the "
            "OOS arbiter."
        ),
        "",
        "## Baseline",
        "",
        "| Venue | Signals | Selected | Trades | Full Return | Component Net | Win Rate | Profit Factor | Sharpe | Active Sharpe | Cluster Sharpe | MAR | Max DD | Fees | Funding |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for venue, row in metrics.items():
        stats = row.get("runner_stats") or {}
        lines.append(
            "| {venue} | {signals} | {selected} | {trades} | {full_ret} | {net} | {wr} | {pf} | {sh} | {ash} | {csh} | {mar} | {dd} | {fees} | {funding} |".format(
                venue=venue,
                signals=row["signals"],
                selected=row["selected_signals"],
                trades=row["trades"],
                full_ret=f"{float(stats.get('total_return_pct', 0.0)):.2f}%" if stats.get("total_return_pct") is not None else "n/a",
                net=_fmt_pct(row["net_pnl"]),
                wr=_fmt_pct(row["win_rate"]),
                pf=_fmt_num(row["profit_factor"]),
                sh=_fmt_num(row["sharpe_calendar"]),
                ash=_fmt_num(row["active_sharpe"]),
                csh=_fmt_num(row["cluster_adjusted_sharpe"]),
                mar=_fmt_num(stats.get("mar")),
                dd=f"{float(stats.get('max_drawdown_pct', 0.0)):.2f}%" if stats.get("max_drawdown_pct") is not None else "n/a",
                fees=_fmt_pct(row["fees"]),
                funding=_fmt_pct(row["funding"]),
            )
        )
    lines.extend(
        [
            "",
            "## Findings",
            "",
        ]
    )
    findings = report_findings(output_root, list(metrics))
    if findings:
        lines.extend([f"- {finding}" for finding in findings])
    else:
        lines.append("- No post-baseline findings were generated.")
    lines.extend(
        [
            "",
            "## Integrity Audit",
            "",
            f"Leakage audit verdict: `{leakage['verdict']}`.",
            "",
            "| Check | Status | Detail |",
            "|---|---|---|",
        ]
    )
    for check in leakage["checks"]:
        detail = json.dumps(check["detail"], default=str) if not isinstance(check["detail"], str) else check["detail"]
        if len(detail) > 220:
            detail = detail[:217] + "..."
        lines.append(f"| {check['check']} | `{check['status']}` | {detail} |")
    lines.extend(
        [
            "",
            "## Key Artifacts",
            "",
        ]
    )
    for name, path in sorted(artifacts.items()):
        lines.append(f"- `{name}`: `{path}`")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- Signal, path-label, component-level skip, and tail tables in this run are diagnostics over candidate/ledger paths; portfolio replay tables listed below are full replays only for their named variants.",
            replay_limitations,
            "- 15m/30m and next-red 15m timing variants use `klines_5m` and require a complete 24h post-entry 5m path; excluded rows are counted in `timing_by_original_signal.csv`.",
            "- Conditional scale-in by-trade tables are path-metric diagnostics; `scale_in_portfolio_replay.csv`, when present, recomputes component MTM and the BTC/ETH hedge for explicit child trades but still excludes order-book queue, margin coupling, liquidation mechanics, and intrabar trigger ordering beyond no same-bar TP.",
            "- Signal-invalidation tables use only explicit future same-symbol candidate rows in the sparse candidate tape; absence of a row is not treated as invalidation, and the tables are not full component+hedge portfolio replays.",
            "- Direct feature/data timestamps are persisted for the frozen signal inputs; OI/depth/order-book predictors are unavailable in this tape and are not used by the frozen signal.",
            "- Binance funding is reported from the engine as modeled/partial depending on component coverage; treat funding-sensitive comparisons conservatively.",
            "",
            "## Recommendation",
            "",
            (
                "Do not increase live size. "
                f"{next_research_step}"
            ),
            "",
        ]
    )
    (output_root / "reports").mkdir(parents=True, exist_ok=True)
    (output_root / "reports" / "final_research_report.md").write_text("\n".join(lines), encoding="utf-8")


def analyze(output_root: Path, venues: list[str], *, run_portfolio_replays: bool = False) -> None:
    tables = output_root / "tables"
    tables.mkdir(parents=True, exist_ok=True)
    metadata = build_metadata(output_root, venues)
    trades = _read_component_trades(output_root, venues)
    candidates = _read_candidate_tapes(output_root, venues)
    _write_df(trades, tables / "trades_component_ledger.parquet")
    artifacts: dict[str, str] = {
        "run_metadata": str(output_root / "run_metadata.json"),
        "trades_component_ledger": str(tables / "trades_component_ledger.parquet"),
    }
    artifacts.update(write_signal_tables(output_root, candidates))
    trades = write_path_metrics(output_root, trades, venues)
    artifacts["trades_enriched"] = str(tables / "trades_enriched.parquet")
    metrics = baseline_metrics(output_root, trades, candidates, venues)
    (tables / "baseline_metrics.json").write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    artifacts["baseline_metrics"] = str(tables / "baseline_metrics.json")
    artifacts.update(write_group_tables(output_root, trades))
    artifacts.update(write_mae_mfe_tables(output_root, trades))
    artifacts.update(write_forward_path_tables(output_root, candidates, trades, venues))
    artifacts.update(write_path_label_tables(output_root, trades))
    artifacts.update(write_component_ablation(output_root, trades))
    artifacts.update(write_worst_tail_and_heat_tables(output_root, trades))
    artifacts.update(write_disaster_sizing_tables(output_root, trades))
    artifacts.update(write_skip_logic_buckets(output_root, candidates, trades))
    artifacts.update(write_stop_frontier(output_root, trades, venues))
    artifacts.update(write_conditional_scale_in_tables(output_root, trades))
    artifacts.update(write_signal_invalidation_tables(output_root, trades, candidates))
    artifacts.update(write_scale_in_portfolio_replay(output_root, venues, run_replays=run_portfolio_replays))
    artifacts.update(write_stop_portfolio_replay(output_root, venues, run_replays=run_portfolio_replays))
    artifacts.update(write_timing_table(output_root, candidates, venues))
    artifacts.update(write_timing_portfolio_replay(output_root, venues, run_replays=run_portfolio_replays))
    artifacts.update(write_regime_portfolio_replay(output_root, venues, run_replays=run_portfolio_replays))
    artifacts.update(write_skip_portfolio_replay(output_root, venues, run_replays=run_portfolio_replays))
    artifacts.update(write_regime_tables(output_root, trades, venues))
    artifacts.update(write_cost_tail_tables(output_root, trades))
    artifacts.update(write_synthetic_squeeze_survival_tables(output_root, trades))
    artifacts.update(write_cluster_risk_of_ruin_tables(output_root, trades))
    artifacts.update(write_dynamic_liquidation_outage_tables(output_root, trades))
    artifacts.update(write_hedge_attribution(output_root, venues))
    artifacts.update(write_overfit_diagnostics(output_root, venues))
    leakage = write_leakage_audit(output_root, candidates, metadata)
    artifacts["leakage_audit"] = str(tables / "leakage_audit.json")
    (tables / "artifact_index.json").write_text(json.dumps(artifacts, indent=2, default=str), encoding="utf-8")
    write_report(output_root, metadata, metrics, artifacts, leakage)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", default=str(REPO_ROOT / "research" / "continuous_fade" / "runs" / RUN_NAME))
    parser.add_argument("--venues", default="bybit,binance")
    parser.add_argument("--skip-baseline", action="store_true", help="Only analyze existing artifacts")
    parser.add_argument("--skip-candidate-tapes", action="store_true", help="Do not rerun components to write candidate tapes")
    parser.add_argument("--run-portfolio-replays", action="store_true", help="Run expensive full portfolio replay diagnostics")
    parser.add_argument("--rerun", action="store_true", help="Force baseline/candidate recomputation")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_root = Path(args.output_root).expanduser()
    venues = [v.strip() for v in args.venues.split(",") if v.strip()]
    bad = sorted(set(venues) - set(VENUES))
    if bad:
        raise SystemExit(f"unknown venue(s): {bad}")
    if not args.skip_baseline:
        run_frozen_baseline(output_root, venues, rerun=args.rerun)
    if not args.skip_candidate_tapes:
        write_candidate_tapes(output_root, venues, rerun=args.rerun)
    analyze(output_root, venues, run_portfolio_replays=args.run_portfolio_replays)
    print(output_root / "reports" / "final_research_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
