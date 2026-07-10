#!/usr/bin/env python3
"""Run the preregistered 2026-07-10 CONTINUOUS tail-survival experiment.

This is a fixed, budget-only experiment dispatcher. It is intentionally not a
generic sweep. Heavy runs checkpoint at cell/venue boundaries and remain
``exploratory``. See docs/preregistration/continuous-tail-survival-2026-07-10.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from liquidity_migration.continuous_component_sources import CONTINUOUS_COMPONENT_SOURCES
from liquidity_migration.continuous_events import ContinuousEventConfig
from liquidity_migration.continuous_forward_replay import frozen_config_hash
from scripts.continuous_deployed_equity_refresh import WINNER_WEIGHTS, frozen_config, run_venue
from scripts.rebuild_continuous_component_ledgers import CELLS as FROZEN_COMPONENT_CELLS
from scripts.rebuild_continuous_component_ledgers import COMMON as FROZEN_COMPONENT_COMMON

PREREG_DATE = "2026-07-10"
START_DATE = "2023-04-01"
END_DATE = "2026-07-10"  # signal boundary, exclusive: through 2026-07-09 UTC
EXIT_DATA_END_DATE = "2026-07-12"  # price/funding boundary, exclusive: through 2026-07-11 UTC
SPLIT_DATE = "2025-06-01"
COMPONENT_TP = 0.12
SHOCK_FRAC = 1.0
RUN_LABEL = "exploratory"
MS_PER_DAY = 86_400_000
EXPECTED_FROZEN_FORWARD_CONFIG_HASH = (
    "c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6"
)
EXPECTED_CONTROL_COMPONENT_CONFIG_HASHES = {
    "turn3p3": "f4f75d9e0547",
    "turn4p3": "6e5f7336851e",
    "turn4p5": "89011515e462",
}
EXPECTED_EFFECTIVE_COMPONENT_CONFIG_HASHES = {
    "control": EXPECTED_CONTROL_COMPONENT_CONFIG_HASHES,
    "budget_010": {
        "turn3p3": "5fbb4bba34cf",
        "turn4p3": "eaf8ebf26d18",
        "turn4p5": "d0b4f8203982",
    },
    "budget_015": {
        "turn3p3": "4c85020e4a61",
        "turn4p3": "44dca1702a0b",
        "turn4p5": "7f401b73a216",
    },
    "budget_025": {
        "turn3p3": "988d54548e6e",
        "turn4p3": "63426af6f5ad",
        "turn4p5": "60dc795a0076",
    },
}
SHARED = Path(os.environ.get("SHARED_DATA", str(Path.home() / "SHARED_DATA"))).expanduser()
DEFAULT_ROOTS = {
    "bybit": SHARED / "bybit_full_pit",
    "binance": SHARED / "binance_full_pit",
}
FUNDING_DATASET = {"bybit": "funding", "binance": "binance_usdm_funding"}
MIN_STABLE_RMOM_SYMBOLS = 20


@dataclass(frozen=True, slots=True)
class CellSpec:
    disaster_budget_frac: float = 0.0
    family: str = "control"


CELLS: dict[str, CellSpec] = {
    "control": CellSpec(),
    "budget_010": CellSpec(disaster_budget_frac=0.0010, family="sizing"),
    "budget_015": CellSpec(disaster_budget_frac=0.0015, family="sizing"),
    "budget_025": CellSpec(disaster_budget_frac=0.0025, family="sizing"),
}


def cell_transform(spec: CellSpec) -> Callable[[ContinuousEventConfig], ContinuousEventConfig]:
    """Apply only the preregistered ex-ante notional cap.

    Exit and heat hooks are explicitly pinned off. Their current implementations
    do not satisfy the causal contract needed for this experiment.
    """

    def transform(config: ContinuousEventConfig) -> ContinuousEventConfig:
        return replace(
            config,
            entry_disaster_loss_budget_frac=spec.disaster_budget_frac,
            entry_disaster_shock_frac=SHOCK_FRAC,
            entry_portfolio_heat_cap_frac=0.0,
            failed_fade_hours=0,
            failed_fade_loss_pct=0.0,
            failed_fade_min_mfe_pct=0.0,
        )

    return transform


def _date_range(start_date: str, end_date: str) -> list[str]:
    day = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date)
    values: list[str] = []
    while day < end:
        values.append(day.isoformat())
        day += dt.timedelta(days=1)
    return values


def _stat_fingerprint(paths: Iterable[Path], *, relative_to: Path) -> dict[str, Any]:
    metadata_digest = hashlib.sha256()
    content_digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    max_mtime_ns = 0
    for path in sorted(set(paths), key=lambda value: str(value)):
        if not path.is_file():
            continue
        stat = path.stat()
        rel = str(path.relative_to(relative_to))
        record = f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
        metadata_digest.update(record)
        content_digest.update(f"{rel}\0{_sha256_file(path)}\n".encode())
        file_count += 1
        byte_count += int(stat.st_size)
        max_mtime_ns = max(max_mtime_ns, int(stat.st_mtime_ns))
    return {
        "file_count": file_count,
        "byte_count": byte_count,
        "max_mtime_ns": max_mtime_ns,
        "path_size_mtime_sha256": metadata_digest.hexdigest(),
        "path_content_sha256": content_digest.hexdigest(),
    }


def _partition_inventory(
    root: Path,
    dataset: str,
    *,
    start_date: str,
    end_date: str,
    hash_contents: bool = True,
) -> dict[str, Any]:
    base = root / dataset
    expected = _date_range(start_date, end_date)
    present = {
        path.name.split("=", 1)[1]: path
        for path in base.glob("date=*")
        if path.is_dir() and "=" in path.name
    } if base.is_dir() else {}
    missing = [date for date in expected if date not in present]
    empty: list[str] = []
    metadata_digest = hashlib.sha256()
    content_digest = hashlib.sha256()
    file_count = 0
    byte_count = 0
    max_mtime_ns = 0
    for date in expected:
        partition = present.get(date)
        if partition is None:
            continue
        partition_files = sorted(
            (path for path in partition.rglob("*") if path.is_file()),
            key=lambda value: str(value),
        )
        if not partition_files:
            empty.append(date)
        for path in partition_files:
            stat = path.stat()
            rel = str(path.relative_to(root))
            metadata_digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
            if hash_contents:
                content_digest.update(f"{rel}\0{_sha256_file(path)}\n".encode())
            file_count += 1
            byte_count += int(stat.st_size)
            max_mtime_ns = max(max_mtime_ns, int(stat.st_mtime_ns))
    fingerprint = {
        "file_count": file_count,
        "byte_count": byte_count,
        "max_mtime_ns": max_mtime_ns,
        "path_size_mtime_sha256": metadata_digest.hexdigest(),
        "path_content_sha256": content_digest.hexdigest() if hash_contents else None,
    }
    return {
        "dataset": dataset,
        "exists": base.is_dir(),
        "required_start_date": start_date,
        "required_end_date_exclusive": end_date,
        "required_partition_count": len(expected),
        "present_required_partition_count": len(expected) - len(missing),
        "first_present_date": min(present) if present else None,
        "last_present_date": max(present) if present else None,
        "missing_partition_count": len(missing),
        "missing_partition_sample": missing[:10],
        "empty_partition_count": len(empty),
        "empty_partition_sample": empty[:10],
        "content": fingerprint,
        "ready": base.is_dir() and not missing and not empty and fingerprint["file_count"] > 0,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _residual_momentum_inventory(
    root: Path,
    *,
    start_date: str,
    signal_end_date: str,
) -> dict[str, Any]:
    path = root / "residual_momentum.parquet"
    required_day = dt.date.fromisoformat(signal_end_date) - dt.timedelta(days=1)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "required_stable_day": required_day.isoformat(),
        "ready": False,
        "failures": [],
    }
    if not path.is_file():
        result["failures"].append("residual_momentum.parquet is missing")
        return result
    stat = path.stat()
    result["content"] = {
        "byte_count": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": _sha256_file(path),
    }
    try:
        schema = pl.read_parquet_schema(path)
        result["schema"] = {name: str(dtype) for name, dtype in schema.items()}
        required = {"symbol", "ts_ms", "residual_momentum", "is_provisional"}
        missing_columns = required - set(schema)
        if missing_columns:
            result["failures"].append(f"missing columns: {sorted(missing_columns)}")
            return result
        if schema["is_provisional"] != pl.Boolean:
            result["failures"].append("is_provisional is not Boolean")
            return result
        if schema["symbol"] != pl.String:
            result["failures"].append("symbol is not String")
            return result
        if schema["ts_ms"] != pl.Int64:
            result["failures"].append("ts_ms is not Int64")
            return result
        if schema["residual_momentum"] not in (pl.Float32, pl.Float64):
            result["failures"].append("residual_momentum is not floating point")
            return result
        frame = pl.read_parquet(path, columns=list(required))
        if frame["is_provisional"].null_count() > 0:
            result["failures"].append("is_provisional contains null provenance")
        invalid_keys = frame.filter(
            pl.col("symbol").is_null()
            | (pl.col("symbol").cast(pl.String).str.strip_chars() == "")
            | pl.col("ts_ms").is_null()
            | ((pl.col("ts_ms") % MS_PER_DAY) != 0)
        )
        if not invalid_keys.is_empty():
            result["failures"].append(
                "symbol/ts_ms contains null, blank, or non-daily keys"
            )
        duplicate_keys = (
            frame.group_by(["symbol", "ts_ms"])
            .len()
            .filter(pl.col("len") > 1)
        )
        if not duplicate_keys.is_empty():
            result["failures"].append(
                f"duplicate (symbol,ts_ms) keys: {duplicate_keys.height}"
            )
        invalid_values = frame.filter(
            pl.col("residual_momentum").is_null()
            | (~pl.col("residual_momentum").is_finite())
        )
        if not invalid_values.is_empty():
            result["failures"].append(
                f"null/non-finite residual_momentum rows: {invalid_values.height}"
            )
        stable = frame.filter(~pl.col("is_provisional"))
        stable_max = None if stable.is_empty() else int(stable["ts_ms"].max())
        result["stable_max_day"] = (
            None
            if stable_max is None
            else dt.datetime.fromtimestamp(stable_max / 1000, tz=dt.timezone.utc).date().isoformat()
        )
        required_ms = int(dt.datetime.combine(required_day, dt.time(), tzinfo=dt.timezone.utc).timestamp() * 1000)
        cutoff = stable.filter(pl.col("ts_ms") == required_ms)
        finite = (
            cutoff.filter(pl.col("residual_momentum").is_not_null().and_(pl.col("residual_momentum").is_finite()))
            if not cutoff.is_empty()
            else cutoff
        )
        result["stable_cutoff_rows"] = cutoff.height
        result["stable_cutoff_finite_rows"] = finite.height
        result["stable_cutoff_symbols"] = (
            int(finite["symbol"].n_unique()) if not finite.is_empty() else 0
        )
        if cutoff.is_empty() or finite.is_empty():
            result["failures"].append(
                f"no stable finite residual-momentum rows on signal cutoff day {required_day}"
            )
        elif result["stable_cutoff_symbols"] < MIN_STABLE_RMOM_SYMBOLS:
            result["failures"].append(
                "stable residual-momentum cutoff cross-section is too small: "
                f"symbols={result['stable_cutoff_symbols']} < min={MIN_STABLE_RMOM_SYMBOLS}"
            )
        expected_days = {
            int(
                dt.datetime.combine(
                    dt.date.fromisoformat(day),
                    dt.time(),
                    tzinfo=dt.timezone.utc,
                ).timestamp()
                * 1000
            )
            for day in _date_range(start_date, signal_end_date)
        }
        stable_daily = {
            int(row["ts_ms"]): int(row["symbols"])
            for row in stable.group_by("ts_ms")
            .agg(pl.col("symbol").n_unique().alias("symbols"))
            .to_dicts()
        }
        missing_days = sorted(expected_days - set(stable_daily))
        short_days = sorted(
            ts_ms
            for ts_ms in expected_days
            if 0 < stable_daily.get(ts_ms, 0) < MIN_STABLE_RMOM_SYMBOLS
        )
        result["required_stable_day_count"] = len(expected_days)
        result["missing_stable_day_count"] = len(missing_days)
        result["short_stable_day_count"] = len(short_days)
        result["missing_stable_day_sample"] = [
            dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date().isoformat()
            for value in missing_days[:10]
        ]
        result["short_stable_day_sample"] = [
            dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date().isoformat()
            for value in short_days[:10]
        ]
        if missing_days or short_days:
            result["failures"].append(
                "stable residual-momentum history is incomplete: "
                f"missing_days={len(missing_days)} short_days={len(short_days)} "
                f"window={start_date}..{signal_end_date}"
            )
    except Exception as exc:  # noqa: BLE001 - readiness must convert corrupt input to refusal
        result["failures"].append(f"unreadable parquet: {type(exc).__name__}: {exc}")
    result["ready"] = not result["failures"]
    return result


def _root_build_receipt(
    root: Path,
    *,
    venue: str,
    data_fingerprint_sha256: str,
    start_date: str,
    signal_end_date: str,
    exit_end_date: str,
) -> dict[str, Any]:
    candidates = [
        root / "root_build_receipt.json",
        root / "_root_build_receipt.json",
        root / "reports" / "root_build_receipt.json",
    ]
    existing = next((path for path in candidates if path.is_file()), None)
    if existing is None:
        return {
            "present": False,
            "valid": False,
            "limitation": (
                "no canonical root-build verification receipt found; structural checks may run, "
                "but this root cannot support a positive registered verdict"
            ),
        }
    result: dict[str, Any] = {
        "present": True,
        "path": str(existing),
        "sha256": _sha256_file(existing),
        "valid": False,
        "failures": [],
    }
    try:
        payload = json.loads(existing.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        result["failures"].append(f"unreadable receipt: {type(exc).__name__}: {exc}")
        return result
    expected = {
        "schema_version": 1,
        "receipt_type": "full_pit_root_verification",
        "status": "passed",
        "venue": venue,
        "root": str(root.resolve()),
        "start_date": start_date,
        "signal_end_date_exclusive": signal_end_date,
        "exit_data_end_date_exclusive": exit_end_date,
        "data_fingerprint_sha256": data_fingerprint_sha256,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            result["failures"].append(
                f"{key} mismatch: got={payload.get(key)!r} expected={value!r}"
            )
    verified_at = str(payload.get("verified_at_utc") or "")
    gates = payload.get("verification_gates")
    if not verified_at.endswith("Z"):
        result["failures"].append("verified_at_utc must be a UTC Z timestamp")
    if not isinstance(gates, list) or not gates or any(not str(gate).strip() for gate in gates):
        result["failures"].append("verification_gates must be a non-empty list")
    result["payload"] = payload
    result["valid"] = not result["failures"]
    return result


def write_root_build_receipt(
    venue: str,
    root: Path,
    *,
    start_date: str = START_DATE,
    signal_end_date: str = END_DATE,
    exit_end_date: str = EXIT_DATA_END_DATE,
    verification_gates: list[str] | None = None,
) -> Path:
    """Bind a successful external full-PIT verification to the exact data root.

    This helper is called only after ``verify_full_pit_rebuild.sh`` completes
    all of its independent gates.  It refuses structurally incomplete data and
    records the exact dispatcher inventory fingerprint, so later root changes
    invalidate the attestation rather than silently inheriting it.
    """
    inventory = root_inventory(
        venue,
        root,
        start_date=start_date,
        signal_end_date=signal_end_date,
        exit_end_date=exit_end_date,
        validate_build_receipt=False,
    )
    if not inventory["data_ready"]:
        raise RuntimeError(
            f"cannot attest incomplete {venue} root: {inventory['failures']}"
        )
    payload = {
        "schema_version": 1,
        "receipt_type": "full_pit_root_verification",
        "status": "passed",
        "venue": venue,
        "root": str(root.resolve()),
        "start_date": start_date,
        "signal_end_date_exclusive": signal_end_date,
        "exit_data_end_date_exclusive": exit_end_date,
        "data_fingerprint_sha256": inventory["data_fingerprint_sha256"],
        "verified_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "verification_gates": verification_gates
        or [
            "data-layer-audit",
            "historical-coverage-parity",
            "full-PIT smoke run",
            "tests",
            "lint",
        ],
    }
    path = root / "root_build_receipt.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def root_inventory(
    venue: str,
    root: Path,
    *,
    start_date: str = START_DATE,
    signal_end_date: str | None = None,
    exit_end_date: str | None = None,
    end_date: str | None = None,
    validate_build_receipt: bool = True,
    exact_content_hash: bool = True,
) -> dict[str, Any]:
    """Deep readiness receipt for signal and forward exit-path data.

    ``end_date`` is retained as a compatibility alias for the signal boundary;
    registered calls always pass both explicit boundaries.
    """
    signal_end = signal_end_date or end_date or END_DATE
    exit_end = exit_end_date or signal_end
    datasets = {
        "klines": _partition_inventory(
            root,
            "klines_1h",
            start_date=start_date,
            end_date=exit_end,
            hash_contents=exact_content_hash,
        ),
        "membership": _partition_inventory(
            root,
            "archive_trade_manifest",
            start_date=start_date,
            end_date=signal_end,
            hash_contents=exact_content_hash,
        ),
        "funding": _partition_inventory(
            root,
            FUNDING_DATASET[venue],
            start_date=start_date,
            end_date=exit_end,
            hash_contents=exact_content_hash,
        ),
    }
    rmom = _residual_momentum_inventory(
        root,
        start_date=start_date,
        signal_end_date=signal_end,
    )
    failures: list[str] = []
    for name, row in datasets.items():
        if not row["ready"]:
            failures.append(
                f"{name}: missing={row['missing_partition_count']} empty={row['empty_partition_count']} "
                f"window={row['required_start_date']}..{row['required_end_date_exclusive']}"
            )
    failures.extend(f"residual_momentum: {failure}" for failure in rmom["failures"])
    fast_content_payload = {
        name: row["content"]["path_size_mtime_sha256"] for name, row in datasets.items()
    }
    fast_content_payload["residual_momentum"] = (rmom.get("content") or {}).get("sha256")
    fast_content_fingerprint_sha256 = _json_hash(fast_content_payload)
    exact_content_payload = {
        name: row["content"]["path_content_sha256"] for name, row in datasets.items()
    }
    exact_content_payload["residual_momentum"] = (rmom.get("content") or {}).get("sha256")
    data_fingerprint_sha256 = (
        _json_hash(exact_content_payload) if exact_content_hash else None
    )
    build_receipt = (
        _root_build_receipt(
            root,
            venue=venue,
            data_fingerprint_sha256=data_fingerprint_sha256,
            start_date=start_date,
            signal_end_date=signal_end,
            exit_end_date=exit_end,
        )
        if validate_build_receipt and data_fingerprint_sha256 is not None
        else {"present": False, "valid": False, "validation_skipped": True}
    )
    data_ready = not failures
    registered_evidence_ready = data_ready and bool(build_receipt.get("valid"))
    return {
        "venue": venue,
        "root": str(root.resolve()),
        "signal_end_date_exclusive": signal_end,
        "exit_data_end_date_exclusive": exit_end,
        "datasets": datasets,
        "residual_momentum": rmom,
        "root_build_receipt": build_receipt,
        "data_fingerprint_sha256": data_fingerprint_sha256,
        "content_fingerprint_sha256": data_fingerprint_sha256,
        "fast_content_fingerprint_sha256": fast_content_fingerprint_sha256,
        "data_ready": data_ready,
        "registered_evidence_ready": registered_evidence_ready,
        # Compatibility alias, now deliberately means both complete data and
        # a valid external full-PIT verification receipt.
        "full_pit_ready": registered_evidence_ready,
        "failures": failures,
    }


def _git_state() -> tuple[str, list[str]]:
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    lines = subprocess.run(
        ["git", "status", "--porcelain"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    dirty = []
    for line in lines:
        path = line[3:] if len(line) > 3 else line
        if path.startswith(".claude/skills/"):
            continue
        dirty.append(line)
    return commit, dirty


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _base_component_configs(
    *,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
) -> dict[str, ContinuousEventConfig]:
    overrides_by_cell = {
        cell: overrides for (_artifact_root, cell), overrides in FROZEN_COMPONENT_CELLS.items()
    }
    result: dict[str, ContinuousEventConfig] = {}
    for component, source in CONTINUOUS_COMPONENT_SOURCES.items():
        cfg = ContinuousEventConfig(**{
            **FROZEN_COMPONENT_COMMON,
            **overrides_by_cell[source.cell],
        })
        result[component] = replace(
            cfg,
            start_date=start_date,
            end_date=end_date,
            take_profit_pct=COMPONENT_TP,
        )
    return result


def effective_component_config_hashes(
    *,
    start_date: str = START_DATE,
    end_date: str = END_DATE,
) -> dict[str, dict[str, str]]:
    base = _base_component_configs(start_date=start_date, end_date=end_date)
    hashes = {
        cell: {
            component: cell_transform(spec)(cfg).config_hash()
            for component, cfg in base.items()
        }
        for cell, spec in CELLS.items()
    }
    if frozen_config_hash() != EXPECTED_FROZEN_FORWARD_CONFIG_HASH:
        raise RuntimeError(
            "FROZEN_FORWARD_CONFIG hash drifted: "
            f"got {frozen_config_hash()} expected {EXPECTED_FROZEN_FORWARD_CONFIG_HASH}"
        )
    if (
        start_date == START_DATE
        and end_date == END_DATE
        and hashes != EXPECTED_EFFECTIVE_COMPONENT_CONFIG_HASHES
    ):
        raise RuntimeError(
            "effective registered component hashes drifted: "
            f"got {hashes} expected {EXPECTED_EFFECTIVE_COMPONENT_CONFIG_HASHES}"
        )
    return hashes


def _prepare_frozen_fallback(output_root: Path, venues: list[str]) -> Path:
    fallback = output_root / "_frozen_config_receipt"
    overrides_by_cell = {
        cell: overrides for (_artifact_root, cell), overrides in FROZEN_COMPONENT_CELLS.items()
    }
    for venue in venues:
        for component, source in CONTINUOUS_COMPONENT_SOURCES.items():
            config = ContinuousEventConfig(**{
                **FROZEN_COMPONENT_COMMON,
                **overrides_by_cell[source.cell],
            })
            path = fallback / "components" / venue / source.cell / "continuous_report.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "receipt_type": "config_only_frozen_reconstruction",
                "component": component,
                "venue": venue,
                "source": "scripts/rebuild_continuous_component_ledgers.py",
                "config_hash": config.config_hash(),
                "config": asdict(config),
            }
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return fallback


def validate_runtime_component_configs(
    *,
    fallback_root: Path,
    venues: list[str],
    expected_hashes: dict[str, dict[str, str]],
    start_date: str,
    end_date: str,
) -> None:
    """Refuse before heavy compute if refresh config-source resolution drifts."""
    for venue in venues:
        base = {
            component: frozen_config(
                component,
                venue,
                end_date=end_date,
                start_date=start_date,
                fallback_root=fallback_root,
                component_take_profit_pct=COMPONENT_TP,
                backtest_leverage=1.0,
            )
            for component in WINNER_WEIGHTS
        }
        for cell, spec in CELLS.items():
            actual = {
                component: cell_transform(spec)(cfg).config_hash()
                for component, cfg in base.items()
            }
            if actual != expected_hashes[cell]:
                raise RuntimeError(
                    f"{venue} {cell} runtime config-source drift: got {actual} "
                    f"expected {expected_hashes[cell]}"
                )


def _calendar_returns(
    equity_csv: Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> np.ndarray:
    start_date = start_date or START_DATE
    end_date = end_date or EXIT_DATA_END_DATE
    frame = pl.read_csv(equity_csv).sort("ts_ms")
    start_day = int(dt.datetime.fromisoformat(start_date).replace(tzinfo=dt.timezone.utc).timestamp()) // 86_400
    end_day = int(dt.datetime.fromisoformat(end_date).replace(tzinfo=dt.timezone.utc).timestamp()) // 86_400
    calendar = np.zeros(max(end_day - start_day, 0), dtype=float)
    if frame.is_empty() or calendar.size == 0:
        return calendar
    days = (frame["ts_ms"].to_numpy().astype(np.int64) // MS_PER_DAY).astype(np.int64)
    returns = frame["basket_return"].fill_null(0.0).to_numpy().astype(float)
    for day, value in zip(days, returns):
        idx = int(day - start_day)
        if 0 <= idx < calendar.size:
            calendar[idx] += float(value)
    return calendar


def _expected_shortfall_loss(returns: np.ndarray, confidence: float) -> float:
    if returns.size == 0:
        return 0.0
    tail_count = max(1, int(np.ceil((1.0 - confidence) * returns.size)))
    return float(-np.sort(returns)[:tail_count].mean())


def _series_metrics(returns: np.ndarray) -> dict[str, float | None]:
    if returns.size == 0:
        return {
            "total_return_frac": 0.0,
            "annualized_return_frac": 0.0,
            "max_drawdown_frac": 0.0,
            "mar": None,
        }
    equity = np.cumprod(1.0 + returns)
    total = float(equity[-1] - 1.0)
    years = returns.size / 365.25
    annualized = float(equity[-1] ** (1.0 / years) - 1.0) if equity[-1] > 0.0 else -1.0
    # Include starting capital in the running peak. Otherwise an initial loss is
    # incorrectly promoted to the first high-water mark and disappears from DD,
    # MAR, split metrics, and the preregistered tail verdict.
    running_peak = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    drawdown = equity / running_peak - 1.0
    max_dd = float(drawdown.min())
    mar = annualized / abs(max_dd) if abs(max_dd) > 1e-15 else None
    return {
        "total_return_frac": total,
        "annualized_return_frac": annualized,
        "max_drawdown_frac": max_dd,
        "mar": mar,
    }


def equity_tail_metrics(
    equity_csv: Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    metric_start = start_date or START_DATE
    returns = _calendar_returns(
        equity_csv,
        start_date=metric_start,
        end_date=end_date or EXIT_DATA_END_DATE,
    )
    series = _series_metrics(returns)
    equity = np.cumprod(1.0 + returns) if returns.size else np.asarray([], dtype=float)
    drawdown_depth = (
        np.maximum(
            1.0
            - equity
            / np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:],
            0.0,
        )
        if equity.size
        else np.asarray([], dtype=float)
    )
    tail_count = max(1, int(np.ceil(0.05 * drawdown_depth.size))) if drawdown_depth.size else 0
    losses = np.sort(np.maximum(-returns, 0.0))[::-1]
    total_losses = float(losses.sum())
    split_idx = max(
        0,
        min(
            returns.size,
            (dt.date.fromisoformat(SPLIT_DATE) - dt.date.fromisoformat(metric_start)).days,
        ),
    )
    rolling_90 = [
        float(np.prod(1.0 + returns[idx - 89:idx + 1]) - 1.0)
        for idx in range(89, returns.size)
    ]
    max_no_new_high = 0
    since_high = 0
    peak = 1.0
    for value in equity:
        if value >= peak - 1e-14:
            peak = max(peak, float(value))
            since_high = 0
        else:
            since_high += 1
            max_no_new_high = max(max_no_new_high, since_high)
    return {
        "calendar_days": int(returns.size),
        "total_return_frac": series["total_return_frac"],
        "total_return_pct": float(series["total_return_frac"] or 0.0) * 100.0,
        "annualized_return_frac": series["annualized_return_frac"],
        "annualized_pct": float(series["annualized_return_frac"] or 0.0) * 100.0,
        "max_drawdown_frac": series["max_drawdown_frac"],
        "max_drawdown_pct": float(series["max_drawdown_frac"] or 0.0) * 100.0,
        "mar": series["mar"],
        "worst_day_frac": float(returns.min()) if returns.size else 0.0,
        "cdar95_loss_frac": (
            float(np.sort(drawdown_depth)[-tail_count:].mean()) if tail_count else 0.0
        ),
        "daily_es95_loss_frac": _expected_shortfall_loss(returns, 0.95),
        "daily_es99_loss_frac": _expected_shortfall_loss(returns, 0.99),
        "negative_tail_concentration_5d": (
            float(losses[:5].sum() / total_losses) if total_losses > 0.0 else 0.0
        ),
        "worst_90d_return_frac": min(rolling_90) if rolling_90 else float(series["total_return_frac"] or 0.0),
        "max_no_new_high_days": max_no_new_high,
        "split_metrics": {
            "pre_2025_06_01": _series_metrics(returns[:split_idx]),
            "post_2025_06_01": _series_metrics(returns[split_idx:]),
        },
    }


def shock_metrics_from_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    events: list[tuple[int, int, str, float]] = []
    for row in rows:
        symbol = str(row.get("symbol") or "")
        weight = abs(float(row.get("ensemble_notional_weight") or 0.0))
        entry_ts = int(row.get("entry_ts_ms") or 0)
        exit_ts = int(row.get("exit_ts_ms") or 0)
        if not symbol or weight <= 0.0 or entry_ts <= 0:
            continue
        events.append((entry_ts, 1, symbol, weight))
        if exit_ts > entry_ts:
            events.append((exit_ts, 0, symbol, -weight))
    active: dict[str, float] = {}
    worst_one = 0.0
    worst_three_half = 0.0
    for _ts, _kind, symbol, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active[symbol] = max(active.get(symbol, 0.0) + delta, 0.0)
        if active[symbol] <= 1e-15:
            active.pop(symbol, None)
        notionals = sorted(active.values(), reverse=True)
        worst_one = max(worst_one, notionals[0] if notionals else 0.0)
        worst_three_half = max(worst_three_half, 0.5 * sum(notionals[:3]))
    return {
        "one_name_100_shock_loss_frac": worst_one,
        "three_name_50_shock_loss_frac": worst_three_half,
    }


def component_trade_metrics(cell_root: Path, venue: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    report_skips: dict[str, int] = {}
    funding_modes: set[str] = set()
    config_hashes: dict[str, str] = {}
    component_candidate_rows = 0
    for component, ensemble_weight in WINNER_WEIGHTS.items():
        source = CONTINUOUS_COMPONENT_SOURCES[component]
        component_dir = cell_root / "components" / venue / source.cell
        trades_path = component_dir / "continuous_trades.csv"
        report_path = component_dir / "continuous_report.json"
        if not report_path.exists():
            raise FileNotFoundError(f"missing component report {report_path}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        config_hashes[component] = str(report.get("config_hash"))
        component_candidate_rows += int(report.get("n_fresh_entries") or 0)
        if report.get("funding_mode"):
            funding_modes.add(str(report["funding_mode"]))
        for key, value in (report.get("skips") or {}).items():
            if isinstance(value, (int, float)):
                report_skips[key] = report_skips.get(key, 0) + int(value)
        if not trades_path.exists():
            raise FileNotFoundError(f"missing component trades {trades_path}")
        trades = pl.read_csv(trades_path)
        for row in trades.to_dicts():
            row["ensemble_notional_weight"] = (
                abs(float(row.get("notional_weight") or 0.0)) * float(ensemble_weight)
            )
            rows.append(row)
    notionals = [float(row["ensemble_notional_weight"]) for row in rows]
    return {
        "component_trade_rows": len(rows),
        "component_candidate_rows": component_candidate_rows,
        "mean_ensemble_notional_weight": float(np.mean(notionals)) if notionals else 0.0,
        "risk_clamped_trade_rows": sum(bool(row.get("entry_risk_size_clamped")) for row in rows),
        "skipped_capacity_rows": int(report_skips.get("skipped_capacity", 0)),
        "funding_modes": sorted(funding_modes),
        "component_config_hashes": config_hashes,
        **shock_metrics_from_rows(rows),
    }


def collect_metrics(
    cell_root: Path,
    venue: str,
    *,
    start_date: str = START_DATE,
    exit_data_end_date: str = EXIT_DATA_END_DATE,
) -> dict[str, Any]:
    equity_csv = cell_root / venue / "continuous_equity.csv"
    if not equity_csv.exists():
        raise FileNotFoundError(f"missing equity artifact {equity_csv}")
    return {
        **equity_tail_metrics(
            equity_csv,
            start_date=start_date,
            end_date=exit_data_end_date,
        ),
        **component_trade_metrics(cell_root, venue),
    }


def _funding_symbol_path(root: Path, venue: str, date: str, symbol: str) -> Path:
    return root / FUNDING_DATASET[venue] / f"date={date}" / f"symbol={symbol}" / "part.parquet"


MAX_FUNDING_SETTLEMENT_INTERVAL_MIN = 480
FUNDING_TIMESTAMP_TOLERANCE_MS = 5 * 60_000


def _validate_funding_file(path: Path, *, symbol: str, date: str) -> str | None:
    if not path.is_file():
        return f"missing {path}"
    try:
        schema = pl.read_parquet_schema(path)
        required = {"symbol", "funding_rate", "ts_ms"}
        missing = required - set(schema)
        if missing:
            return f"missing columns {sorted(missing)} in {path}"
        columns = sorted(required | ({"funding_interval_min"} & set(schema)))
        frame = pl.read_parquet(path, columns=columns)
    except Exception as exc:  # noqa: BLE001
        return f"unreadable {path}: {exc}"
    if frame.is_empty():
        return f"empty {path}"
    if set(frame["symbol"].drop_nulls().unique().to_list()) != {symbol}:
        return f"symbol mismatch in {path}"
    if frame["funding_rate"].null_count() or not frame["funding_rate"].is_finite().all():
        return f"non-finite funding in {path}"
    if frame["ts_ms"].null_count():
        return f"null funding timestamps in {path}"
    if frame.select(pl.col("ts_ms").n_unique()).item() != frame.height:
        return f"duplicate funding timestamps in {path}"
    day_start_ms = int(
        dt.datetime.combine(
            dt.date.fromisoformat(date),
            dt.time(),
            tzinfo=dt.timezone.utc,
        ).timestamp()
        * 1000
    )
    day_end_ms = day_start_ms + MS_PER_DAY
    timestamps = sorted(int(value) for value in frame["ts_ms"].to_list())
    if timestamps[0] < day_start_ms or timestamps[-1] >= day_end_ms:
        return f"funding timestamp outside date={date} partition in {path}"
    gaps_min = [
        (right - left) // 60_000
        for left, right in zip(timestamps, timestamps[1:])
        if right > left
    ]
    interval_candidates: list[int] = []
    if "funding_interval_min" in frame.columns:
        interval_candidates.extend(
            int(value)
            for value in frame["funding_interval_min"].drop_nulls().to_list()
            if 0 < int(value) <= MAX_FUNDING_SETTLEMENT_INTERVAL_MIN
        )
    if gaps_min:
        counts: dict[int, int] = {}
        for gap in gaps_min:
            if 0 < gap <= MAX_FUNDING_SETTLEMENT_INTERVAL_MIN:
                counts[gap] = counts.get(gap, 0) + 1
        if counts:
            modal_gap = min(counts, key=lambda gap: (-counts[gap], gap))
            interval_candidates.append(modal_gap)
    expected_interval_min = (
        min(interval_candidates)
        if interval_candidates
        else MAX_FUNDING_SETTLEMENT_INTERVAL_MIN
    )
    interval_ms = expected_interval_min * 60_000
    if timestamps[0] > day_start_ms + interval_ms + FUNDING_TIMESTAMP_TOLERANCE_MS:
        return f"funding leading settlement gap exceeds {expected_interval_min}min in {path}"
    if timestamps[-1] < day_end_ms - interval_ms - FUNDING_TIMESTAMP_TOLERANCE_MS:
        return f"funding trailing settlement gap exceeds {expected_interval_min}min in {path}"
    if any(
        right - left > interval_ms + FUNDING_TIMESTAMP_TOLERANCE_MS
        for left, right in zip(timestamps, timestamps[1:])
    ):
        return f"internal funding settlement gap exceeds {expected_interval_min}min in {path}"
    return None


def validate_trade_data_planes(
    cell_root: Path,
    venue: str,
    root: Path,
    *,
    start_date: str | None = None,
    exit_data_end_date: str | None = None,
) -> dict[str, Any]:
    """Validate the exact membership/funding records consumed by completed trades."""
    start_date = start_date or START_DATE
    exit_data_end_date = exit_data_end_date or EXIT_DATA_END_DATE
    membership_cache: dict[str, set[str]] = {}
    funding_keys: set[tuple[str, str]] = set()
    membership_keys: set[tuple[str, str]] = set()
    failures: list[str] = []
    for component in WINNER_WEIGHTS:
        source = CONTINUOUS_COMPONENT_SOURCES[component]
        path = cell_root / "components" / venue / source.cell / "continuous_trades.csv"
        trades = pl.read_csv(path)
        if trades.is_empty():
            continue
        required = {"symbol", "entry_signal_ts_ms", "entry_ts_ms", "exit_ts_ms"}
        if not required.issubset(trades.columns):
            failures.append(f"{path}: missing {sorted(required - set(trades.columns))}")
            continue
        for row in trades.select(sorted(required)).iter_rows(named=True):
            symbol = str(row["symbol"])
            signal_date = dt.datetime.fromtimestamp(
                int(row["entry_signal_ts_ms"]) / 1000, tz=dt.timezone.utc
            ).date().isoformat()
            membership_keys.add((signal_date, symbol))
            entry_day = dt.datetime.fromtimestamp(
                int(row["entry_ts_ms"]) / 1000, tz=dt.timezone.utc
            ).date()
            exit_day = dt.datetime.fromtimestamp(
                int(row["exit_ts_ms"]) / 1000, tz=dt.timezone.utc
            ).date()
            day = entry_day
            while day <= exit_day:
                funding_keys.add((day.isoformat(), symbol))
                day += dt.timedelta(days=1)
    for date, symbol in sorted(membership_keys):
        if date not in membership_cache:
            path = root / "archive_trade_manifest" / f"date={date}" / "part.parquet"
            if not path.is_file():
                failures.append(f"missing membership manifest {path}")
                membership_cache[date] = set()
            else:
                try:
                    frame = pl.read_parquet(path, columns=["symbol", "date"])
                    if not frame.is_empty() and set(frame["date"].drop_nulls().unique().to_list()) != {date}:
                        failures.append(f"membership date mismatch in {path}")
                    membership_cache[date] = set(frame["symbol"].drop_nulls().to_list())
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"unreadable membership {path}: {exc}")
                    membership_cache[date] = set()
        if symbol not in membership_cache[date]:
            failures.append(f"{symbol} absent from PIT membership on {date}")
    # Hedge legs must never silently receive zero funding anywhere in the full
    # registered signal/exit window, even on flat component days.
    for date in _date_range(start_date, exit_data_end_date):
        funding_keys.add((date, "BTCUSDT"))
        funding_keys.add((date, "ETHUSDT"))
    for date, symbol in sorted(funding_keys):
        failure = _validate_funding_file(
            _funding_symbol_path(root, venue, date, symbol),
            symbol=symbol,
            date=date,
        )
        if failure:
            failures.append(failure)
            if len(failures) >= 50:
                break
    if failures:
        raise RuntimeError(
            f"{venue} exact PIT/funding validation failed ({len(failures)} shown/capped): "
            + "; ".join(failures[:10])
        )
    return {
        "membership_trade_keys": len(membership_keys),
        "funding_symbol_days": len(funding_keys),
        "hedge_instruments": ["BTCUSDT", "ETHUSDT"],
        "status": "validated",
    }


def _relative_improvement(control_loss: float, candidate_loss: float) -> float:
    return (control_loss - candidate_loss) / control_loss if control_loss > 1e-15 else 0.0


def cell_verdict(cell: str, rows_by_venue: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if cell == "control":
        missing = [venue for venue in ("bybit", "binance") if rows_by_venue.get(venue, {}).get(cell) is None]
        return {
            "status": "incomplete" if missing else "control",
            "reasons": [f"missing {venue} control receipt" for venue in missing],
        }
    missing = [
        venue
        for venue in ("bybit", "binance")
        if rows_by_venue.get(venue, {}).get("control") is None
        or rows_by_venue.get(venue, {}).get(cell) is None
    ]
    if missing:
        return {
            "status": "incomplete",
            "reasons": [f"{venue}: missing matching control/candidate receipt" for venue in missing],
        }
    reasons: list[str] = []
    for venue in ("bybit", "binance"):
        control = rows_by_venue[venue]["control"]
        candidate = rows_by_venue[venue][cell]
        if float(candidate.get("total_return_frac") or 0.0) <= 0.0:
            reasons.append(f"{venue}: non-positive return")
        if candidate.get("funding_modes") != ["modeled"]:
            reasons.append(f"{venue}: funding mode is not fully modeled")
        control_mar = float(control.get("mar") or 0.0)
        candidate_mar = float(candidate.get("mar") or 0.0)
        if control_mar > 0.0 and candidate_mar < control_mar * 0.95:
            reasons.append(f"{venue}: raw MAR below 95% control floor")
        control_dd = abs(float(control.get("max_drawdown_frac") or 0.0))
        candidate_dd = abs(float(candidate.get("max_drawdown_frac") or 0.0))
        if candidate_dd > control_dd * 1.05:
            reasons.append(f"{venue}: max drawdown worsened >5%")
        for metric, threshold, label in (
            ("cdar95_loss_frac", 0.15, "CDaR95"),
            ("daily_es99_loss_frac", 0.10, "ES99"),
            ("one_name_100_shock_loss_frac", 0.20, "one-name shock"),
            ("three_name_50_shock_loss_frac", 0.20, "three-name shock"),
        ):
            gain = _relative_improvement(
                float(control.get(metric) or 0.0), float(candidate.get(metric) or 0.0)
            )
            if gain < threshold:
                reasons.append(f"{venue}: {label} improvement <{threshold:.0%}")
        if int(candidate.get("risk_clamped_trade_rows") or 0) <= 0:
            reasons.append(f"{venue}: budget never bound")
        for count_key in ("component_trade_rows", "component_candidate_rows", "skipped_capacity_rows"):
            if int(candidate.get(count_key) or 0) != int(control.get(count_key) or 0):
                reasons.append(f"{venue}: {count_key} changed; sizing-only isolation failed")
        for split_name in ("pre_2025_06_01", "post_2025_06_01"):
            control_split = (control.get("split_metrics") or {}).get(split_name) or {}
            candidate_split = (candidate.get("split_metrics") or {}).get(split_name) or {}
            if float(candidate_split.get("total_return_frac") or 0.0) <= 0.0:
                reasons.append(f"{venue}: {split_name} return is non-positive")
            control_split_mar = float(control_split.get("mar") or 0.0)
            candidate_split_mar = float(candidate_split.get("mar") or 0.0)
            if control_split_mar > 0.0 and candidate_split_mar < control_split_mar * 0.90:
                reasons.append(f"{venue}: {split_name} raw MAR below 90% control")
        control_worst_90 = max(-float(control.get("worst_90d_return_frac") or 0.0), 0.0)
        candidate_worst_90 = max(-float(candidate.get("worst_90d_return_frac") or 0.0), 0.0)
        if candidate_worst_90 > control_worst_90 * 1.05:
            reasons.append(f"{venue}: worst-90d loss worsened >5%")
        control_drought = int(control.get("max_no_new_high_days") or 0)
        candidate_drought = int(candidate.get("max_no_new_high_days") or 0)
        if candidate_drought > max(control_drought + 7, math.ceil(control_drought * 1.10)):
            reasons.append(f"{venue}: no-new-high duration worsened beyond tolerance")
    return {"status": "pass_followup_only" if not reasons else "reject", "reasons": reasons}


def _receipt_row(receipt: dict[str, Any]) -> dict[str, Any]:
    metrics = receipt.get("metrics") or {}
    return {
        "run_id": receipt.get("run_id"),
        "cell": receipt.get("cell"),
        "family": receipt.get("family"),
        "venue": receipt.get("venue"),
        "status": receipt.get("status"),
        "total_return_frac": metrics.get("total_return_frac"),
        "max_drawdown_frac": metrics.get("max_drawdown_frac"),
        "mar": metrics.get("mar"),
        "cdar95_loss_frac": metrics.get("cdar95_loss_frac"),
        "daily_es99_loss_frac": metrics.get("daily_es99_loss_frac"),
        "worst_90d_return_frac": metrics.get("worst_90d_return_frac"),
        "max_no_new_high_days": metrics.get("max_no_new_high_days"),
        "one_name_100_shock_loss_frac": metrics.get("one_name_100_shock_loss_frac"),
        "three_name_50_shock_loss_frac": metrics.get("three_name_50_shock_loss_frac"),
        "component_candidate_rows": metrics.get("component_candidate_rows"),
        "component_trade_rows": metrics.get("component_trade_rows"),
        "risk_clamped_trade_rows": metrics.get("risk_clamped_trade_rows"),
        "skipped_capacity_rows": metrics.get("skipped_capacity_rows"),
        "funding_modes": ",".join(metrics.get("funding_modes") or []),
        "elapsed_seconds": receipt.get("elapsed_seconds"),
        "receipt": receipt.get("receipt_path"),
    }


def _matching_receipts(output_root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    run_id = manifest["run_id"]
    for cell in CELLS:
        for venue in manifest["registered_venues"]:
            path = output_root / cell / venue / "cell_receipt.json"
            if not path.exists():
                continue
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                receipt.get("status") == "complete"
                and receipt.get("run_id") == run_id
                and receipt.get("cell") == cell
                and receipt.get("venue") == venue
                and bool(receipt.get("diagnostic_only")) == bool(manifest["diagnostic_only"])
                and receipt.get("artifact_fingerprint")
                == _cell_artifact_fingerprint(output_root / cell, venue)
            ):
                receipts.append(receipt)
    return receipts


def write_summary(output_root: Path, *, manifest: dict[str, Any]) -> None:
    receipts = _matching_receipts(output_root, manifest)
    rows = [_receipt_row(receipt) for receipt in receipts]
    summary_csv = output_root / "summary.csv"
    if rows:
        with summary_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    elif summary_csv.exists():
        summary_csv.unlink()
    rows_by_venue: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        rows_by_venue.setdefault(str(receipt["venue"]), {})[str(receipt["cell"])] = receipt["metrics"]
    complete = (
        set(manifest["registered_venues"]) == {"bybit", "binance"}
        and all(
            venue in rows_by_venue and all(cell in rows_by_venue[venue] for cell in CELLS)
            for venue in ("bybit", "binance")
        )
    )
    verdicts = {cell: cell_verdict(cell, rows_by_venue) for cell in CELLS}
    for cell, verdict in list(verdicts.items()):
        if cell == "control":
            continue
        if not complete and verdict["status"] != "incomplete":
            verdicts[cell] = {
                "status": "incomplete",
                "reasons": [
                    "the clean registered two-venue full matrix is incomplete",
                    *verdict["reasons"],
                ],
            }
        elif verdict["status"] != "incomplete" and manifest["diagnostic_only"]:
            verdicts[cell] = {
                "status": "diagnostic_only",
                "reasons": [
                    "dirty, single-venue, or changed-window overrides cannot produce a verdict",
                    *verdict["reasons"],
                ],
            }
    payload = {
        "preregistration_date": PREREG_DATE,
        "run_label": RUN_LABEL,
        "run_id": manifest["run_id"],
        "diagnostic_only": manifest["diagnostic_only"],
        "complete_registered_matrix": complete,
        "matching_receipt_count": len(receipts),
        "verdicts": verdicts,
        "rows": rows,
    }
    (output_root / "summary.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )
    md = [
        "# Continuous tail-survival verdict",
        "",
        f"Run label: `{RUN_LABEL}`",
        f"Run ID: `{manifest['run_id']}`",
        f"Complete registered matrix: `{complete}`",
        f"Diagnostic-only run: `{manifest['diagnostic_only']}`",
        "",
        "No result authorizes deployment or real money. A pass only authorizes a separate forward-shadow implementation review.",
        "",
        "| Cell | Verdict | Reasons |",
        "| --- | --- | --- |",
    ]
    for cell, verdict in verdicts.items():
        md.append(f"| {cell} | {verdict['status']} | {'; '.join(verdict['reasons']) or '-'} |")
    md.extend(["", f"Metrics: `{summary_csv}`", ""])
    (output_root / "verdict.md").write_text("\n".join(md), encoding="utf-8")


def _parse_venues(raw: list[str]) -> list[str]:
    venues: list[str] = []
    for item in raw:
        venues.extend(part.strip().lower() for part in item.split(",") if part.strip())
    unknown = sorted(set(venues) - set(DEFAULT_ROOTS))
    if unknown:
        raise ValueError(f"unknown venues {unknown}; expected bybit/binance")
    return list(dict.fromkeys(venues))


def _parse_cells(raw: list[str] | None) -> list[str]:
    if not raw:
        return list(CELLS)
    cells: list[str] = []
    for item in raw:
        cells.extend(part.strip() for part in item.split(",") if part.strip())
    unknown = sorted(set(cells) - set(CELLS))
    if unknown:
        raise ValueError(f"unknown cells {unknown}; expected one of {list(CELLS)}")
    ordered = ["control", *[cell for cell in cells if cell != "control"]]
    return list(dict.fromkeys(ordered))


def _cell_artifact_fingerprint(cell_root: Path, venue: str) -> dict[str, Any]:
    paths: list[Path] = []
    roots = [
        cell_root / venue,
        cell_root / "components" / venue,
        cell_root / "_btc_risk_decision_components" / "components" / venue,
        cell_root / "btc_risk" / venue,
    ]
    for root in roots:
        if root.exists():
            paths.extend(
                path
                for path in root.rglob("*")
                if path.is_file() and path.name != "cell_receipt.json"
            )
    return _stat_fingerprint(paths, relative_to=cell_root)


def _purge_cell_venue(cell_root: Path, venue: str) -> None:
    targets = [
        cell_root / venue,
        cell_root / "components" / venue,
        cell_root / "_btc_risk_decision_components" / "components" / venue,
        cell_root / "btc_risk" / venue,
    ]
    for target in targets:
        if target.exists():
            shutil.rmtree(target)


def _begin_cell_venue(
    output_root: Path,
    *,
    manifest: dict[str, Any],
    cell: str,
    venue: str,
    signature_payload: dict[str, Any],
    signature: str,
) -> Path:
    """Invalidate an old complete receipt before destructive rerun work.

    The top-level verdict is rewritten while the receipt is marked ``running``.
    A crash during purge or compute therefore cannot leave a prior pass
    advertised as current.
    """
    cell_root = output_root / cell
    receipt_path = cell_root / venue / "cell_receipt.json"
    marker = {
        **signature_payload,
        "signature": signature,
        "status": "running",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    def write_marker() -> None:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    write_marker()
    write_summary(output_root, manifest=manifest)
    _purge_cell_venue(cell_root, venue)
    write_marker()
    return receipt_path


def _manifest_payload(
    *,
    commit: str,
    dirty: list[str],
    roots: dict[str, dict[str, Any]],
    registered_venues: list[str],
    diagnostic_only: bool,
    diagnostic_overrides: dict[str, bool],
    component_hashes: dict[str, dict[str, str]],
    start_date: str = START_DATE,
    signal_end_date: str = END_DATE,
    exit_data_end_date: str = EXIT_DATA_END_DATE,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "preregistration_date": PREREG_DATE,
        "run_label": RUN_LABEL,
        "git_commit": commit,
        "dirty_paths": dirty,
        "start_date": start_date,
        "signal_end_date_exclusive": signal_end_date,
        "exit_data_end_date_exclusive": exit_data_end_date,
        "registered_venues": registered_venues,
        "registered_cells": {cell: asdict(spec) for cell, spec in CELLS.items()},
        "component_take_profit_pct": COMPONENT_TP,
        "btc_risk_sizing": True,
        "btc_risk_tape_policy": "endogenous_per_cell_no_reuse",
        "frozen_forward_config_hash": EXPECTED_FROZEN_FORWARD_CONFIG_HASH,
        "effective_component_config_hashes": component_hashes,
        "roots": roots,
        "diagnostic_only": diagnostic_only,
        "diagnostic_overrides": diagnostic_overrides,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bybit-root", default=str(DEFAULT_ROOTS["bybit"]))
    parser.add_argument("--binance-root", default=str(DEFAULT_ROOTS["binance"]))
    parser.add_argument("--output-root", default=str(SHARED / "continuous_tail_survival_2026-07-10"))
    parser.add_argument("--venues", nargs="+", default=["bybit", "binance"])
    parser.add_argument("--cells", nargs="+", default=None)
    parser.add_argument("--start-date", default=START_DATE)
    parser.add_argument("--end-date", default=END_DATE)
    parser.add_argument("--exit-data-end-date", default=EXIT_DATA_END_DATE)
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--allow-single-venue-diagnostic", action="store_true")
    parser.add_argument("--allow-window-diagnostic", action="store_true")
    parser.add_argument("--allow-dirty-diagnostic", action="store_true")
    args = parser.parse_args()

    try:
        start_day = dt.date.fromisoformat(args.start_date)
        signal_end_day = dt.date.fromisoformat(args.end_date)
        exit_end_day = dt.date.fromisoformat(args.exit_data_end_date)
    except ValueError as exc:
        parser.error(f"invalid ISO date: {exc}")
    if not start_day < signal_end_day <= exit_end_day:
        parser.error("dates must satisfy start_date < end_date <= exit_data_end_date")

    venues = _parse_venues(args.venues)
    cells = _parse_cells(args.cells)
    diagnostic_only = False
    diagnostic_overrides = {
        "single_venue": False,
        "window": False,
        "dirty": False,
        "unverified_root_receipt": False,
    }
    if set(venues) != {"bybit", "binance"}:
        if not args.allow_single_venue_diagnostic:
            parser.error(
                "registered evidence requires both venues; use --allow-single-venue-diagnostic only for diagnostics"
            )
        diagnostic_only = True
        diagnostic_overrides["single_venue"] = True
    if (
        args.start_date != START_DATE
        or args.end_date != END_DATE
        or args.exit_data_end_date != EXIT_DATA_END_DATE
    ):
        if not args.allow_window_diagnostic:
            parser.error(
                f"registered signal window is {START_DATE}..{END_DATE} exclusive and exit data ends "
                f"{EXIT_DATA_END_DATE} exclusive"
            )
        diagnostic_only = True
        diagnostic_overrides["window"] = True
    commit, dirty = _git_state()
    if dirty:
        if not args.allow_dirty_diagnostic:
            print("REFUSED: relevant worktree changes are uncommitted:\n" + "\n".join(dirty), file=sys.stderr)
            return 2
        diagnostic_only = True
        diagnostic_overrides["dirty"] = True

    try:
        component_hashes = effective_component_config_hashes(
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except RuntimeError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    roots = {
        "bybit": Path(args.bybit_root).expanduser(),
        "binance": Path(args.binance_root).expanduser(),
    }
    inventories = {
        venue: root_inventory(
            venue,
            roots[venue],
            start_date=args.start_date,
            signal_end_date=args.end_date,
            exit_end_date=args.exit_data_end_date,
        )
        for venue in venues
    }
    if all(inventory["data_ready"] for inventory in inventories.values()) and not all(
        inventory["registered_evidence_ready"] for inventory in inventories.values()
    ):
        diagnostic_only = True
        diagnostic_overrides["unverified_root_receipt"] = True
    manifest_base = _manifest_payload(
        commit=commit,
        dirty=dirty,
        roots=inventories,
        registered_venues=venues,
        diagnostic_only=diagnostic_only,
        diagnostic_overrides=diagnostic_overrides,
        component_hashes=component_hashes,
        start_date=args.start_date,
        signal_end_date=args.end_date,
        exit_data_end_date=args.exit_data_end_date,
    )
    run_id = _json_hash(manifest_base)
    manifest = {**manifest_base, "run_id": run_id}
    plan = {
        **manifest,
        "execution_selection": {"venues": venues, "cells": cells},
    }
    print(json.dumps(plan, indent=2, default=str), flush=True)
    if not all(inventory["data_ready"] for inventory in inventories.values()):
        print(
            "REFUSED: refresh every signal/exit kline and funding partition, PIT membership, and "
            "stable residual momentum through the fixed boundaries.",
            file=sys.stderr,
        )
        return 2
    if diagnostic_overrides["unverified_root_receipt"]:
        print(
            "WARNING: one or more roots lack a matching full-PIT verification receipt; "
            "the run is diagnostic-only and cannot emit a positive registered verdict. "
            "Run scripts/verify_full_pit_rebuild.sh after the fixed exit-data boundary "
            "to attest the exact roots.",
            file=sys.stderr,
        )
    if args.plan:
        return 0

    output_root = Path(args.output_root).expanduser()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "run_manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if existing.get("run_id") != run_id:
            print(
                "REFUSED: output root belongs to a different run manifest; use a new output root "
                "instead of mixing commits, roots, windows, or diagnostics.",
                file=sys.stderr,
            )
            return 2
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (output_root / "config.json").write_text(
        json.dumps(plan, indent=2, default=str), encoding="utf-8"
    )
    write_summary(output_root, manifest=manifest)
    fallback_root = _prepare_frozen_fallback(output_root, venues)
    try:
        validate_runtime_component_configs(
            fallback_root=fallback_root,
            venues=venues,
            expected_hashes=component_hashes,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    for cell in cells:
        spec = CELLS[cell]
        cell_root = output_root / cell
        for venue in venues:
            signature_payload = {
                "run_id": run_id,
                "cell": cell,
                "spec": asdict(spec),
                "venue": venue,
                "effective_component_config_hashes": component_hashes[cell],
            }
            signature = _json_hash(signature_payload)
            receipt_path = cell_root / venue / "cell_receipt.json"
            if receipt_path.exists() and not args.force:
                existing = json.loads(receipt_path.read_text(encoding="utf-8"))
                artifacts = _cell_artifact_fingerprint(cell_root, venue)
                if (
                    existing.get("status") == "complete"
                    and existing.get("run_id") == run_id
                    and existing.get("signature") == signature
                    and existing.get("artifact_fingerprint") == artifacts
                ):
                    print(f"[skip] {venue} {cell}: complete matching receipt", flush=True)
                    continue
            receipt_path = _begin_cell_venue(
                output_root,
                manifest=manifest,
                cell=cell,
                venue=venue,
                signature_payload=signature_payload,
                signature=signature,
            )
            print(f"[run ] {venue} {cell}", flush=True)
            started = time.perf_counter()
            run_venue(
                venue,
                output_root=cell_root,
                end_date=args.end_date,
                exit_data_end_date=args.exit_data_end_date,
                start_date=args.start_date,
                frozen_fallback=fallback_root,
                data_root=roots[venue],
                chart_leverage=1.0,
                component_take_profit_pct=COMPONENT_TP,
                btc_risk_sizing=True,
                btc_risk_lookup_root=None,
                config_transform=cell_transform(spec),
                write_candidate_tape=True,
                strict_hedge_coverage=True,
                strict_btc_risk_lookup=True,
                isolate_research_state=True,
            )
            metrics = collect_metrics(
                cell_root,
                venue,
                start_date=args.start_date,
                exit_data_end_date=args.exit_data_end_date,
            )
            if metrics["component_config_hashes"] != component_hashes[cell]:
                raise RuntimeError(
                    f"{venue} {cell} component config drift: got {metrics['component_config_hashes']} "
                    f"expected {component_hashes[cell]}"
                )
            data_plane_validation = validate_trade_data_planes(
                cell_root,
                venue,
                roots[venue],
                start_date=args.start_date,
                exit_data_end_date=args.exit_data_end_date,
            )
            after_inventory = root_inventory(
                venue,
                roots[venue],
                start_date=args.start_date,
                signal_end_date=args.end_date,
                exit_end_date=args.exit_data_end_date,
                validate_build_receipt=False,
                exact_content_hash=False,
            )
            if (
                after_inventory["fast_content_fingerprint_sha256"]
                != inventories[venue]["fast_content_fingerprint_sha256"]
            ):
                raise RuntimeError(
                    f"{venue} root content changed during the run; discard this cell and rerun"
                )
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_fingerprint = _cell_artifact_fingerprint(cell_root, venue)
            receipt = {
                **signature_payload,
                "signature": signature,
                "status": "complete",
                "family": spec.family,
                "run_label": RUN_LABEL,
                "diagnostic_only": diagnostic_only,
                "diagnostic_overrides": diagnostic_overrides,
                "git_commit": commit,
                "start_date": args.start_date,
                "signal_end_date_exclusive": args.end_date,
                "exit_data_end_date_exclusive": args.exit_data_end_date,
                "root_content_fingerprint_sha256": inventories[venue]["content_fingerprint_sha256"],
                "data_plane_validation": data_plane_validation,
                "metrics": metrics,
                "artifact_fingerprint": artifact_fingerprint,
                "elapsed_seconds": time.perf_counter() - started,
                "receipt_path": str(receipt_path),
            }
            receipt_path.write_text(
                json.dumps(receipt, indent=2, default=str), encoding="utf-8"
            )
            write_summary(output_root, manifest=manifest)
    for venue in venues:
        final_inventory = root_inventory(
            venue,
            roots[venue],
            start_date=args.start_date,
            signal_end_date=args.end_date,
            exit_end_date=args.exit_data_end_date,
        )
        if (
            final_inventory["data_fingerprint_sha256"]
            != inventories[venue]["data_fingerprint_sha256"]
            or final_inventory["root_build_receipt"].get("sha256")
            != inventories[venue]["root_build_receipt"].get("sha256")
        ):
            raise RuntimeError(
                f"{venue} root bytes or verification receipt changed during the matrix; "
                "discard this run"
            )
    write_summary(output_root, manifest=manifest)
    print(f"summary: {output_root / 'verdict.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
