"""Freeze and enforce the operational strategy candidate population.

The artifact is a forward population contract, not a claim of historical PIT
membership: it stops a post-freeze listing from entering the evidence window,
while LONG and CONT keep their own ranking and signal logic. Venue-announced
retirements are recorded prospectively and leave the entry population only
while the bound account is provably flat.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file
from liquidity_migration.core.venue_realm import REALM_REST_ENDPOINTS, VenueRealm, venue_realm
from liquidity_migration.core.config import DEFAULT_EXCLUDED_SYMBOLS, UniverseConfig
from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe
from liquidity_migration.data.downloaders import _normalize_instruments, _normalize_tickers
from liquidity_migration.data.universe import CRYPTO_LINEAR_SYMBOL_TYPES, build_current_universe_table


_LOGGER = logging.getLogger(__name__)

# Schema 4 requires all three profiles. Older two-profile artifacts are a
# different contract and must be re-frozen, not reinterpreted.
CANDIDATE_UNIVERSE_SCHEMA_VERSION = 4
CANDIDATE_UNIVERSE_KIND = "account_execution_candidate_universe"
_PROFILE_NAMES = ("long", "continuous", "carry")
_RETIREMENT_REGISTRY_SCHEMA_VERSION = 1
_RETIREMENT_REGISTRY_KIND = "candidate_retirement_registry"


@dataclass(frozen=True, slots=True)
class FrozenCandidateUniverse:
    path: Path
    symbols: tuple[str, ...]
    artifact_sha256: str
    file_sha256: str
    snapshot_ts_ns: int
    profile_inputs: Mapping[str, Mapping[str, Any]]
    profile_symbols: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class ScheduledCandidateRetirement:
    symbol: str
    delivery_time_ms: int
    first_observed_ts_ms: int
    observed_status: str
    evidence_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "delivery_time_ms": self.delivery_time_ms,
            "first_observed_ts_ms": self.first_observed_ts_ms,
            "observed_status": self.observed_status,
            "evidence_source": self.evidence_source,
        }


@dataclass(frozen=True, slots=True)
class TemporarilyIneligibleCandidate:
    symbol: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CandidatePopulationReconciliation:
    profile: str
    active_symbols: tuple[str, ...]
    temporarily_ineligible: tuple[TemporarilyIneligibleCandidate, ...]
    scheduled_retirements: tuple[ScheduledCandidateRetirement, ...]

    def temporarily_ineligible_rows(self) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self.temporarily_ineligible]

    def retirement_rows(self) -> list[dict[str, Any]]:
        return [row.to_dict() for row in self.scheduled_retirements]


def _self_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({**dict(payload), "artifact_sha256": ""})
    ).hexdigest()


def _symbol(value: object) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or not symbol.isascii() or not symbol.replace("-", "").isalnum():
        raise ValueError(f"invalid candidate-universe symbol {value!r}")
    return symbol


def _symbol_type(value: object) -> str:
    if value is None:
        return ""
    if type(value) is not str:
        raise ValueError("candidate-universe symbolType must be a string or null")
    normalized = value.strip().lower()
    if normalized and (
        not normalized.isascii() or not normalized.replace("_", "").isalnum()
    ):
        raise ValueError(f"invalid candidate-universe symbolType {value!r}")
    return normalized


def _partition_strategy_instrument_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    list[dict[str, Any]],
]:
    """Retain all source rows while separating the crypto strategy domain."""

    all_rows: dict[str, Mapping[str, Any]] = {}
    strategy_rows: dict[str, Mapping[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    supported = set(CRYPTO_LINEAR_SYMBOL_TYPES)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{label}[{index}] must be an object")
        symbol = _symbol(row.get("symbol"))
        if symbol in all_rows:
            raise ValueError(f"{label} contains duplicate symbol {symbol!r}")
        all_rows[symbol] = row
        symbol_type = _symbol_type(row.get("symbolType"))
        if symbol_type in supported:
            strategy_rows[symbol] = row
        else:
            excluded.append({
                "row_index": index,
                "symbol": symbol,
                "symbol_type": symbol_type,
                "reason": "outside_crypto_perp_strategy_domain",
            })
    return all_rows, strategy_rows, excluded


def _partition_ticker_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    instrument_symbols: set[str],
    label: str,
) -> tuple[dict[str, Mapping[str, Any]], list[dict[str, Any]]]:
    """Separate canonical candidate tickers from out-of-domain source rows.

    Bybit's linear ticker endpoint can carry synthetic rows absent from the
    instrument snapshot whose label is not a valid strategy symbol; they are
    kept as raw source evidence but excluded from the join. Missing symbols,
    duplicate labels, and rows mapping to a validated instrument fail closed.
    """

    output: dict[str, Mapping[str, Any]] = {}
    rejected: list[dict[str, Any]] = []
    rejected_labels: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"{label}[{index}] must be an object")
        raw_symbol = row.get("symbol")
        try:
            symbol = _symbol(raw_symbol)
        except ValueError:
            if not isinstance(raw_symbol, str) or not raw_symbol.strip():
                raise
            source_label = raw_symbol
            comparison_label = source_label.strip().upper()
            if comparison_label in instrument_symbols:
                raise
            if comparison_label in rejected_labels:
                raise ValueError(
                    f"{label} contains duplicate symbol {comparison_label!r}"
                )
            rejected_labels.add(comparison_label)
            rejected.append({
                "row_index": index,
                "raw_symbol": source_label,
                "reason": "noncanonical_ticker_only_symbol",
            })
            continue
        if symbol in output:
            raise ValueError(f"{label} contains duplicate symbol {symbol!r}")
        output[symbol] = row
    return output, rejected


def profile_universe_inputs(
    *,
    long_config: object,
    continuous_config: object,
) -> dict[str, dict[str, Any]]:
    """Extract the complete pre-signal universe knobs from effective configs."""

    return {
        "long": long_profile_universe_inputs(long_config),
        "continuous": continuous_profile_universe_inputs(continuous_config),
        "carry": carry_profile_universe_inputs(continuous_config),
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


def carry_profile_universe_inputs(continuous_config: object) -> dict[str, Any]:
    """Pre-signal population for the CARRY sleeve (lane2_carry_hold_v4).

    Runtime universe is the top 100 by trailing 24h turnover; enforcement uses a
    top-150 superset so rank churn cannot starve the book between freezes. The
    7-day maturity floor is the 168h of settled funding the engine needs before
    a name is decidable.
    """

    return {
        "min_turnover_24h": float(
            getattr(continuous_config, "universe_min_turnover_24h")
        ),
        "min_age_days": 7,
        "max_age_days": 0,
        "rank_start": 1,
        "rank_end": 150,
        "max_symbols": 150,
        "exclude_symbols": sorted({_symbol(value) for value in DEFAULT_EXCLUDED_SYMBOLS}),
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
        raise ValueError("candidate profile inputs must contain long, continuous, and carry")
    instrument_map, strategy_instrument_map, _ = _partition_strategy_instrument_rows(
        instrument_rows,
        label="instrument_rows",
    )
    ticker_map, _ = _partition_ticker_rows(
        ticker_rows,
        instrument_symbols=set(instrument_map),
        label="ticker_rows",
    )
    instruments = _normalize_instruments([
        dict(row) for row in strategy_instrument_map.values()
    ])
    tickers = _normalize_tickers([dict(row) for row in ticker_map.values()])
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
        raise ValueError("candidate profile inputs must contain long, continuous, and carry")
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
    if _symbol_type(instrument.get("symbolType")) not in set(
        CRYPTO_LINEAR_SYMBOL_TYPES
    ):
        reasons.append("outside_crypto_perp_strategy_domain")
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
    union = eligible["long"] | eligible["continuous"] | eligible["carry"]
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
    realm: VenueRealm | str = VenueRealm.DEMO,
) -> dict[str, Any]:
    """Build the self-hashed population artifact from one venue snapshot.

    ``realm`` is recorded in the artifact and pins the endpoint it must have
    been read from.
    """

    selected = venue_realm(realm)
    if snapshot_ts_ns <= 0:
        raise ValueError("snapshot_ts_ns must be positive")
    snapshot_ts_ms = snapshot_ts_ns // 1_000_000
    instruments, strategy_instruments, excluded_instruments = (
        _partition_strategy_instrument_rows(
            instrument_rows,
            label="instrument_rows",
        )
    )
    tickers, rejected_tickers = _partition_ticker_rows(
        ticker_rows,
        instrument_symbols=set(instruments),
        label="ticker_rows",
    )
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
    symbols = sorted(eligible["long"] | eligible["continuous"] | eligible["carry"])
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
        "environment": selected.value,
        "endpoint": _realm_endpoint_host(selected),
        "snapshot_ts_ns": snapshot_ts_ns,
        "strategy_domain": "crypto_perpetuals",
        "strategy_symbol_types": list(CRYPTO_LINEAR_SYMBOL_TYPES),
        "profile_inputs": json_safe(inputs),
        "profile_input_sha256": {
            profile: hashlib.sha256(canonical_json(inputs[profile])).hexdigest()
            for profile in _PROFILE_NAMES
        },
        "raw_source": {
            "instrument_rows": len(instrument_rows),
            "strategy_instrument_rows": len(strategy_instruments),
            "excluded_instrument_rows": len(excluded_instruments),
            "ticker_rows": len(ticker_rows),
            "evaluated_ticker_rows": len(tickers),
            "rejected_ticker_rows": len(rejected_tickers),
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
        "rejected_ticker_rows": rejected_tickers,
        "excluded_instrument_rows": excluded_instruments,
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
            "non_crypto_linear_products_are_excluded_before_liquidity_ranking",
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


#: Parsed artifacts by (content hash, realm). The artifact is frozen, so its own
#: hash is an exact key; the loader's ownership and permission checks still run
#: against the live file on every call.
_CANDIDATE_UNIVERSE_MEMO: dict[tuple[str, str, str], "FrozenCandidateUniverse"] = {}


def _realm_endpoint_host(realm: VenueRealm) -> str:
    """Host portion of the REST endpoint the artifact must have been read from."""

    return REALM_REST_ENDPOINTS[realm].removeprefix("https://").rstrip("/")


def load_candidate_universe(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
    realm: VenueRealm | str = VenueRealm.DEMO,
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
    # The artifact is frozen and every producer re-derived the whole thing each
    # cycle. Its own content hash keys the result exactly: the ownership and
    # permission checks above still run on every call against the live stat, so
    # the memo accelerates the parse, not the verification.
    # The path is part of what this returns, so two artifacts with identical
    # content at different paths are different answers.
    memo_key = (str(snapshot.path), snapshot.sha256, venue_realm(realm).value)
    memoized = _CANDIDATE_UNIVERSE_MEMO.get(memo_key)
    if memoized is not None:
        return memoized
    data = snapshot.data
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate-universe artifact is not valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("candidate-universe artifact must be a JSON object")
    if payload.get("schema_version") != CANDIDATE_UNIVERSE_SCHEMA_VERSION:
        raise ValueError("candidate-universe schema_version is unsupported")
    if payload.get("kind") != CANDIDATE_UNIVERSE_KIND:
        raise ValueError("candidate-universe identity is invalid")
    try:
        artifact_realm = venue_realm(payload.get("environment"))
    except ValueError as exc:
        raise ValueError("candidate-universe identity is invalid") from exc
    if artifact_realm is not venue_realm(realm):
        raise ValueError(
            f"candidate-universe artifact is for realm {artifact_realm.value!r}, "
            f"not {venue_realm(realm).value!r}"
        )
    expected_endpoint = _realm_endpoint_host(artifact_realm)
    if payload.get("endpoint") != expected_endpoint:
        raise ValueError(f"candidate-universe endpoint is not {expected_endpoint}")
    if payload.get("strategy_domain") != "crypto_perpetuals":
        raise ValueError("candidate-universe strategy domain is invalid")
    if payload.get("strategy_symbol_types") != list(CRYPTO_LINEAR_SYMBOL_TYPES):
        raise ValueError("candidate-universe strategy symbol types are invalid")
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
    instrument_map, strategy_instrument_map, rebuilt_excluded_instruments = (
        _partition_strategy_instrument_rows(
            raw_instruments,
            label="raw instrument_rows",
        )
    )
    ticker_map, rebuilt_rejected_tickers = _partition_ticker_rows(
        raw_tickers,
        instrument_symbols=set(instrument_map),
        label="raw ticker_rows",
    )
    declared_rejected_tickers = payload.get("rejected_ticker_rows")
    if (
        not isinstance(declared_rejected_tickers, list)
        or canonical_json({"rows": declared_rejected_tickers})
        != canonical_json({"rows": rebuilt_rejected_tickers})
        or raw_source.get("evaluated_ticker_rows") != len(ticker_map)
        or raw_source.get("rejected_ticker_rows") != len(rebuilt_rejected_tickers)
    ):
        raise ValueError("candidate-universe rejected ticker rows are inconsistent")
    declared_excluded_instruments = payload.get("excluded_instrument_rows")
    if (
        not isinstance(declared_excluded_instruments, list)
        or canonical_json({"rows": declared_excluded_instruments})
        != canonical_json({"rows": rebuilt_excluded_instruments})
        or raw_source.get("strategy_instrument_rows")
        != len(strategy_instrument_map)
        or raw_source.get("excluded_instrument_rows")
        != len(rebuilt_excluded_instruments)
    ):
        raise ValueError("candidate-universe excluded instrument rows are inconsistent")
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
    profile_symbols: dict[str, tuple[str, ...]] = {}
    for profile in _PROFILE_NAMES:
        values = declared_eligible.get(profile)
        if (
            not isinstance(values, list)
            or any(type(value) is not str for value in values)
            or values != sorted(rebuilt_eligible[profile])
        ):
            raise ValueError(f"candidate-universe {profile} population is inconsistent")
        profile_symbols[profile] = tuple(_symbol(value) for value in values)
    decisions = payload.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("candidate-universe decisions must be a list")
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
    universe = FrozenCandidateUniverse(
        path=snapshot.path,
        symbols=symbols,
        artifact_sha256=artifact_hash,
        file_sha256=snapshot.sha256,
        snapshot_ts_ns=snapshot_ts_ns,
        profile_inputs={
            profile: dict(profile_inputs[profile]) for profile in _PROFILE_NAMES
        },
        profile_symbols=profile_symbols,
    )
    # Bounded: one live artifact per realm, plus a little slack across a swap.
    if len(_CANDIDATE_UNIVERSE_MEMO) >= 8:
        _CANDIDATE_UNIVERSE_MEMO.clear()
    _CANDIDATE_UNIVERSE_MEMO[memo_key] = universe
    return universe


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


def _load_retirement_registry(
    path: Path,
    *,
    frozen: FrozenCandidateUniverse,
) -> dict[str, ScheduledCandidateRetirement]:
    if path.is_symlink():
        raise ValueError("candidate-retirement registry must not be a symbolic link")
    if not path.exists():
        return {}
    snapshot = read_stable_file(
        path,
        label="candidate-retirement registry",
        reject_empty=True,
        require_mode=0o600,
        require_single_link=True,
    )
    try:
        payload = json.loads(snapshot.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate-retirement registry is invalid JSON") from exc
    expected = {
        "schema_version",
        "kind",
        "candidate_universe_artifact_sha256",
        "records",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("candidate-retirement registry fields are invalid")
    if (
        payload["schema_version"] != _RETIREMENT_REGISTRY_SCHEMA_VERSION
        or payload["kind"] != _RETIREMENT_REGISTRY_KIND
        or payload["candidate_universe_artifact_sha256"] != frozen.artifact_sha256
    ):
        raise ValueError("candidate-retirement registry identity is invalid")
    raw_records = payload["records"]
    if not isinstance(raw_records, list):
        raise ValueError("candidate-retirement registry records must be a list")
    output: dict[str, ScheduledCandidateRetirement] = {}
    record_fields = {
        "symbol",
        "delivery_time_ms",
        "first_observed_ts_ms",
        "observed_status",
        "evidence_source",
    }
    for raw in raw_records:
        if not isinstance(raw, Mapping) or set(raw) != record_fields:
            raise ValueError("candidate-retirement registry record fields are invalid")
        symbol = _symbol(raw["symbol"])
        delivery_time_ms = int(raw["delivery_time_ms"])
        first_observed_ts_ms = int(raw["first_observed_ts_ms"])
        source = str(raw["evidence_source"])
        if (
            symbol in output
            or delivery_time_ms <= 0
            or first_observed_ts_ms <= 0
            or delivery_time_ms <= frozen.snapshot_ts_ns // 1_000_000
            or delivery_time_ms <= first_observed_ts_ms
            or source != "live_instrument_delivery_time"
        ):
            raise ValueError("candidate-retirement registry record is invalid")
        output[symbol] = ScheduledCandidateRetirement(
            symbol=symbol,
            delivery_time_ms=delivery_time_ms,
            first_observed_ts_ms=first_observed_ts_ms,
            observed_status=str(raw["observed_status"]),
            evidence_source=source,
        )
    if list(output) != sorted(output):
        raise ValueError("candidate-retirement registry records must be sorted")
    return output


def _write_retirement_registry(
    path: Path,
    *,
    frozen: FrozenCandidateUniverse,
    records: Mapping[str, ScheduledCandidateRetirement],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("candidate-retirement registry must not be a symbolic link")
    payload = {
        "schema_version": _RETIREMENT_REGISTRY_SCHEMA_VERSION,
        "kind": _RETIREMENT_REGISTRY_KIND,
        "candidate_universe_artifact_sha256": frozen.artifact_sha256,
        "records": [records[symbol].to_dict() for symbol in sorted(records)],
    }
    data = canonical_json(payload) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            str(temporary),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(data):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                raise OSError("candidate-retirement registry write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _instrument_rows_by_symbol(
    instruments: pl.DataFrame,
    *,
    candidate_symbols: set[str],
) -> dict[str, Mapping[str, Any]]:
    if "symbol" not in instruments.columns:
        raise RuntimeError("current instrument frame lacks symbol")
    output: dict[str, Mapping[str, Any]] = {}
    for row in instruments.to_dicts():
        raw_symbol = str(row.get("symbol") or "").strip().upper()
        if raw_symbol not in candidate_symbols:
            continue
        symbol = _symbol(row.get("symbol"))
        if symbol in output:
            raise RuntimeError(f"current instrument frame repeats {symbol}")
        output[symbol] = row
    return output


def _ticker_rows_by_symbol(
    tickers: pl.DataFrame,
    *,
    candidate_symbols: set[str],
) -> dict[str, Mapping[str, Any]]:
    if "symbol" not in tickers.columns:
        raise RuntimeError("current ticker frame lacks symbol")
    output: dict[str, Mapping[str, Any]] = {}
    for row in tickers.to_dicts():
        raw_symbol = str(row.get("symbol") or "").strip().upper()
        if raw_symbol not in candidate_symbols:
            continue
        symbol = _symbol(row.get("symbol"))
        if symbol in output:
            raise RuntimeError(f"current ticker frame repeats {symbol}")
        output[symbol] = row
    return output


def _temporary_ineligibility_reasons(
    *,
    symbol: str,
    instrument: Mapping[str, Any] | None,
    ticker: Mapping[str, Any] | None,
    config: UniverseConfig,
    snapshot_ts_ms: int,
) -> tuple[str, ...] | None:
    """Explain reversible live-filter drift without masking input corruption.

    Missing venue rows, malformed market data, and structural contract changes
    remain hard failures.
    """

    if instrument is None or ticker is None:
        return None
    symbol_type = str(instrument.get("symbol_type") or "").strip().lower()
    contract_type = str(instrument.get("contract_type") or "")
    raw_turnover_24h = ticker.get("turnover_24h")
    if raw_turnover_24h is None:
        return None
    try:
        delivery_time_ms = int(instrument.get("delivery_time_ms") or 0)
        turnover_24h = float(raw_turnover_24h)
    except (TypeError, ValueError):
        return None
    if (
        str(instrument.get("status") or "") != "Trading"
        or str(instrument.get("settle_coin") or "") != "USDT"
        or symbol_type not in set(CRYPTO_LINEAR_SYMBOL_TYPES)
        or bool(instrument.get("is_prelisting"))
        or contract_type not in {"LinearPerpetual", "linear", "Linear"}
        or delivery_time_ms != 0
        or symbol in set(config.exclude_symbols)
        or not math.isfinite(turnover_24h)
        or turnover_24h < 0.0
    ):
        return None

    reasons: list[str] = []
    if turnover_24h < config.min_turnover_24h:
        reasons.append("turnover_below_floor")
    if config.min_age_days > 0 or config.max_age_days > 0:
        try:
            launch_time_ms = int(instrument.get("launch_time_ms") or 0)
        except (TypeError, ValueError):
            return None
        if launch_time_ms <= 0 or launch_time_ms > snapshot_ts_ms:
            return None
        age_days = (snapshot_ts_ms - launch_time_ms) / 86_400_000.0
        if config.min_age_days > 0 and age_days < config.min_age_days:
            reasons.append("listing_age_below_floor")
        if config.max_age_days > 0 and age_days > config.max_age_days:
            reasons.append("listing_age_above_ceiling")
    if not reasons:
        reasons.append("outside_configured_liquidity_rank")
    return tuple(reasons)


def enforce_frozen_candidate_frames(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    frozen: FrozenCandidateUniverse,
    *,
    profile: str,
    snapshot_ts_ms: int,
    context: str,
    retirement_registry_path: str | Path,
) -> CandidatePopulationReconciliation:
    if profile not in _PROFILE_NAMES:
        raise ValueError(f"unknown candidate-universe profile {profile!r}")
    tables = build_profile_universe_tables_from_frames(
        instruments,
        tickers,
        snapshot_ts_ms=snapshot_ts_ms,
        profile_inputs=frozen.profile_inputs,
    )
    current = (
        {_symbol(value) for value in tables[profile]["symbol"].to_list()}
        if not tables[profile].is_empty()
        else set()
    )
    required = set(frozen.profile_symbols[profile])
    missing = sorted(required - current)
    registry_path = Path(retirement_registry_path).expanduser()
    registry = _load_retirement_registry(registry_path, frozen=frozen)
    instrument_rows = _instrument_rows_by_symbol(
        instruments,
        candidate_symbols=required,
    )
    ticker_rows = _ticker_rows_by_symbol(
        tickers,
        candidate_symbols=required,
    )
    retirements: dict[str, ScheduledCandidateRetirement] = {}
    temporarily_ineligible: dict[str, TemporarilyIneligibleCandidate] = {}
    unexplained: list[str] = []
    changed = False
    reactivated = sorted(set(registry) & current)
    if reactivated:
        # A venue can cancel or move a delisting. The symbol stays non-tradable
        # on its recorded delivery evidence and the cycle continues.
        _LOGGER.warning(
            "%s: scheduled retirement re-entered current eligibility; kept "
            "non-tradable: %s",
            context,
            ",".join(reactivated),
        )
        for symbol in reactivated:
            retirements[symbol] = registry[symbol]
            temporarily_ineligible[symbol] = TemporarilyIneligibleCandidate(
                symbol=symbol,
                reasons=("scheduled_retirement_reentered_eligibility",),
            )
    profile_config = _universe_config(frozen.profile_inputs[profile])
    for symbol in missing:
        row = instrument_rows.get(symbol)
        try:
            delivery_time_ms = int((row or {}).get("delivery_time_ms") or 0)
        except (TypeError, ValueError):
            delivery_time_ms = -1
        prior = registry.get(symbol)
        if prior is not None and delivery_time_ms > 0:
            if prior.delivery_time_ms != delivery_time_ms:
                # Venue moved the delivery date: take the new one but keep the
                # original first-observed timestamp as the causal anchor.
                _LOGGER.warning(
                    "%s: %s delivery time changed from %d to %d; registry updated",
                    context,
                    symbol,
                    prior.delivery_time_ms,
                    delivery_time_ms,
                )
                updated = ScheduledCandidateRetirement(
                    symbol=symbol,
                    delivery_time_ms=delivery_time_ms,
                    first_observed_ts_ms=prior.first_observed_ts_ms,
                    observed_status=str((row or {}).get("status") or ""),
                    evidence_source="live_instrument_delivery_time_updated",
                )
                registry[symbol] = updated
                changed = True
                retirements[symbol] = updated
            else:
                retirements[symbol] = prior
        elif (
            delivery_time_ms > frozen.snapshot_ts_ns // 1_000_000
            and delivery_time_ms > snapshot_ts_ms
        ):
            observed = ScheduledCandidateRetirement(
                symbol=symbol,
                delivery_time_ms=delivery_time_ms,
                first_observed_ts_ms=snapshot_ts_ms,
                observed_status=str((row or {}).get("status") or ""),
                evidence_source="live_instrument_delivery_time",
            )
            registry[symbol] = observed
            retirements[symbol] = observed
            changed = True
        elif row is None and prior is not None:
            # Instrument row gone: the earlier delivery-time observation is
            # still the causal evidence.
            retirements[symbol] = prior
        else:
            temporary_reasons = _temporary_ineligibility_reasons(
                symbol=symbol,
                instrument=row,
                ticker=ticker_rows.get(symbol),
                config=profile_config,
                snapshot_ts_ms=snapshot_ts_ms,
            )
            if temporary_reasons is None:
                unexplained.append(symbol)
            else:
                temporarily_ineligible[symbol] = TemporarilyIneligibleCandidate(
                    symbol=symbol,
                    reasons=temporary_reasons,
                )
    if unexplained:
        # A symbol vanishing without delivery evidence (abrupt delist, rename,
        # venue hiccup) drops to temporarily-ineligible and returns on its own
        # if the venue restores it.
        preview = ",".join(unexplained[:20])
        suffix = "..." if len(unexplained) > 20 else ""
        _LOGGER.warning(
            "%s: frozen %s candidate population lost %d unexplained symbol(s): %s%s",
            context,
            profile,
            len(unexplained),
            preview,
            suffix,
        )
        for symbol in unexplained:
            temporarily_ineligible[symbol] = TemporarilyIneligibleCandidate(
                symbol=symbol,
                reasons=("unexplained_absence_from_venue",),
            )
    if changed:
        _write_retirement_registry(registry_path, frozen=frozen, records=registry)
    active = tuple(
        symbol
        for symbol in frozen.profile_symbols[profile]
        if symbol in current and symbol not in temporarily_ineligible
    )
    return CandidatePopulationReconciliation(
        profile=profile,
        active_symbols=active,
        temporarily_ineligible=tuple(
            temporarily_ineligible[symbol]
            for symbol in sorted(temporarily_ineligible)
        ),
        scheduled_retirements=tuple(retirements[symbol] for symbol in sorted(retirements)),
    )


def require_scheduled_retirements_flat(
    reconciliation: CandidatePopulationReconciliation,
    *,
    route: object,
    context: str,
    tolerance: float = 1e-12,
    journal_cursor: object | None = None,
) -> None:
    """Fail closed if a retired entry symbol has any account or queue exposure.

    A per-cycle caller passes its resumable ``journal_cursor`` so the account
    state comes from an incremental verified read; without one the check pays
    a full cold journal read, which is fine for CLI and offline callers.
    """

    if not reconciliation.scheduled_retirements:
        return
    from liquidity_migration.account.account_kernel import (  # noqa: PLC0415
        AccountJournalCursor,
        read_account_journal,
        reduce_account_events,
    )
    from liquidity_migration.account.account_service import AccountIntentInbox  # noqa: PLC0415

    account_path = getattr(route, "account_path", None)
    if not isinstance(account_path, Path):
        raise TypeError("scheduled-retirement flatness requires a verified AccountRoute")
    retired = {row.symbol for row in reconciliation.scheduled_retirements}
    inbox = AccountIntentInbox(route)  # type: ignore[arg-type]
    unresolved = inbox.unresolved_requests()
    pending = sorted(
        {
            item.intent.symbol.upper()
            for request in unresolved
            for item in request.intents
            if item.intent.symbol.upper() in retired
            and abs(float(item.intent.signed_notional_usdt)) > tolerance
        }
    )
    if isinstance(journal_cursor, AccountJournalCursor):
        state = journal_cursor.read(account_path).state
    else:
        state = reduce_account_events(read_account_journal(account_path, verify=True))
    problems: dict[str, list[str]] = {symbol: [] for symbol in retired}
    for symbol in sorted(retired):
        position = state.positions.get(symbol)
        if position is not None and abs(position.signed_qty) > tolerance:
            problems[symbol].append("position")
        if abs(float(state.aggregate_targets.get(symbol, 0.0))) > tolerance:
            problems[symbol].append("aggregate_target")
        if abs(state.working_signed_qty(symbol)) > tolerance:
            problems[symbol].append("working_order")
        for label, targets in (
            ("component_target", state.component_targets),
            ("component_desire", state.component_target_desires),
        ):
            if any(
                str(target.get("symbol") or "").upper() == symbol
                and abs(float(target.get("signed_qty") or 0.0)) > tolerance
                for target in targets.values()
            ):
                problems[symbol].append(label)
        if symbol in pending:
            problems[symbol].append("unresolved_nonzero_request")
    active = {
        symbol: tuple(labels) for symbol, labels in problems.items() if labels
    }
    if active:
        detail = "; ".join(
            f"{symbol}={','.join(labels)}" for symbol, labels in sorted(active.items())
        )
        raise RuntimeError(
            f"{context}: scheduled-retirement symbols are not account-flat: {detail}"
        )
