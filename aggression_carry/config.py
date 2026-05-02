from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only in missing dependency envs
    yaml = None


DEFAULT_VOLUME_HORIZONS_D = (1, 3, 7)
DEFAULT_VOLUME_QUANTILES = (0.20, 0.30, 0.50)


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
class VolumeAlphaConfig:
    horizons_d: tuple[int, ...] = DEFAULT_VOLUME_HORIZONS_D
    quantiles: tuple[float, ...] = DEFAULT_VOLUME_QUANTILES


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
    volume_alpha: VolumeAlphaConfig = field(default_factory=VolumeAlphaConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    data_root: Path = Path("data/volume_alpha")


def _merge_dataclass(cls: type, payload: dict[str, Any] | None):
    payload = dict(payload or {})
    return cls(**payload)


def _merge_volume_alpha_config(payload: dict[str, Any] | None) -> VolumeAlphaConfig:
    payload = dict(payload or {})
    horizons = tuple(int(item) for item in payload.get("horizons_d", DEFAULT_VOLUME_HORIZONS_D))
    quantiles = tuple(float(item) for item in payload.get("quantiles", DEFAULT_VOLUME_QUANTILES))
    return VolumeAlphaConfig(horizons_d=horizons, quantiles=quantiles)


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

    root = Path(data_root or raw.get("data_root") or "data/volume_alpha")
    return ResearchConfig(
        exchange=_merge_dataclass(ExchangeConfig, raw.get("exchange")),
        trade_flow=_merge_dataclass(TradeFlowConfig, raw.get("trade_flow")),
        volume_alpha=_merge_volume_alpha_config(raw.get("volume_alpha")),
        costs=_merge_dataclass(CostConfig, raw.get("cost_model")),
        data_root=root,
    )
