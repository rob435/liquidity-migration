from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only in missing dependency envs
    yaml = None


DEFAULT_MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
DEFAULT_STABLECOIN_SYMBOLS = (
    "BUSDUSDT",
    "DAIUSDT",
    "FDUSDUSDT",
    "FRAXUSDT",
    "GUSDUSDT",
    "LUSDUSDT",
    "PYUSDUSDT",
    "SUSDUSDT",
    "TUSDUSDT",
    "USD1USDT",
    "USDCUSDT",
    "USDDUSDT",
    "USDEUSDT",
    "USDPUSDT",
    "USDYUSDT",
)
DEFAULT_MANUAL_EXCLUDED_SYMBOLS = ("XRPUSDT", "TRXUSDT")
DEFAULT_EXCLUDED_SYMBOLS = DEFAULT_MAJOR_SYMBOLS + DEFAULT_STABLECOIN_SYMBOLS + DEFAULT_MANUAL_EXCLUDED_SYMBOLS


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    name: str = "bybit"
    category: str = "linear"
    settle_coin: str = "USDT"
    testnet: bool = False


@dataclass(frozen=True, slots=True)
class TradeFlowConfig:
    exclude_block_trades: bool = True
    exclude_rpi_trades: bool = True


@dataclass(frozen=True, slots=True)
class TradeLifecycleConfig:
    score: str = "dollar_volume_rank"
    start_date: str = ""
    end_date: str = ""
    quantile: float = 0.50
    hold_days: int = 7
    rebalance_days: int = 7
    gross_exposure: float = 1.0
    entry_delay_hours: int = 1
    stop_mode: str = "fixed"
    stop_loss_pct: float = 0.08
    vol_stop_multiplier: float = 3.0
    vol_stop_lookback_days: int = 20
    min_stop_loss_pct: float = 0.0
    max_stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    min_symbols: int = 4
    cost_multiplier: float = 1.0
    side_mode: str = "long_high_short_low"
    rank_exit_enabled: bool = False
    rank_exit_threshold: float = 0.50
    universe_rank_min: int = 1
    universe_rank_max: int = 0
    universe_min_daily_turnover: float = 0.0
    include_symbols: tuple[str, ...] = ()
    exclude_symbols: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    min_turnover_24h: float = 2_000_000.0
    min_age_days: int = 30
    max_age_days: int = 0
    rank_start: int = 1
    rank_end: int = 120
    max_symbols: int = 120
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS


@dataclass(frozen=True, slots=True)
class CostConfig:
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 5.5
    maker_adverse_selection_bps: float = 1.0
    taker_slippage_bps_liquid: float = 2.0
    maker_fill_probability: float = 0.60

    @property
    def base_entry_exit_cost_bps(self) -> float:
        maker_cost = self.maker_fee_bps + self.maker_adverse_selection_bps
        taker_cost = self.taker_fee_bps + self.taker_slippage_bps_liquid
        blended = (
            self.maker_fill_probability * maker_cost
            + (1.0 - self.maker_fill_probability) * taker_cost
        )
        return 2.0 * blended


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    trade_flow: TradeFlowConfig = field(default_factory=TradeFlowConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    data_root: Path = Path("data/liquidity_migration")


def _merge_dataclass(cls: type, payload: dict[str, Any] | None):
    payload = dict(payload or {})
    return cls(**payload)


def _tuple_str(payload: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in payload.get(key, default))


def _merge_universe_config(payload: dict[str, Any] | None) -> UniverseConfig:
    payload = dict(payload or {})
    return UniverseConfig(
        min_turnover_24h=float(payload.get("min_turnover_24h", 2_000_000.0)),
        min_age_days=int(payload.get("min_age_days", 30)),
        max_age_days=int(payload.get("max_age_days", 0)),
        rank_start=int(payload.get("rank_start", 1)),
        rank_end=int(payload.get("rank_end", 120)),
        max_symbols=int(payload.get("max_symbols", 120)),
        exclude_symbols=_tuple_str(payload, "exclude_symbols", DEFAULT_EXCLUDED_SYMBOLS),
    )


def load_config(path: str | Path | None = None, *, data_root: str | Path | None = None) -> ResearchConfig:
    raw: dict[str, Any] = {}
    if path is not None:
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(config_path)
        if yaml is None:
            raise RuntimeError("PyYAML is required to load YAML config files")
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded:
            raw = dict(loaded)

    root = Path(data_root or raw.get("data_root") or "data/liquidity_migration")
    return ResearchConfig(
        exchange=_merge_dataclass(ExchangeConfig, raw.get("exchange")),
        trade_flow=_merge_dataclass(TradeFlowConfig, raw.get("trade_flow")),
        universe=_merge_universe_config(raw.get("universe")),
        costs=_merge_dataclass(CostConfig, raw.get("cost_model")),
        data_root=root,
    )
