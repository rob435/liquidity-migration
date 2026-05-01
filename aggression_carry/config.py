from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only in missing dependency envs
    yaml = None


DEFAULT_HORIZONS_H = (4, 12, 24, 72)


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    name: str = "bybit"
    category: str = "linear"
    settle_coin: str = "USDT"
    testnet: bool = False


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    aggression_fast_span_h: int = 6
    aggression_slow_span_h: int = 24
    volume_fast_span_h: int = 6
    volume_slow_span_h: int = 72
    momentum_windows_h: tuple[int, int, int] = (12, 24, 72)
    momentum_weights: tuple[float, float, float] = (0.50, 0.30, 0.20)
    carry_ema_span: int = 3
    oi_impulse_window_h: int = 12
    robust_z_clip: float = 3.0
    exclude_block_trades_from_aggression: bool = True
    exclude_rpi_trades_from_aggression: bool = True


@dataclass(frozen=True, slots=True)
class SignalConfig:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "aggression_confirmed": 0.42,
            "momentum": 0.18,
            "carry": 0.20,
            "quality": 0.12,
            "oi_impulse": 0.08,
        }
    )
    min_abs_score_entry: float = 0.25
    long_quantile: float = 0.20
    short_quantile: float = 0.20


@dataclass(frozen=True, slots=True)
class PortfolioConfig:
    rebalance_interval_h: int = 4
    base_gross_exposure: float = 1.0
    max_net_exposure_abs: float = 0.10
    max_single_position_weight: float = 0.05
    volatility_window_h: int = 168


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
    features: FeatureConfig = field(default_factory=FeatureConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H
    data_root: Path = Path("data/aggression_carry")


def _merge_dataclass(cls: type, payload: dict[str, Any] | None):
    payload = dict(payload or {})
    return cls(**payload)


def _merge_signal_config(payload: dict[str, Any] | None) -> SignalConfig:
    payload = dict(payload or {})
    weights = {**SignalConfig().weights, **payload.pop("weights", {})}
    return SignalConfig(weights=weights, **payload)


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

    root = Path(data_root or raw.get("data_root") or "data/aggression_carry")
    return ResearchConfig(
        exchange=_merge_dataclass(ExchangeConfig, raw.get("exchange")),
        features=_merge_dataclass(FeatureConfig, raw.get("features")),
        signals=_merge_signal_config(raw.get("signals")),
        portfolio=_merge_dataclass(PortfolioConfig, raw.get("portfolio")),
        costs=_merge_dataclass(CostConfig, raw.get("cost_model")),
        horizons_h=tuple(raw.get("horizons_h", DEFAULT_HORIZONS_H)),
        data_root=root,
    )
