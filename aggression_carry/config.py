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
DEFAULT_GRID_HOLD_DAYS = (3, 7, 14)
DEFAULT_GRID_QUANTILES = (0.30, 0.50)
DEFAULT_GRID_FIXED_STOPS = (0.0, 0.12, 0.20, 0.30)
DEFAULT_GRID_VOL_STOPS = (3.0, 4.0)
DEFAULT_MAJOR_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
DEFAULT_CLOSE_FADE_SIGNAL_MINUTES = (22 * 60,)
DEFAULT_CLOSE_FADE_TOP_NS = (3, 5)
DEFAULT_CLOSE_FADE_HOLD_MINUTES = (60, 120, 180)


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
class VolumeBacktestConfig:
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
class VolumeGridConfig:
    scores: tuple[str, ...] = ("dollar_volume_rank",)
    quantiles: tuple[float, ...] = DEFAULT_GRID_QUANTILES
    hold_days: tuple[int, ...] = DEFAULT_GRID_HOLD_DAYS
    fixed_stop_loss_pcts: tuple[float, ...] = DEFAULT_GRID_FIXED_STOPS
    vol_stop_multipliers: tuple[float, ...] = DEFAULT_GRID_VOL_STOPS
    rank_exit_modes: tuple[bool, ...] = (False, True)
    include_reverse_side: bool = False
    take_profit_pcts: tuple[float, ...] = (0.0,)
    cost_multipliers: tuple[float, ...] = (1.0,)


@dataclass(frozen=True, slots=True)
class DailyCloseFadeConfig:
    signal_minute: int = 23 * 60
    top_n: int = 3
    hold_minutes: int = 120
    entry_delay_minutes: int = 1
    entry_twap_minutes: int = 0
    score: str = "vol_adjusted_day_return"
    pump_filter: str = "all"
    min_age_days: int = 10
    min_day_turnover: float = 0.0
    min_last_60m_turnover: float = 0.0
    vol_lookback_days: int = 20
    liquidity_lookback_days: int = 7
    liquidity_rank_min: int = 1
    liquidity_rank_max: int = 0
    min_baseline_turnover: float = 0.0
    account_equity: float = 10_000.0
    max_position_weight: float = 0.0
    max_trade_notional_pct_of_day_turnover: float = 0.0
    max_trade_notional_pct_of_baseline_turnover: float = 0.0
    market_impact_bps_per_1pct_turnover: float = 1.0
    gross_exposure: float = 1.0
    coin_excess_vs_market_min: float = 0.0
    coin_vwap_extension_min: float = 0.0
    coin_vwap_extension_max: float = 0.0
    coin_late_volume_ratio_min: float = 0.0
    market_median_day_return_max: float = 0.0
    organic_move_score_max: float = 0.0
    manipulation_risk_score_min: float = 0.0
    squeeze_risk_score_max: float = 0.0
    require_funding_context: bool = False
    require_open_interest_context: bool = False
    require_premium_context: bool = False
    require_trade_flow_context: bool = False
    require_all_context: bool = False
    position_sizing: str = "equal"
    score_weight_power: float = 1.0
    stop_loss_pct: float = 0.0
    take_profit_pct: float = 0.0
    time_decay_take_profit_floor_pct: float = 0.0
    time_decay_take_profit_minutes: int = 0
    basket_stop_loss_pct: float = 0.0
    trailing_stop_pct: float = 0.0
    trailing_activation_pct: float = 0.01
    vol_trailing_stop_mult: float = 0.0
    vol_trailing_activation_mult: float = 0.0
    mfe_giveback_activation_pct: float = 0.0
    mfe_giveback_pct: float = 0.0
    vwap_reversion_pct: float = 0.0
    stop_delay_minutes: int = 15
    profit_protection_delay_minutes: int | None = None
    twap_stop_adding_pct: float = 0.0
    cost_multiplier: float = 1.0
    min_symbols: int = 1
    include_symbols: tuple[str, ...] = ()
    exclude_symbols: tuple[str, ...] = DEFAULT_MAJOR_SYMBOLS
    require_archive_membership: bool = False


@dataclass(frozen=True, slots=True)
class DailyCloseFadeGridConfig:
    signal_minutes: tuple[int, ...] = DEFAULT_CLOSE_FADE_SIGNAL_MINUTES
    top_ns: tuple[int, ...] = DEFAULT_CLOSE_FADE_TOP_NS
    hold_minutes: tuple[int, ...] = DEFAULT_CLOSE_FADE_HOLD_MINUTES
    gross_exposures: tuple[float, ...] = (1.0,)
    scores: tuple[str, ...] = ("vol_adjusted_day_return", "context_fade_score", "day_return")
    pump_filters: tuple[str, ...] = ("all", "pump", "non_pump")
    stop_loss_pcts: tuple[float, ...] = (0.0, 0.03, 0.05, 0.08)
    take_profit_pcts: tuple[float, ...] = (0.0,)
    time_decay_take_profit_floor_pcts: tuple[float, ...] = (0.0,)
    time_decay_take_profit_minutes: tuple[int, ...] = (0,)
    basket_stop_loss_pcts: tuple[float, ...] = (0.0,)
    trailing_stop_pcts: tuple[float, ...] = (0.0, 0.015, 0.025)
    trailing_activation_pcts: tuple[float, ...] = (0.01, 0.02)
    vol_trailing_stop_mults: tuple[float, ...] = (0.0,)
    vol_trailing_activation_mults: tuple[float, ...] = (0.0,)
    mfe_giveback_activation_pcts: tuple[float, ...] = (0.0,)
    mfe_giveback_pcts: tuple[float, ...] = (0.0,)
    vwap_reversion_pcts: tuple[float, ...] = (0.0,)
    profit_protection_delay_minutes: tuple[int, ...] = (15,)
    liquidity_lookback_days: tuple[int, ...] = (7,)
    liquidity_rank_mins: tuple[int, ...] = (1,)
    liquidity_rank_maxs: tuple[int, ...] = (0,)
    min_baseline_turnovers: tuple[float, ...] = (0.0,)
    coin_vwap_extension_maxs: tuple[float, ...] = (0.0,)
    market_median_day_return_maxs: tuple[float, ...] = (0.0,)
    organic_move_score_maxs: tuple[float, ...] = (0.0,)
    manipulation_risk_score_mins: tuple[float, ...] = (0.0,)
    squeeze_risk_score_maxs: tuple[float, ...] = (0.0,)
    require_open_interest_contexts: tuple[bool, ...] = (False,)
    require_all_contexts: tuple[bool, ...] = (False,)
    account_equities: tuple[float, ...] = (10_000.0,)
    max_position_weights: tuple[float, ...] = (0.0,)
    max_trade_notional_pct_day_turnovers: tuple[float, ...] = (0.0,)
    max_trade_notional_pct_baseline_turnovers: tuple[float, ...] = (0.0,)
    market_impact_bps_per_1pct_turnovers: tuple[float, ...] = (1.0,)
    cost_multipliers: tuple[float, ...] = (1.0, 2.0, 3.0)
    start_ms: int = 0
    end_ms: int = 0


ACTIVE_DAILY_CLOSE_FADE_DEFAULT = DailyCloseFadeConfig(
    signal_minute=23 * 60,
    top_n=1,
    hold_minutes=360,
    entry_delay_minutes=0,
    entry_twap_minutes=45,
    score="day_return",
    pump_filter="pump",
    liquidity_rank_min=226,
    liquidity_rank_max=0,
    max_position_weight=0.0,
    max_trade_notional_pct_of_day_turnover=0.0005,
    max_trade_notional_pct_of_baseline_turnover=0.001,
    market_impact_bps_per_1pct_turnover=3.0,
    position_sizing="score_capped",
    stop_loss_pct=0.08,
    take_profit_pct=0.10,
    time_decay_take_profit_floor_pct=0.04,
    time_decay_take_profit_minutes=120,
    stop_delay_minutes=0,
    vol_trailing_stop_mult=0.0,
    mfe_giveback_activation_pct=0.0,
    mfe_giveback_pct=0.0,
    profit_protection_delay_minutes=120,
    twap_stop_adding_pct=0.06,
    require_archive_membership=True,
    exclude_symbols=(
        "BTCUSDT",
        "ETHUSDT",
        "XRPUSDT",
        "BNBUSDT",
        "TRXUSDT",
        "SOLUSDT",
        "DOGEUSDT",
        "HYPEUSDT",
        "TONUSDT",
        "XMRUSDT",
        "LINKUSDT",
        "SUIUSDT",
        "ADAUSDT",
        "AVAXUSDT",
        "BCHUSDT",
        "XLMUSDT",
        "LTCUSDT",
        "HBARUSDT",
        "DOTUSDT",
        "SHIB1000USDT",
        "1000PEPEUSDT",
        "APTUSDT",
        "NEARUSDT",
        "ETCUSDT",
        "ICPUSDT",
        "FILUSDT",
        "ATOMUSDT",
        "POLUSDT",
        "ALGOUSDT",
        "VETUSDT",
        "ARBUSDT",
        "OPUSDT",
        "INJUSDT",
        "AAVEUSDT",
        "UNIUSDT",
        "ONDOUSDT",
        "TAOUSDT",
        "RENDERUSDT",
        "KASUSDT",
        "MNTUSDT",
        "WIFUSDT",
        "1000BONKUSDT",
        "1000FLOKIUSDT",
        "FETUSDT",
        "SEIUSDT",
        "JUPUSDT",
        "PYTHUSDT",
        "TIAUSDT",
        "STXUSDT",
        "IMXUSDT",
        "ENAUSDT",
        "WLDUSDT",
        "ZECUSDT",
    ),
)


@dataclass(frozen=True, slots=True)
class ForwardTestConfig:
    name: str = "daily-close-fade-paper"
    min_turnover_24h: float = 2_000_000.0
    max_spread_bps: float = 80.0
    min_open_interest_value: float = 0.0
    max_symbols: int = 0
    workers: int = 16
    max_entry_lag_minutes: int = 10
    send_telegram: bool = False


@dataclass(frozen=True, slots=True)
class UniverseConfig:
    min_turnover_24h: float = 2_000_000.0
    min_age_days: int = 30
    max_age_days: int = 0
    rank_start: int = 1
    rank_end: int = 120
    max_symbols: int = 120
    exclude_symbols: tuple[str, ...] = DEFAULT_MAJOR_SYMBOLS


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
    volume_backtest: VolumeBacktestConfig = field(default_factory=VolumeBacktestConfig)
    volume_grid: VolumeGridConfig = field(default_factory=VolumeGridConfig)
    daily_close_fade: DailyCloseFadeConfig = field(default_factory=lambda: ACTIVE_DAILY_CLOSE_FADE_DEFAULT)
    daily_close_fade_grid: DailyCloseFadeGridConfig = field(default_factory=DailyCloseFadeGridConfig)
    forward_test: ForwardTestConfig = field(default_factory=ForwardTestConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
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


def _merge_volume_backtest_config(payload: dict[str, Any] | None) -> VolumeBacktestConfig:
    payload = dict(payload or {})
    return VolumeBacktestConfig(
        score=str(payload.get("score", "dollar_volume_rank")),
        start_date=str(payload.get("start_date", "")),
        end_date=str(payload.get("end_date", "")),
        quantile=float(payload.get("quantile", 0.50)),
        hold_days=int(payload.get("hold_days", 7)),
        rebalance_days=int(payload.get("rebalance_days", payload.get("hold_days", 7))),
        gross_exposure=float(payload.get("gross_exposure", 1.0)),
        entry_delay_hours=int(payload.get("entry_delay_hours", 1)),
        stop_mode=str(payload.get("stop_mode", "fixed")),
        stop_loss_pct=float(payload.get("stop_loss_pct", 0.08)),
        vol_stop_multiplier=float(payload.get("vol_stop_multiplier", 3.0)),
        vol_stop_lookback_days=int(payload.get("vol_stop_lookback_days", 20)),
        min_stop_loss_pct=float(payload.get("min_stop_loss_pct", 0.0)),
        max_stop_loss_pct=float(payload.get("max_stop_loss_pct", 0.0)),
        take_profit_pct=float(payload.get("take_profit_pct", 0.0)),
        min_symbols=int(payload.get("min_symbols", 4)),
        cost_multiplier=float(payload.get("cost_multiplier", 1.0)),
        side_mode=str(payload.get("side_mode", "long_high_short_low")),
        rank_exit_enabled=bool(payload.get("rank_exit_enabled", False)),
        rank_exit_threshold=float(payload.get("rank_exit_threshold", 0.50)),
        universe_rank_min=int(payload.get("universe_rank_min", 1)),
        universe_rank_max=int(payload.get("universe_rank_max", 0)),
        universe_min_daily_turnover=float(payload.get("universe_min_daily_turnover", 0.0)),
        include_symbols=_tuple_str(payload, "include_symbols", ()),
        exclude_symbols=_tuple_str(payload, "exclude_symbols", ()),
    )


def _tuple_str(payload: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(item) for item in payload.get(key, default))


def _tuple_int(payload: dict[str, Any], key: str, default: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(int(item) for item in payload.get(key, default))


def _tuple_float(payload: dict[str, Any], key: str, default: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(float(item) for item in payload.get(key, default))


def _tuple_bool(payload: dict[str, Any], key: str, default: tuple[bool, ...]) -> tuple[bool, ...]:
    return tuple(bool(item) for item in payload.get(key, default))


def _merge_volume_grid_config(payload: dict[str, Any] | None) -> VolumeGridConfig:
    payload = dict(payload or {})
    return VolumeGridConfig(
        scores=_tuple_str(payload, "scores", ("dollar_volume_rank",)),
        quantiles=_tuple_float(payload, "quantiles", DEFAULT_GRID_QUANTILES),
        hold_days=_tuple_int(payload, "hold_days", DEFAULT_GRID_HOLD_DAYS),
        fixed_stop_loss_pcts=_tuple_float(payload, "fixed_stop_loss_pcts", DEFAULT_GRID_FIXED_STOPS),
        vol_stop_multipliers=_tuple_float(payload, "vol_stop_multipliers", DEFAULT_GRID_VOL_STOPS),
        rank_exit_modes=_tuple_bool(payload, "rank_exit_modes", (False, True)),
        include_reverse_side=bool(payload.get("include_reverse_side", False)),
        take_profit_pcts=_tuple_float(payload, "take_profit_pcts", (0.0,)),
        cost_multipliers=_tuple_float(payload, "cost_multipliers", (1.0,)),
    )


def _merge_daily_close_fade_config(payload: dict[str, Any] | None) -> DailyCloseFadeConfig:
    payload = dict(payload or {})
    default = ACTIVE_DAILY_CLOSE_FADE_DEFAULT
    return DailyCloseFadeConfig(
        signal_minute=int(payload.get("signal_minute", default.signal_minute)),
        top_n=int(payload.get("top_n", default.top_n)),
        hold_minutes=int(payload.get("hold_minutes", default.hold_minutes)),
        entry_delay_minutes=int(payload.get("entry_delay_minutes", default.entry_delay_minutes)),
        entry_twap_minutes=int(payload.get("entry_twap_minutes", default.entry_twap_minutes)),
        score=str(payload.get("score", default.score)),
        pump_filter=str(payload.get("pump_filter", default.pump_filter)),
        min_age_days=int(payload.get("min_age_days", default.min_age_days)),
        min_day_turnover=float(payload.get("min_day_turnover", default.min_day_turnover)),
        min_last_60m_turnover=float(payload.get("min_last_60m_turnover", default.min_last_60m_turnover)),
        vol_lookback_days=int(payload.get("vol_lookback_days", default.vol_lookback_days)),
        liquidity_lookback_days=int(payload.get("liquidity_lookback_days", default.liquidity_lookback_days)),
        liquidity_rank_min=int(payload.get("liquidity_rank_min", default.liquidity_rank_min)),
        liquidity_rank_max=int(payload.get("liquidity_rank_max", default.liquidity_rank_max)),
        min_baseline_turnover=float(payload.get("min_baseline_turnover", default.min_baseline_turnover)),
        account_equity=float(payload.get("account_equity", default.account_equity)),
        max_position_weight=float(payload.get("max_position_weight", default.max_position_weight)),
        max_trade_notional_pct_of_day_turnover=float(
            payload.get("max_trade_notional_pct_of_day_turnover", default.max_trade_notional_pct_of_day_turnover)
        ),
        max_trade_notional_pct_of_baseline_turnover=float(
            payload.get(
                "max_trade_notional_pct_of_baseline_turnover",
                default.max_trade_notional_pct_of_baseline_turnover,
            )
        ),
        market_impact_bps_per_1pct_turnover=float(
            payload.get("market_impact_bps_per_1pct_turnover", default.market_impact_bps_per_1pct_turnover)
        ),
        gross_exposure=float(payload.get("gross_exposure", default.gross_exposure)),
        coin_excess_vs_market_min=float(payload.get("coin_excess_vs_market_min", default.coin_excess_vs_market_min)),
        coin_vwap_extension_min=float(payload.get("coin_vwap_extension_min", default.coin_vwap_extension_min)),
        coin_vwap_extension_max=float(payload.get("coin_vwap_extension_max", default.coin_vwap_extension_max)),
        coin_late_volume_ratio_min=float(payload.get("coin_late_volume_ratio_min", default.coin_late_volume_ratio_min)),
        market_median_day_return_max=float(
            payload.get("market_median_day_return_max", default.market_median_day_return_max)
        ),
        organic_move_score_max=float(payload.get("organic_move_score_max", default.organic_move_score_max)),
        manipulation_risk_score_min=float(
            payload.get("manipulation_risk_score_min", default.manipulation_risk_score_min)
        ),
        squeeze_risk_score_max=float(payload.get("squeeze_risk_score_max", default.squeeze_risk_score_max)),
        require_funding_context=bool(payload.get("require_funding_context", default.require_funding_context)),
        require_open_interest_context=bool(
            payload.get("require_open_interest_context", default.require_open_interest_context)
        ),
        require_premium_context=bool(payload.get("require_premium_context", default.require_premium_context)),
        require_trade_flow_context=bool(
            payload.get("require_trade_flow_context", default.require_trade_flow_context)
        ),
        require_all_context=bool(payload.get("require_all_context", default.require_all_context)),
        position_sizing=str(payload.get("position_sizing", default.position_sizing)),
        score_weight_power=float(payload.get("score_weight_power", default.score_weight_power)),
        stop_loss_pct=float(payload.get("stop_loss_pct", default.stop_loss_pct)),
        take_profit_pct=float(payload.get("take_profit_pct", default.take_profit_pct)),
        time_decay_take_profit_floor_pct=float(
            payload.get("time_decay_take_profit_floor_pct", default.time_decay_take_profit_floor_pct)
        ),
        time_decay_take_profit_minutes=int(
            payload.get("time_decay_take_profit_minutes", default.time_decay_take_profit_minutes)
        ),
        basket_stop_loss_pct=float(payload.get("basket_stop_loss_pct", default.basket_stop_loss_pct)),
        trailing_stop_pct=float(payload.get("trailing_stop_pct", default.trailing_stop_pct)),
        trailing_activation_pct=float(payload.get("trailing_activation_pct", default.trailing_activation_pct)),
        vol_trailing_stop_mult=float(payload.get("vol_trailing_stop_mult", default.vol_trailing_stop_mult)),
        vol_trailing_activation_mult=float(payload.get("vol_trailing_activation_mult", default.vol_trailing_activation_mult)),
        mfe_giveback_activation_pct=float(
            payload.get("mfe_giveback_activation_pct", default.mfe_giveback_activation_pct)
        ),
        mfe_giveback_pct=float(payload.get("mfe_giveback_pct", default.mfe_giveback_pct)),
        vwap_reversion_pct=float(payload.get("vwap_reversion_pct", default.vwap_reversion_pct)),
        stop_delay_minutes=int(payload.get("stop_delay_minutes", default.stop_delay_minutes)),
        profit_protection_delay_minutes=(
            int(payload["profit_protection_delay_minutes"])
            if payload.get("profit_protection_delay_minutes") is not None
            else default.profit_protection_delay_minutes
        ),
        twap_stop_adding_pct=float(payload.get("twap_stop_adding_pct", default.twap_stop_adding_pct)),
        cost_multiplier=float(payload.get("cost_multiplier", default.cost_multiplier)),
        min_symbols=int(payload.get("min_symbols", default.min_symbols)),
        include_symbols=_tuple_str(payload, "include_symbols", default.include_symbols),
        exclude_symbols=_tuple_str(payload, "exclude_symbols", default.exclude_symbols),
        require_archive_membership=bool(payload.get("require_archive_membership", default.require_archive_membership)),
    )


def _merge_daily_close_fade_grid_config(payload: dict[str, Any] | None) -> DailyCloseFadeGridConfig:
    payload = dict(payload or {})
    return DailyCloseFadeGridConfig(
        signal_minutes=_tuple_int(payload, "signal_minutes", DEFAULT_CLOSE_FADE_SIGNAL_MINUTES),
        top_ns=_tuple_int(payload, "top_ns", DEFAULT_CLOSE_FADE_TOP_NS),
        hold_minutes=_tuple_int(payload, "hold_minutes", DEFAULT_CLOSE_FADE_HOLD_MINUTES),
        gross_exposures=_tuple_float(payload, "gross_exposures", (1.0,)),
        scores=_tuple_str(payload, "scores", ("vol_adjusted_day_return", "context_fade_score", "day_return")),
        pump_filters=_tuple_str(payload, "pump_filters", ("all", "pump", "non_pump")),
        stop_loss_pcts=_tuple_float(payload, "stop_loss_pcts", (0.0, 0.03, 0.05, 0.08)),
        take_profit_pcts=_tuple_float(payload, "take_profit_pcts", (0.0,)),
        time_decay_take_profit_floor_pcts=_tuple_float(payload, "time_decay_take_profit_floor_pcts", (0.0,)),
        time_decay_take_profit_minutes=_tuple_int(payload, "time_decay_take_profit_minutes", (0,)),
        basket_stop_loss_pcts=_tuple_float(payload, "basket_stop_loss_pcts", (0.0,)),
        trailing_stop_pcts=_tuple_float(payload, "trailing_stop_pcts", (0.0, 0.015, 0.025)),
        trailing_activation_pcts=_tuple_float(payload, "trailing_activation_pcts", (0.01, 0.02)),
        vol_trailing_stop_mults=_tuple_float(payload, "vol_trailing_stop_mults", (0.0,)),
        vol_trailing_activation_mults=_tuple_float(payload, "vol_trailing_activation_mults", (0.0,)),
        mfe_giveback_activation_pcts=_tuple_float(payload, "mfe_giveback_activation_pcts", (0.0,)),
        mfe_giveback_pcts=_tuple_float(payload, "mfe_giveback_pcts", (0.0,)),
        vwap_reversion_pcts=_tuple_float(payload, "vwap_reversion_pcts", (0.0,)),
        profit_protection_delay_minutes=_tuple_int(payload, "profit_protection_delay_minutes", (15,)),
        liquidity_lookback_days=_tuple_int(payload, "liquidity_lookback_days", (7,)),
        liquidity_rank_mins=_tuple_int(payload, "liquidity_rank_mins", (1,)),
        liquidity_rank_maxs=_tuple_int(payload, "liquidity_rank_maxs", (0,)),
        min_baseline_turnovers=_tuple_float(payload, "min_baseline_turnovers", (0.0,)),
        coin_vwap_extension_maxs=_tuple_float(payload, "coin_vwap_extension_maxs", (0.0,)),
        market_median_day_return_maxs=_tuple_float(payload, "market_median_day_return_maxs", (0.0,)),
        organic_move_score_maxs=_tuple_float(payload, "organic_move_score_maxs", (0.0,)),
        manipulation_risk_score_mins=_tuple_float(payload, "manipulation_risk_score_mins", (0.0,)),
        squeeze_risk_score_maxs=_tuple_float(payload, "squeeze_risk_score_maxs", (0.0,)),
        require_open_interest_contexts=_tuple_bool(payload, "require_open_interest_contexts", (False,)),
        require_all_contexts=_tuple_bool(payload, "require_all_contexts", (False,)),
        account_equities=_tuple_float(payload, "account_equities", (10_000.0,)),
        max_position_weights=_tuple_float(payload, "max_position_weights", (0.0,)),
        max_trade_notional_pct_day_turnovers=_tuple_float(
            payload, "max_trade_notional_pct_day_turnovers", (0.0,)
        ),
        max_trade_notional_pct_baseline_turnovers=_tuple_float(
            payload, "max_trade_notional_pct_baseline_turnovers", (0.0,)
        ),
        market_impact_bps_per_1pct_turnovers=_tuple_float(
            payload, "market_impact_bps_per_1pct_turnovers", (1.0,)
        ),
        cost_multipliers=_tuple_float(payload, "cost_multipliers", (1.0, 2.0, 3.0)),
        start_ms=int(payload.get("start_ms", 0)),
        end_ms=int(payload.get("end_ms", 0)),
    )


def _merge_forward_test_config(payload: dict[str, Any] | None) -> ForwardTestConfig:
    payload = dict(payload or {})
    return ForwardTestConfig(
        name=str(payload.get("name", "daily-close-fade-paper")),
        min_turnover_24h=float(payload.get("min_turnover_24h", 2_000_000.0)),
        max_spread_bps=float(payload.get("max_spread_bps", 80.0)),
        min_open_interest_value=float(payload.get("min_open_interest_value", 0.0)),
        max_symbols=int(payload.get("max_symbols", 0)),
        workers=int(payload.get("workers", 16)),
        max_entry_lag_minutes=int(payload.get("max_entry_lag_minutes", 10)),
        send_telegram=bool(payload.get("send_telegram", False)),
    )


def _merge_universe_config(payload: dict[str, Any] | None) -> UniverseConfig:
    payload = dict(payload or {})
    return UniverseConfig(
        min_turnover_24h=float(payload.get("min_turnover_24h", 2_000_000.0)),
        min_age_days=int(payload.get("min_age_days", 30)),
        max_age_days=int(payload.get("max_age_days", 0)),
        rank_start=int(payload.get("rank_start", 1)),
        rank_end=int(payload.get("rank_end", 120)),
        max_symbols=int(payload.get("max_symbols", 120)),
        exclude_symbols=_tuple_str(payload, "exclude_symbols", DEFAULT_MAJOR_SYMBOLS),
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

    root = Path(data_root or raw.get("data_root") or "data/volume_alpha")
    return ResearchConfig(
        exchange=_merge_dataclass(ExchangeConfig, raw.get("exchange")),
        trade_flow=_merge_dataclass(TradeFlowConfig, raw.get("trade_flow")),
        volume_alpha=_merge_volume_alpha_config(raw.get("volume_alpha")),
        volume_backtest=_merge_volume_backtest_config(raw.get("volume_backtest")),
        volume_grid=_merge_volume_grid_config(raw.get("volume_grid")),
        daily_close_fade=_merge_daily_close_fade_config(raw.get("daily_close_fade")),
        daily_close_fade_grid=_merge_daily_close_fade_grid_config(raw.get("daily_close_fade_grid")),
        forward_test=_merge_forward_test_config(raw.get("forward_test")),
        universe=_merge_universe_config(raw.get("universe")),
        costs=_merge_dataclass(CostConfig, raw.get("cost_model")),
        data_root=root,
    )
