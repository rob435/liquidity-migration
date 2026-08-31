"""Persistent client for the Rust directional decision contract."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tomllib
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import polars as pl

from liquidity_migration.core.deterministic_serialization import canonical_json
from liquidity_migration.rules.carry_hold import (
    DEFAULT_ENTER_BP,
    PERSISTENCE_WINDOW,
    CarryHoldConfig,
    FinancedLongsError,
)
from liquidity_migration.rules.long_config import StrategyConfig as LongStrategyConfig
from liquidity_migration.rules.long_models import (
    DecisionAction as LongDecisionAction,
    DecisionInput as LongDecisionInput,
    DecisionOutput as LongDecisionOutput,
    PriorState as LongPriorState,
)


_ROOT = Path(__file__).resolve().parents[2]
_ENV_BIN = "LIQUIDITY_MIGRATION_STRATEGY_CONTRACT_BIN"
_NATIVE_PLUG_BY_SLEEVE = {
    "long": "long_native",
    "carry": "carry_native",
    "exodus": "exodus_native",
}
_LONG_FEATURE_FLOAT_FIELDS = (
    "close",
    "turnover_quote",
    "log_return",
    "realized_vol",
    "sigma_daily_30d",
    "turnover_median_90d",
    "today_volume_rank",
    "universe_rank",
    "pump_3d_log",
    "pump_7d_log",
    "close_location",
    "close_loc_3d",
    "close_loc_7d",
    "atr_14d_pct",
    "btc_rv_30",
)
_LONG_DECISION_OUTPUT_FIELDS = {
    "schema_version",
    "action",
    "reason",
    "decision_ts_ms",
    "symbol",
    "signal_ts_ms",
    "entry_reason",
    "position_weight",
    "target_fraction_of_equity",
    "target_notional_usdt",
    "entry_leverage",
    "stop_loss_fraction",
    "stop_decay_after_ms",
    "decayed_stop_loss_fraction",
    "max_hold_duration_ms",
    "entry_valid_until_ms",
    "wake_at_or_below",
}
_CARRY_RESEARCH_FLOAT_FIELDS = (
    "by_close",
    "by_turnover_quote",
    "by_funding",
    "by_funding_age_h",
    "adv24",
    "trail_fund_24h",
    "momentum",
    "ret_3d",
    "vol_30d_daily",
    "dtrail_2d",
    "crowd_persistence",
    "turn_growth_3d",
    "d_tt_ls_3d",
)
_LONG_CLASSIFICATION_FIELDS = {
    "schema_version",
    "threshold_1d",
    "threshold_3d",
    "threshold_7d",
    "trigger_1d",
    "trigger_3d",
    "trigger_7d",
    "trigger_any",
    "source_strength",
    "pattern",
    "stop_loss_fraction",
    "max_hold_days",
}
_CONTRACT_BATCH_ROWS = 2_048


class StrategyContract(Protocol):
    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]: ...


def _command() -> list[str]:
    override = os.environ.get(_ENV_BIN)
    if override:
        return [str(Path(override).expanduser())]
    release = _ROOT / "engine" / "target" / "release" / "strategy_contract"
    if release.is_file():
        return [str(release)]
    return [
        "cargo",
        "run",
        "--quiet",
        "--manifest-path",
        str(_ROOT / "engine" / "Cargo.toml"),
        "-p",
        "engine-strategies",
        "--bin",
        "strategy_contract",
    ]


class RustStrategyContract:
    """One long-lived JSONL child shared across a research replay."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None

    def __enter__(self) -> "RustStrategyContract":
        self._process = subprocess.Popen(
            _command(),
            cwd=_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        return self

    def request(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("Rust strategy contract is not running")
        process.stdin.write(json.dumps(payload, allow_nan=False, separators=(",", ":")) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"Rust strategy contract stopped without a response: {stderr}")
        response = json.loads(line)
        if set(response) == {"ok", "error"} and response["ok"] is False:
            raise ValueError(f"Rust strategy contract refused the request: {response['error']}")
        if set(response) != {"ok", "output"} or response["ok"] is not True:
            raise RuntimeError("Rust strategy contract returned an invalid response")
        output = response["output"]
        if not isinstance(output, dict):
            raise RuntimeError("Rust strategy contract output must be an object")
        return output

    def __exit__(self, _exc_type: object, exc: object, _traceback: object) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("Rust strategy contract did not stop after stdin closed")
        if return_code != 0 and exc is None:
            stderr = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"Rust strategy contract exited {return_code}: {stderr}")

def load_rendered_native_config(*, realm: str, sleeve: str) -> dict[str, Any]:
    """Read one renderer-owned config from the checked-in engine template."""

    if realm not in {"demo", "mainnet"}:
        raise ValueError("native strategy realm must be demo or mainnet")
    try:
        plug_name = _NATIVE_PLUG_BY_SLEEVE[sleeve]
    except KeyError as error:
        raise ValueError("native strategy sleeve must be long, carry, or exodus") from error
    path = _ROOT / "deploy" / f"engine.{realm}.toml.template"
    template = tomllib.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in template.get("strategy", []) if isinstance(row, dict) and row.get("name") == plug_name]
    if len(rows) != 1 or rows[0].get("sleeve") != sleeve:
        raise ValueError(f"engine template has no unique {plug_name} strategy row")
    config_json = rows[0].get("config_json")
    if not isinstance(config_json, str):
        raise ValueError(f"engine template {plug_name} config_json is missing")
    config = json.loads(config_json)
    if not isinstance(config, dict):
        raise ValueError(f"engine template {plug_name} config_json must be an object")
    if config.get("environment") != realm:
        raise ValueError(f"engine template {plug_name} realm does not match")
    return config


def _optional_finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _long_feature_row(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    symbol_age = row.get("symbol_age_days")
    if symbol_age is None or isinstance(symbol_age, bool):
        symbol_age_days = None
    else:
        try:
            symbol_age_days = int(symbol_age)
        except (TypeError, ValueError, OverflowError):
            symbol_age_days = None
    output: dict[str, Any] = {
        "symbol": str(row.get("symbol") or ""),
        "ts_ms": int(row.get("ts_ms") or 0),
        "in_universe": bool(row.get("in_universe")),
        "regime_on": bool(row.get("regime_on")),
        "eth_regime_on": bool(row.get("eth_regime_on")),
        "symbol_age_days": symbol_age_days,
    }
    output.update(
        {field: _optional_finite_float(row.get(field)) for field in _LONG_FEATURE_FLOAT_FIELDS}
    )
    return output


def _native_long_config(config: LongStrategyConfig, *, realm: str) -> dict[str, Any]:
    native = deepcopy(load_rendered_native_config(realm=realm, sleeve="long"))
    rule_fields = tuple(native["rule"])
    rule = {field: getattr(config.rule, field) for field in rule_fields}
    native.update(
        {
            "profile_name": config.profile_name,
            "environment": realm,
            "entries_enabled": True,
            "rule": rule,
            "notional_multiplier": config.notional_multiplier,
            "entry_leverage": config.entry_leverage,
            "order_notional_pct_equity": config.order_notional_pct_equity,
            "wallet_balance_fraction": config.wallet_balance_fraction,
            "max_new_entries_per_cycle": config.max_new_entries_per_cycle,
            "signal_freshness_ms": config.signal_freshness_ms,
            "book_validity_ms": config.book_validity_ms,
            "entry_floor_usdt": config.entry_floor_usdt,
            "resize_floor_usdt": config.resize_floor_usdt,
            "resize_floor_fraction": config.resize_floor_fraction,
            "engine_entry_cutoff_ms": config.engine_entry_cutoff_ms,
        }
    )
    native["rule_sha256"] = hashlib.sha256(canonical_json(rule)).hexdigest()
    native["operational_profile_sha256"] = hashlib.sha256(
        canonical_json(
            {
                "realm": realm,
                "notional_multiplier": config.notional_multiplier,
                "entry_leverage": config.entry_leverage,
                "order_notional_pct_equity": config.order_notional_pct_equity,
                "wallet_balance_fraction": config.wallet_balance_fraction,
                "max_new_entries_per_cycle": config.max_new_entries_per_cycle,
                "signal_freshness_ms": config.signal_freshness_ms,
                "book_validity_ms": config.book_validity_ms,
                "entry_floor_usdt": config.entry_floor_usdt,
                "resize_floor_usdt": config.resize_floor_usdt,
                "resize_floor_fraction": config.resize_floor_fraction,
                "engine_entry_cutoff_ms": config.engine_entry_cutoff_ms,
            }
        )
    ).hexdigest()
    return native


def _long_decision_output(payload: Mapping[str, Any]) -> LongDecisionOutput:
    if set(payload) != _LONG_DECISION_OUTPUT_FIELDS or payload.get("schema_version") != 1:
        raise RuntimeError("Rust LONG reducer returned an invalid decision output")
    return LongDecisionOutput(
        action=LongDecisionAction(str(payload["action"])),
        reason=str(payload["reason"]),
        decision_ts_ms=int(payload["decision_ts_ms"]),
        symbol=str(payload["symbol"]),
        signal_ts_ms=int(payload["signal_ts_ms"]),
        entry_reason=str(payload["entry_reason"]),
        position_weight=float(payload["position_weight"]),
        target_fraction_of_equity=float(payload["target_fraction_of_equity"]),
        target_notional_usdt=float(payload["target_notional_usdt"]),
        entry_leverage=float(payload["entry_leverage"]),
        stop_loss_fraction=float(payload["stop_loss_fraction"]),
        stop_decay_after_ms=int(payload["stop_decay_after_ms"]),
        decayed_stop_loss_fraction=float(payload["decayed_stop_loss_fraction"]),
        max_hold_duration_ms=int(payload["max_hold_duration_ms"]),
        entry_valid_until_ms=int(payload["entry_valid_until_ms"]),
        wake_at_or_below=(
            None
            if payload["wake_at_or_below"] is None
            else float(payload["wake_at_or_below"])
        ),
    )


class RustLongDecisionReducer:
    """Typed LONG decisions over a caller-owned persistent Rust process."""

    def __init__(
        self,
        contract: StrategyContract,
        config: LongStrategyConfig,
        *,
        realm: str = "mainnet",
    ) -> None:
        self._contract = contract
        self._config = _native_long_config(config, realm=realm)

    def decide(
        self,
        decision_input: LongDecisionInput,
        prior_state: LongPriorState,
    ) -> LongDecisionOutput:
        input_payload = decision_input.as_json_dict()
        input_payload.pop("schema_version")
        input_payload["feature_row"] = _long_feature_row(decision_input.feature_row)
        output = self._contract.request(
            {
                "schema_version": 1,
                "operation": "long_decide",
                "config": self._config,
                "input": input_payload,
                "prior": prior_state.as_json_dict(),
            }
        )
        return _long_decision_output(output)


@dataclass(frozen=True, slots=True)
class LongResearchClassification:
    """Rust-owned pump diagnostics and final LONG entry classification."""

    threshold_1d: float
    threshold_3d: float
    threshold_7d: float
    trigger_1d: bool
    trigger_3d: bool
    trigger_7d: bool
    trigger_any: bool
    source_strength: float | None
    pattern: str | None
    stop_loss_fraction: float
    max_hold_days: int


def _long_classification(payload: Mapping[str, Any]) -> LongResearchClassification:
    if set(payload) != _LONG_CLASSIFICATION_FIELDS or payload.get("schema_version") != 1:
        raise RuntimeError("Rust LONG classifier returned an invalid output")
    for field in (
        "trigger_1d",
        "trigger_3d",
        "trigger_7d",
        "trigger_any",
    ):
        if type(payload[field]) is not bool:
            raise RuntimeError("Rust LONG classifier returned an invalid output")
    numeric = {
        field: _optional_finite_float(payload[field])
        for field in (
            "threshold_1d",
            "threshold_3d",
            "threshold_7d",
            "stop_loss_fraction",
        )
    }
    if any(value is None for value in numeric.values()):
        raise RuntimeError("Rust LONG classifier returned an invalid output")
    strength = _optional_finite_float(payload["source_strength"])
    if payload["source_strength"] is not None and strength is None:
        raise RuntimeError("Rust LONG classifier returned an invalid output")
    pattern = payload["pattern"]
    if pattern not in {None, "fomo_chase"}:
        raise RuntimeError("Rust LONG classifier returned an invalid output")
    hold_days = payload["max_hold_days"]
    if type(hold_days) is not int or hold_days < 0:
        raise RuntimeError("Rust LONG classifier returned an invalid output")
    return LongResearchClassification(
        threshold_1d=cast(float, numeric["threshold_1d"]),
        threshold_3d=cast(float, numeric["threshold_3d"]),
        threshold_7d=cast(float, numeric["threshold_7d"]),
        trigger_1d=payload["trigger_1d"],
        trigger_3d=payload["trigger_3d"],
        trigger_7d=payload["trigger_7d"],
        trigger_any=payload["trigger_any"],
        source_strength=strength,
        pattern=cast(str | None, pattern),
        stop_loss_fraction=cast(float, numeric["stop_loss_fraction"]),
        max_hold_days=hold_days,
    )


class RustLongResearchClassifier:
    """Batch LONG research rows through the native Rust classifier."""

    def __init__(
        self,
        contract: StrategyContract,
        config: LongStrategyConfig,
        *,
        realm: str = "mainnet",
    ) -> None:
        self._contract = contract
        self._rule = _native_long_config(config, realm=realm)["rule"]

    def classify(
        self,
        rows: Iterable[Mapping[str, Any]],
    ) -> list[LongResearchClassification]:
        batch: list[dict[str, Any]] = []
        output: list[LongResearchClassification] = []
        for row in rows:
            shaped = _long_feature_row(row)
            if shaped is None:
                raise ValueError("LONG classifier row cannot be empty")
            batch.append(shaped)
            if len(batch) == _CONTRACT_BATCH_ROWS:
                output.extend(self._classify_batch(batch))
                batch = []
        if batch:
            output.extend(self._classify_batch(batch))
        return output

    def _classify_batch(
        self,
        rows: list[dict[str, Any]],
    ) -> list[LongResearchClassification]:
        response = self._contract.request(
            {
                "schema_version": 1,
                "operation": "long_classify",
                "config": self._rule,
                "rows": rows,
            }
        )
        if set(response) != {"schema_version", "classifications"}:
            raise RuntimeError("Rust LONG classifier returned an invalid batch")
        if response["schema_version"] != 1 or not isinstance(
            response["classifications"], list
        ):
            raise RuntimeError("Rust LONG classifier returned an invalid batch")
        classifications = [
            _long_classification(item)
            for item in response["classifications"]
            if isinstance(item, dict)
        ]
        if len(classifications) != len(rows):
            raise RuntimeError("Rust LONG classifier changed the row count")
        return classifications


def _carry_research_rule(config: CarryHoldConfig) -> dict[str, Any]:
    persisted = config.persistence_window is not None
    if persisted and config.persistence_window != PERSISTENCE_WINDOW:
        raise FinancedLongsError(
            f"{config.config_id}: persistence window {config.persistence_window} but the "
            f"prepared column is built over {PERSISTENCE_WINDOW} settlements"
        )
    if persisted and config.enter_bp != DEFAULT_ENTER_BP:
        raise FinancedLongsError(
            f"{config.config_id}: persistence counts settlements deeper than "
            f"{DEFAULT_ENTER_BP} bp but this config enters at {config.enter_bp} bp"
        )
    return {
        "config_id": config.config_id,
        "enter_bp": config.enter_bp,
        "exit_bp": config.exit_bp,
        "per_name_cap": config.per_name_cap,
        "gross_cap": config.gross_cap,
        "depth_ref_bp_per_day": config.depth_ref_bp_per_day,
        "depth_floor": config.depth_floor,
        "depth_exponent": config.depth_exponent,
        "toxic_band_ret3d": config.toxic_band_ret3d,
        "min_vol30_daily": config.min_vol30_daily,
        "trail_recovery_exit_bp_2d": config.trail_recovery_exit_bp_2d,
        "persistence_cut": config.persistence_cut if persisted else None,
        "persistence_lo": config.persistence_lo,
        "flow_cut": config.flow_cut,
        "flow_lo": config.flow_lo,
        "whale_cut": config.whale_cut,
        "whale_lo": config.whale_lo,
    }


def _carry_research_rows(
    universe: pl.DataFrame,
    config: CarryHoldConfig,
) -> list[dict[str, Any]]:
    need = (
        ["trail_fund_24h"] * (config.depth_ref_bp_per_day is not None)
        + ["ret_3d"] * (config.toxic_band_ret3d is not None)
        + ["vol_30d_daily"] * (config.min_vol30_daily is not None)
        + ["dtrail_2d"] * (config.trail_recovery_exit_bp_2d is not None)
        + ["crowd_persistence"] * (config.persistence_window is not None)
        + ["turn_growth_3d"] * (config.flow_cut is not None)
        + ["d_tt_ls_3d"] * (config.whale_cut is not None)
    )
    missing = [field for field in dict.fromkeys(need) if field not in universe.columns]
    if missing:
        raise FinancedLongsError(
            f"{config.config_id}: enabled features require prepared columns {missing}"
        )
    invalid_funding = universe.filter(
        pl.col("by_funding").is_not_null()
        & ~pl.col("by_funding").is_finite().fill_null(False)
    )
    if invalid_funding.height:
        raise FinancedLongsError(
            f"{config.config_id}: by_funding contains {invalid_funding.height} non-finite values"
        )
    available = [field for field in _CARRY_RESEARCH_FLOAT_FIELDS if field in universe.columns]
    rows: list[dict[str, Any]] = []
    for raw in universe.select("bar_ts_ms", "symbol", *available).iter_rows(named=True):
        row: dict[str, Any] = {
            "bar_ts_ms": int(raw["bar_ts_ms"]),
            "symbol": str(raw["symbol"]),
            "adv_rank": None,
            "in_universe": True,
        }
        row.update({field: _optional_finite_float(raw.get(field)) for field in available})
        rows.append(row)
    return rows


class RustCarryResearchScorer:
    """Score an already-ranked CARRY frame through Rust."""

    def __init__(self, contract: StrategyContract) -> None:
        self._contract = contract

    def weights(
        self,
        universe: pl.DataFrame,
        config: CarryHoldConfig,
    ) -> pl.DataFrame:
        response = self._contract.request(
            {
                "schema_version": 1,
                "operation": "carry_research_score",
                "config": _carry_research_rule(config),
                "rows": _carry_research_rows(universe, config),
            }
        )
        if set(response) != {"schema_version", "weights"}:
            raise RuntimeError("Rust CARRY scorer returned an invalid output")
        raw_weights = response["weights"]
        if response["schema_version"] != 1 or not isinstance(raw_weights, list):
            raise RuntimeError("Rust CARRY scorer returned an invalid output")
        rows: list[dict[str, object]] = []
        for raw in raw_weights:
            if not isinstance(raw, dict) or set(raw) != {"bar_ts_ms", "symbol", "w"}:
                raise RuntimeError("Rust CARRY scorer returned an invalid weight")
            weight = _optional_finite_float(raw["w"])
            if (
                type(raw["bar_ts_ms"]) is not int
                or not isinstance(raw["symbol"], str)
                or weight is None
                or weight <= 0.0
            ):
                raise RuntimeError("Rust CARRY scorer returned an invalid weight")
            rows.append(
                {
                    "bar_ts_ms": raw["bar_ts_ms"],
                    "symbol": raw["symbol"],
                    "w": weight,
                }
            )
        return pl.DataFrame(
            rows,
            schema={"bar_ts_ms": pl.Int64, "symbol": pl.String, "w": pl.Float64},
        )


def rust_carry_research_weights(
    universe: pl.DataFrame,
    config: CarryHoldConfig,
    *,
    strategy_contract: StrategyContract | None = None,
) -> pl.DataFrame:
    if strategy_contract is not None:
        return RustCarryResearchScorer(strategy_contract).weights(universe, config)
    with RustStrategyContract() as contract:
        return RustCarryResearchScorer(contract).weights(universe, config)
