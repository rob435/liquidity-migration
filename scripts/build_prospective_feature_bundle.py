#!/usr/bin/env python3
"""Build create-only, PIT-filtered features from a verified snapshot reconstruction.

The command never reads a shared-root feature file and never emits returns,
trades, P&L, or aggregate alpha metrics. Raw Parquet bytes are checked against
the immutable SQLite container before those exact in-memory bytes are decoded.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import types
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Iterable, Sequence

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _install_import_only_windows_fcntl_guard() -> None:
    """Permit pure feature imports while failing closed on any lock call."""

    if os.name != "nt" or "fcntl" in sys.modules:
        return
    module = types.ModuleType("fcntl")
    module.LOCK_SH = 1  # type: ignore[attr-defined]
    module.LOCK_EX = 2  # type: ignore[attr-defined]
    module.LOCK_NB = 4  # type: ignore[attr-defined]
    module.LOCK_UN = 8  # type: ignore[attr-defined]

    def _forbidden_flock(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("immutable feature reconstruction must not invoke POSIX locking")

    module.flock = _forbidden_flock  # type: ignore[attr-defined]
    sys.modules["fcntl"] = module


_install_import_only_windows_fcntl_guard()

from liquidity_migration._common import (  # noqa: E402
    _exclude_symbols,
    date_ms,
)
from liquidity_migration.continuous_events import (  # noqa: E402
    ContinuousEventConfig,
    continuous_source_decile_panel,
    cross_sectional_decile,
    per_symbol_timeseries_features,
    require_stable_residual_momentum,
)
from liquidity_migration.continuous_profile import (  # noqa: E402
    ACTIVE_CONTINUOUS_COMPONENTS,
)
from liquidity_migration.daily_feature_panel import _aggregate_daily_klines  # noqa: E402
from liquidity_migration.long_native import (  # noqa: E402
    _filter_signal_window,
    build_long_features_from_daily,
    long_v11a_profile,
)
from liquidity_migration.momentum_signals import daily_bars  # noqa: E402
from liquidity_migration.residual_momentum import (  # noqa: E402
    RMOM_CAUSAL_SHIFT,
    RMOM_MIN_SAMPLES,
    RMOM_WINDOW,
    residual_momentum_from_residuals,
)
from liquidity_migration.risk_model import (  # noqa: E402
    COMMON4_FACTOR_COLUMNS,
    build_factor_panel_from_daily,
    fit_factor_returns,
)
from liquidity_migration.volume_events_pit import (  # noqa: E402
    _full_pit_universe_coverage,
    filter_klines_to_pit_membership,
)

RAW_COLUMNS = (
    "ts_ms",
    "symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "turnover_quote",
    "volume_base",
)
CONTINUOUS_TAIL_ROWS = 888
FEATURE_SET = ("max_ret168",)
RMOM_QUANTILE = 0.25


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _payload_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


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


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object: {path}")
    return value


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _write_parquet_create_only(path: Path, frame: pl.DataFrame) -> dict[str, Any]:
    if path.exists():
        raise FileExistsError(f"create-only feature output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    day = start
    while day < end:
        yield day
        day += dt.timedelta(days=1)


def _chunks(start: dt.date, end: dt.date, days: int) -> list[tuple[dt.date, dt.date]]:
    if days < 1:
        raise ValueError("chunk-days must be positive")
    output: list[tuple[dt.date, dt.date]] = []
    cursor = start
    while cursor < end:
        boundary = min(cursor + dt.timedelta(days=days), end)
        output.append((cursor, boundary))
        cursor = boundary
    return output


def _chunk_tag(start: dt.date, end: dt.date) -> str:
    return f"{start.isoformat()}_{end.isoformat()}"


def _expected_rows(
    connection: sqlite3.Connection,
    *,
    dataset: str,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> list[tuple[str, int, str]]:
    if start is None or end is None:
        rows = connection.execute(
            "SELECT relative_path,size,sha256 FROM files WHERE dataset=? ORDER BY relative_path",
            (dataset,),
        ).fetchall()
    else:
        lower = f"{dataset}/date={start.isoformat()}"
        upper = f"{dataset}/date={end.isoformat()}"
        rows = connection.execute(
            """
            SELECT relative_path,size,sha256
            FROM files
            WHERE dataset=? AND relative_path>=? AND relative_path<?
            ORDER BY relative_path
            """,
            (dataset, lower, upper),
        ).fetchall()
    return [(str(path), int(size), str(digest)) for path, size, digest in rows]


def _assert_regular_descriptor(path: Path, data: bytes, before: os.stat_result, after: os.stat_result) -> None:
    if not stat.S_ISREG(after.st_mode):
        raise RuntimeError(f"snapshot reconstruction contains a non-regular file: {path}")
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if getattr(after, "st_file_attributes", 0) & reparse:
        raise RuntimeError(f"snapshot reconstruction contains a reparse point: {path}")
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or len(data) != after.st_size:
        raise RuntimeError(f"snapshot reconstruction file changed during its stable read: {path}")


def _decode_parquet_buffers(buffers: list[io.BytesIO], columns: Sequence[str] | None) -> pl.DataFrame:
    if not buffers:
        return pl.DataFrame()
    try:
        return pl.read_parquet(
            buffers,
            columns=list(columns) if columns is not None else None,
            rechunk=True,
            missing_columns="insert",
        )
    except (pl.exceptions.PolarsError, OSError):
        frames: list[pl.DataFrame] = []
        for buffer in buffers:
            buffer.seek(0)
            frames.append(pl.read_parquet(buffer))
        output = pl.concat(frames, how="diagonal_relaxed", rechunk=True)
        if columns is not None:
            output = output.select([column for column in columns if column in output.columns])
        return output


def _verified_read(
    connection: sqlite3.Connection,
    *,
    reconstruction_root: Path,
    dataset: str,
    columns: Sequence[str] | None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> tuple[pl.DataFrame, dict[str, Any]]:
    expected = _expected_rows(connection, dataset=dataset, start=start, end=end)
    if not expected:
        window = "all" if start is None else f"[{start},{end})"
        raise FileNotFoundError(f"snapshot container has no {dataset} files for {window}")
    buffers: list[io.BytesIO] = []
    identity_digest = hashlib.sha256()
    total_bytes = 0
    for relative, expected_size, expected_sha in expected:
        path = reconstruction_root.joinpath(*relative.split("/"))
        if path.is_symlink():
            raise RuntimeError(f"snapshot reconstruction contains a symlink: {path}")
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
        _assert_regular_descriptor(path, data, before, after)
        actual_sha = hashlib.sha256(data).hexdigest()
        if len(data) != expected_size or actual_sha != expected_sha:
            raise RuntimeError(
                "snapshot reconstruction content mismatch: "
                f"{relative} expected_size={expected_size} actual_size={len(data)} "
                f"expected_sha256={expected_sha} actual_sha256={actual_sha}"
            )
        identity = {"path": relative, "bytes": expected_size, "sha256": expected_sha}
        identity_digest.update(_canonical_json(identity))
        identity_digest.update(b"\n")
        total_bytes += expected_size
        buffers.append(io.BytesIO(data))
    frame = _decode_parquet_buffers(buffers, columns)
    return frame, {
        "algorithm": "sha256(sorted canonical {path,bytes,sha256})",
        "file_count": len(expected),
        "bytes": total_bytes,
        "aggregate_sha256": identity_digest.hexdigest(),
        "exact_container_bytes_decoded": True,
    }


def _validate_raw_klines(frame: pl.DataFrame, *, tag: str) -> None:
    missing = sorted(set(RAW_COLUMNS) - set(frame.columns))
    if missing:
        raise RuntimeError(f"{tag} klines missing required columns: {missing}")
    duplicate_count = frame.height - frame.select("symbol", "ts_ms").unique().height
    if duplicate_count:
        raise RuntimeError(f"{tag} klines have duplicate (symbol,ts_ms) keys: {duplicate_count}")
    invalid = frame.filter(
        pl.col("symbol").is_null()
        | (pl.col("symbol").str.strip_chars() == "")
        | pl.col("ts_ms").is_null()
        | pl.any_horizontal(
            [
                pl.col(column).is_null()
                | ~pl.col(column).is_finite()
                | (pl.col(column) <= 0.0)
                for column in ("open", "high", "low", "close")
            ]
        )
        | pl.col("turnover_quote").is_null()
        | ~pl.col("turnover_quote").is_finite()
        | (pl.col("turnover_quote") < 0.0)
        | pl.col("volume_base").is_null()
        | ~pl.col("volume_base").is_finite()
        | (pl.col("volume_base") < 0.0)
    )
    if not invalid.is_empty():
        raise RuntimeError(f"{tag} klines contain invalid structural/numeric rows: {invalid.height}")


def _frame_summary(frame: pl.DataFrame, *, keys: Sequence[str]) -> dict[str, Any]:
    present_keys = [key for key in keys if key in frame.columns]
    unique_keys = frame.select(present_keys).unique().height if present_keys else 0
    nulls = frame.null_count()
    return {
        "rows": frame.height,
        "schema": {name: str(dtype) for name, dtype in frame.schema.items()},
        "keys": present_keys,
        "unique_keys": unique_keys,
        "duplicate_keys": frame.height - unique_keys if present_keys else None,
        "null_counts": {name: int(nulls[name][0]) for name in nulls.columns},
    }


def _continuous_chunk_features(
    raw_chunk: pl.DataFrame,
    carry: pl.DataFrame,
    rmom: pl.DataFrame,
    *,
    chunk_start_ms: int,
    chunk_end_ms: int,
    output_start_ms: int,
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Compute one exact rolling-state chunk and its next raw-row carry."""

    columns = ["ts_ms", "symbol", "close", "turnover_quote"]
    current = raw_chunk.select(columns)
    combined = (
        pl.concat([carry.select(columns), current], how="vertical_relaxed")
        if not carry.is_empty()
        else current
    )
    combined = combined.unique(["symbol", "ts_ms"]).sort(["symbol", "ts_ms"])
    featured = per_symbol_timeseries_features(combined)
    target_start = max(chunk_start_ms, output_start_ms)
    target = featured.filter(
        (pl.col("ts_ms") >= target_start) & (pl.col("ts_ms") < chunk_end_ms)
    )
    next_carry = (
        combined.group_by("symbol", maintain_order=True)
        .tail(CONTINUOUS_TAIL_ROWS)
        .sort(["symbol", "ts_ms"])
    )
    if target.is_empty():
        return pl.DataFrame(), pl.DataFrame(), next_carry
    source = continuous_source_decile_panel(target, rmom, feature_set=FEATURE_SET)
    active = cross_sectional_decile(
        target,
        rmom,
        rmom_quantile=RMOM_QUANTILE,
        feature_set=FEATURE_SET,
    )
    return source, active, next_carry


def _validate_existing_final(output: Path) -> dict[str, Any]:
    receipt_path = output / "feature_receipt.json"
    if not receipt_path.is_file():
        raise FileExistsError(f"feature output exists without a receipt: {output}")
    receipt = _read_json(receipt_path)
    for relative, identity in receipt.get("files", {}).items():
        path = output.joinpath(*relative.split("/"))
        if not path.is_file() or path.stat().st_size != identity.get("bytes") or _sha256(path) != identity.get("sha256"):
            raise RuntimeError(f"existing feature artifact failed identity verification: {path}")
    return receipt


def _validate_identity_inputs(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve(strict=True)
    contract = args.contract.expanduser().resolve(strict=True)
    snapshot_receipt_path = args.snapshot_receipt.expanduser().resolve(strict=True)
    reconstruction_receipt_path = args.reconstruction_receipt.expanduser().resolve(strict=True)
    snapshot = _read_json(snapshot_receipt_path)
    reconstruction = _read_json(reconstruction_receipt_path)
    container = Path(snapshot["container"]["path"]).resolve(strict=True)
    contract_sha = _sha256(contract)
    snapshot_receipt_sha = _sha256(snapshot_receipt_path)
    reconstruction_receipt_sha = _sha256(reconstruction_receipt_path)
    container_sha = _sha256(container)
    if contract_sha != snapshot.get("contract_sha256"):
        raise RuntimeError("contract bytes do not match the snapshot receipt")
    if container_sha != snapshot.get("container", {}).get("sha256"):
        raise RuntimeError("snapshot container SHA-256 does not match its receipt")
    if snapshot_receipt_sha != reconstruction.get("source_receipt_sha256"):
        raise RuntimeError("reconstruction does not point to the supplied snapshot receipt")
    if str(root) != str(Path(reconstruction["output_root"]).resolve()):
        raise RuntimeError("--root does not match the verified reconstruction receipt")
    for field in ("container_sha256", "logical_sha256", "file_count", "total_bytes"):
        expected = snapshot["container"]["sha256"] if field == "container_sha256" else snapshot[field]
        if reconstruction.get(field) != expected:
            raise RuntimeError(f"snapshot/reconstruction {field} mismatch")
    if not reconstruction.get("full_content_verified_before_and_during_extraction"):
        raise RuntimeError("reconstruction receipt lacks full content verification")
    return {
        "root": root,
        "contract": contract,
        "contract_sha256": contract_sha,
        "snapshot_receipt": snapshot_receipt_path,
        "snapshot_receipt_sha256": snapshot_receipt_sha,
        "reconstruction_receipt": reconstruction_receipt_path,
        "reconstruction_receipt_sha256": reconstruction_receipt_sha,
        "container": container,
        "container_sha256": container_sha,
        "snapshot": snapshot,
        "reconstruction": reconstruction,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot-receipt", type=Path, required=True)
    parser.add_argument("--reconstruction-receipt", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--start", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--end", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chunk-days", type=int, default=14)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.end <= args.start:
        raise ValueError("feature end must be after start")
    identities = _validate_identity_inputs(args)
    snapshot = identities["snapshot"]
    raw_start = dt.date.fromisoformat(snapshot["window"]["start"])
    snapshot_end = dt.date.fromisoformat(snapshot["window"]["end_exclusive"])
    if args.start < raw_start or args.end > snapshot_end:
        raise RuntimeError("requested feature window falls outside the immutable snapshot")
    head = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain=v1"))
    if dirty and not args.allow_dirty:
        raise RuntimeError("feature reconstruction requires clean code; use --allow-dirty only for exploration")
    output = args.out.expanduser().resolve()
    if output.exists():
        print(json.dumps(_validate_existing_final(output), sort_keys=True))
        return 0

    chunk_ranges = _chunks(raw_start, args.end, args.chunk_days)
    run_identity = {
        "schema_version": 1,
        "kind": "prospective_pit_feature_bundle",
        "code_commit": head,
        "git_dirty": dirty,
        "contract_sha256": identities["contract_sha256"],
        "snapshot_receipt_sha256": identities["snapshot_receipt_sha256"],
        "reconstruction_receipt_sha256": identities["reconstruction_receipt_sha256"],
        "container_sha256": identities["container_sha256"],
        "raw_window": {"start": raw_start.isoformat(), "end_exclusive": args.end.isoformat()},
        "feature_window": {"start": args.start.isoformat(), "end_exclusive": args.end.isoformat()},
        "chunk_days": args.chunk_days,
        "command": [str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
        "writes_shared_root": False,
        "reads_shared_features": False,
        "outcomes_inspected": False,
    }
    run_identity_sha = _payload_sha256(run_identity)
    preflight = {
        **run_identity,
        "run_identity_sha256": run_identity_sha,
        "snapshot_file_count": snapshot["file_count"],
        "snapshot_total_bytes": snapshot["total_bytes"],
        "chunk_count": len(chunk_ranges),
    }
    if args.preflight:
        print(json.dumps(preflight, sort_keys=True))
        return 0

    work = output.with_name(f".{output.name}.working")
    work.mkdir(parents=True, exist_ok=True)
    checkpoint_path = work / "build_checkpoint.json"
    if checkpoint_path.exists():
        checkpoint = _read_json(checkpoint_path)
        if checkpoint.get("run_identity_sha256") != run_identity_sha:
            raise RuntimeError(f"feature working directory belongs to another run: {work}")
    else:
        checkpoint = {
            "schema_version": 1,
            "run_identity_sha256": run_identity_sha,
            "daily_chunks": {},
            "continuous_chunks": {},
            "core_features": None,
        }
        _write_json_atomic(checkpoint_path, checkpoint)

    uri = f"file:{identities['container'].as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        manifest, manifest_identity = _verified_read(
            connection,
            reconstruction_root=identities["root"],
            dataset="archive_trade_manifest",
            columns=None,
        )
        if manifest.is_empty() or not {"date", "symbol"}.issubset(manifest.columns):
            raise RuntimeError("verified archive_trade_manifest is empty or malformed")

        for index, (chunk_start, chunk_end) in enumerate(chunk_ranges, start=1):
            tag = _chunk_tag(chunk_start, chunk_end)
            if tag in checkpoint["daily_chunks"]:
                continue
            print(f"daily-input chunk {index}/{len(chunk_ranges)} {tag}", flush=True)
            raw, input_identity = _verified_read(
                connection,
                reconstruction_root=identities["root"],
                dataset="klines_1h",
                columns=RAW_COLUMNS,
                start=chunk_start,
                end=chunk_end,
            )
            _validate_raw_klines(raw, tag=tag)
            coverage = raw.group_by(["date", "symbol"]).agg(pl.len().alias("hourly_bars"))
            filtered, pit_receipt = filter_klines_to_pit_membership(raw, manifest)
            pit_pairs = filtered.select("date", "symbol").unique().sort(["date", "symbol"])
            factor_daily = _aggregate_daily_klines(filtered)
            long_daily = daily_bars(filtered)
            paths = {
                "factor": work / "intermediate" / "daily_factor" / f"{tag}.parquet",
                "long": work / "intermediate" / "daily_long" / f"{tag}.parquet",
                "coverage": work / "intermediate" / "coverage" / f"{tag}.parquet",
                "pit_pairs": work / "intermediate" / "pit_pairs" / f"{tag}.parquet",
            }
            chunk_file_identities = {
                name: _write_parquet_create_only(path, frame)
                for name, path, frame in (
                    ("factor", paths["factor"], factor_daily),
                    ("long", paths["long"], long_daily),
                    ("coverage", paths["coverage"], coverage),
                    ("pit_pairs", paths["pit_pairs"], pit_pairs),
                )
            }
            checkpoint["daily_chunks"][tag] = {
                "input_identity": input_identity,
                "pit_filter": pit_receipt,
                "files": {
                    name: {
                        "path": path.relative_to(work).as_posix(),
                        **chunk_file_identities[name],
                    }
                    for name, path in paths.items()
                },
            }
            _write_json_atomic(checkpoint_path, checkpoint)

        daily_factor = pl.concat(
            [
                pl.read_parquet(work / checkpoint["daily_chunks"][_chunk_tag(a, b)]["files"]["factor"]["path"])
                for a, b in chunk_ranges
            ],
            how="vertical_relaxed",
            rechunk=True,
        ).sort(["symbol", "ts_ms"])
        daily_long = pl.concat(
            [
                pl.read_parquet(work / checkpoint["daily_chunks"][_chunk_tag(a, b)]["files"]["long"]["path"])
                for a, b in chunk_ranges
            ],
            how="vertical_relaxed",
            rechunk=True,
        ).sort(["symbol", "ts_ms"])
        coverage_frame = pl.concat(
            [
                pl.read_parquet(work / checkpoint["daily_chunks"][_chunk_tag(a, b)]["files"]["coverage"]["path"])
                for a, b in chunk_ranges
            ],
            how="vertical_relaxed",
            rechunk=True,
        )
        pit_pairs = pl.concat(
            [
                pl.read_parquet(work / checkpoint["daily_chunks"][_chunk_tag(a, b)]["files"]["pit_pairs"]["path"])
                for a, b in chunk_ranges
            ],
            how="vertical_relaxed",
            rechunk=True,
        ).unique(["date", "symbol"])

        manifest_window = manifest.filter(
            (pl.col("date") >= raw_start.isoformat()) & (pl.col("date") < args.end.isoformat())
        )
        all_kline_pairs = coverage_frame.select("date", "symbol").unique()
        covered_pairs = {
            (str(date), str(symbol))
            for date, symbol in coverage_frame.filter(pl.col("hourly_bars") >= 20)
            .select("date", "symbol")
            .iter_rows()
        }
        coverage = _full_pit_universe_coverage(
            all_kline_pairs,
            manifest_window,
            kline_covered_date_symbols=covered_pairs,
        )
        missing_required = sorted(coverage.missing_required_date_symbols)
        missing_frame = pl.DataFrame(
            {
                "date": [date for date, _symbol in missing_required],
                "symbol": [symbol for _date, symbol in missing_required],
            },
            schema={"date": pl.String, "symbol": pl.String},
        )
        manifest_keys = {
            (str(date), str(symbol))
            for date, symbol in manifest_window.select("date", "symbol").unique().iter_rows()
        }
        unmatched_kline = sorted(
            (str(date), str(symbol))
            for date, symbol in all_kline_pairs.iter_rows()
            if (str(date), str(symbol)) not in manifest_keys
        )
        unmatched_frame = pl.DataFrame(
            {
                "date": [date for date, _symbol in unmatched_kline],
                "symbol": [symbol for _date, symbol in unmatched_kline],
            },
            schema={"date": pl.String, "symbol": pl.String},
        )
        used_membership = pit_pairs.join(
            manifest_window.unique(["date", "symbol"]),
            on=["date", "symbol"],
            how="left",
        )
        provenance_columns = [
            column
            for column in ("source", "membership_source", "membership_inferred")
            if column in used_membership.columns
        ]
        provenance_counts = (
            used_membership.group_by(provenance_columns)
            .agg(pl.len().alias("date_symbol_pairs"))
            .sort(provenance_columns)
            .to_dicts()
            if provenance_columns
            else []
        )
        pit_files = {
            "missing_required": _write_parquet_create_only(
                work / "pit" / "missing_required.parquet", missing_frame
            ),
            "unmatched_kline": _write_parquet_create_only(
                work / "pit" / "unmatched_kline_pairs.parquet", unmatched_frame
            ),
        }
        pit_report = {
            "schema_version": 1,
            "full_pit_universe_pass": coverage.passed,
            "manifest_symbols": len(coverage.manifest_symbols),
            "kline_symbols": len(coverage.kline_symbols),
            "missing_symbols": len(coverage.missing_symbols),
            "required_date_symbol_pairs": len(coverage.required_date_symbols),
            "covered_date_symbol_pairs": len(coverage.covered_date_symbols),
            "missing_required_date_symbol_pairs": len(missing_required),
            "raw_kline_pairs_without_membership": len(unmatched_kline),
            "pit_member_date_symbol_pairs": pit_pairs.height,
            "provenance_counts": provenance_counts,
            "files": pit_files,
            "outcomes_inspected": False,
        }
        _write_json_atomic(work / "pit" / "pit_report.json", pit_report)
        if not coverage.passed:
            raise RuntimeError(
                "PIT coverage failed before feature construction; "
                f"missing_symbols={len(coverage.missing_symbols)} "
                f"missing_required_pairs={len(missing_required)}"
            )

        if checkpoint["core_features"] is None:
            print("building LONG and residual-momentum feature owners", flush=True)
            long_config = replace(
                long_v11a_profile(),
                start_date=args.start.isoformat(),
                end_date=args.end.isoformat(),
            )
            long_input = _exclude_symbols(daily_long, long_config.exclude_symbols)
            long_features = _filter_signal_window(
                build_long_features_from_daily(long_input, config=long_config),
                start=long_config.start_date,
                end=long_config.end_date,
            )
            factor_panel = build_factor_panel_from_daily(
                daily_factor,
                start=args.start.isoformat(),
                end=args.end.isoformat(),
            )
            _factor_returns, residuals = fit_factor_returns(
                factor_panel,
                factor_cols=list(COMMON4_FACTOR_COLUMNS),
            )
            rmom = residual_momentum_from_residuals(
                residuals.select("symbol", "ts_ms", "residual_return"),
                end=args.end.isoformat(),
            )
            long_path = work / "long_features.parquet"
            rmom_path = work / "residual_momentum.parquet"
            long_identity = _write_parquet_create_only(long_path, long_features)
            rmom_identity = _write_parquet_create_only(rmom_path, rmom)
            checkpoint["core_features"] = {
                "long": {
                    "path": long_path.relative_to(work).as_posix(),
                    **long_identity,
                    "summary": _frame_summary(long_features, keys=("symbol", "ts_ms")),
                },
                "residual_momentum": {
                    "path": rmom_path.relative_to(work).as_posix(),
                    **rmom_identity,
                    "summary": _frame_summary(rmom, keys=("symbol", "ts_ms")),
                    "provisional_rows": rmom.filter(pl.col("is_provisional")).height,
                },
                "factor_panel_structural": _frame_summary(
                    factor_panel,
                    keys=("symbol", "ts_ms"),
                ),
                "residual_owner": {
                    "window": RMOM_WINDOW,
                    "causal_shift": RMOM_CAUSAL_SHIFT,
                    "minimum_observations": RMOM_MIN_SAMPLES,
                    "factor_columns": list(COMMON4_FACTOR_COLUMNS),
                },
                "long_config": asdict(long_config),
            }
            _write_json_atomic(checkpoint_path, checkpoint)
        rmom_path = work / checkpoint["core_features"]["residual_momentum"]["path"]
        if _sha256(rmom_path) != checkpoint["core_features"]["residual_momentum"]["sha256"]:
            raise RuntimeError("checkpointed residual-momentum feature failed identity verification")
        rmom = require_stable_residual_momentum(
            pl.read_parquet(rmom_path),
            source=rmom_path,
        ).rename({"ts_ms": "day_ts"})

        carry = pl.DataFrame(
            schema={
                "ts_ms": pl.Int64,
                "symbol": pl.String,
                "close": pl.Float64,
                "turnover_quote": pl.Float64,
            }
        )
        completed_continuous: list[str] = []
        for prior_start, prior_end in chunk_ranges:
            prior_tag = _chunk_tag(prior_start, prior_end)
            if prior_tag not in checkpoint["continuous_chunks"]:
                break
            completed_continuous.append(prior_tag)
        if completed_continuous:
            latest = checkpoint["continuous_chunks"][completed_continuous[-1]]
            carry = pl.read_parquet(work / latest["tail"]["path"])

        for index, (chunk_start, chunk_end) in enumerate(chunk_ranges, start=1):
            tag = _chunk_tag(chunk_start, chunk_end)
            if tag in checkpoint["continuous_chunks"]:
                continue
            print(f"continuous-feature chunk {index}/{len(chunk_ranges)} {tag}", flush=True)
            raw, second_identity = _verified_read(
                connection,
                reconstruction_root=identities["root"],
                dataset="klines_1h",
                columns=RAW_COLUMNS,
                start=chunk_start,
                end=chunk_end,
            )
            first_identity = checkpoint["daily_chunks"][tag]["input_identity"]
            if second_identity != first_identity:
                raise RuntimeError(f"second verified input read changed for {tag}")
            _validate_raw_klines(raw, tag=tag)
            filtered, second_pit = filter_klines_to_pit_membership(raw, manifest)
            first_pit = checkpoint["daily_chunks"][tag]["pit_filter"]
            if second_pit != first_pit:
                raise RuntimeError(f"second PIT filter receipt changed for {tag}")
            filtered = _exclude_symbols(filtered, ContinuousEventConfig().exclude_symbols)
            source, active, carry = _continuous_chunk_features(
                filtered,
                carry,
                rmom,
                chunk_start_ms=date_ms(chunk_start.isoformat()),
                chunk_end_ms=date_ms(chunk_end.isoformat()),
                output_start_ms=date_ms(args.start.isoformat()),
            )
            tail_path = work / "intermediate" / "continuous_tail" / f"{tag}.parquet"
            record: dict[str, Any] = {
                "input_identity": second_identity,
                "pit_filter": second_pit,
                "tail": {
                    "path": tail_path.relative_to(work).as_posix(),
                    **_write_parquet_create_only(tail_path, carry),
                },
                "source": None,
                "active": None,
            }
            if not source.is_empty():
                source_path = work / "continuous_source" / f"{tag}.parquet"
                record["source"] = {
                    "path": source_path.relative_to(work).as_posix(),
                    **_write_parquet_create_only(source_path, source),
                    "summary": _frame_summary(source, keys=("symbol", "ts_ms")),
                }
            if not active.is_empty():
                active_path = work / "continuous_active" / f"{tag}.parquet"
                record["active"] = {
                    "path": active_path.relative_to(work).as_posix(),
                    **_write_parquet_create_only(active_path, active),
                    "summary": _frame_summary(active, keys=("symbol", "ts_ms")),
                }
            checkpoint["continuous_chunks"][tag] = record
            _write_json_atomic(checkpoint_path, checkpoint)
    finally:
        connection.close()

    artifact_files: dict[str, dict[str, Any]] = {}
    for path in sorted(work.glob("**/*")):
        if not path.is_file() or path.name == "feature_receipt.json":
            continue
        relative = path.relative_to(work).as_posix()
        artifact_files[relative] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    source_summaries = [
        record["source"]["summary"]
        for record in checkpoint["continuous_chunks"].values()
        if record.get("source") is not None
    ]
    active_summaries = [
        record["active"]["summary"]
        for record in checkpoint["continuous_chunks"].values()
        if record.get("active") is not None
    ]
    continuous_config = ContinuousEventConfig(
        start_date=args.start.isoformat(),
        end_date=args.end.isoformat(),
    )
    receipt = {
        **run_identity,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "run_identity_sha256": run_identity_sha,
        "snapshot_logical_sha256": snapshot["logical_sha256"],
        "manifest_input_identity": manifest_identity,
        "klines_input_verification": {
            "first_and_second_full_content_reads_match_container": True,
            "chunks": len(checkpoint["daily_chunks"]),
            "files": sum(
                record["input_identity"]["file_count"]
                for record in checkpoint["daily_chunks"].values()
            ),
            "bytes": sum(
                record["input_identity"]["bytes"]
                for record in checkpoint["daily_chunks"].values()
            ),
        },
        "pit": _read_json(work / "pit" / "pit_report.json"),
        "core_features": checkpoint["core_features"],
        "continuous": {
            "config": asdict(continuous_config),
            "components": [asdict(component) for component in ACTIVE_CONTINUOUS_COMPONENTS],
            "feature_set": list(FEATURE_SET),
            "rmom_quantile": RMOM_QUANTILE,
            "tail_rows_per_symbol": CONTINUOUS_TAIL_ROWS,
            "source_chunks": len(source_summaries),
            "source_rows": sum(summary["rows"] for summary in source_summaries),
            "active_chunks": len(active_summaries),
            "active_rows": sum(summary["rows"] for summary in active_summaries),
            "source_null_counts": {
                column: sum(summary["null_counts"].get(column, 0) for summary in source_summaries)
                for column in sorted(
                    {column for summary in source_summaries for column in summary["null_counts"]}
                )
            },
            "active_null_counts": {
                column: sum(summary["null_counts"].get(column, 0) for summary in active_summaries)
                for column in sorted(
                    {column for summary in active_summaries for column in summary["null_counts"]}
                )
            },
            "chunk_key_uniqueness": "exact within disjoint timestamp chunks",
        },
        "files": artifact_files,
        "outcomes_inspected": False,
        "explicit_non_conclusions": [
            "no return or alpha conclusion",
            "no strategy or profile change",
            "no deployment-readiness conclusion",
            "no mainnet or real-money authority",
        ],
    }
    receipt["receipt_payload_sha256"] = _payload_sha256(receipt)
    _write_json_atomic(work / "feature_receipt.json", receipt)
    os.replace(work, output)
    print(
        json.dumps(
            {
                "output": str(output),
                "receipt_sha256": _sha256(output / "feature_receipt.json"),
                "pit_pass": receipt["pit"]["full_pit_universe_pass"],
                "outcomes_inspected": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
