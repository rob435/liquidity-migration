#!/usr/bin/env python3
"""Run the registered production-function LONG/CONTINUOUS comparator."""

from __future__ import annotations

import argparse
import bisect
import contextlib
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import polars as pl

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def _install_import_only_windows_fcntl_guard() -> None:
    if os.name != "nt" or "fcntl" in sys.modules:
        return
    module = types.ModuleType("fcntl")
    module.LOCK_SH = 1  # type: ignore[attr-defined]
    module.LOCK_EX = 2  # type: ignore[attr-defined]
    module.LOCK_NB = 4  # type: ignore[attr-defined]
    module.LOCK_UN = 8  # type: ignore[attr-defined]

    def _forbidden_flock(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "active comparator must install its explicit single-process I/O port"
        )

    module.flock = _forbidden_flock  # type: ignore[attr-defined]
    sys.modules["fcntl"] = module


_install_import_only_windows_fcntl_guard()

import liquidity_migration.account_kernel as account_kernel_module  # noqa: E402
import liquidity_migration.account_route as account_route_module  # noqa: E402
import liquidity_migration.continuous_btc_risk as btc_risk_module  # noqa: E402
import liquidity_migration.historical_account_replay as replay_module  # noqa: E402
import liquidity_migration.storage as storage_module  # noqa: E402
from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from liquidity_migration.account_kernel import (  # noqa: E402
    AccountRiskPolicy,
    InstrumentRules,
    read_account_journal,
)
from liquidity_migration.account_route import ensure_account_route  # noqa: E402
from liquidity_migration.active_runtime_comparator import (  # noqa: E402
    ActiveRuntimeComparator,
    ComparatorRunConfig,
    HistoricalHourlyCloseProvider,
)
from liquidity_migration.artifact_snapshot import StableFileSnapshot  # noqa: E402
from liquidity_migration.continuous_demo import (  # noqa: E402
    ContinuousDemoCycleConfig,
    apply_continuous_demo_profile,
)
from liquidity_migration.execution_adapters import (  # noqa: E402
    ExecutionTwinConfig,
    LatencyProfile,
)
from liquidity_migration.historical_account_replay import HistoricalAccountSession  # noqa: E402
from liquidity_migration.long_native import long_pump_family, long_v11a_profile  # noqa: E402
from liquidity_migration.long_native_event_demo import LongNativeDemoCycleConfig  # noqa: E402
from liquidity_migration.strategy_event_clock import MemoryStrategyEventTape  # noqa: E402
from liquidity_migration.strategy_funnel import payload_sha256  # noqa: E402

EPOCH_ROOT = REPO / "reports/prospective-runtime-parity-execution-epoch-2026-07-18"
FEATURE_ROOT = EPOCH_ROOT / "features/bybit-baseline"
RECONSTRUCTED_ROOT = EPOCH_ROOT / "reconstructed/bybit-baseline"
KLINE_ROOT = RECONSTRUCTED_ROOT / "klines_1h"
MANIFEST_ROOT = RECONSTRUCTED_ROOT / "archive_trade_manifest"
BASE_CONTRACT = REPO / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18.md"
AMENDMENTS = REPO / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_amendments.md"
FEATURE_RECEIPT = FEATURE_ROOT / "feature_receipt.json"
RECONSTRUCTION_RECEIPT = EPOCH_ROOT / "reconstruction/bybit-baseline.receipt.json"
DEFAULT_OUT = EPOCH_ROOT / "runtime-parity/active-production-comparator"
FIRST_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-467030bbb3c4"
)
FIRST_FAILED_ATTEMPT_TERMINATION = FIRST_FAILED_ATTEMPT_ROOT / "termination.json"
INDEXED_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-e45d90c55aa5"
)
INDEXED_FAILED_ATTEMPT_TERMINATION = (
    INDEXED_FAILED_ATTEMPT_ROOT / "termination.json"
)

EXPECTED_BASE_CONTRACT_SHA256 = "15edc498adf2bd068c33ff2f791fa3e46f161196db673a839adcf317aba35a31"
EXPECTED_AMENDMENTS_SHA256 = "ddfc778c96f5b985464848232f8daed7235c17978ee04d837341358e595e822d"
EXPECTED_FEATURE_RECEIPT_SHA256 = "1d50aeb731e0cc82a1963d57576f032228df5b375dbdb20375c01541d397af31"
EXPECTED_RECONSTRUCTION_RECEIPT_SHA256 = "c0aa73d8b2f9851f4cb5d46ba2b238bdb411da34eed0736997aeeb825c10d45a"
EXPECTED_RECONSTRUCTION_LOGICAL_SHA256 = "9fa1e3a87e813e7449464cf6b512c40cb82d0a13dbce60978e01079e688a81fe"
EXPECTED_FEATURE_PAYLOAD_SHA256 = "eff681990a9262a3b30781588ee80a7f7b2f67ca16c812b4edda8b86203061b0"
EXPECTED_FAILED_ATTEMPT_TERMINATION_SHA256 = "dd5df88b6d77fe181ba1fb1737b97fa3a62841065d16c425b1f26499954063d1"
EXPECTED_INDEXED_FAILED_ATTEMPT_TERMINATION_SHA256 = "701386b22989a35c39bb5cd544dc9377a02caa66d10dbd6b2c6fefa688cbc8ed"
EXPECTED_PREFIX_IDENTITIES = {
    "traces/continuous_gates/part-00000.parquet": (
        "ae7d56f33b6642a43227b8f4affd4c054f8be59f2fc90f27d9c777a4b5a41eb2"
    ),
    "traces/long_funnel/part-00000.parquet": (
        "31f4d87816b8972b18626eb8297e726ab6aa15efb48ea8286f977fed7090d83e"
    ),
}

LONG_START_MS = 1_677_628_800_000  # 2023-03-01T00:00:00Z
CONTINUOUS_START_MS = 1_680_307_200_000  # 2023-04-01T00:00:00Z
END_MS = 1_783_641_600_000  # 2026-07-10T00:00:00Z
CAPITAL_USDT = 1_000_000.0
MAX_ELAPSED_SECONDS = 14_400
PORTABLE_SEGMENT_MAX_EVENTS = 4096


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


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


def _write_json_create(path: Path, value: Any) -> None:
    if path.exists():
        raise FileExistsError(f"create-only comparator artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, path)


def _write_parquet_create(path: Path, frame: pl.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(f"create-only comparator artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_PORTABLE_MUTEX = threading.RLock()
_ORIGINAL_WRITE_TRANSACTION = account_kernel_module._write_transaction
_TRANSACTION_BUFFER: list[tuple[Path, tuple[Any, ...]]] = []


@contextlib.contextmanager
def _single_process_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
    with _PORTABLE_MUTEX:
        yield


def _portable_atomic_replace(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.portable.tmp")
    if temporary.write_bytes(data) != len(data):
        raise OSError("portable account write made no progress")
    os.replace(temporary, path)


def _buffer_transaction(root: str | Path, events: Sequence[Any]) -> Path:
    if not events:
        raise ValueError("cannot buffer an empty account transaction")
    directory = account_kernel_module.account_transactions_path(root)
    directory.mkdir(parents=True, exist_ok=True)
    batch = tuple(events)
    _TRANSACTION_BUFFER.append((Path(root), batch))
    return directory / f"{batch[0].sequence:020d}-{batch[-1].sequence:020d}.buffered"


def _portable_stable_read(path: str | Path, **_kwargs: object) -> StableFileSnapshot:
    resolved = Path(path).expanduser().resolve(strict=True)
    return StableFileSnapshot(
        path=resolved,
        data=resolved.read_bytes(),
        metadata=resolved.stat(),
    )


def _portable_route_create(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.write_bytes(data) != len(data):
        raise OSError("portable route write made no progress")


def _enable_historical_io() -> None:
    storage_module.exclusive_file_lock = _single_process_lock
    account_kernel_module.exclusive_file_lock = _single_process_lock
    account_kernel_module._atomic_replace = _portable_atomic_replace
    account_kernel_module._write_transaction = _buffer_transaction
    account_kernel_module._append_jsonl_projection = lambda *_args, **_kwargs: None
    replay_module.JsonlStrategyEventTape = lambda _path: MemoryStrategyEventTape()
    if os.name == "nt":
        account_route_module.exclusive_file_lock = _single_process_lock
        account_route_module._atomic_create = _portable_route_create
        account_route_module._fsync_directory = lambda _path: None
        account_route_module.read_stable_file = _portable_stable_read
        btc_risk_module._fsync_file = lambda _path: None
        btc_risk_module._fsync_directory = lambda _path: None


def _materialize_transactions(account_root: Path) -> dict[str, Any]:
    batches: list[tuple[Any, ...]] = []
    for root, events in _TRANSACTION_BUFFER:
        if root != account_root:
            raise RuntimeError("portable transaction buffer crossed account roots")
        batches.append(events)
    if not batches:
        return {
            "original_kernel_transactions": 0,
            "compact_authoritative_segments": 0,
            "original_transaction_boundaries_sha256": hashlib.sha256(
                _canonical_json({"boundaries": []})
            ).hexdigest(),
            "max_events_per_compact_segment": PORTABLE_SEGMENT_MAX_EVENTS,
        }
    boundaries = [
        {
            "first_sequence": int(batch[0].sequence),
            "last_sequence": int(batch[-1].sequence),
            "events": len(batch),
            "first_event_hash": str(batch[0].event_hash),
            "last_event_hash": str(batch[-1].event_hash),
        }
        for batch in batches
    ]
    current: list[Any] = []
    segments = 0
    for batch in batches:
        if current and len(current) + len(batch) > PORTABLE_SEGMENT_MAX_EVENTS:
            _ORIGINAL_WRITE_TRANSACTION(account_root, current)
            segments += 1
            current = []
        current.extend(batch)
    if current:
        _ORIGINAL_WRITE_TRANSACTION(account_root, current)
        segments += 1
    _TRANSACTION_BUFFER.clear()
    return {
        "original_kernel_transactions": len(batches),
        "compact_authoritative_segments": segments,
        "original_transaction_boundaries_sha256": hashlib.sha256(
            _canonical_json({"boundaries": boundaries})
        ).hexdigest(),
        "max_events_per_compact_segment": PORTABLE_SEGMENT_MAX_EVENTS,
    }


def _normalized_trace_row(row: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, (Mapping, list, tuple, set, frozenset)):
            output[str(key)] = _canonical_json(value).decode("utf-8")
        elif hasattr(value, "item") and callable(value.item):
            output[str(key)] = value.item()
        else:
            output[str(key)] = value
    return output


class _PartitionWriter:
    def __init__(self, root: Path, name: str, *, flush_rows: int = 20_000) -> None:
        self.root = root / "traces" / name
        self.flush_rows = flush_rows
        self.rows: list[dict[str, Any]] = []
        self.part = 0
        self.row_count = 0

    def append(self, row: Mapping[str, Any]) -> None:
        self.rows.append(_normalized_trace_row(row))
        if len(self.rows) >= self.flush_rows:
            self.flush()

    def flush(self) -> None:
        if not self.rows:
            return
        frame = pl.from_dicts(self.rows, infer_schema_length=None)
        _write_parquet_create(self.root / f"part-{self.part:05d}.parquet", frame)
        self.row_count += frame.height
        self.part += 1
        self.rows = []


class _ComparatorTraceWriter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cycles = _PartitionWriter(root, "cycles")
        self.long = _PartitionWriter(root, "long_funnel")
        self.decisions = _PartitionWriter(root, "source_decisions")
        self.requests = _PartitionWriter(root, "requests")
        self.intents = _PartitionWriter(root, "request_intents")
        self.gate_root = root / "traces/continuous_gates"
        self.gate_frames: list[pl.DataFrame] = []
        self.gate_buffer_rows = 0
        self.gate_rows = 0
        self.gate_part = 0
        self.accepted_requests = 0
        self.rejected_requests = 0

    def cycle(self, row: Mapping[str, Any]) -> None:
        self.cycles.append(row)

    def continuous_gates(self, frame: pl.DataFrame) -> None:
        self.gate_frames.append(frame)
        self.gate_buffer_rows += frame.height
        if self.gate_buffer_rows >= 100_000:
            self._flush_gates()

    def _flush_gates(self) -> None:
        if not self.gate_frames:
            return
        frame = pl.concat(self.gate_frames, how="diagonal_relaxed", rechunk=True)
        _write_parquet_create(
            self.gate_root / f"part-{self.gate_part:05d}.parquet",
            frame,
        )
        self.gate_rows += frame.height
        self.gate_part += 1
        self.gate_frames = []
        self.gate_buffer_rows = 0

    def long_funnel(self, row: Mapping[str, Any]) -> None:
        self.long.append(row)

    def source_decision(self, row: Mapping[str, Any]) -> None:
        self.decisions.append(row)

    def request(self, row: Mapping[str, Any]) -> None:
        if bool(row.get("accepted")):
            self.accepted_requests += 1
        else:
            self.rejected_requests += 1
        self.requests.append(row)

    def request_intent(self, row: Mapping[str, Any]) -> None:
        self.intents.append(row)

    def close(self) -> dict[str, int]:
        self._flush_gates()
        for writer in (
            self.cycles,
            self.long,
            self.decisions,
            self.requests,
            self.intents,
        ):
            writer.flush()
        return {
            "cycles": self.cycles.row_count,
            "continuous_gate_rows": self.gate_rows,
            "long_funnel_rows": self.long.row_count,
            "source_decisions": self.decisions.row_count,
            "requests": self.requests.row_count,
            "request_intents": self.intents.row_count,
            "accepted_requests": self.accepted_requests,
            "rejected_requests": self.rejected_requests,
        }


class _TimeFrameIndex:
    def __init__(self, frame: pl.DataFrame, *, key: str) -> None:
        self.key = key
        self.frame = frame.sort([key, "symbol"])
        counts = self.frame.group_by(key, maintain_order=True).len()
        self.timestamps = [int(value) for value in counts[key].to_list()]
        self._ranges: dict[int, tuple[int, int]] = {}
        offset = 0
        for timestamp, count in counts.iter_rows():
            length = int(count)
            self._ranges[int(timestamp)] = (offset, length)
            offset += length
        if offset != self.frame.height:
            raise RuntimeError("time-frame index failed to cover its source")

    def at(self, timestamp: int) -> pl.DataFrame:
        location = self._ranges.get(int(timestamp))
        if location is None:
            return pl.DataFrame(schema=self.frame.schema)
        return self.frame.slice(*location)

    def recent(self, timestamp: int, *, count: int) -> pl.DataFrame:
        high = bisect.bisect_right(self.timestamps, int(timestamp))
        selected = self.timestamps[max(0, high - count):high]
        if not selected:
            return pl.DataFrame(schema=self.frame.schema)
        return pl.concat([self.at(value) for value in selected], how="vertical")


def _registered_inputs() -> dict[str, dict[str, Any]]:
    expected = {
        "base_contract": (BASE_CONTRACT, EXPECTED_BASE_CONTRACT_SHA256),
        "amendments": (AMENDMENTS, EXPECTED_AMENDMENTS_SHA256),
        "feature_receipt": (FEATURE_RECEIPT, EXPECTED_FEATURE_RECEIPT_SHA256),
        "reconstruction_receipt": (
            RECONSTRUCTION_RECEIPT,
            EXPECTED_RECONSTRUCTION_RECEIPT_SHA256,
        ),
        "failed_attempt_termination": (
            FIRST_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "indexed_failed_attempt_termination": (
            INDEXED_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_INDEXED_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
    }
    output: dict[str, dict[str, Any]] = {}
    for name, (path, expected_sha) in expected.items():
        resolved = path.resolve(strict=True)
        actual = _sha256(resolved)
        if actual != expected_sha:
            raise RuntimeError(
                f"registered {name} identity changed: {actual} != {expected_sha}"
            )
        output[name] = {
            "path": str(resolved),
            "bytes": resolved.stat().st_size,
            "sha256": actual,
        }
    reconstruction = json.loads(RECONSTRUCTION_RECEIPT.read_text(encoding="utf-8"))
    if reconstruction.get("logical_sha256") != EXPECTED_RECONSTRUCTION_LOGICAL_SHA256:
        raise RuntimeError("reconstruction receipt logical identity changed")
    if reconstruction.get("full_content_verified_before_and_during_extraction") is not True:
        raise RuntimeError("reconstruction receipt is not fully verified")
    feature = json.loads(FEATURE_RECEIPT.read_text(encoding="utf-8"))
    if feature.get("receipt_payload_sha256") != EXPECTED_FEATURE_PAYLOAD_SHA256:
        raise RuntimeError("feature receipt payload identity changed")
    if feature.get("outcomes_inspected") is not False:
        raise RuntimeError("feature receipt is not outcome-blind")
    if feature.get("pit", {}).get("full_pit_universe_pass") is not True:
        raise RuntimeError("feature receipt PIT gate did not pass")
    return output


def _prefix_equivalence(work: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for relative, expected_sha in EXPECTED_PREFIX_IDENTITIES.items():
        path = work.joinpath(*relative.split("/"))
        if not path.is_file():
            raise RuntimeError(f"registered prefix artifact is missing: {path}")
        actual_sha = _sha256(path)
        matches = actual_sha == expected_sha
        files[relative] = {
            "bytes": path.stat().st_size,
            "expected_sha256": expected_sha,
            "actual_sha256": actual_sha,
            "matches": matches,
        }
        if not matches:
            raise RuntimeError(
                f"performance-refactor prefix identity changed: {relative}"
            )
    return {"status": "pass", "files": files}


def _verify_feature_files(receipt: Mapping[str, Any]) -> list[Path]:
    consumed = [
        "long_features.parquet",
        *sorted(
            relative
            for relative in receipt["files"]
            if str(relative).startswith("continuous_active/")
        ),
    ]
    paths: list[Path] = []
    for relative in consumed:
        identity = receipt["files"].get(relative)
        if not isinstance(identity, Mapping):
            raise RuntimeError(f"feature receipt lacks {relative}")
        path = FEATURE_ROOT.joinpath(*str(relative).split("/"))
        if (
            not path.is_file()
            or path.stat().st_size != int(identity["bytes"])
            or _sha256(path) != identity["sha256"]
        ):
            raise RuntimeError(f"feature artifact identity failed: {path}")
        paths.append(path)
    return paths


CONTINUOUS_COLUMNS = [
    "symbol",
    "ts_ms",
    "decision_ts_ms",
    "decile",
    "composite",
    "turnover_quote",
    "rv_168h",
    "ret1",
    "max_ret168",
    "prior6_ret1_max",
    "giveback_from_prior6_high",
    "turnover_spike_168h",
]


def _load_features(
    feature_paths: Sequence[Path],
) -> tuple[pl.DataFrame, pl.DataFrame]:
    long_path = next(path for path in feature_paths if path.name == "long_features.parquet")
    continuous_paths = [
        path for path in feature_paths if path.parent.name == "continuous_active"
    ]
    long = pl.read_parquet(long_path).filter(
        (pl.col("ts_ms") >= LONG_START_MS) & (pl.col("ts_ms") < END_MS)
    )
    continuous = (
        pl.read_parquet(continuous_paths, columns=CONTINUOUS_COLUMNS)
        .filter(
            (pl.col("decision_ts_ms") >= CONTINUOUS_START_MS)
            & (pl.col("decision_ts_ms") < END_MS)
        )
        .sort(["ts_ms", "symbol"])
    )
    if long.is_empty() or continuous.is_empty():
        raise RuntimeError("registered feature windows are empty")
    for name, frame, keys in (
        ("long", long, ["symbol", "ts_ms"]),
        ("continuous", continuous, ["symbol", "ts_ms"]),
    ):
        if frame.select(pl.struct(keys).n_unique()).item() != frame.height:
            raise RuntimeError(f"{name} feature window has duplicate logical keys")
        if frame.filter(
            pl.col("symbol").is_null() | (pl.col("symbol").str.strip_chars() == "")
        ).height:
            raise RuntimeError(f"{name} feature window has blank symbols")
    availability_failures = continuous.filter(
        pl.col("decision_ts_ms") != pl.col("ts_ms") + MS_PER_HOUR
    )
    if not availability_failures.is_empty():
        raise RuntimeError("continuous feature decision clock changed")
    return long.sort(["ts_ms", "symbol"]), continuous


def _long_pump_sources(
    long_features: pl.DataFrame,
    *,
    strategy: Any,
) -> tuple[pl.DataFrame, set[int]]:
    rows: list[dict[str, Any]] = []
    timestamps: set[int] = set()
    for row in long_features.iter_rows(named=True):
        pump = long_pump_family(row, strategy)
        if not bool(pump["trigger_any"]):
            continue
        timestamp = int(row["ts_ms"])
        timestamps.add(timestamp)
        rows.append(
            {
                "symbol": str(row["symbol"]).upper(),
                "signal_ts_ms": timestamp,
                "source_strength": pump["source_strength"],
                "trigger_1d": bool(pump["trigger_1d"]),
                "trigger_3d": bool(pump["trigger_3d"]),
                "trigger_7d": bool(pump["trigger_7d"]),
                "in_universe": bool(row.get("in_universe")),
                "regime_on": bool(row.get("regime_on")),
                "eth_regime_on": bool(row.get("eth_regime_on")),
                "today_volume_rank": row.get("today_volume_rank"),
                "symbol_age_days": row.get("symbol_age_days"),
                "turnover_median_90d": row.get("turnover_median_90d"),
                "close_location": row.get("close_location"),
                "close_loc_3d": row.get("close_loc_3d"),
                "close_loc_7d": row.get("close_loc_7d"),
                "atr_14d_pct": row.get("atr_14d_pct"),
            }
        )
    frame = (
        pl.from_dicts(rows, infer_schema_length=None).sort(["signal_ts_ms", "symbol"])
        if rows
        else pl.DataFrame()
    )
    return frame, timestamps


def _load_first_archive_days() -> tuple[dict[str, int], list[Path]]:
    paths = sorted(MANIFEST_ROOT.glob("date=*/part.parquet"))
    if not paths:
        raise RuntimeError("reconstructed PIT manifest is empty")
    frame = pl.read_parquet(
        paths,
        columns=[
            "symbol",
            "membership_source",
            "membership_inferred",
            "first_archive_observed_date",
        ],
    ).filter(
        (pl.col("membership_source") == "bybit_public_trading_archive")
        & (~pl.col("membership_inferred"))
    )
    if frame.is_empty():
        raise RuntimeError("PIT manifest has no direct archive observations")
    firsts = (
        frame.group_by("symbol")
        .agg(
            pl.col("first_archive_observed_date").min().alias("first"),
            pl.col("first_archive_observed_date").max().alias("last"),
        )
        .sort("symbol")
    )
    if firsts.filter(pl.col("first") != pl.col("last")).height:
        raise RuntimeError("PIT manifest first-observation identity is inconsistent")
    output: dict[str, int] = {}
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    for symbol, first, _last in firsts.iter_rows():
        day = dt.datetime.combine(
            dt.date.fromisoformat(str(first)),
            dt.time.min,
            tzinfo=dt.timezone.utc,
        )
        output[str(symbol).upper()] = int((day - epoch).total_seconds() * 1000)
    return output, paths


def _load_btc_klines() -> tuple[pl.DataFrame, list[Path]]:
    paths = sorted(KLINE_ROOT.glob("date=*/symbol=BTCUSDT/part.parquet"))
    if not paths:
        raise RuntimeError("reconstructed BTC hourly tape is empty")
    frame = (
        pl.read_parquet(paths, columns=["symbol", "ts_ms", "close"])
        .filter(
            (pl.col("ts_ms") < END_MS)
            & pl.col("close").is_not_null()
            & pl.col("close").is_finite()
            & (pl.col("close") > 0.0)
        )
        .sort("ts_ms")
    )
    if frame.is_empty() or frame.select(pl.col("ts_ms").n_unique()).item() != frame.height:
        raise RuntimeError("reconstructed BTC tape has empty or duplicate timestamps")
    return frame, paths


def _instrument_rules(symbols: set[str]) -> dict[str, InstrumentRules]:
    observed_ts_ns = LONG_START_MS * 1_000_000
    return {
        symbol: InstrumentRules(
            symbol=symbol,
            qty_step=1e-12,
            min_qty=1e-12,
            min_notional=0.0,
            tick_size=1e-12,
            max_order_qty=1e15,
            max_leverage=10.0,
            source="synthetic_hourly_comparator_no_venue_rule_claim",
            environment="historical_synthetic",
            observed_ts_ns=observed_ts_ns,
        )
        for symbol in sorted(symbols)
    }


def _artifact_identities(root: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in sorted(root.glob("**/*")):
        if not path.is_file() or path.name == "receipt.json":
            continue
        relative = path.relative_to(root).as_posix()
        output[relative] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return output


def _consumed_input_manifest(
    *,
    price_paths: set[Path],
    btc_paths: Sequence[Path],
    manifest_paths: Sequence[Path],
) -> pl.DataFrame:
    paths = sorted(set(price_paths) | set(btc_paths) | set(manifest_paths))
    return pl.from_dicts(
        [
            {
                "relative_path": path.relative_to(RECONSTRUCTED_ROOT).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
            for path in paths
        ],
        infer_schema_length=None,
    ).sort("relative_path")


def _validate_existing(output: Path) -> dict[str, Any]:
    receipt_path = output / "receipt.json"
    if not receipt_path.is_file():
        raise FileExistsError(f"comparator output exists without receipt: {output}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    for relative, identity in receipt.get("files", {}).items():
        path = output.joinpath(*str(relative).split("/"))
        if (
            not path.is_file()
            or path.stat().st_size != int(identity["bytes"])
            or _sha256(path) != identity["sha256"]
        ):
            raise RuntimeError(f"existing comparator artifact identity failed: {path}")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--preflight", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.out.expanduser().resolve()
    inputs = _registered_inputs()
    head = _git("rev-parse", "HEAD")
    dirty = bool(_git("status", "--porcelain=v1"))
    run_identity: dict[str, Any] = {
        "schema_version": 1,
        "kind": "active_production_function_runtime_comparator",
        "code_commit": head,
        "git_dirty": dirty,
        "registered_inputs": inputs,
        "long_source_window": {
            "start_inclusive_ms": LONG_START_MS,
            "end_exclusive_ms": END_MS,
        },
        "continuous_source_window": {
            "start_inclusive_ms": CONTINUOUS_START_MS,
            "end_exclusive_ms": END_MS,
        },
        "account_equity_usdt": CAPITAL_USDT,
        "schedule_order": ["protection", "long", "continuous"],
        "required_performance_refactor_prefix_identities": (
            EXPECTED_PREFIX_IDENTITIES
        ),
        "clock_offsets_ns": {
            "protection": 0,
            "long": 100_000,
            "continuous": 200_000,
            "boundary_flat": 900_000,
        },
        "execution_port": {
            "fee_bps": 0.0,
            "max_decision_age_ns": 1_000_000,
            "latency_ns": 0,
            "funding": "zero_modeled_separately",
            "price": "complete_hourly_close",
            "depth": "effectively_unlimited_single_level",
            "tick_and_step": 1e-12,
        },
        "command": [str(Path(sys.argv[0]).resolve()), *sys.argv[1:]],
        "monetary_outcomes_inspected": False,
    }
    if args.preflight:
        feature = json.loads(FEATURE_RECEIPT.read_text(encoding="utf-8"))
        continuous_files = sum(
            str(relative).startswith("continuous_active/")
            for relative in feature["files"]
        )
        print(
            json.dumps(
                {
                    **run_identity,
                    "mode": "preflight",
                    "output_absent": not output.exists(),
                    "continuous_feature_files": continuous_files,
                    "long_feature_present": "long_features.parquet" in feature["files"],
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    if dirty:
        raise RuntimeError("active runtime comparator requires a clean code commit")
    if output.exists():
        existing = _validate_existing(output)
        print(
            json.dumps(
                {
                    "output": str(output),
                    "status": existing["status"],
                    "receipt_sha256": _sha256(output / "receipt.json"),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 0

    work = output.with_name(f".{output.name}.working-{head[:12]}")
    if work.exists():
        raise FileExistsError(f"preserved comparator attempt already exists: {work}")
    work.mkdir(parents=True)
    started_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    _write_json_create(
        work / "attempt.json",
        {
            **run_identity,
            "started_at": started_at,
            "attempt_number": 1,
            "maximum_elapsed_seconds": MAX_ELAPSED_SECONDS,
        },
    )
    started = time.perf_counter()
    _enable_historical_io()
    if _TRANSACTION_BUFFER:
        raise RuntimeError("portable transaction buffer was not empty at run start")

    feature_receipt = json.loads(FEATURE_RECEIPT.read_text(encoding="utf-8"))
    feature_paths = _verify_feature_files(feature_receipt)
    long_features, continuous_features = _load_features(feature_paths)
    long_strategy = long_v11a_profile()
    long_pumps, long_pump_times = _long_pump_sources(
        long_features,
        strategy=long_strategy,
    )
    _write_parquet_create(work / "source/long_pump_sources.parquet", long_pumps)
    first_archive_days, manifest_paths = _load_first_archive_days()
    btc_klines, btc_paths = _load_btc_klines()
    long_index = _TimeFrameIndex(long_features, key="ts_ms")
    continuous_index = _TimeFrameIndex(continuous_features, key="ts_ms")
    symbols = {
        str(value).upper()
        for value in long_features["symbol"].unique().to_list()
    } | {
        str(value).upper()
        for value in continuous_features["symbol"].unique().to_list()
    }
    rules = _instrument_rules(symbols)

    account_root = work / "account"
    inbox_root = work / "inbox"
    route = ensure_account_route(
        account_id="active-runtime-comparator-demo",
        environment="demo",
        account_root=account_root,
        inbox_root=inbox_root,
    )
    execution = ExecutionTwinConfig(
        fee_bps=0.0,
        latency=LatencyProfile(0, 0, 0),
        max_decision_age_ns=1_000_000,
    )
    session = HistoricalAccountSession(
        account_root,
        account_id=route.account_id,
        risk_policy=AccountRiskPolicy(
            max_component_gross_notional_usdt=CAPITAL_USDT * 10.0,
            max_account_gross_notional_usdt=CAPITAL_USDT * 100.0,
            max_symbol_notional_usdt=CAPITAL_USDT * 10.0,
            max_initial_margin_usdt=CAPITAL_USDT * 100.0,
            max_leverage=10.0,
        ),
        instrument_rules=rules,
        execution_config=execution,
        id_seed="active-runtime-comparator:account",
        execution_id_seed="active-runtime-comparator:execution",
        unsafe_single_process_inplace_research=True,
    )
    long_demo = LongNativeDemoCycleConfig(
        execution_environment="demo",
        account_execution_root=str(account_root),
        account_intent_inbox_root=str(inbox_root),
        notional_multiplier=1.0,
        entry_leverage=10.0,
        max_new_entries_per_cycle=5,
        lookback_days=100,
    )
    continuous_demo = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            execution_environment="demo",
            account_execution_root=str(account_root),
            account_intent_inbox_root=str(inbox_root),
            btc_trend_gate="uptrend",
            max_active=25,
            max_new_entries_per_cycle=5,
            entry_leverage=10.0,
            notional_multiplier=10.0,
            per_position_notional_pct_equity=2.0,
        )
    )
    trace = _ComparatorTraceWriter(work)
    price_port = HistoricalHourlyCloseProvider(KLINE_ROOT)
    comparator = ActiveRuntimeComparator(
        route=route,
        session=session,
        instrument_rules=rules,
        execution_config=execution,
        price_port=price_port,
        long_demo=long_demo,
        long_strategy=long_strategy,
        continuous_demo=continuous_demo,
        btc_klines=btc_klines,
        first_archive_day_by_symbol=first_archive_days,
        btc_state_root=work / "btc-risk",
        run_config=ComparatorRunConfig(
            equity_usdt=CAPITAL_USDT,
            long_source_start_ms=LONG_START_MS,
            continuous_source_start_ms=CONTINUOUS_START_MS,
            source_end_ms=END_MS,
        ),
        trace_sink=trace,
    )

    total_hours = (END_MS - LONG_START_MS) // MS_PER_HOUR + 1
    for ordinal, boundary_ms in enumerate(
        range(LONG_START_MS, END_MS + MS_PER_HOUR, MS_PER_HOUR),
        start=1,
    ):
        recent_long = long_index.recent(boundary_ms, count=2)
        if recent_long.is_empty() or not any(
            int(value) in long_pump_times
            for value in recent_long["ts_ms"].unique().to_list()
        ):
            recent_long = pl.DataFrame(schema=long_features.schema)
        continuous_state = continuous_index.at(boundary_ms - 2 * MS_PER_HOUR)
        comparator.process_hour(
            boundary_ms,
            long_recent_features=recent_long,
            continuous_entry_state=continuous_state,
        )
        if ordinal % (7 * 24) == 0 or ordinal == total_hours:
            print(
                json.dumps(
                    {
                        "progress_hours": ordinal,
                        "total_hours": total_hours,
                        "boundary_ts_ms": boundary_ms,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        if time.perf_counter() - started > MAX_ELAPSED_SECONDS:
            raise RuntimeError("active comparator exceeded its registered four-hour cap")

    boundary_flats = comparator.boundary_flatten(END_MS)
    structural = comparator.final_structural_summary()
    long_lifecycle = comparator.canonical_long_trades
    continuous_lifecycle = comparator.canonical_continuous_trades
    _write_parquet_create(work / "lifecycle/long.parquet", long_lifecycle)
    _write_parquet_create(
        work / "lifecycle/continuous.parquet",
        continuous_lifecycle,
    )
    trace_counts = trace.close()
    if trace_counts["continuous_gate_rows"] != continuous_features.height:
        raise RuntimeError(
            "continuous gate trace does not cover the active feature population"
        )
    if trace_counts["cycles"] != total_hours:
        raise RuntimeError("cycle trace does not cover the registered clock")
    if trace_counts["requests"] != structural["requests"]:
        raise RuntimeError("request trace count disagrees with comparator summary")
    prefix_equivalence = _prefix_equivalence(work)

    persistence = _materialize_transactions(account_root)
    persisted_events = read_account_journal(account_root, verify=True)
    journal_verified = (
        len(persisted_events) == structural["account_events"]
        and (persisted_events[-1].event_hash if persisted_events else "")
        == structural["last_event_hash"]
        and (persisted_events[-1].state_hash if persisted_events else "")
        == structural["final_state_hash"]
    )
    if not journal_verified:
        raise RuntimeError("materialized account journal identity changed")

    print(
        json.dumps(
            {
                "stage": "hash_consumed_inputs",
                "price_files": len(price_port.consumed_paths),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    consumed = _consumed_input_manifest(
        price_paths=price_port.consumed_paths,
        btc_paths=btc_paths,
        manifest_paths=manifest_paths,
    )
    _write_parquet_create(work / "inputs/consumed_reconstruction_files.parquet", consumed)
    _write_json_create(
        work / "resolved_config.json",
        {
            "long_demo": asdict(long_demo),
            "long_strategy": asdict(long_strategy),
            "continuous_demo": asdict(continuous_demo),
            "risk_policy": asdict(session.risk_policy),
            "execution_config": asdict(execution),
            "instrument_rule_count": len(rules),
        },
    )

    elapsed = time.perf_counter() - started
    status = "pass" if (
        structural["final_flat"]
        and journal_verified
        and prefix_equivalence["status"] == "pass"
        and structural["btc_risk_reconciliation_error"] == 0
        and trace_counts["continuous_gate_rows"] == continuous_features.height
        and trace_counts["cycles"] == total_hours
    ) else "fail"
    files = _artifact_identities(work)
    receipt: dict[str, Any] = {
        **run_identity,
        "started_at": started_at,
        "completed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "elapsed_seconds": elapsed,
        "status": status,
        "scope": "production_function_structural_comparator_only",
        "source_population": {
            "long_feature_rows": long_features.height,
            "long_pump_source_rows": long_pumps.height,
            "continuous_active_rows": continuous_features.height,
            "continuous_source_population": "pinned_by_feature_receipt",
        },
        "trace_counts": trace_counts,
        "performance_refactor_prefix_equivalence": prefix_equivalence,
        "boundary_flat_targets": boundary_flats,
        "structural": structural,
        "journal_verified": journal_verified,
        "persistence": persistence,
        "consumed_reconstruction_files": consumed.height,
        "price_lookups": price_port.lookups,
        "files": files,
        "monetary_outcomes_inspected": False,
        "explicit_non_conclusions": [
            "no alpha, return, thesis, or profile conclusion",
            "no calibrated venue execution, cost, fill, or capacity claim",
            "no live daemon interleaving or intrabar parity claim",
            "no deployment, mainnet, capital, or real-money authority",
            "forward demo/paper structural validation remains required",
        ],
    }
    receipt["receipt_payload_sha256"] = payload_sha256(receipt)
    _write_json_create(work / "receipt.json", receipt)
    os.replace(work, output)
    print(
        json.dumps(
            {
                "output": str(output),
                "status": status,
                "receipt_sha256": _sha256(output / "receipt.json"),
                "requests": structural["requests"],
                "risk_rejected_requests": trace_counts["rejected_requests"],
                "final_flat": structural["final_flat"],
                "monetary_outcomes_inspected": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    if status != "pass":
        raise RuntimeError("active production-function comparator failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
