from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# audit2b: top-level YAML keys load_config actually consumes. Anything else in a
# config file (e.g. the committed `trade_flow` block) is currently unwired; warn
# so it is surfaced rather than silently dropped, matching this module's
# fail-loud-on-unknown-keys philosophy without breaking the happy path.
_CONSUMED_TOP_LEVEL_KEYS = frozenset({"exchange", "universe", "cost_model", "data_root"})

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only in missing dependency envs
    yaml = None  # type: ignore[assignment]  # optional-dep fallback (mypy: Module vs None)


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
    "USTCUSDT",
    "USDYUSDT",
)
DEFAULT_EXCLUDED_SYMBOLS = DEFAULT_STABLECOIN_SYMBOLS
DEFAULT_RESEARCH_DATA_ROOT = Path("~/SHARED_DATA/bybit_full_pit")


@dataclass(frozen=True, slots=True)
class ExchangeConfig:
    name: str = "bybit"
    category: str = "linear"
    settle_coin: str = "USDT"
    testnet: bool = False


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
    take_profit_pct: float = 0.0
    mfe_giveback_trigger_pct: float = 0.0
    mfe_giveback_retain_pct: float = 0.0
    failed_fade_exit_hours: int = 0
    failed_fade_min_mfe_pct: float = 0.0
    failed_fade_loss_pct: float = 0.0
    failed_fade_close_location_min: float = 1.0
    # Breakeven trailing stop: once MFE >= breakeven_arm_pct, exit if close
    # returns to or past entry price. Disabled when 0.0.
    breakeven_arm_pct: float = 0.0
    min_symbols: int = 4
    cost_multiplier: float = 1.0
    side_mode: str = "long_high_short_low"
    rank_exit_enabled: bool = False
    rank_exit_threshold: float = 0.50
    # Negative control: deterministic per-(symbol,bar) hash exit at this
    # per-bar probability (no market content). 0.0 = OFF (byte-identical).
    hash_exit_prob: float = 0.0
    universe_rank_max: int = 0
    universe_min_daily_turnover: float = 0.0
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
    # Share of fills assumed passive (maker). The LIVE runner sends Market orders
    # on both legs = 100% taker, so the dataclass default is 0.0 to model the
    # deployed execution exactly (base becomes 2*(taker_fee+taker_slippage)=15 bps)
    # rather than relying on the scenario cost_multiplier to paper over a maker
    # blend the live engine never gets. The committed config YAML
    # (configs/volume_alpha.default.yaml) also sets 0.0, so this is a no-op for the
    # official entrypoints that load it; making it the default hardens every other
    # consumer (ad-hoc backtest, REPL, a future CLI/sweep, a refactor that drops
    # cost_config) against the ~36% under-costing that a 0.60 maker blend produces
    # — the canonical "cost-too-low flatters returns" error. Raise it only once
    # passive execution (R12 sniper / limit-chase exit) is actually deployed. (E3;
    # M2-audit reconciliation-drift errors #6/#24)
    maker_fill_probability: float = 0.0
    # E4: per-leg cost asymmetry. The exit leg of a short is a buy-to-close,
    # which is more expensive than the sell-to-open entry — especially covering
    # into a stress spike. exit_cost_multiplier scales ONLY the exit leg's cost.
    # Default 1.0 = symmetric (legacy behavior, base = 2*blended); a value >1
    # charges the cover leg more. A global down-payment toward R6's per-name
    # per-bar cost model.
    exit_cost_multiplier: float = 1.0

    @property
    def base_entry_exit_cost_bps(self) -> float:
        maker_cost = self.maker_fee_bps + self.maker_adverse_selection_bps
        taker_cost = self.taker_fee_bps + self.taker_slippage_bps_liquid
        blended = (
            self.maker_fill_probability * maker_cost
            + (1.0 - self.maker_fill_probability) * taker_cost
        )
        # entry leg + exit leg; exit leg optionally costlier (E4 asymmetry).
        return blended * (1.0 + self.exit_cost_multiplier)


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    data_root: Path = DEFAULT_RESEARCH_DATA_ROOT


# audit2b: type-aware coercion so the generic dataclass merge matches the
# numeric coercion UniverseConfig already does (float()/int()). Without it a
# quoted-numeric YAML value (e.g. maker_fee_bps: "2.0") flows straight into the
# frozen dataclass and only blows up later in str-arithmetic
# (base_entry_exit_cost_bps). Keyed on the *string* annotation because this
# module uses `from __future__ import annotations`, so fields(cls)[i].type is
# the annotation text, not the type object. Unknown annotations pass through
# unchanged, so the happy path (YAML already gives float/bool) is a no-op.
_COERCERS: dict[str, Any] = {"float": float, "int": int, "bool": bool, "str": str}


def _coerce_field(annotation: Any, value: Any) -> Any:
    coercer = _COERCERS.get(annotation) if isinstance(annotation, str) else None
    return coercer(value) if coercer is not None else value


def _merge_dataclass(cls: type, payload: dict[str, Any] | None):
    payload = dict(payload or {})
    annotations = {item.name: item.type for item in fields(cls)}
    allowed = set(annotations)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise TypeError(
            f"Unknown {cls.__name__} keys in config: {unknown}. Allowed: {sorted(allowed)}"
        )
    return cls(
        **{
            key: _coerce_field(annotations[key], payload[key])
            for key in allowed
            if key in payload
        }
    )


def ensure_data_root_exists(data_root: str | Path) -> Path:
    """Raise FileNotFoundError when the research data root is missing."""
    root = Path(data_root).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"Data root does not exist: {root}")
    return root


def _tuple_str(payload: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in payload.get(key, default))


def _merge_universe_config(payload: dict[str, Any] | None) -> UniverseConfig:
    payload = dict(payload or {})
    # Reject unknown YAML keys for the same fail-loud behaviour as _merge_dataclass —
    # a typo'd universe key would otherwise be silently dropped (audit 2026-06-02 #42).
    allowed = {item.name for item in fields(UniverseConfig)}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise TypeError(
            f"Unknown UniverseConfig keys in config: {unknown}. Allowed: {sorted(allowed)}"
        )
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

    # audit2b: surface (don't silently drop) any top-level block load_config
    # does not consume — e.g. the committed `trade_flow` block, which is not
    # wired into ResearchConfig. Warn rather than raise so the happy path
    # (loading the default YAML) still returns an identical ResearchConfig.
    unconsumed = sorted(set(raw) - _CONSUMED_TOP_LEVEL_KEYS)
    if unconsumed:
        _logger.warning(
            "load_config ignoring unconsumed top-level config keys: %s (consumed: %s)",
            unconsumed,
            sorted(_CONSUMED_TOP_LEVEL_KEYS),
        )

    root = Path(data_root or raw.get("data_root") or DEFAULT_RESEARCH_DATA_ROOT).expanduser()
    return ResearchConfig(
        exchange=_merge_dataclass(ExchangeConfig, raw.get("exchange")),
        universe=_merge_universe_config(raw.get("universe")),
        costs=_merge_dataclass(CostConfig, raw.get("cost_model")),
        data_root=root,
    )
