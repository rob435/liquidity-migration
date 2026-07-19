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
import traceback
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
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
from liquidity_migration.venue_lifecycle import (  # noqa: E402
    load_venue_delisting_settlements,
)

EPOCH_ROOT = REPO / "reports/prospective-runtime-parity-execution-epoch-2026-07-18"
FEATURE_ROOT = EPOCH_ROOT / "features/bybit-baseline"
RECONSTRUCTED_ROOT = EPOCH_ROOT / "reconstructed/bybit-baseline"
KLINE_ROOT = RECONSTRUCTED_ROOT / "klines_1h"
MANIFEST_ROOT = RECONSTRUCTED_ROOT / "archive_trade_manifest"
BASE_CONTRACT = REPO / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18.md"
AMENDMENTS = REPO / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_amendments.md"
POST17_AMENDMENTS = (
    REPO
    / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_post17_amendments.md"
)
POST18_AMENDMENTS = (
    REPO
    / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_post18_amendments.md"
)
POST19_AMENDMENTS = (
    REPO
    / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_post19_amendments.md"
)
POST20_AMENDMENTS = (
    REPO
    / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_post20_amendments.md"
)
POST21_AMENDMENTS = (
    REPO
    / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_post21_amendments.md"
)
POST22_AMENDMENTS = (
    REPO
    / "docs/preregistration/prospective_runtime_parity_execution_epoch_2026-07-18_post22_amendments.md"
)
FEATURE_RECEIPT = FEATURE_ROOT / "feature_receipt.json"
RECONSTRUCTION_RECEIPT = EPOCH_ROOT / "reconstruction/bybit-baseline.receipt.json"
LIFECYCLE_ROOT = EPOCH_ROOT / "venue-lifecycle/bybit-census-search-v2"
LIFECYCLE_RECEIPT = LIFECYCLE_ROOT / "receipt.json"
LIFECYCLE_EVENTS = LIFECYCLE_ROOT / "events.parquet"
LIFECYCLE_SEARCH_QUERIES = LIFECYCLE_ROOT / "search_queries.parquet"
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
SHARED_PROJECTION_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-573ca637763b"
)
SHARED_PROJECTION_FAILED_ATTEMPT_TERMINATION = (
    SHARED_PROJECTION_FAILED_ATTEMPT_ROOT / "termination.json"
)
PRICE_OVERREQUEST_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-55be92283972"
)
PRICE_OVERREQUEST_FAILED_ATTEMPT_TERMINATION = (
    PRICE_OVERREQUEST_FAILED_ATTEMPT_ROOT / "termination.json"
)
OBSERVER_PRICE_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-3d1c407dcdf1"
)
OBSERVER_PRICE_FAILED_ATTEMPT_TERMINATION = (
    OBSERVER_PRICE_FAILED_ATTEMPT_ROOT / "termination.json"
)
DELISTING_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-9b2ba6d9bc9f"
)
DELISTING_FAILED_ATTEMPT_TERMINATION = (
    DELISTING_FAILED_ATTEMPT_ROOT / "termination.json"
)
FOUR_HOUR_CAP_FAILED_ATTEMPT_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-f0ece1a035cc-four-hour-cap"
)
FOUR_HOUR_CAP_FAILED_ATTEMPT_TERMINATION = (
    FOUR_HOUR_CAP_FAILED_ATTEMPT_ROOT / "termination.json"
)
BOUNDARY_FLAT_FAILED_ATTEMPT_TERMINATION = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-d54eb524c208-boundary-flat-xrp/termination.json"
)
PREFIX_BASELINE_ROOT = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-d54eb524c208-boundary-flat-xrp"
)
REPAIR_PREFIX_FAILED_ATTEMPT_TERMINATION = (
    EPOCH_ROOT
    / "runtime-parity/.active-production-comparator.working-8f3fd034d199/termination.json"
)
SEARCH_HIGHLIGHT_FAILED_ATTEMPT_TERMINATION = (
    EPOCH_ROOT
    / "venue-lifecycle/.bybit-census-search-v2.working-query-highlight/termination.json"
)

EXPECTED_BASE_CONTRACT_SHA256 = "15edc498adf2bd068c33ff2f791fa3e46f161196db673a839adcf317aba35a31"
EXPECTED_AMENDMENTS_SHA256 = "b1e00187f94c796dc74862fc5ff38efac3ce3cf5864b0f4244c327db5a0fb282"
EXPECTED_POST17_AMENDMENTS_SHA256 = "5c094359fc7052ed2d2e56eb6a44f5d53efd71b4a25ae5006de5492b8479db49"
EXPECTED_POST18_AMENDMENTS_SHA256 = "ffc366010e374be572874c1c5609e04394a44f5dbd907e42edbb80d6647cb8b8"
EXPECTED_POST19_AMENDMENTS_SHA256 = "a168d845fa8ede5052c802baf06464537474d2cd1cd0e287ce91b11c955d24fa"
EXPECTED_POST20_AMENDMENTS_SHA256 = "c859894a6cb49a93450fd7a7f3d321980a7312ddd17e2a7131f33a5c90941fc4"
EXPECTED_POST21_AMENDMENTS_SHA256 = "14816e4710f98d4735f3e3621fcfcada116ea0b0f097a7f5f1dc9cb3e0230ba6"
EXPECTED_POST22_AMENDMENTS_SHA256 = "c10fd87913310ff6b6f6bade08e532f9b58a966b3ad5ee95dddfb2c79b70d13e"
EXPECTED_FEATURE_RECEIPT_SHA256 = "1d50aeb731e0cc82a1963d57576f032228df5b375dbdb20375c01541d397af31"
EXPECTED_RECONSTRUCTION_RECEIPT_SHA256 = "c0aa73d8b2f9851f4cb5d46ba2b238bdb411da34eed0736997aeeb825c10d45a"
EXPECTED_RECONSTRUCTION_LOGICAL_SHA256 = "9fa1e3a87e813e7449464cf6b512c40cb82d0a13dbce60978e01079e688a81fe"
EXPECTED_FEATURE_PAYLOAD_SHA256 = "eff681990a9262a3b30781588ee80a7f7b2f67ca16c812b4edda8b86203061b0"
EXPECTED_LIFECYCLE_RECEIPT_SHA256 = "19c8a3bc88681f9b36420d99eed6c9d31150e331ccf8bc0097d308bb2e3d4327"
EXPECTED_LIFECYCLE_EVENTS_SHA256 = "7ca1de951837b5659aee8b9ceefe95b8c661960ba671a453d65fb457d7fdc4c1"
EXPECTED_LIFECYCLE_SEARCH_QUERIES_SHA256 = "c16eb2d370d15afbbe3e79d882dbdb22f8da3633d83a252cf50d9232df03c09e"
EXPECTED_FAILED_ATTEMPT_TERMINATION_SHA256 = "dd5df88b6d77fe181ba1fb1737b97fa3a62841065d16c425b1f26499954063d1"
EXPECTED_INDEXED_FAILED_ATTEMPT_TERMINATION_SHA256 = "701386b22989a35c39bb5cd544dc9377a02caa66d10dbd6b2c6fefa688cbc8ed"
EXPECTED_SHARED_PROJECTION_FAILED_ATTEMPT_TERMINATION_SHA256 = "28eac53954835d799e066642ecba4843037ab05f8fddb587ba4fa1b89360a738"
EXPECTED_PRICE_OVERREQUEST_FAILED_ATTEMPT_TERMINATION_SHA256 = "ec37b1bb95d8e7aac4780716504f74d87aba6a93ec9082e824d796906167c80a"
EXPECTED_OBSERVER_PRICE_FAILED_ATTEMPT_TERMINATION_SHA256 = "75382d0ed1c6e75f9fbdb2bd0f018c955a488850ca39539b9d9f427d211d6dae"
EXPECTED_DELISTING_FAILED_ATTEMPT_TERMINATION_SHA256 = "aa4ed1e13dfb8c0828647d10dea4dd09fac5532764f907cfc52321f08e12288e"
EXPECTED_FOUR_HOUR_CAP_FAILED_ATTEMPT_TERMINATION_SHA256 = "56c51f48f05ebe289ed9abe6e4b6beb59762bf5fd33fafff1b54de3d2be50c6b"
EXPECTED_BOUNDARY_FLAT_FAILED_ATTEMPT_TERMINATION_SHA256 = "7f5959e06e973617f511e74849dda0187a305032856c92340a7dbf2cddbc437c"
EXPECTED_REPAIR_PREFIX_FAILED_ATTEMPT_TERMINATION_SHA256 = "b4f7e4a383e475eea4ddcf05b2d98de7a15c724cd6321a3c57a8101c6d16f4e7"
EXPECTED_SEARCH_HIGHLIGHT_FAILED_ATTEMPT_TERMINATION_SHA256 = "f9eb3ff6c9311da43db8a156ce883022ea963b35a9245b8fdfafc0f54d3d961f"
EXPECTED_PREFIX_IDENTITIES = {
    "traces/continuous_gates/part-00000.parquet": (
        "ae7d56f33b6642a43227b8f4affd4c054f8be59f2fc90f27d9c777a4b5a41eb2"
    ),
    "traces/continuous_gates/part-00001.parquet": (
        "ce4086a7b5f01ee70b23a5e6f39bb07890680ba81383f50ab79ed7c98a8a6054"
    ),
    "traces/continuous_gates/part-00002.parquet": (
        "9ce06fc85d0b600b86b4731cf2eca73ff72ff7eb93df85c2707ffea0a7399c0f"
    ),
    "traces/continuous_gates/part-00003.parquet": (
        "d223d167e11d8d0a807a609e782503cf6d52fca07cb82190598d37ed31378c35"
    ),
    "traces/continuous_gates/part-00004.parquet": (
        "d5edac73cfbac65471db89b5d47b143cd8cbcef3484c8fc24fc152a1e226be30"
    ),
    "traces/continuous_gates/part-00005.parquet": (
        "3fd8b82c94c2d0960832c58c6ffaba766b1b11fd553d5524ecb6dc8029686e8f"
    ),
    "traces/continuous_gates/part-00006.parquet": (
        "873e0ca1c26a7871bc83360adf604432b80f14d2c7ab59ebd73ea6082fdd7cdf"
    ),
    "traces/continuous_gates/part-00007.parquet": (
        "cbe927f058e8060ce9f99aff886e62c9037c8482b444a99f4554fc631d36db1f"
    ),
    "traces/continuous_gates/part-00008.parquet": (
        "4a45f75d5f5d04b553fda09333c84309ebb54de7d323dbf285f3dcb7c6d1f945"
    ),
    "traces/long_funnel/part-00000.parquet": (
        "31f4d87816b8972b18626eb8297e726ab6aa15efb48ea8286f977fed7090d83e"
    ),
    "traces/long_funnel/part-00001.parquet": (
        "03f7659d0ab210ff9f4d5d5d83bd21e98d204dce4af0c9ef88cc9003a00862c8"
    ),
    "traces/long_funnel/part-00002.parquet": (
        "3eab1390e33bf5f41223fbf6304e3fa149b75f7166edfd7ff813968e7de6ec3e"
    ),
    "traces/long_funnel/part-00003.parquet": (
        "00f75a19c9f7fc757b6ef1734f21b3711b818eac9edccfcf03b9cc49e2aa340b"
    ),
    "traces/long_funnel/part-00004.parquet": (
        "047d26d9478f971118474cecccbcba641cb3a0387db22b52e85e7dd071d735c7"
    ),
    "traces/long_funnel/part-00005.parquet": (
        "b116c2437d01247684c4a3fd04ddffe46c2de695836015b36508309631a34e4a"
    ),
    "traces/long_funnel/part-00006.parquet": (
        "d078a04ec9f8ccc7ea02d083389524c755660fdfab5feb4bdb45bcf4ce980f86"
    ),
    "traces/long_funnel/part-00007.parquet": (
        "22520e6269f2b72cbf9191653c6aa3d159cb63826fa525d4f93e3e25ece08573"
    ),
    "traces/long_funnel/part-00008.parquet": (
        "27f3473e0c5057e186114c7e6eaa66a86b63f7d15a24873dde612f03f5fe1fd6"
    ),
    "traces/long_funnel/part-00009.parquet": (
        "e0f6cd79eebd712bc9b6b26186e0356fccc44e6ebb166b75975614364aa8a06f"
    ),
    "traces/long_funnel/part-00010.parquet": (
        "43a860c274df386607186adab4803229d06eb6a145623e734390626d6d182ecf"
    ),
    "traces/long_funnel/part-00011.parquet": (
        "ef046c6c65a646da8ecb7d1dadcec64661d77d3bc82104aabdb35ef74ab41b7e"
    ),
}

LONG_START_MS = 1_677_628_800_000  # 2023-03-01T00:00:00Z
CONTINUOUS_START_MS = 1_680_307_200_000  # 2023-04-01T00:00:00Z
END_MS = 1_783_641_600_000  # 2026-07-10T00:00:00Z
CAPITAL_USDT = 1_000_000.0
MAX_ELAPSED_SECONDS = 28_800
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
    setattr(
        replay_module,
        "JsonlStrategyEventTape",
        lambda _path: MemoryStrategyEventTape(),
    )
    if os.name == "nt":
        account_route_module.exclusive_file_lock = _single_process_lock
        account_route_module._atomic_create = _portable_route_create
        setattr(account_route_module, "_fsync_directory", lambda _path: None)
        account_route_module.read_stable_file = _portable_stable_read
        setattr(btc_risk_module, "_fsync_file", lambda _path: None)
        setattr(btc_risk_module, "_fsync_directory", lambda _path: None)


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
        self.lifecycle = _PartitionWriter(root, "venue_lifecycle")
        self.gate_root = root / "traces/continuous_gates"
        self.gate_frames: list[pl.DataFrame] = []
        self.gate_buffer_rows = 0
        self.gate_rows = 0
        self.gate_part = 0
        self.accepted_requests = 0
        self.rejected_requests = 0
        self.last_request_feedback: dict[str, Any] | None = None

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
        self.last_request_feedback = _normalized_trace_row(row)
        self.requests.append(row)

    def request_intent(self, row: Mapping[str, Any]) -> None:
        self.intents.append(row)

    def venue_lifecycle(self, row: Mapping[str, Any]) -> None:
        self.lifecycle.append(row)

    def close(self) -> dict[str, int]:
        self._flush_gates()
        for writer in (
            self.cycles,
            self.long,
            self.decisions,
            self.requests,
            self.intents,
            self.lifecycle,
        ):
            writer.flush()
        return {
            "cycles": self.cycles.row_count,
            "continuous_gate_rows": self.gate_rows,
            "long_funnel_rows": self.long.row_count,
            "source_decisions": self.decisions.row_count,
            "requests": self.requests.row_count,
            "request_intents": self.intents.row_count,
            "venue_lifecycle": self.lifecycle.row_count,
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
        "post17_amendments": (
            POST17_AMENDMENTS,
            EXPECTED_POST17_AMENDMENTS_SHA256,
        ),
        "post18_amendments": (
            POST18_AMENDMENTS,
            EXPECTED_POST18_AMENDMENTS_SHA256,
        ),
        "post19_amendments": (
            POST19_AMENDMENTS,
            EXPECTED_POST19_AMENDMENTS_SHA256,
        ),
        "post20_amendments": (
            POST20_AMENDMENTS,
            EXPECTED_POST20_AMENDMENTS_SHA256,
        ),
        "post21_amendments": (
            POST21_AMENDMENTS,
            EXPECTED_POST21_AMENDMENTS_SHA256,
        ),
        "post22_amendments": (
            POST22_AMENDMENTS,
            EXPECTED_POST22_AMENDMENTS_SHA256,
        ),
        "feature_receipt": (FEATURE_RECEIPT, EXPECTED_FEATURE_RECEIPT_SHA256),
        "reconstruction_receipt": (
            RECONSTRUCTION_RECEIPT,
            EXPECTED_RECONSTRUCTION_RECEIPT_SHA256,
        ),
        "venue_lifecycle_receipt": (
            LIFECYCLE_RECEIPT,
            EXPECTED_LIFECYCLE_RECEIPT_SHA256,
        ),
        "venue_lifecycle_events": (
            LIFECYCLE_EVENTS,
            EXPECTED_LIFECYCLE_EVENTS_SHA256,
        ),
        "venue_lifecycle_search_queries": (
            LIFECYCLE_SEARCH_QUERIES,
            EXPECTED_LIFECYCLE_SEARCH_QUERIES_SHA256,
        ),
        "failed_attempt_termination": (
            FIRST_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "indexed_failed_attempt_termination": (
            INDEXED_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_INDEXED_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "shared_projection_failed_attempt_termination": (
            SHARED_PROJECTION_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_SHARED_PROJECTION_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "price_overrequest_failed_attempt_termination": (
            PRICE_OVERREQUEST_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_PRICE_OVERREQUEST_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "observer_price_failed_attempt_termination": (
            OBSERVER_PRICE_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_OBSERVER_PRICE_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "delisting_failed_attempt_termination": (
            DELISTING_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_DELISTING_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "four_hour_cap_failed_attempt_termination": (
            FOUR_HOUR_CAP_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_FOUR_HOUR_CAP_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "boundary_flat_failed_attempt_termination": (
            BOUNDARY_FLAT_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_BOUNDARY_FLAT_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "repair_prefix_failed_attempt_termination": (
            REPAIR_PREFIX_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_REPAIR_PREFIX_FAILED_ATTEMPT_TERMINATION_SHA256,
        ),
        "search_highlight_failed_attempt_termination": (
            SEARCH_HIGHLIGHT_FAILED_ATTEMPT_TERMINATION,
            EXPECTED_SEARCH_HIGHLIGHT_FAILED_ATTEMPT_TERMINATION_SHA256,
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
    lifecycle = json.loads(LIFECYCLE_RECEIPT.read_text(encoding="utf-8"))
    lifecycle_pass = (
        lifecycle.get("status") == "pass"
        and lifecycle.get("coverage_valid") is True
        and lifecycle.get("monetary_outcomes_inspected") is False
        and int(lifecycle.get("search_queries_completed") or 0) == 286
        and int(lifecycle.get("terminal_manifest_symbols") or 0) == 286
        and int(lifecycle.get("admitted_events") or 0) == 235
        and int(lifecycle.get("registered_klay_event_count") or 0) == 1
        and int(lifecycle.get("duplicate_admissible_events") or 0) == 0
        and int(lifecycle.get("admissible_event_index_failures") or 0) == 0
        and not lifecycle.get("coverage_errors")
        and not lifecycle.get("critical_article_failures")
    )
    if not lifecycle_pass:
        raise RuntimeError("venue lifecycle census did not pass its registered gates")
    return output


_REPAIR_AWARE_PREFIX_COLUMNS = frozenset(
    {"gate_existing_exposure", "gate_cooldown", "gate_state_sha256"}
)
_REQUIRED_REPAIR_AWARE_PREFIX_COLUMNS = frozenset(
    {
        "symbol",
        "gate_existing_exposure",
        "gate_cooldown",
        "gate_state_sha256",
        "first_rejection",
        "barebones_accepted",
    }
)


def _series_exact(left: pl.Series, right: pl.Series) -> bool:
    return left.equals(
        right,
        check_dtypes=True,
        check_names=True,
        null_equal=True,
    )


def _repair_aware_prefix_file(
    *,
    relative: str,
    baseline_path: Path,
    actual_path: Path,
    expected_sha: str,
) -> dict[str, Any]:
    if not baseline_path.is_file():
        raise RuntimeError(
            f"registered prefix baseline artifact is missing: {baseline_path}"
        )
    baseline_sha = _sha256(baseline_path)
    if baseline_sha != expected_sha:
        raise RuntimeError(
            f"registered prefix baseline identity changed: {relative}: {baseline_sha} != {expected_sha}"
        )
    if not actual_path.is_file():
        raise RuntimeError(f"registered prefix artifact is missing: {actual_path}")

    actual_sha = _sha256(actual_path)
    common = {
        "baseline_bytes": baseline_path.stat().st_size,
        "actual_bytes": actual_path.stat().st_size,
        "expected_baseline_sha256": expected_sha,
        "baseline_sha256": baseline_sha,
        "actual_sha256": actual_sha,
        "byte_identical": actual_sha == baseline_sha,
    }
    if actual_sha == baseline_sha:
        return {
            **common,
            "semantic_equivalence": True,
            "rows": int(pl.scan_parquet(actual_path).select(pl.len()).collect().item()),
            "changed_rows": 0,
            "gate_existing_exposure_fail_to_pass": 0,
            "gate_cooldown_pass_to_fail": 0,
            "gate_state_sha256_changes": 0,
            "barebones_accepted_exact": True,
            "first_rejection_exact": True,
        }

    baseline = pl.read_parquet(baseline_path)
    actual = pl.read_parquet(actual_path)
    if baseline.schema != actual.schema:
        raise RuntimeError(f"prefix schema changed: {relative}")
    if baseline.height != actual.height:
        raise RuntimeError(
            f"prefix row count changed: {relative}: {baseline.height} != {actual.height}"
        )
    if baseline.equals(actual, null_equal=True):
        return {
            **common,
            "semantic_equivalence": True,
            "rows": baseline.height,
            "changed_rows": 0,
            "gate_existing_exposure_fail_to_pass": 0,
            "gate_cooldown_pass_to_fail": 0,
            "gate_state_sha256_changes": 0,
            "barebones_accepted_exact": True,
            "first_rejection_exact": True,
        }

    if not relative.startswith("traces/long_funnel/"):
        raise RuntimeError(f"non-LONG registered prefix values changed: {relative}")
    missing = _REQUIRED_REPAIR_AWARE_PREFIX_COLUMNS.difference(baseline.columns)
    if missing:
        raise RuntimeError(
            f"repair-aware prefix columns missing from {relative}: {sorted(missing)}"
        )

    for column in baseline.columns:
        if column in _REPAIR_AWARE_PREFIX_COLUMNS:
            continue
        if not _series_exact(baseline[column], actual[column]):
            raise RuntimeError(
                f"unregistered prefix field changed: {relative}: {column}"
            )

    symbols = baseline["symbol"].to_list()
    baseline_exposure = baseline["gate_existing_exposure"].to_list()
    actual_exposure = actual["gate_existing_exposure"].to_list()
    baseline_cooldown = baseline["gate_cooldown"].to_list()
    actual_cooldown = actual["gate_cooldown"].to_list()
    baseline_hashes = baseline["gate_state_sha256"].to_list()
    actual_hashes = actual["gate_state_sha256"].to_list()

    exposure_transitions = 0
    cooldown_transitions = 0
    hash_changes = 0
    changed_rows = 0
    for row_index, values in enumerate(
        zip(
            symbols,
            baseline_exposure,
            actual_exposure,
            baseline_cooldown,
            actual_cooldown,
            baseline_hashes,
            actual_hashes,
            strict=True,
        )
    ):
        (
            symbol,
            old_exposure,
            new_exposure,
            old_cooldown,
            new_cooldown,
            old_hash,
            new_hash,
        ) = values
        exposure_changed = old_exposure != new_exposure
        cooldown_changed = old_cooldown != new_cooldown
        hash_changed = old_hash != new_hash
        gate_changed = exposure_changed or cooldown_changed
        if not gate_changed and not hash_changed:
            continue
        changed_rows += 1
        if symbol != "XRPUSDT":
            raise RuntimeError(
                f"repair-aware prefix change is not XRPUSDT: {relative}: row {row_index}"
            )
        if exposure_changed:
            if (old_exposure, new_exposure) != ("fail", "pass"):
                raise RuntimeError(
                    f"invalid exposure-gate transition: {relative}: row {row_index}"
                )
            exposure_transitions += 1
        if cooldown_changed:
            if (old_cooldown, new_cooldown) != ("pass", "fail"):
                raise RuntimeError(
                    f"invalid cooldown-gate transition: {relative}: row {row_index}"
                )
            cooldown_transitions += 1
        if hash_changed != gate_changed:
            raise RuntimeError(
                f"derived gate hash does not track allowed transition: {relative}: row {row_index}"
            )
        hash_changes += int(hash_changed)

    return {
        **common,
        "semantic_equivalence": True,
        "rows": baseline.height,
        "changed_rows": changed_rows,
        "gate_existing_exposure_fail_to_pass": exposure_transitions,
        "gate_cooldown_pass_to_fail": cooldown_transitions,
        "gate_state_sha256_changes": hash_changes,
        "barebones_accepted_exact": True,
        "first_rejection_exact": True,
    }


def _prefix_equivalence(work: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    totals = {
        "files": 0,
        "byte_identical_files": 0,
        "semantic_only_files": 0,
        "changed_rows": 0,
        "gate_existing_exposure_fail_to_pass": 0,
        "gate_cooldown_pass_to_fail": 0,
        "gate_state_sha256_changes": 0,
    }
    for relative, expected_sha in EXPECTED_PREFIX_IDENTITIES.items():
        result = _repair_aware_prefix_file(
            relative=relative,
            baseline_path=PREFIX_BASELINE_ROOT.joinpath(*relative.split("/")),
            actual_path=work.joinpath(*relative.split("/")),
            expected_sha=expected_sha,
        )
        files[relative] = result
        totals["files"] += 1
        if result["byte_identical"]:
            totals["byte_identical_files"] += 1
        else:
            totals["semantic_only_files"] += 1
        for key in (
            "changed_rows",
            "gate_existing_exposure_fail_to_pass",
            "gate_cooldown_pass_to_fail",
            "gate_state_sha256_changes",
        ):
            totals[key] += int(result[key])

    if totals["gate_existing_exposure_fail_to_pass"] <= 0:
        raise RuntimeError(
            "repair-aware prefix guard observed no XRP exposure-gate transition"
        )
    return {
        "status": "pass",
        "comparison": "repair_aware_field_exact_v1",
        "baseline_root": str(PREFIX_BASELINE_ROOT.resolve()),
        "barebones_accepted_exact": True,
        "first_rejection_exact": True,
        "totals": totals,
        "files": files,
    }


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


@dataclass(slots=True)
class _FailureContext:
    work: Path
    run_identity: Mapping[str, Any]
    started_at: str
    started_perf: float
    trace: _ComparatorTraceWriter | None = None
    account_root: Path | None = None
    session: HistoricalAccountSession | None = None
    comparator: ActiveRuntimeComparator | None = None
    progress_hours: int = 0
    total_hours: int = 0
    last_boundary_ts_ms: int = 0


_ACTIVE_FAILURE_CONTEXT: _FailureContext | None = None


def _capture_structural_failure(
    context: _FailureContext,
    exc: BaseException,
) -> None:
    """Best-effort create-only evidence; never calculate monetary outcomes."""

    if not context.work.is_dir():
        return
    termination_path = context.work / "termination.json"
    if termination_path.exists():
        return
    diagnostic_errors: dict[str, str] = {}
    trace_counts: Mapping[str, int] | None = None
    if context.trace is not None:
        try:
            trace_counts = context.trace.close()
        except Exception as trace_exc:  # noqa: BLE001 - preserve original failure
            diagnostic_errors["trace_flush"] = (
                f"{type(trace_exc).__name__}: {trace_exc}"
            )

    persistence: Mapping[str, Any] | None = None
    journal_identity: dict[str, Any] | None = None
    if context.account_root is not None:
        try:
            persistence = _materialize_transactions(context.account_root)
            events = read_account_journal(context.account_root, verify=True)
            journal_identity = {
                "verified": True,
                "events": len(events),
                "last_event_hash": events[-1].event_hash if events else "",
                "last_state_hash": events[-1].state_hash if events else "",
            }
        except Exception as journal_exc:  # noqa: BLE001 - preserve original failure
            diagnostic_errors["journal_materialization"] = (
                f"{type(journal_exc).__name__}: {journal_exc}"
            )

    prefix_equivalence: Mapping[str, Any] | None = None
    try:
        prefix_equivalence = _prefix_equivalence(context.work)
    except Exception as prefix_exc:  # noqa: BLE001 - evidence can be partial
        diagnostic_errors["prefix_equivalence"] = (
            f"{type(prefix_exc).__name__}: {prefix_exc}"
        )

    state_identity: dict[str, Any] | None = None
    if context.session is not None and context.session.kernel is not None:
        try:
            state = context.session.kernel._state_ref()
            state_identity = {
                "state_hash": state.state_hash(),
                "events_applied": state.events_applied,
                "component_targets": {
                    key: {
                        "symbol": str(target.get("symbol") or "").upper(),
                        "signed_qty": float(target.get("signed_qty") or 0.0),
                    }
                    for key, target in sorted(state.component_targets.items())
                    if abs(float(target.get("signed_qty") or 0.0)) > 1e-12
                },
                "positions": {
                    symbol: float(position.signed_qty)
                    for symbol, position in sorted(state.positions.items())
                    if abs(float(position.signed_qty)) > 1e-12
                },
                "working_symbols": sorted(
                    state.working_symbols(tolerance=1e-12)
                ),
            }
        except Exception as state_exc:  # noqa: BLE001 - evidence can be partial
            diagnostic_errors["state_identity"] = (
                f"{type(state_exc).__name__}: {state_exc}"
            )

    try:
        files = _artifact_identities(context.work)
    except Exception as files_exc:  # noqa: BLE001 - evidence can be partial
        diagnostic_errors["file_identities"] = (
            f"{type(files_exc).__name__}: {files_exc}"
        )
        files = {}
    termination = {
        **dict(context.run_identity),
        "status": "invalid_structural_failure",
        "started_at": context.started_at,
        "failed_at": dt.datetime.now(dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
        "elapsed_seconds": time.perf_counter() - context.started_perf,
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
        "exception_traceback": "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        "progress_hours": context.progress_hours,
        "total_hours": context.total_hours,
        "last_boundary_ts_ms": context.last_boundary_ts_ms,
        "trace_counts": trace_counts,
        "last_request_feedback": (
            context.trace.last_request_feedback
            if context.trace is not None
            else None
        ),
        "performance_refactor_prefix_equivalence": prefix_equivalence,
        "persistence": persistence,
        "journal": journal_identity,
        "state": state_identity,
        "diagnostic_errors": diagnostic_errors,
        "files": files,
        "monetary_outcomes_inspected": False,
        "explicit_non_conclusions": [
            "invalid for runtime parity",
            "no alpha, return, cost, fill, thesis, or deployment conclusion",
            "no mainnet, capital, or real-money authority",
        ],
    }
    termination["receipt_payload_sha256"] = payload_sha256(termination)
    _write_json_create(termination_path, termination)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--preflight", action="store_true")
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    global _ACTIVE_FAILURE_CONTEXT
    _ACTIVE_FAILURE_CONTEXT = None
    args = _parser().parse_args(argv)
    output = args.out.expanduser().resolve()
    inputs = _registered_inputs()
    venue_lifecycle_events = load_venue_delisting_settlements(LIFECYCLE_EVENTS)
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
        "schedule_order": [
            "venue_lifecycle",
            "protection",
            "long",
            "continuous",
        ],
        "venue_lifecycle_registered_events": len(venue_lifecycle_events),
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
                    "venue_lifecycle_events": len(venue_lifecycle_events),
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
    _ACTIVE_FAILURE_CONTEXT = _FailureContext(
        work=work,
        run_identity=run_identity,
        started_at=started_at,
        started_perf=started,
    )
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
    } | {event.symbol for event in venue_lifecycle_events}
    rules = _instrument_rules(symbols)

    account_root = work / "account"
    inbox_root = work / "inbox"
    _ACTIVE_FAILURE_CONTEXT.account_root = account_root
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
        route=route,
    )
    _ACTIVE_FAILURE_CONTEXT.session = session
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
    _ACTIVE_FAILURE_CONTEXT.trace = trace
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
        venue_lifecycle_events=venue_lifecycle_events,
        trace_sink=trace,
    )
    _ACTIVE_FAILURE_CONTEXT.comparator = comparator

    total_hours = (END_MS - LONG_START_MS) // MS_PER_HOUR + 1
    _ACTIVE_FAILURE_CONTEXT.total_hours = total_hours
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
        _ACTIVE_FAILURE_CONTEXT.progress_hours = ordinal
        _ACTIVE_FAILURE_CONTEXT.last_boundary_ts_ms = boundary_ms
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
            raise RuntimeError("active comparator exceeded its registered elapsed-time cap")

    pre_boundary_trace_counts = trace.close()
    pre_boundary_prefix = _prefix_equivalence(work)
    if pre_boundary_prefix["status"] != "pass":
        raise RuntimeError("pre-boundary registered prefix equivalence failed")
    pre_boundary_persistence = _materialize_transactions(account_root)
    pre_boundary_events = read_account_journal(account_root, verify=True)
    pre_boundary_state = session.kernel._state_ref() if session.kernel is not None else None
    pre_boundary_journal_verified = (
        pre_boundary_state is not None
        and len(pre_boundary_events) == pre_boundary_state.events_applied
        and (pre_boundary_events[-1].state_hash if pre_boundary_events else "")
        == pre_boundary_state.state_hash()
    )
    if not pre_boundary_journal_verified:
        raise RuntimeError("pre-boundary materialized account journal identity changed")
    _write_json_create(
        work / "checkpoints/pre_boundary.json",
        {
            "kind": "active_comparator_pre_boundary_structural_checkpoint",
            "code_commit": head,
            "boundary_ts_ms": END_MS,
            "progress_hours": total_hours,
            "trace_counts": pre_boundary_trace_counts,
            "performance_refactor_prefix_equivalence": pre_boundary_prefix,
            "persistence": pre_boundary_persistence,
            "journal": {
                "verified": True,
                "events": len(pre_boundary_events),
                "last_event_hash": (
                    pre_boundary_events[-1].event_hash
                    if pre_boundary_events
                    else ""
                ),
                "last_state_hash": (
                    pre_boundary_events[-1].state_hash
                    if pre_boundary_events
                    else ""
                ),
            },
            "monetary_outcomes_inspected": False,
        },
    )

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
    expected_lifecycle_trace_rows = (
        int(structural["venue_lifecycle_observed_events"])
        + int(structural["venue_lifecycle_blocked_entries"])
    )
    if trace_counts["venue_lifecycle"] != expected_lifecycle_trace_rows:
        raise RuntimeError(
            "venue lifecycle trace count disagrees with comparator summary"
        )
    prefix_equivalence = _prefix_equivalence(work)

    terminal_persistence = _materialize_transactions(account_root)
    persistence = {
        "pre_boundary": pre_boundary_persistence,
        "terminal": terminal_persistence,
    }
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
            "venue_lifecycle": {
                "event_count": len(venue_lifecycle_events),
                "event_table_sha256": EXPECTED_LIFECYCLE_EVENTS_SHA256,
                "receipt_sha256": EXPECTED_LIFECYCLE_RECEIPT_SHA256,
            },
        },
    )

    elapsed = time.perf_counter() - started
    status = "pass" if (
        structural["final_flat"]
        and journal_verified
        and prefix_equivalence["status"] == "pass"
        and structural["btc_risk_reconciliation_error"] == 0
        and structural["rejected_strict_risk_reduction_batches"] == 0
        and trace_counts["continuous_gate_rows"] == continuous_features.height
        and trace_counts["cycles"] == total_hours
        and structural["venue_lifecycle_observed_events"]
        == len(venue_lifecycle_events)
        and trace_counts["venue_lifecycle"]
        == expected_lifecycle_trace_rows
    ) else "fail"
    if status != "pass":
        raise RuntimeError("active production-function comparator failed")
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
            "no exact venue per-second delisting settlement-price claim",
            "no live daemon interleaving or intrabar parity claim",
            "no deployment, mainnet, capital, or real-money authority",
            "forward demo/paper structural validation remains required",
        ],
    }
    receipt["receipt_payload_sha256"] = payload_sha256(receipt)
    _write_json_create(work / "receipt.json", receipt)
    os.replace(work, output)
    _ACTIVE_FAILURE_CONTEXT = None
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
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    global _ACTIVE_FAILURE_CONTEXT

    try:
        return _main(argv)
    except BaseException as exc:
        context = _ACTIVE_FAILURE_CONTEXT
        if context is not None:
            try:
                _capture_structural_failure(context, exc)
            except Exception as capture_exc:  # noqa: BLE001 - preserve root cause
                print(
                    json.dumps(
                        {
                            "stage": "failure_capture_failed",
                            "root_exception": f"{type(exc).__name__}: {exc}",
                            "capture_exception": (
                                f"{type(capture_exc).__name__}: {capture_exc}"
                            ),
                        },
                        sort_keys=True,
                    ),
                    file=sys.stderr,
                    flush=True,
                )
        _ACTIVE_FAILURE_CONTEXT = None
        raise


if __name__ == "__main__":
    raise SystemExit(main())
