#!/usr/bin/env python3
"""Append-first market refresh, derived-feature refresh, backtest, and reconciliation.

Routine runs use ``--data-mode tail``: current data is refreshed with an
overlap, Bybit's independent manifest is rebuilt over its canonical history,
and all-root manifest validation remains mandatory.  ``--data-mode canonical``
uses the repository's full-PIT builders and is the appropriate mode when a
registered claim requires a complete rebuild.

The optional demo/paper/backtest reconciliation compares accepted entry keys.
It deliberately keeps execution/accounting evidence separate from historical
performance.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import polars as pl  # noqa: E402

from liquidity_migration.binance_vision import validate_usdm_usdt_symbols  # noqa: E402
from liquidity_migration.deterministic_serialization import canonical_json  # noqa: E402
from liquidity_migration.three_way_reconciliation import (  # noqa: E402
    reconcile_account_roots_to_backtest,
    write_three_way_artifacts,
)


PYTHON = REPO / ".venv" / "bin" / "python"
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)

DEFAULT_ROOTS = {
    "bybit": Path.home() / "SHARED_DATA" / "bybit_full_pit",
    "binance": Path.home() / "SHARED_DATA" / "binance_full_pit",
}
BASE_START = {"bybit": dt.date(2021, 1, 1), "binance": dt.date(2019, 9, 1)}
BYBIT_ANCILLARY = (
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
)
BINANCE_ANCILLARY = (
    "binance_usdm_funding",
    "binance_usdm_open_interest",
    "binance_usdm_mark_price_1h",
    "binance_usdm_index_price_1h",
    "binance_usdm_premium_index_1h",
    "binance_usdm_taker_flow_1h",
)
VALID_VENUES = tuple(DEFAULT_ROOTS)
VALID_SLEEVES = ("long", "continuous")


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO date {value!r}") from exc


def _date_ms(value: dt.date) -> int:
    return int(dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc).timestamp() * 1000)


def _shift_years(value: dt.date, years: int) -> dt.date:
    if years <= 0:
        raise ValueError("benchmark years must be positive")
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _csv_set(value: str, *, valid: Sequence[str], label: str) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
    bad = sorted(set(selected) - set(valid))
    if not selected or bad:
        raise ValueError(f"invalid {label}: {bad or value!r}; valid: {', '.join(valid)}")
    return selected


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_step_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "._-" else "_" for character in value)


def latest_partition_date(root: Path, dataset: str) -> dt.date | None:
    base = root / dataset
    try:
        children = list(base.iterdir())
    except OSError:
        return None
    latest: dt.date | None = None
    for child in children:
        if not child.is_dir() or not child.name.startswith("date="):
            continue
        try:
            value = dt.date.fromisoformat(child.name.removeprefix("date="))
        except ValueError:
            continue
        if latest is None or value > latest:
            latest = value
    return latest


def dataset_signature(root: Path, dataset: str) -> dict[str, object]:
    base = root / dataset
    try:
        children = [child for child in base.iterdir() if child.is_dir() and child.name.startswith("date=")]
        stat = base.stat()
    except OSError:
        return {"latest": None, "date_partitions": 0, "dataset_mtime_ns": None}
    dates: list[dt.date] = []
    for child in children:
        try:
            dates.append(dt.date.fromisoformat(child.name.removeprefix("date=")))
        except ValueError:
            continue
    return {
        "latest": max(dates).isoformat() if dates else None,
        "date_partitions": len(dates),
        "dataset_mtime_ns": stat.st_mtime_ns,
    }


def coverage_snapshot(root: Path, *, venue: str) -> dict[str, dict[str, object]]:
    datasets = ("archive_trade_manifest", "klines_1h") + (
        BYBIT_ANCILLARY if venue == "bybit" else BINANCE_ANCILLARY
    )
    return {dataset: dataset_signature(root, dataset) for dataset in datasets}


def tail_start(
    root: Path,
    datasets: Sequence[str],
    *,
    base_start: dt.date,
    end: dt.date,
    overlap_days: int,
) -> dt.date:
    if overlap_days < 1:
        raise ValueError("overlap days must be positive")
    latest = [value for dataset in datasets if (value := latest_partition_date(root, dataset)) is not None]
    if not latest:
        return base_start
    candidate = min(latest) - dt.timedelta(days=overlap_days)
    # Never create an empty range when a root happens to contain a future or
    # accidentally over-padded partition.
    return max(base_start, min(candidate, end - dt.timedelta(days=1)))


def _manifest_symbols(root: Path, *, venue: str) -> tuple[str, ...]:
    pattern = str(root / "archive_trade_manifest" / "**" / "*.parquet")
    try:
        symbols = (
            pl.scan_parquet(pattern)
            .select(pl.col("symbol").cast(pl.String))
            .unique()
            .sort("symbol")
            .collect()["symbol"]
            .to_list()
        )
    except Exception as exc:  # noqa: BLE001 - emit a root-specific integrity failure
        raise RuntimeError(f"cannot read canonical {venue} manifest under {root}: {exc}") from exc
    normalized = tuple(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip())
    if venue == "binance":
        return tuple(
            validate_usdm_usdt_symbols(
                normalized,
                source="research refresh canonical Binance manifest",
                ignore_non_usdt_entries=False,
            )
        )
    bad = [symbol for symbol in normalized if not symbol.endswith("USDT")]
    if bad:
        raise RuntimeError(f"non-USDT symbols in canonical Bybit manifest: {bad[:10]}")
    if not normalized:
        raise RuntimeError(f"canonical {venue} manifest is empty under {root}")
    return normalized


def _redacted_command(command: Sequence[str]) -> list[str]:
    output = list(command)
    if "--symbols" in output:
        index = output.index("--symbols") + 1
        if index < len(output):
            symbols = [value for value in output[index].split(",") if value]
            digest = hashlib.sha256(output[index].encode("utf-8")).hexdigest()[:16]
            output[index] = f"<{len(symbols)}-symbols-sha256:{digest}>"
    return output


@dataclass(frozen=True, slots=True)
class CommandStep:
    step_id: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    expected_paths: tuple[Path, ...] = ()
    fingerprint_extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def display_command(self) -> tuple[str, ...]:
        return tuple(_redacted_command(self.command))

    @property
    def fingerprint(self) -> str:
        material = {
            "step_id": self.step_id,
            "command": list(self.display_command),
            "env": dict(sorted(self.env.items())),
            "extra": dict(self.fingerprint_extra),
        }
        return hashlib.sha256(canonical_json(material)).hexdigest()


class RunLedger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.path = run_dir / "events.jsonl"
        self.logs = run_dir / "logs"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)

    def append(self, payload: Mapping[str, Any]) -> None:
        row = {
            "schema_version": 1,
            "kind": "research_refresh_event",
            "recorded_at": _utc_now().isoformat(),
            **dict(payload),
        }
        descriptor = os.open(
            self.path,
            os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            data = canonical_json(row) + b"\n"
            view = memoryview(data)
            offset = 0
            while offset < len(data):
                written = os.write(descriptor, view[offset:])
                if written <= 0:
                    raise OSError("refresh ledger append made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def completed(self, step: CommandStep) -> bool:
        if not self.path.is_file() or any(not path.exists() for path in step.expected_paths):
            return False
        succeeded = False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                return False
            if row.get("step_id") == step.step_id and row.get("fingerprint") == step.fingerprint:
                if row.get("status") == "succeeded":
                    succeeded = True
                elif row.get("status") == "failed":
                    succeeded = False
        return succeeded

    def run(self, step: CommandStep, *, check: bool = True) -> bool:
        if self.completed(step):
            print(f"[resume] {step.step_id}", flush=True)
            self.append(
                {
                    "step_id": step.step_id,
                    "fingerprint": step.fingerprint,
                    "status": "skipped_completed",
                }
            )
            return True

        command_text = shlex.join(step.display_command)
        print(f"\n[{step.step_id}] $ {command_text}", flush=True)
        self.append(
            {
                "step_id": step.step_id,
                "fingerprint": step.fingerprint,
                "status": "started",
                "command": list(step.display_command),
                "env": dict(sorted(step.env.items())),
            }
        )
        environment = {**os.environ, **step.env, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
        log_path = self.logs / f"{_safe_step_name(step.step_id)}.log"
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n$ {command_text}\n")
            process = subprocess.Popen(  # noqa: S603 - commands are code-defined, not shell-evaluated
                list(step.command),
                cwd=REPO,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                print(line, end="", flush=True)
                log.write(line)
            returncode = process.wait()
            log.flush()
            os.fsync(log.fileno())

        missing = [str(path) for path in step.expected_paths if not path.exists()]
        success = returncode == 0 and not missing
        self.append(
            {
                "step_id": step.step_id,
                "fingerprint": step.fingerprint,
                "status": "succeeded" if success else "failed",
                "returncode": returncode,
                "missing_expected_paths": missing,
                "log_path": str(log_path),
            }
        )
        if not success and check:
            detail = f"exit={returncode}"
            if missing:
                detail += f" missing={missing}"
            raise RuntimeError(f"refresh step {step.step_id!r} failed: {detail}")
        return success


def _print_step(step: CommandStep) -> None:
    env = " ".join(f"{key}={shlex.quote(value)}" for key, value in sorted(step.env.items()))
    prefix = f"{env} " if env else ""
    print(f"  [{step.step_id}] {prefix}{shlex.join(step.display_command)}")


def _canonical_builder_step(
    *,
    venue: str,
    root: Path,
    end: dt.date,
    args: argparse.Namespace,
) -> CommandStep:
    if venue == "bybit":
        env = {
            "BYBIT_FULL_ROOT": str(root),
            "BYBIT_START": BASE_START[venue].isoformat(),
            "BYBIT_END": end.isoformat(),
            "MANIFEST_WORKERS": str(args.manifest_workers),
            "KLINE_WORKERS": str(args.kline_workers),
            "ANCILLARY_WORKERS": str(args.ancillary_workers),
            "PYTHON_BIN": str(PYTHON),
        }
        script = REPO / "scripts" / "build_full_pit_bybit.sh"
    else:
        env = {
            "BINANCE_FULL_ROOT": str(root),
            "BINANCE_START": BASE_START[venue].isoformat(),
            "BINANCE_END": end.isoformat(),
            "BINANCE_WORKERS": str(args.binance_workers),
            "BINANCE_JOB_BATCH_SIZE": str(args.binance_job_batch_size),
            "BINANCE_MAX_FAILURE_RATIO": "0",
            "ANCILLARY_WORKERS": str(args.ancillary_workers),
            "PYTHON_BIN": str(PYTHON),
        }
        script = REPO / "scripts" / "build_full_pit_binance.sh"
    target = end - dt.timedelta(days=1)
    return CommandStep(
        step_id=f"data.{venue}.canonical",
        command=("bash", str(script)),
        env=env,
        expected_paths=(
            root / "archive_trade_manifest" / f"date={target.isoformat()}",
            root / "klines_1h" / f"date={target.isoformat()}",
        ),
        fingerprint_extra={"coverage_before": coverage_snapshot(root, venue=venue)},
    )


def _bybit_tail_steps(
    root: Path,
    *,
    end: dt.date,
    args: argparse.Namespace,
) -> tuple[CommandStep, CommandStep, CommandStep]:
    kline_start = tail_start(
        root,
        ("archive_trade_manifest", "klines_1h"),
        base_start=BASE_START["bybit"],
        end=end,
        overlap_days=args.overlap_days,
    )
    manifest = CommandStep(
        step_id="data.bybit.manifest",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration",
            "--data-root",
            str(root),
            "archive-manifest",
            "--start",
            BASE_START["bybit"].isoformat(),
            "--end",
            end.isoformat(),
            "--workers",
            str(args.manifest_workers),
        ),
        fingerprint_extra={"coverage_before": coverage_snapshot(root, venue="bybit")},
    )
    klines = CommandStep(
        step_id="data.bybit.klines_tail",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration",
            "--data-root",
            str(root),
            "archive-download-klines-1h-api",
            "--category",
            "linear",
            "--start",
            kline_start.isoformat(),
            "--end",
            end.isoformat(),
            "--workers",
            str(args.kline_workers),
            "--min-existing-bars",
            "20",
        ),
        fingerprint_extra={"tail_start": kline_start.isoformat()},
    )
    validate = CommandStep(
        step_id="data.bybit.validate_manifest",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration.binance_vision",
            "validate-manifest",
            "--data-root",
            str(root),
        ),
    )
    return manifest, klines, validate


def _bybit_full_missing_kline_step(root: Path, *, end: dt.date, args: argparse.Namespace) -> CommandStep:
    return CommandStep(
        step_id="data.bybit.klines_full_missing_fallback",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration",
            "--data-root",
            str(root),
            "archive-download-klines-1h-api",
            "--category",
            "linear",
            "--start",
            BASE_START["bybit"].isoformat(),
            "--end",
            end.isoformat(),
            "--workers",
            str(args.kline_workers),
            "--min-existing-bars",
            "20",
        ),
        fingerprint_extra={"fallback": "all-root validation failed after tail"},
    )


def _bybit_ancillary_step(root: Path, *, end: dt.date, args: argparse.Namespace) -> CommandStep:
    start = tail_start(
        root,
        BYBIT_ANCILLARY,
        base_start=BASE_START["bybit"],
        end=end,
        overlap_days=args.overlap_days,
    )
    symbols = _manifest_symbols(root, venue="bybit")
    return CommandStep(
        step_id="data.bybit.ancillary_tail",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration",
            "--data-root",
            str(root),
            "download-data",
            "--symbols",
            ",".join(symbols),
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--datasets",
            "funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h",
            "--workers",
            str(args.ancillary_workers),
        ),
        fingerprint_extra={"tail_start": start.isoformat(), "symbols": len(symbols)},
    )


def _binance_tail_steps(
    root: Path,
    *,
    end: dt.date,
    args: argparse.Namespace,
) -> tuple[CommandStep, CommandStep]:
    start = tail_start(
        root,
        ("archive_trade_manifest", "klines_1h"),
        base_start=BASE_START["binance"],
        end=end,
        overlap_days=args.overlap_days,
    )
    topup = CommandStep(
        step_id="data.binance.daily_tail",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration.binance_vision",
            "topup-daily-klines",
            "--data-root",
            str(root),
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--workers",
            str(args.binance_workers),
            "--max-failure-ratio",
            "0",
        ),
        fingerprint_extra={"tail_start": start.isoformat()},
    )
    validate = CommandStep(
        step_id="data.binance.validate_manifest",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration.binance_vision",
            "validate-manifest",
            "--data-root",
            str(root),
        ),
    )
    return topup, validate


def _binance_ancillary_step(root: Path, *, end: dt.date, args: argparse.Namespace) -> CommandStep:
    start = tail_start(
        root,
        BINANCE_ANCILLARY,
        base_start=BASE_START["binance"],
        end=end,
        overlap_days=args.overlap_days,
    )
    symbols = _manifest_symbols(root, venue="binance")
    return CommandStep(
        step_id="data.binance.ancillary_tail",
        command=(
            str(PYTHON),
            "-m",
            "liquidity_migration",
            "--data-root",
            str(root),
            "download-binance-proxy",
            "--symbols",
            ",".join(symbols),
            "--start",
            start.isoformat(),
            "--end",
            end.isoformat(),
            "--datasets",
            "funding,open_interest,mark_price_1h,index_price_1h,premium_index_1h,taker_flow_1h",
            "--workers",
            str(args.ancillary_workers),
        ),
        fingerprint_extra={"tail_start": start.isoformat(), "symbols": len(symbols)},
    )


def _require_coverage(root: Path, *, venue: str, end: dt.date) -> None:
    target = end - dt.timedelta(days=1)
    datasets = ("archive_trade_manifest", "klines_1h") + (
        BYBIT_ANCILLARY if venue == "bybit" else BINANCE_ANCILLARY
    )
    stale = {
        dataset: latest.isoformat() if latest else None
        for dataset in datasets
        if (latest := latest_partition_date(root, dataset)) is None or latest < target
    }
    if stale:
        raise RuntimeError(
            f"{venue} refresh did not reach required completed UTC day {target}: {stale}"
        )


def _refresh_data(
    *,
    venue: str,
    root: Path,
    end: dt.date,
    args: argparse.Namespace,
    ledger: RunLedger | None,
) -> None:
    if args.data_mode == "canonical":
        step = _canonical_builder_step(venue=venue, root=root, end=end, args=args)
        if ledger is None:
            _print_step(step)
            return
        ledger.run(step)
        _require_coverage(root, venue=venue, end=end)
        return

    if venue == "bybit":
        manifest, klines, validate = _bybit_tail_steps(root, end=end, args=args)
        if ledger is None:
            for step in (manifest, klines, validate):
                _print_step(step)
            print("  [fallback] full missing-only Bybit kline scan if all-root validation fails")
            try:
                _print_step(_bybit_ancillary_step(root, end=end, args=args))
            except RuntimeError as exc:
                print(f"  [dynamic] ancillary command available after manifest refresh: {exc}")
            return
        ledger.run(manifest)
        ledger.run(klines)
        if not ledger.run(validate, check=False):
            ledger.run(_bybit_full_missing_kline_step(root, end=end, args=args))
            # Use a distinct identity so a prior failed validation never hides
            # the post-fallback proof.
            post_fallback = CommandStep(
                step_id="data.bybit.validate_manifest_after_fallback",
                command=validate.command,
                fingerprint_extra={"after_full_missing_fallback": True},
            )
            ledger.run(post_fallback)
        ledger.run(_bybit_ancillary_step(root, end=end, args=args))
    else:
        target = end - dt.timedelta(days=1)
        latest = latest_partition_date(root, "klines_1h")
        if latest is None or (latest.year, latest.month) != (target.year, target.month):
            print(
                "[data.binance] tail mode crossed an unmaterialized month; using the canonical monthly builder",
                flush=True,
            )
            step = _canonical_builder_step(venue=venue, root=root, end=end, args=args)
            if ledger is None:
                _print_step(step)
                return
            ledger.run(step)
        else:
            topup, validate = _binance_tail_steps(root, end=end, args=args)
            if ledger is None:
                _print_step(topup)
                _print_step(validate)
                try:
                    _print_step(_binance_ancillary_step(root, end=end, args=args))
                except RuntimeError as exc:
                    print(f"  [dynamic] ancillary command available after daily top-up: {exc}")
                return
            ledger.run(topup)
            ledger.run(validate)
            ledger.run(_binance_ancillary_step(root, end=end, args=args))
    if ledger is not None:
        _require_coverage(root, venue=venue, end=end)


def _rmom_step(root: Path, *, venue: str, end: dt.date) -> CommandStep:
    path = root / "residual_momentum.parquet"
    full_rewrite = False
    schema: list[str] = []
    if path.is_file():
        try:
            schema = pl.read_parquet(path, n_rows=0).columns
        except Exception:  # noqa: BLE001 - the owner will perform an atomic replacement
            full_rewrite = True
        else:
            full_rewrite = "is_provisional" not in schema
    command = [
        str(PYTHON),
        "-u",
        str(REPO / "scripts" / "precompute_residual_momentum.py"),
        "--root",
        str(root),
        "--start",
        "2023-03-01",
        "--end",
        end.isoformat(),
    ]
    if full_rewrite:
        command.append("--full-rewrite")
    return CommandStep(
        step_id=f"features.{venue}.residual_momentum",
        command=tuple(command),
        env={"POLARS_MAX_THREADS": "8"},
        expected_paths=(path,),
        fingerprint_extra={"schema_before": schema, "full_rewrite": full_rewrite},
    )


def _validate_rmom(root: Path, *, venue: str, end: dt.date) -> None:
    path = root / "residual_momentum.parquet"
    frame = pl.scan_parquet(path)
    columns = set(frame.collect_schema().names())
    required = {"symbol", "ts_ms", "residual_momentum", "is_provisional"}
    if missing := sorted(required - columns):
        raise RuntimeError(f"{venue} residual momentum still lacks columns: {missing}")
    maximum = frame.select(pl.col("ts_ms").max()).collect().item()
    target_ms = _date_ms(end - dt.timedelta(days=1))
    if maximum is None or int(maximum) < target_ms:
        raise RuntimeError(
            f"{venue} residual momentum is stale: max_ts_ms={maximum}, required>={target_ms}"
        )


def _backtest_step(
    *,
    venue: str,
    sleeve: str,
    root: Path,
    start: dt.date,
    end: dt.date,
) -> CommandStep:
    report_root = root / "reports" / "equity_curves"
    command = (
        "bash",
        str(REPO / "scripts" / "equity_curves.sh"),
        "--sleeves",
        sleeve,
        "--venue",
        venue,
        "--root",
        str(root),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--continuous-chart-leverage",
        "1",
        "--continuous-backtest-leverage",
        "1",
        "--out",
        str(report_root),
    )
    if sleeve == "long":
        expected = report_root / "long" / "long_native_research_report.json"
    else:
        expected = report_root / "continuous" / venue / "continuous_equity_summary.json"
    return CommandStep(
        step_id=f"backtest.{venue}.{sleeve}",
        command=command,
        expected_paths=(expected,),
        fingerprint_extra={
            "coverage": coverage_snapshot(root, venue=venue),
            "rmom_sha256": _sha256(root / "residual_momentum.parquet")
            if (root / "residual_momentum.parquet").is_file()
            else None,
        },
    )


def _validate_backtest_report(
    *,
    venue: str,
    sleeve: str,
    root: Path,
    start: dt.date,
    end: dt.date,
) -> None:
    report_root = root / "reports" / "equity_curves"
    if sleeve == "long":
        path = report_root / "long" / "long_native_research_report.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        config = payload.get("config") or {}
        if config.get("start_date") != start.isoformat() or config.get("end_date") != end.isoformat():
            raise RuntimeError(f"LONG report window mismatch under {path}")
        return
    summary_path = report_root / "continuous" / venue / "continuous_equity_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if summary.get("start_date") != start.isoformat() or summary.get("end_date") != end.isoformat():
        raise RuntimeError(f"CONTINUOUS report window mismatch under {summary_path}")
    if summary.get("chart_leverage") != 1.0 or summary.get("backtest_leverage") != 1.0:
        raise RuntimeError(f"CONTINUOUS leverage mismatch under {summary_path}")


def _artifact_files(report_root: Path, *, venue: str) -> list[Path]:
    files: list[Path] = []
    patterns = (
        "long/long_native_*.csv",
        "long/long_native_*.json",
        "long/long_native_*.md",
        "continuous/*/*.csv",
        "continuous/*/*.json",
        "continuous/*/*.md",
        f"continuous/components/{venue}/*/continuous_*.csv",
        f"continuous/components/{venue}/*/continuous_*.json",
    )
    for pattern in patterns:
        files.extend(path for path in report_root.glob(pattern) if path.is_file())
    return sorted(set(files))


def _run_summary(
    *,
    run_dir: Path,
    roots: Mapping[str, Path],
    venues: Sequence[str],
    sleeves: Sequence[str],
    start: dt.date,
    end: dt.date,
    code_commit: str,
) -> tuple[dict[str, Any], Path]:
    cells: dict[str, Any] = {}
    artifacts: list[dict[str, object]] = []
    coverage: dict[str, Any] = {}
    for venue in venues:
        root = roots[venue]
        report_root = root / "reports" / "equity_curves"
        coverage[venue] = coverage_snapshot(root, venue=venue)
        for sleeve in sleeves:
            key = f"{venue}.{sleeve}"
            if sleeve == "long":
                path = report_root / "long" / "long_native_research_report.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                cells[key] = {
                    "run_label": payload.get("run_label"),
                    "methodology_run_label": payload.get("methodology_run_label"),
                    "tainted": payload.get("tainted"),
                    "summary": payload.get("summary"),
                    "warnings": payload.get("warnings"),
                    "date_range": payload.get("date_range"),
                    "pit_manifest": payload.get("pit_manifest"),
                    "account_journal": payload.get("account_journal"),
                    "report": str(path),
                }
            else:
                path = report_root / "continuous" / venue / "continuous_equity_summary.json"
                payload = json.loads(path.read_text(encoding="utf-8"))
                cells[key] = {
                    "run_label": payload.get("run_label"),
                    "strategy_run_label": payload.get("strategy_run_label"),
                    "stats": payload.get("stats"),
                    "funding_modes": payload.get("funding_modes"),
                    "window": payload.get("window"),
                    "components": payload.get("components"),
                    "report": str(path),
                }
        for path in _artifact_files(report_root, venue=venue):
            artifacts.append(
                {
                    "venue": venue,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                }
            )
    material = {
        "schema_version": 1,
        "kind": "research_refresh_summary",
        "code_commit": code_commit,
        "window": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
        "coverage": coverage,
        "cells": cells,
        "artifacts": artifacts,
    }
    snapshot_id = hashlib.sha256(canonical_json(material)).hexdigest()
    payload = {**material, "snapshot_id": snapshot_id}
    path = run_dir / f"summary.{snapshot_id[:16]}.json"
    data = canonical_json(payload) + b"\n"
    if path.exists() and path.read_bytes() != data:
        raise FileExistsError(f"run summary hash collision or changed immutable artifact: {path}")
    if not path.exists():
        path.write_bytes(data)
    return payload, path


def _manifest_configuration(args: argparse.Namespace) -> dict[str, Any]:
    end: dt.date = args.end
    start: dt.date = args.start or _shift_years(end, args.years)
    venues = _csv_set(args.venues, valid=VALID_VENUES, label="venues")
    sleeves = _csv_set(args.sleeves, valid=VALID_SLEEVES, label="sleeves")
    roots = {
        "bybit": str(Path(args.bybit_root).expanduser().resolve()),
        "binance": str(Path(args.binance_root).expanduser().resolve()),
    }
    preregistration: dict[str, object] | None = None
    if args.preregistration:
        path = Path(args.preregistration).expanduser()
        if not path.is_absolute():
            path = REPO / path
        path = path.resolve(strict=True)
        preregistration = {"path": str(path), "sha256": _sha256(path)}
    return {
        "end_exclusive": end.isoformat(),
        "window_start": start.isoformat(),
        "years": args.years,
        "venues": list(venues),
        "sleeves": list(sleeves),
        "roots": roots,
        "data_mode": args.data_mode,
        "overlap_days": args.overlap_days,
        "skip_data": args.skip_data,
        "skip_features": args.skip_features,
        "skip_backtests": args.skip_backtests,
        "preregistration": preregistration,
        "modeled_leverage": 1.0,
        "chart_leverage": 1.0,
        "boundary_semantics": "[start, end) at UTC midnight; end is latest completed day boundary",
    }


def _prepare_run_manifest(
    *,
    run_dir: Path,
    configuration: Mapping[str, Any],
    code_commit: str,
    dirty: bool,
) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("configuration") != dict(configuration):
            raise RuntimeError(f"run ID already exists with different configuration: {run_dir}")
        if existing.get("code_commit") != code_commit or bool(existing.get("git_dirty")) != dirty:
            raise RuntimeError(f"run ID already exists with a different source identity: {run_dir}")
        return existing
    payload = {
        "schema_version": 1,
        "kind": "research_refresh_run",
        "created_at": _utc_now().isoformat(),
        "code_commit": code_commit,
        "git_dirty": dirty,
        "configuration": dict(configuration),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        data = canonical_json(payload) + b"\n"
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, data[offset:])
            if written <= 0:
                raise OSError("run manifest write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return payload


def _plan(args: argparse.Namespace) -> int:
    configuration = _manifest_configuration(args)
    end = dt.date.fromisoformat(str(configuration["end_exclusive"]))
    start = dt.date.fromisoformat(str(configuration["window_start"]))
    roots = {venue: Path(path) for venue, path in _as_mapping(configuration["roots"]).items()}
    print("research-refresh plan")
    print(f"  window: [{start}, {end}) UTC")
    print(f"  data mode: {args.data_mode}")
    print(f"  venues: {', '.join(configuration['venues'])}")
    print(f"  sleeves: {', '.join(configuration['sleeves'])}")
    for venue in configuration["venues"]:
        root = roots[str(venue)]
        print(f"\n{str(venue).upper()} root: {root}")
        print(json.dumps(coverage_snapshot(root, venue=str(venue)), indent=2))
        if not args.skip_data:
            _refresh_data(venue=str(venue), root=root, end=end, args=args, ledger=None)
        if not args.skip_features and "continuous" in configuration["sleeves"]:
            _print_step(_rmom_step(root, venue=str(venue), end=end))
        if not args.skip_backtests:
            for sleeve in configuration["sleeves"]:
                _print_step(
                    _backtest_step(
                        venue=str(venue),
                        sleeve=str(sleeve),
                        root=root,
                        start=start,
                        end=end,
                    )
                )
    print("\nPlan only: no files, data, reports, journals, or venue state were changed.")
    return 0


def _as_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return value


def _run_reconciliation(
    *,
    args: argparse.Namespace,
    run_dir: Path,
    configuration: Mapping[str, Any],
    code_commit: str,
    ledger: RunLedger | None = None,
) -> dict[str, str] | None:
    demo_root = str(args.demo_account_root or "").strip()
    paper_root = str(args.paper_account_root or "").strip()
    if bool(demo_root) != bool(paper_root):
        raise ValueError("demo and paper account roots must be supplied together")
    if not demo_root:
        if ledger is not None:
            ledger.append(
                {
                    "step_id": "reconcile.demo_paper_backtest",
                    "status": "skipped_no_account_snapshots",
                }
            )
        return None
    roots = _as_mapping(configuration["roots"])
    report_root = Path(args.backtest_report_root).expanduser().resolve() if getattr(
        args, "backtest_report_root", None
    ) else Path(str(roots["bybit"])) / "reports" / "equity_curves"
    end = dt.date.fromisoformat(str(configuration["end_exclusive"]))
    start_ms = _date_ms(args.reconcile_start) if args.reconcile_start else None
    report = reconcile_account_roots_to_backtest(
        demo_account_root=demo_root,
        paper_account_root=paper_root,
        backtest_report_root=report_root,
        venue="bybit",
        start_ms=start_ms,
        end_ms=_date_ms(end),
        code_commit=code_commit,
        account_snapshot_commit=args.account_snapshot_commit,
        sleeves=tuple(str(value) for value in configuration["sleeves"]),
    )
    out = Path(args.reconcile_out).expanduser().resolve() if getattr(args, "reconcile_out", None) else (
        run_dir / "reconciliation"
    )
    paths = write_three_way_artifacts(report, out)
    if ledger is not None:
        ledger.append(
            {
                "step_id": "reconcile.demo_paper_backtest",
                "status": "succeeded",
                "snapshot_id": report["snapshot_id"],
                "artifacts": paths,
            }
        )
    return paths


def _execute(args: argparse.Namespace) -> int:
    configuration = _manifest_configuration(args)
    end = dt.date.fromisoformat(str(configuration["end_exclusive"]))
    start = dt.date.fromisoformat(str(configuration["window_start"]))
    if end <= start:
        raise ValueError("benchmark end must be later than start")
    code_commit = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain=v1"))
    if dirty and not args.allow_dirty:
        raise RuntimeError("research refresh requires a clean source tree; use --allow-dirty only for exploration")
    run_id = args.run_id or f"{end.isoformat()}-{code_commit[:12]}"
    output_root = Path(args.output_root).expanduser()
    if not output_root.is_absolute():
        output_root = REPO / output_root
    run_dir = output_root.resolve() / run_id
    _prepare_run_manifest(
        run_dir=run_dir,
        configuration=configuration,
        code_commit=code_commit,
        dirty=dirty,
    )
    ledger = RunLedger(run_dir)
    ledger.append({"step_id": "run", "status": "started_or_resumed"})
    roots = {venue: Path(path) for venue, path in _as_mapping(configuration["roots"]).items()}
    venues = tuple(str(value) for value in configuration["venues"])
    sleeves = tuple(str(value) for value in configuration["sleeves"])

    for venue in venues:
        root = roots[venue]
        root.mkdir(parents=True, exist_ok=True)
        if not args.skip_data:
            _refresh_data(venue=venue, root=root, end=end, args=args, ledger=ledger)
        if not args.skip_features and "continuous" in sleeves:
            ledger.run(_rmom_step(root, venue=venue, end=end))
            _validate_rmom(root, venue=venue, end=end)
        if not args.skip_backtests:
            for sleeve in sleeves:
                ledger.run(_backtest_step(venue=venue, sleeve=sleeve, root=root, start=start, end=end))
                _validate_backtest_report(
                    venue=venue,
                    sleeve=sleeve,
                    root=root,
                    start=start,
                    end=end,
                )

    if not args.skip_backtests:
        summary, summary_path = _run_summary(
            run_dir=run_dir,
            roots=roots,
            venues=venues,
            sleeves=sleeves,
            start=start,
            end=end,
            code_commit=code_commit,
        )
        ledger.append(
            {
                "step_id": "summary",
                "status": "succeeded",
                "snapshot_id": summary["snapshot_id"],
                "path": str(summary_path),
            }
        )
    _run_reconciliation(
        args=args,
        run_dir=run_dir,
        configuration=configuration,
        code_commit=code_commit,
        ledger=ledger,
    )
    ledger.append({"step_id": "run", "status": "succeeded"})
    print(f"\nresearch refresh complete: {run_dir}")
    return 0


def _reconcile_only(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).expanduser().resolve(strict=True)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    configuration = _as_mapping(manifest.get("configuration"))
    code_commit = str(manifest.get("code_commit") or "")
    if len(code_commit) != 40:
        raise RuntimeError("run manifest lacks a full code commit")
    paths = _run_reconciliation(
        args=args,
        run_dir=run_dir,
        configuration=configuration,
        code_commit=code_commit,
        ledger=RunLedger(run_dir),
    )
    if paths is None:
        raise RuntimeError("reconcile requires demo and paper account roots")
    print(json.dumps(paths, indent=2))
    return 0


def _add_refresh_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--end",
        type=_date,
        default=_utc_now().date(),
        help="exclusive UTC date boundary; default is today's UTC boundary (latest completed day)",
    )
    parser.add_argument("--start", type=_date, default=None, help="benchmark start; default end minus --years")
    parser.add_argument("--years", type=int, default=3, help="benchmark window when --start is absent")
    parser.add_argument("--venues", default="bybit,binance", help="comma list: bybit,binance")
    parser.add_argument("--sleeves", default="long,continuous", help="comma list: long,continuous")
    parser.add_argument("--bybit-root", default=str(DEFAULT_ROOTS["bybit"]))
    parser.add_argument("--binance-root", default=str(DEFAULT_ROOTS["binance"]))
    parser.add_argument(
        "--data-mode",
        choices=("tail", "canonical"),
        default="tail",
        help="tail is append-first; canonical invokes the complete full-PIT builders",
    )
    parser.add_argument("--overlap-days", type=int, default=7, help="tail days recomputed before append")
    parser.add_argument("--output-root", default="reports/research-refresh")
    parser.add_argument("--run-id", default=None, help="stable ID enables exact-step resume")
    parser.add_argument("--preregistration", default=None, help="optional frozen contract path to hash")
    parser.add_argument("--skip-data", action="store_true")
    parser.add_argument("--skip-features", action="store_true")
    parser.add_argument("--skip-backtests", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true", help="label and allow exploratory dirty-source run")
    parser.add_argument("--manifest-workers", type=int, default=16)
    parser.add_argument("--kline-workers", type=int, default=8)
    parser.add_argument("--ancillary-workers", type=int, default=4)
    parser.add_argument("--binance-workers", type=int, default=24)
    parser.add_argument("--binance-job-batch-size", type=int, default=48)
    parser.add_argument("--demo-account-root", default=None, help="frozen read-only demo account snapshot")
    parser.add_argument("--paper-account-root", default=None, help="frozen read-only paper account snapshot")
    parser.add_argument("--reconcile-start", type=_date, default=None)
    parser.add_argument("--account-snapshot-commit", default=None)
    parser.add_argument("--backtest-report-root", default=None)
    parser.add_argument("--reconcile-out", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="print the exact workflow without mutation")
    _add_refresh_arguments(plan)
    run = sub.add_parser("run", help="execute or resume the workflow")
    _add_refresh_arguments(run)
    reconcile = sub.add_parser("reconcile", help="reconcile frozen account snapshots with a completed run")
    reconcile.add_argument("--run-dir", required=True)
    reconcile.add_argument("--demo-account-root", required=True)
    reconcile.add_argument("--paper-account-root", required=True)
    reconcile.add_argument("--reconcile-start", type=_date, default=None)
    reconcile.add_argument("--account-snapshot-commit", default=None)
    reconcile.add_argument("--backtest-report-root", default=None)
    reconcile.add_argument("--reconcile-out", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        return _plan(args)
    if args.command == "run":
        return _execute(args)
    if args.command == "reconcile":
        return _reconcile_only(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
