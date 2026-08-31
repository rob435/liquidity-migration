"""Build the immutable candidate population consumed by the Rust signal worker.

The artifact holds two different kinds of thing, and the difference matters:

* ``strategy_instruments`` — every crypto-linear perpetual the venue listed at
  snapshot time, minus the shared exclusions. A venue fact. It is what the
  account may trade, and no sleeve owns it.
* one profile per live sleeve (``long``, ``carry``) — that sleeve's own
  narrower pre-signal population, which it binds to and is checked against.

The artifact is a forward population contract, not a claim of historical PIT
membership. Rust validates its identity, realm, hash, and sorted populations
before public-signal collection starts.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

import polars as pl

from liquidity_migration.core.config import DEFAULT_EXCLUDED_SYMBOLS, UniverseConfig
from liquidity_migration.core.deterministic_serialization import canonical_json, json_safe
from liquidity_migration.core.venue_realm import REALM_REST_ENDPOINTS, VenueRealm, venue_realm
from liquidity_migration.data.downloaders import _normalize_instruments, _normalize_tickers
from liquidity_migration.data.universe import CRYPTO_LINEAR_SYMBOL_TYPES, build_current_universe_table


CANDIDATE_UNIVERSE_SCHEMA_VERSION = 5
CANDIDATE_UNIVERSE_KIND = "account_execution_candidate_universe"
#: Live sleeves. A profile is a strategy's own pre-signal population, and a
#: sleeve may bind to one; nothing else belongs here.
_PROFILE_NAMES = ("long", "carry")
#: The venue's instrument set. Not a profile, so it is named separately
#: everywhere a caller could mistake it for one.
STRATEGY_INSTRUMENTS_POPULATION = "strategy_instruments"
_POPULATION_NAMES = (*_PROFILE_NAMES, STRATEGY_INSTRUMENTS_POPULATION)


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


def _realm_endpoint_host(realm: VenueRealm) -> str:
    return REALM_REST_ENDPOINTS[realm].removeprefix("https://").rstrip("/")


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
    long_universe_superset_size: int,
) -> dict[str, dict[str, Any]]:
    """Pre-signal universe knobs for each live sleeve's own profile."""

    return {
        "long": long_profile_universe_inputs(long_universe_superset_size),
        "carry": carry_profile_universe_inputs(),
    }


def population_universe_inputs(
    *,
    long_universe_superset_size: int,
) -> dict[str, dict[str, Any]]:
    """Knobs for every population the artifact freezes.

    The two sleeve profiles plus the venue instrument set, keyed the way the
    table builder and the artifact want them.
    """

    return {
        **profile_universe_inputs(
            long_universe_superset_size=long_universe_superset_size
        ),
        STRATEGY_INSTRUMENTS_POPULATION: strategy_instruments_universe_inputs(),
    }


def long_profile_universe_inputs(long_universe_superset_size: int) -> dict[str, Any]:
    long_rank_end = int(long_universe_superset_size)
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


def strategy_instruments_universe_inputs() -> dict[str, Any]:
    """Every crypto-linear perpetual the venue listed, minus the exclusions.

    This is a fact about the venue at snapshot time, not a strategy's choice.
    No sleeve owns it and any sleeve may read it: every gate is off — no
    turnover floor, no listing-age floor or ceiling, no rank or symbol cap —
    so what comes out is the whole tradable instrument set that passed the
    shared stablecoin exclusions.

    Changing a value here changes what the live account may trade. The Rust
    signal worker validates this block before it collects public signals.
    """

    return {
        "min_turnover_24h": 0.0,
        "min_age_days": 0,
        "max_age_days": 0,
        "rank_start": 1,
        "rank_end": 0,
        "max_symbols": 0,
        "exclude_symbols": sorted({_symbol(value) for value in DEFAULT_EXCLUDED_SYMBOLS}),
    }


def carry_profile_universe_inputs() -> dict[str, Any]:
    """Pre-signal population for the CARRY sleeve (lane2_carry_hold_v7).

    Runtime universe is the top 100 by trailing 24h turnover; enforcement uses a
    top-150 superset so rank churn cannot starve the book between freezes. The
    7-day maturity floor is the 168h of settled funding the engine needs before
    a name is decidable.
    """

    return {
        "min_turnover_24h": 0.0,
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


def _require_known_populations(population_inputs: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject a population name nothing in this module defines.

    Callers may ask for a subset — a per-cycle sleeve wants only its own
    table — but a name outside the known set is a typo that would otherwise
    silently return no table at all.
    """

    unknown = sorted(set(population_inputs) - set(_POPULATION_NAMES))
    if unknown or not population_inputs:
        raise ValueError(
            "candidate population inputs must name only "
            f"{', '.join(_POPULATION_NAMES)}"
        )


def build_profile_universe_tables(
    instrument_rows: Sequence[Mapping[str, Any]],
    ticker_rows: Sequence[Mapping[str, Any]],
    *,
    population_inputs: Mapping[str, Mapping[str, Any]],
    snapshot_ts_ms: int,
) -> dict[str, pl.DataFrame]:
    """Rebuild each requested pre-signal population from one venue snapshot."""

    if snapshot_ts_ms <= 0:
        raise ValueError("snapshot_ts_ms must be positive")
    _require_known_populations(population_inputs)
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
        population_inputs=population_inputs,
    )


def build_profile_universe_tables_from_frames(
    instruments: pl.DataFrame,
    tickers: pl.DataFrame,
    *,
    population_inputs: Mapping[str, Mapping[str, Any]],
    snapshot_ts_ms: int,
) -> dict[str, pl.DataFrame]:
    if snapshot_ts_ms <= 0:
        raise ValueError("snapshot_ts_ms must be positive")
    _require_known_populations(population_inputs)
    return {
        population: build_current_universe_table(
            instruments,
            tickers,
            universe_config=_universe_config(population_inputs[population]),
            snapshot_ts_ms=snapshot_ts_ms,
        )
        for population in sorted(population_inputs)
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
    population_inputs: Mapping[str, Mapping[str, Any]],
    snapshot_ts_ms: int,
) -> list[dict[str, Any]]:
    tradable = eligible[STRATEGY_INSTRUMENTS_POPULATION]
    decisions: list[dict[str, Any]] = []
    for symbol in sorted(set(instruments) | set(tickers)):
        population_rows: dict[str, Any] = {}
        for population in _POPULATION_NAMES:
            included = symbol in eligible[population]
            reasons = _base_reasons(
                symbol=symbol,
                instrument=instruments.get(symbol),
                ticker=tickers.get(symbol),
                config=_universe_config(population_inputs[population]),
                snapshot_ts_ms=snapshot_ts_ms,
            )
            if not included and not reasons:
                reasons = ["outside_configured_liquidity_rank"]
            population_rows[population] = {
                "included": included,
                "reasons": [] if included else sorted(set(reasons)),
            }
        decisions.append({
            "symbol": symbol,
            "included_in_strategy_instruments": symbol in tradable,
            "populations": population_rows,
        })
    return decisions


def build_candidate_universe_artifact(
    instrument_rows: Sequence[Mapping[str, Any]],
    ticker_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_ts_ns: int,
    long_universe_superset_size: int,
    realm: VenueRealm | str = VenueRealm.DEMO,
) -> dict[str, Any]:
    """Build the self-hashed population artifact from one venue snapshot.

    ``realm`` is recorded in the artifact and pins the endpoint it must have
    been read from.
    """

    return _build_candidate_universe_artifact(
        instrument_rows,
        ticker_rows,
        snapshot_ts_ns=snapshot_ts_ns,
        population_inputs=population_universe_inputs(
            long_universe_superset_size=long_universe_superset_size
        ),
        realm=realm,
    )


def _build_candidate_universe_artifact(
    instrument_rows: Sequence[Mapping[str, Any]],
    ticker_rows: Sequence[Mapping[str, Any]],
    *,
    snapshot_ts_ns: int,
    population_inputs: Mapping[str, Mapping[str, Any]],
    realm: VenueRealm | str = VenueRealm.DEMO,
) -> dict[str, Any]:
    """Build the artifact from the complete population contract."""

    selected = venue_realm(realm)
    if snapshot_ts_ns <= 0:
        raise ValueError("snapshot_ts_ns must be positive")
    if set(population_inputs) != set(_POPULATION_NAMES):
        raise ValueError(
            "candidate-universe artifact must define every population: "
            f"{', '.join(_POPULATION_NAMES)}"
        )
    inputs = {
        population: dict(population_inputs[population])
        for population in _POPULATION_NAMES
    }
    snapshot_ts_ms = snapshot_ts_ns // 1_000_000
    instruments, crypto_linear_rows, excluded_instruments = (
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
    tables = build_profile_universe_tables(
        instrument_rows,
        ticker_rows,
        snapshot_ts_ms=snapshot_ts_ms,
        population_inputs=inputs,
    )
    eligible = {
        population: set(table["symbol"].to_list()) if not table.is_empty() else set()
        for population, table in tables.items()
    }
    # Not a union. Every sleeve profile is the venue instrument set with extra
    # gates switched on, so each one is already inside it; unioning them back
    # together only ever returned the instrument set. Saying so directly means
    # no reader has to rediscover that.
    symbols = sorted(eligible[STRATEGY_INSTRUMENTS_POPULATION])
    decisions = _decision_rows(
        instruments,
        tickers,
        eligible=eligible,
        population_inputs=inputs,
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
        "profile_inputs": json_safe(
            {profile: inputs[profile] for profile in _PROFILE_NAMES}
        ),
        "profile_input_sha256": {
            profile: hashlib.sha256(canonical_json(inputs[profile])).hexdigest()
            for profile in _PROFILE_NAMES
        },
        "strategy_instruments_inputs": json_safe(
            inputs[STRATEGY_INSTRUMENTS_POPULATION]
        ),
        "strategy_instruments_input_sha256": hashlib.sha256(
            canonical_json(inputs[STRATEGY_INSTRUMENTS_POPULATION])
        ).hexdigest(),
        "raw_source": {
            "instrument_rows": len(instrument_rows),
            "strategy_instrument_rows": len(crypto_linear_rows),
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
        "strategy_instruments": symbols,
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
