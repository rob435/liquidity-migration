"""Typed effective configuration for the CARRY producer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from liquidity_migration.core.config import ExchangeConfig
from liquidity_migration.policy.execution_environment import (
    ExecutionEnvironment,
    execution_environment,
)
from liquidity_migration.rules.carry_hold import CarryHoldConfig


REPLAY_DAYS = 90
MIN_REPLAY_DAYS = 45
CARRY_CYCLES_DATASET = "carry_hold_demo_cycles"
CARRY_MAINNET_CYCLES_DATASET = "carry_hold_mainnet_cycles"
EARLY_EXIT_STATE_NAME = "carry_early_exits.json"

_CONFIGS_DIR = Path(__file__).resolve().parents[2] / "configs"
CARRY_CONFIG_PATH = _CONFIGS_DIR / "lane2_carry_hold_v7.json"
CARRY_STRATEGY_PROFILE_CHOICES: tuple[str, ...] = ("v3", "v4", "v6", "v7")
DEFAULT_CARRY_STRATEGY_PROFILE = "v7"


@dataclass(frozen=True, slots=True)
class CarryStrategyProfile:
    profile_name: str
    config_path: Path
    presettle_exit: bool = False


_CARRY_STRATEGY_PROFILES: dict[str, CarryStrategyProfile] = {
    "v3": CarryStrategyProfile("carry_hold_v3_live_v1", _CONFIGS_DIR / "lane2_carry_hold_v3.json"),
    "v4": CarryStrategyProfile("carry_hold_v4_live_v1", _CONFIGS_DIR / "lane2_carry_hold_v4.json"),
    "v6": CarryStrategyProfile("carry_hold_v6_live_v1", CARRY_CONFIG_PATH),
    "v7": CarryStrategyProfile("carry_hold_v7_live_v1", CARRY_CONFIG_PATH, presettle_exit=True),
}


def resolve_carry_strategy_profile(name: str) -> CarryStrategyProfile:
    try:
        return _CARRY_STRATEGY_PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown CARRY strategy profile {name!r}; supported: "
            f"{', '.join(CARRY_STRATEGY_PROFILE_CHOICES)}"
        ) from None


@dataclass(frozen=True, slots=True)
class CarryDemoCycleConfig:
    execution_environment: str = ""
    candidate_universe_file: str = ""
    presettlement_event_path: str = ""
    strategy_profile: str = DEFAULT_CARRY_STRATEGY_PROFILE
    early_exit_enabled: bool = False
    notional_multiplier: float = 1.0
    entry_leverage: float = 2.0
    declared_stop_loss_fraction: float = 0.35
    max_new_entries_per_cycle: int = 10
    capital_reference_usdt: float = 0.0
    operational_profile_sha256: str = ""
    replay_days: int = REPLAY_DAYS
    workers: int = 4
    ws_klines_enabled: bool = True
    ws_klines_bootstrap_workers: int = 16
    ws_klines_lookback_days: int = REPLAY_DAYS + 2
    ws_klines_universe_refresh_seconds: float = 3600.0
    ws_klines_topics_per_connection: int = 180
    ws_klines_stale_warning_seconds: float = 60.0
    ws_klines_stale_reconnect_seconds: float = 180.0


def validate_carry_demo_config(config: CarryDemoCycleConfig) -> None:
    execution_environment(config.execution_environment)
    resolve_carry_strategy_profile(config.strategy_profile)
    if config.presettlement_event_path and not Path(
        config.presettlement_event_path
    ).expanduser().is_absolute():
        raise ValueError("presettlement_event_path must be absolute when supplied")
    if bool(getattr(config, "telegram", False)):
        raise ValueError("strategy producers do not own Telegram controls")
    if not math.isfinite(config.notional_multiplier) or config.notional_multiplier <= 0.0:
        raise ValueError("notional_multiplier must be positive")
    if not math.isfinite(config.entry_leverage) or config.entry_leverage <= 0.0:
        raise ValueError("entry_leverage must be positive")
    if not 0.0 < config.declared_stop_loss_fraction < 1.0:
        raise ValueError("declared_stop_loss_fraction must be a fraction in (0, 1)")
    if config.max_new_entries_per_cycle < 1:
        raise ValueError("max_new_entries_per_cycle must be >= 1")
    if not math.isfinite(config.capital_reference_usdt) or config.capital_reference_usdt < 0.0:
        raise ValueError("capital_reference_usdt must be finite and non-negative")
    if config.replay_days < MIN_REPLAY_DAYS:
        raise ValueError(f"replay_days must be >= {MIN_REPLAY_DAYS} (engine floor)")
    if config.workers < 1:
        raise ValueError("workers must be >= 1")
    if config.ws_klines_enabled and config.ws_klines_lookback_days < config.replay_days + 1:
        raise ValueError(
            "ws_klines_lookback_days must cover replay_days + 1 when the WS kline plane is on"
        )


def carry_cycles_dataset(config: CarryDemoCycleConfig) -> str:
    return {
        ExecutionEnvironment.MAINNET: CARRY_MAINNET_CYCLES_DATASET,
    }.get(execution_environment(config.execution_environment), CARRY_CYCLES_DATASET)


@dataclass(frozen=True, slots=True)
class CarryConfigProvenance:
    field: str
    source: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CarryEffectiveConfig:
    cycle: CarryDemoCycleConfig
    profile: CarryStrategyProfile
    rule: CarryHoldConfig
    exchange: ExchangeConfig
    data_root: Path
    sizing_anchor_path: Path
    early_exit_state_path: Path
    presettlement_event_path: Path
    cycles_dataset: str
    target_book_path: Path
    engine_heartbeat_path: Path
    expected_account_user_id: str
    invocation_id: str
    provenance: tuple[CarryConfigProvenance, ...]

    def provenance_by_field(self) -> dict[str, dict[str, str]]:
        return {
            row.field: {"source": row.source, "detail": row.detail}
            for row in self.provenance
        }
