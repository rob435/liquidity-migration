"""Freeze and enforce the natural-cutover strategy candidate population.

The artifact is deliberately a forward, point-in-time population contract.  It
does not claim historical PIT membership.  LONG and CONT keep their own ranking
and signal logic; this module only prevents a post-freeze listing from entering
the evidence window and fails closed when a frozen tradable symbol disappears.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

from .artifact_snapshot import StableFileSnapshot, read_stable_file
from .config import DEFAULT_EXCLUDED_SYMBOLS, UniverseConfig
from .deterministic_serialization import canonical_json, json_safe
from .downloaders import _normalize_instruments, _normalize_tickers
from .universe import build_current_universe_table


CANDIDATE_UNIVERSE_SCHEMA_VERSION = 1
CANDIDATE_UNIVERSE_KIND = "account_execution_candidate_universe"
_PROFILE_NAMES = ("long", "continuous")


@dataclass(frozen=True, slots=True)
class FrozenCandidateUniverse:
    path: Path
    symbols: tuple[str, ...]
    artifact_sha256: str
    file_sha256: str
    snapshot_ts_ns: int
    profile_inputs: Mapping[str, Mapping[str, Any]]


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or not symbol.isascii() or not symbol.replace("-", "").isalnum():
        raise ValueError(f"invalid candidate-universe symbol {value!r}")
    return symbol


def _unique_rows(rows: Sequence[Mapping[str, Any]], *, label: str) -> dict[str, Mapping[str, Any]]:
    output: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{label}[{index}] must be an object")
        symbol = _symbol(row.get("symbol"))
        if symbol in output:
            raise ValueError(f"{label} contains duplicate symbol {symbol!r}")
        output[symbol] = row
    return output


def profile_universe_inputs(
    *,
    long_config: object,
    continuous_config: object,
) -> dict[str, dict[str, Any]]:
    """Extract the complete pre-signal universe knobs from effective configs."""

    return {
        "long": long_profile_universe_inputs(long_config),
        "continuous": continuous_profile_universe_inputs(continuous_config),
    }


def long_profile_universe_inputs(long_config: object) -> dict[str, Any]:
    long_rank_end = int(getattr(long_config, "universe_superset_size"))
    if long_rank_end <= 0:
        raise ValueError("LONG universe_superset_size must be positive")
    long_exclude = tuple(
        sorted({_symbol(value) for value in DEFAULT_EXCLUDED_SYMBOLS})
    )
    return {
        "min_turnover_24h": 2_000_000.0,
        "min_age_days": 30,
        "max_age_days": 0,
        "rank_start": 1,
        "rank_end": long_rank_end,
        "max_symbols": long_rank_end,
        "exclude_symbols": list(long_exclude),
    }


def continuous_profile_universe_inputs(continuous_config: object) -> dict[str, Any]:
    continuous_rank_end = int(getattr(continuous_config, "universe_rank_end"))
    continuous_max = int(getattr(continuous_config, "universe_max_symbols"))
    if continuous_rank_end < 0 or continuous_max < 0:
        raise ValueError("CONT universe bounds must be non-negative")
    continuous_unlimited = continuous_rank_end == 0 and continuous_max == 0
    continuous_exclude = tuple(
        sorted(
            {
                _symbol(value)
                for value in getattr(
                    continuous_config,
                    "exclude_symbols",
                    DEFAULT_EXCLUDED_SYMBOLS,
                )
            }
        )
    )
    return {
        "min_turnover_24h": float(
            getattr(continuous_config, "universe_min_turnover_24h")
        ),
        "min_age_days": 0 if continuous_unlimited else 30,
        "max_age_days": 0,
        "rank_start": 1,
        "rank_end": continuous_rank_end,
        "max_symbols": continuous_max,
        "exclude_symbols": list(continuous_exclude),
    }


def _universe_config(payload: Mapping[str, Any]) -> UniverseConfig:
    expected = {
        "min_turnover_24h",
        "min_age_days",
        "max_age_days",
        "rank_start",
        "rank_end",
        "max_symbols",
        "exclude_symbols",
    }
    if set(payload) != expected:
        raise ValueError("candidate profile inputs have an unexpected schema")
    exclude = payload["exclude_symbols"]
    if not isinstance(exclude, list) or any(type(value) is not str for value in exclude):
        raise ValueError("candidate profile exclude_symbols must be a string list")
    return UniverseConfig(
        min_turnover_24h=float(payload["min_turnover_24h"]),
        min_age_days=int(payload["min_age_days"]),
        max_age_days=int(payload["max_age_days"]),
        rank_start=int(payload["rank_start"]),
        rank_end=int(payload["rank_end"]),
        max_symbols=int(payload["max_symbols"]),
        exclude_symbols=tuple(_symbol(value) for value in exclude),
    )


def build_profile_universe_tables(
    instrument_rows: Sequence[Mapping[str, Any]],
    ticker_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_ts_ms: int,
    profile_inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, pl.DataFrame]:
    """Rebuild the two authoritative pre-signal strategy populations."""

    if snapshot_ts_ms <= 0:
        raise ValueError("snapshot_ts_ms must be positive")
    if set(profile_inputs) != set(_PROFILE_NAMES):
        raise ValueError("candidate profile inputs must contain long and continuous")
    _unique_rows(instrument_rows, label="instrument_rows")
    _unique_rows(ticker_rows, label="ticker_rows")
    instruments = _normalize_instruments([dict(row) for row in instrument_rows])
    tickers = _normalize_tickers([dict(row) for row in ticker_rows])
    return build_profile_universe_tables_from_frames(
        instruments,
        tickers,
        snapshot_ts_ms=snapshot_ts_ms,
        profile_inputs=profile_inputs,
    )


def build_profile_universe_tables_from_frames(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    *,
    snapshot_ts_ms: int,
    profile_inputs: Mapping[str, Mapping[str, Any]],
) -> dict[str, pl.DataFrame]:
    if snapshot_ts_ms <= 0:
        raise ValueError("snapshot_ts_ms must be positive")
    if set(profile_inputs) != set(_PROFILE_NAMES):
        raise ValueError("candidate profile inputs must contain long and continuous")
    return {
        profile: build_current_universe_table(
            instruments,
            tickers,
            universe_config=_universe_config(profile_inputs[profile]),
            snapshot_ts_ms=snapshot_ts_ms,
        )
        for profile in _PROFILE_NAMES
    }


def _base_reasons(
    *,
    symbol: str,
    instrument: Mapping[str, Any] | None,
    ticker: Mapping[str, Any] | None,
    config: UniverseConfig,
    snapshot_ts_ms: int,
) -> list[str]:
    reasons: list[str] = []
    if instrument is None:
        return ["missing_instrument"]
    if ticker is None:
        reasons.append("missing_ticker")
    if str(instrument.get("status") or "") != "Trading":
        reasons.append("status_not_trading")
    if str(instrument.get("settleCoin") or "") != "USDT":
        reasons.append("settle_coin_not_usdt")
    if bool(instrument.get("isPreListing")):
        reasons.append("prelisting")
    contract_type = str(instrument.get("contractType") or "")
    if contract_type not in {"LinearPerpetual", "linear", "Linear"}:
        reasons.append("not_linear_perpetual")
    try:
        delivery_ms = int(instrument.get("deliveryTime") or 0)
    except (TypeError, ValueError):
        delivery_ms = -1
    if delivery_ms != 0:
        reasons.append("dated_or_invalid_delivery")
    if symbol in set(config.exclude_symbols):
        reasons.append("excluded_by_config")
    if ticker is not None:
        raw_turnover = ticker.get("turnover24h")
        try:
            turnover = float(str(raw_turnover))
        except (TypeError, ValueError):
            turnover = float("nan")
        if turnover != turnover:
            reasons.append("turnover_missing_or_invalid")
        elif turnover < config.min_turnover_24h:
            reasons.append("turnover_below_floor")
    if config.min_age_days > 0 or config.max_age_days > 0:
        try:
            launch_ms = int(instrument.get("launchTime") or 0)
        except (TypeError, ValueError):
            launch_ms = 0
        if launch_ms <= 0 or launch_ms > snapshot_ts_ms:
            reasons.append("listing_age_missing_or_invalid")
        else:
            age_days = (snapshot_ts_ms - launch_ms) / 86_400_000.0
            if config.min_age_days > 0 and age_days < config.min_age_days:
                reasons.append("listing_age_below_floor")
            if config.max_age_days > 0 and age_days > config.max_age_days:
                reasons.append("listing_age_above_ceiling")
    return reasons


def _decision_rows(
    instruments: Mapping[str, Mapping[str, Any]],
    tickers: Mapping[str, Mapping[str, Any]],
    *,
    eligible: Mapping[str, set[str]],
    profile_inputs: Mapping[str, Mapping[str, Any]],
    snapshot_ts_ms: int,
) -> list[dict[str, Any]]:
    union = eligible["long"] | eligible["continuous"]
    decisions: list[dict[str, Any]] = []
    for symbol in sorted(set(instruments) | set(tickers)):
        profile_rows: dict[str, Any] = {}
        for profile in _PROFILE_NAMES:
            included = symbol in eligible[profile]
            reasons = _base_reasons(
                symbol=symbol,
                instrument=instruments.get(symbol),
                ticker=tickers.get(symbol),
                config=_universe_config(profile_inputs[profile]),
                snapshot_ts_ms=snapshot_ts_ms,
            )
            if not included and not reasons:
                reasons = ["outside_configured_liquidity_rank"]
            profile_rows[profile] = {
                "included": included,
                "reasons": [] if included else sorted(set(reasons)),
            }
        decisions.append({
            "symbol": symbol,
            "included_in_union": symbol in union,
            "profiles": profile_rows,
        })
    return decisions


def build_candidate_universe_artifact(
    instrument_rows: Sequence[Mapping[str, Any]],
    ticker_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_ts_ns: int,
    long_config: object,
    continuous_config: object,
) -> dict[str, Any]:
    """Build the self-hashed population artifact from one public demo snapshot."""

    if snapshot_ts_ns <= 0:
        raise ValueError("snapshot_ts_ns must be positive")
    snapshot_ts_ms = snapshot_ts_ns // 1_000_000
    instruments = _unique_rows(instrument_rows, label="instrument_rows")
    tickers = _unique_rows(ticker_rows, label="ticker_rows")
    inputs = profile_universe_inputs(
        long_config=long_config,
        continuous_config=continuous_config,
    )
    tables = build_profile_universe_tables(
        instrument_rows,
        ticker_rows,
        snapshot_ts_ms=snapshot_ts_ms,
        profile_inputs=inputs,
    )
    eligible = {
        profile: set(table["symbol"].to_list()) if not table.is_empty() else set()
        for profile, table in tables.items()
    }
    symbols = sorted(eligible["long"] | eligible["continuous"])
    decisions = _decision_rows(
        instruments,
        tickers,
        eligible=eligible,
        profile_inputs=inputs,
        snapshot_ts_ms=snapshot_ts_ms,
    )
    raw_instruments = json_safe([dict(row) for row in instrument_rows])
    raw_tickers = json_safe([dict(row) for row in ticker_rows])
    payload: dict[str, Any] = {
        "schema_version": CANDIDATE_UNIVERSE_SCHEMA_VERSION,
        "kind": CANDIDATE_UNIVERSE_KIND,
        "environment": "demo",
        "endpoint": "api-demo.bybit.com",
        "snapshot_ts_ns": snapshot_ts_ns,
        "profile_inputs": json_safe(inputs),
        "profile_input_sha256": {
            profile: hashlib.sha256(canonical_json(inputs[profile])).hexdigest()
            for profile in _PROFILE_NAMES
        },
        "raw_source": {
            "instrument_rows": len(instrument_rows),
            "ticker_rows": len(ticker_rows),
            "instrument_rows_sha256": hashlib.sha256(
                canonical_json({"rows": raw_instruments})
            ).hexdigest(),
            "ticker_rows_sha256": hashlib.sha256(
                canonical_json({"rows": raw_tickers})
            ).hexdigest(),
        },
        "raw_snapshot": {
            "instrument_rows": raw_instruments,
            "ticker_rows": raw_tickers,
        },
        "profile_eligible_symbols": {
            profile: sorted(eligible[profile]) for profile in _PROFILE_NAMES
        },
        "symbols": symbols,
        "symbol_count": len(symbols),
        "decisions": decisions,
        "limitations": [
            "forward_point_in_time_population_not_historical_pit_membership",
            "strategy_signal_rank_return_and_entry_conditions_not_evaluated",
            "post_snapshot_listings_are_ignored_for_the_bounded_evidence_window",
        ],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = _self_hash(payload)
    return payload


def write_candidate_universe(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Create, never overwrite, a mode-0600 immutable-source artifact."""

    if str(payload.get("artifact_sha256") or "") != _self_hash(payload):
        raise ValueError("candidate-universe artifact_sha256 is missing or invalid")
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return output.resolve(strict=True)


def load_candidate_universe(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> FrozenCandidateUniverse:
    if snapshot is None:
        try:
            snapshot = read_stable_file(
                path,
                label="candidate-universe artifact",
                require_owner=True,
                require_single_link=False,
            )
        except ValueError as exc:
            if "symbolic link" in str(exc):
                raise ValueError(
                    "candidate-universe artifact must be a non-symlink regular file"
                ) from exc
            raise
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("candidate-universe artifact snapshot path differs")
    if snapshot.mode & 0o077:
        raise ValueError("candidate-universe artifact must not be group/world accessible")
    if snapshot.uid != os.geteuid():
        raise ValueError("candidate-universe artifact must be owned by the verifier")
    data = snapshot.data
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate-universe artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("candidate-universe artifact must be a JSON object")
    if payload.get("schema_version") != CANDIDATE_UNIVERSE_SCHEMA_VERSION:
        raise ValueError("candidate-universe schema_version is unsupported")
    if payload.get("kind") != CANDIDATE_UNIVERSE_KIND or payload.get("environment") != "demo":
        raise ValueError("candidate-universe identity is invalid")
    if payload.get("endpoint") != "api-demo.bybit.com":
        raise ValueError("candidate-universe endpoint is not api-demo.bybit.com")
    artifact_hash = str(payload.get("artifact_sha256") or "")
    if artifact_hash != _self_hash(payload):
        raise ValueError("candidate-universe artifact_sha256 is invalid")
    raw_symbols = payload.get("symbols")
    if not isinstance(raw_symbols, list) or any(type(value) is not str for value in raw_symbols):
        raise ValueError("candidate-universe symbols must be a string list")
    symbols = tuple(_symbol(value) for value in raw_symbols)
    if not symbols or list(symbols) != sorted(set(symbols)):
        raise ValueError("candidate-universe symbols must be non-empty, unique, and sorted")
    if payload.get("symbol_count") != len(symbols):
        raise ValueError("candidate-universe symbol_count is inconsistent")
    profile_inputs = payload.get("profile_inputs")
    if not isinstance(profile_inputs, Mapping) or set(profile_inputs) != set(_PROFILE_NAMES):
        raise ValueError("candidate-universe profile_inputs are invalid")
    for profile in _PROFILE_NAMES:
        if not isinstance(profile_inputs[profile], Mapping):
            raise ValueError(f"candidate-universe {profile} profile inputs are invalid")
        _universe_config(profile_inputs[profile])
    profile_hashes = payload.get("profile_input_sha256")
    if not isinstance(profile_hashes, Mapping):
        raise ValueError("candidate-universe profile_input_sha256 is invalid")
    for profile in _PROFILE_NAMES:
        expected_profile_hash = hashlib.sha256(
            canonical_json(dict(profile_inputs[profile]))
        ).hexdigest()
        if profile_hashes.get(profile) != expected_profile_hash:
            raise ValueError(f"candidate-universe {profile} profile hash is invalid")
    raw_snapshot = payload.get("raw_snapshot")
    raw_source = payload.get("raw_source")
    if not isinstance(raw_snapshot, Mapping) or not isinstance(raw_source, Mapping):
        raise ValueError("candidate-universe raw snapshot metadata is invalid")
    raw_instruments = raw_snapshot.get("instrument_rows")
    raw_tickers = raw_snapshot.get("ticker_rows")
    if (
        not isinstance(raw_instruments, list)
        or not isinstance(raw_tickers, list)
        or any(not isinstance(row, Mapping) for row in raw_instruments)
        or any(not isinstance(row, Mapping) for row in raw_tickers)
    ):
        raise ValueError("candidate-universe raw snapshot rows are invalid")
    if raw_source.get("instrument_rows") != len(raw_instruments) or raw_source.get(
        "ticker_rows"
    ) != len(raw_tickers):
        raise ValueError("candidate-universe raw row counts are inconsistent")
    if raw_source.get("instrument_rows_sha256") != hashlib.sha256(
        canonical_json({"rows": raw_instruments})
    ).hexdigest() or raw_source.get("ticker_rows_sha256") != hashlib.sha256(
        canonical_json({"rows": raw_tickers})
    ).hexdigest():
        raise ValueError("candidate-universe raw source hashes are invalid")
    snapshot_ts_ns = int(payload.get("snapshot_ts_ns") or 0)
    if snapshot_ts_ns <= 0:
        raise ValueError("candidate-universe snapshot_ts_ns must be positive")
    if "snapshot_started_ts_ns" in payload or "snapshot_completed_ts_ns" in payload:
        started = int(payload.get("snapshot_started_ts_ns") or 0)
        completed = int(payload.get("snapshot_completed_ts_ns") or 0)
        if started <= 0 or not started <= snapshot_ts_ns <= completed:
            raise ValueError("candidate-universe acquisition interval is invalid")
    rebuilt_tables = build_profile_universe_tables(
        raw_instruments,
        raw_tickers,
        snapshot_ts_ms=snapshot_ts_ns // 1_000_000,
        profile_inputs={
            profile: dict(profile_inputs[profile]) for profile in _PROFILE_NAMES
        },
    )
    rebuilt_eligible = {
        profile: (
            set(table["symbol"].to_list()) if not table.is_empty() else set()
        )
        for profile, table in rebuilt_tables.items()
    }
    declared_eligible = payload.get("profile_eligible_symbols")
    if not isinstance(declared_eligible, Mapping):
        raise ValueError("candidate-universe profile populations are invalid")
    for profile in _PROFILE_NAMES:
        values = declared_eligible.get(profile)
        if (
            not isinstance(values, list)
            or any(type(value) is not str for value in values)
            or values != sorted(rebuilt_eligible[profile])
        ):
            raise ValueError(f"candidate-universe {profile} population is inconsistent")
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("candidate-universe decisions must be a list")
    instrument_map = _unique_rows(raw_instruments, label="raw instrument_rows")
    ticker_map = _unique_rows(raw_tickers, label="raw ticker_rows")
    rebuilt_decisions = _decision_rows(
        instrument_map,
        ticker_map,
        eligible=rebuilt_eligible,
        profile_inputs={
            profile: dict(profile_inputs[profile]) for profile in _PROFILE_NAMES
        },
        snapshot_ts_ms=snapshot_ts_ns // 1_000_000,
    )
    if canonical_json({"decisions": decisions}) != canonical_json(
        {"decisions": rebuilt_decisions}
    ):
        raise ValueError("candidate-universe decision table is not reproducible")
    included = sorted(
        _symbol(row.get("symbol"))
        for row in decisions
        if isinstance(row, Mapping) and row.get("included_in_union") is True
    )
    if included != list(symbols):
        raise ValueError("candidate-universe decisions do not reproduce symbols")
    return FrozenCandidateUniverse(
        path=snapshot.path,
        symbols=symbols,
        artifact_sha256=artifact_hash,
        file_sha256=snapshot.sha256,
        snapshot_ts_ns=snapshot_ts_ns,
        profile_inputs={
            profile: dict(profile_inputs[profile]) for profile in _PROFILE_NAMES
        },
    )


def enforce_frozen_candidate_population(
    current_eligible_symbols: Sequence[str],
    frozen: FrozenCandidateUniverse,
    *,
    context: str,
) -> tuple[str, ...]:
    """Fail on frozen-symbol disappearance and drop only post-freeze additions."""

    current = {_symbol(value) for value in current_eligible_symbols}
    required = set(frozen.symbols)
    missing = sorted(required - current)
    if missing:
        preview = ",".join(missing[:20])
        suffix = "..." if len(missing) > 20 else ""
        raise RuntimeError(
            f"{context}: frozen candidate population lost {len(missing)} symbol(s): "
            f"{preview}{suffix}"
        )
    return tuple(symbol for symbol in frozen.symbols if symbol in current)


def require_profile_binding(
    frozen: FrozenCandidateUniverse,
    *,
    profile: str,
    current_inputs: Mapping[str, Any],
) -> None:
    if profile not in _PROFILE_NAMES:
        raise ValueError(f"unknown candidate-universe profile {profile!r}")
    if canonical_json(dict(current_inputs)) != canonical_json(
        dict(frozen.profile_inputs[profile])
    ):
        raise RuntimeError(
            f"{profile}: effective universe config differs from frozen candidate artifact"
        )


def enforce_frozen_candidate_frames(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    frozen: FrozenCandidateUniverse,
    *,
    snapshot_ts_ms: int,
    context: str,
) -> tuple[str, ...]:
    tables = build_profile_universe_tables_from_frames(
        instruments,
        tickers,
        snapshot_ts_ms=snapshot_ts_ms,
        profile_inputs=frozen.profile_inputs,
    )
    current_union: set[str] = set()
    for table in tables.values():
        if not table.is_empty():
            current_union.update(str(value) for value in table["symbol"].to_list())
    return enforce_frozen_candidate_population(
        sorted(current_union),
        frozen,
        context=context,
    )
