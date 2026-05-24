"""Cross-sectional momentum sleeve — backtest entry, report, run label.

Long-only counterpart to the event-driven short sleeve in `volume_events.py`.
Design spec: `docs/cross_sectional_momentum_proposal.md`.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from ._common import MS_PER_DAY, MS_PER_HOUR, date_ms, pct
from .config import CostConfig, DEFAULT_EXCLUDED_SYMBOLS, TradeLifecycleConfig
from .momentum_events import (
    EXIT_DATA_END,
    MomentumEventsConfig,
    detect_entry_events,
    exit_reason_for_position,
)
from .momentum_signals import (
    RANKER_CLENOW,
    RANKERS,
    MomentumSignalsConfig,
    build_momentum_features,
)
from .storage import read_dataset, read_dataset_columns
from .trade_lifecycle import (
    _funding_lookup,
    _perp_funding_return,
    _price_bars_by_symbol,
    build_equity_curve,
    summarize_baskets,
    summarize_trade_backtest,
)
# These private helpers are stable across the package and reused by the
# strategy_tribunal and report tooling; importing them here keeps the PIT and
# run-label semantics identical between the short and momentum sleeves.
from .volume_events import (
    _date_range,
    _exclude_symbols,
    _full_pit_universe_error,
    _full_pit_universe_pass,
    _iso_date,
    _iso_month,
    _pit_manifest_metadata,
)


SPLITS = (
    ("train_2023_2024", "2023-05-03", "2024-05-03"),
    ("validation_2024_2025", "2024-05-03", "2025-05-03"),
    ("oos_2025_2026", "2025-05-03", "2026-05-03"),
)

POSITION_SIZING_EQUAL = "equal"
POSITION_SIZING_INVERSE_VOL = "inverse_vol"
POSITION_SIZINGS = (POSITION_SIZING_EQUAL, POSITION_SIZING_INVERSE_VOL)


@dataclass(frozen=True, slots=True)
class CrossSectionalMomentumConfig:
    # --- universe / ranker (passed through to MomentumSignalsConfig) ---
    liquidity_tier_size: int = 30
    liquidity_volume_window_days: int = 90
    min_listing_history_days: int = 180
    ranker: str = RANKER_CLENOW
    ranker_lookback_days: int = 90
    vol_short_window_days: int = 30
    vol_long_window_days: int = 90
    atr_window_days: int = 30
    breakout_window_days: int = 60
    sma_trend_break_days: int = 100
    sma_regime_days: int = 200
    regime_symbol: str = "BTCUSDT"
    vol_floor_annual: float = 0.30
    coil_release_min_compress_days: int = 7
    funding_overheat_window_days: int = 90
    funding_overheat_percentile: float = 0.95

    # --- events (passed through to MomentumEventsConfig) ---
    rank_entry_min_norm: float = 0.75
    rank_exit_max_norm: float = 0.50
    vol_shock_multiple: float = 3.0
    trailing_atr_multiple: float = 4.0
    require_regime_on_entry: bool = True
    require_funding_not_overheated: bool = True
    require_coil_release: bool = True
    require_breakout: bool = True

    # --- portfolio / lifecycle ---
    max_concurrent_positions: int = 8
    gross_exposure: float = 1.0
    position_sizing: str = POSITION_SIZING_EQUAL
    position_weight_min: float = 0.5
    position_weight_max: float = 2.0
    cooldown_days: int = 0
    entry_delay_hours: int = 1
    cost_multiplier: float = 3.0

    # --- gates / run controls ---
    start_date: str = ""
    end_date: str = ""
    require_pit_membership: bool = True
    require_full_pit_universe: bool = True
    promotion_max_drawdown: float = -0.25
    promotion_min_avg_sharpe: float = 0.50
    exclude_symbols: tuple[str, ...] = DEFAULT_EXCLUDED_SYMBOLS


def run_cross_sectional_momentum_research(
    data_root: str | Path,
    *,
    config: CrossSectionalMomentumConfig | None = None,
    cost_config: CostConfig | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    cfg = config or CrossSectionalMomentumConfig()
    costs = cost_config or CostConfig()
    _validate_config(cfg)
    root = Path(data_root).expanduser()
    output_dir = (
        Path(report_dir)
        if report_dir
        else root / "reports" / "cross_sectional_momentum_research"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_klines = read_dataset_columns(
        root,
        "klines_1h",
        columns=["ts_ms", "symbol", "date", "open", "high", "low", "close", "turnover_quote", "volume_base"],
    )
    if raw_klines.is_empty():
        raise RuntimeError("klines_1h is empty; run download-data first")
    funding = read_dataset(root, "funding")
    archive_manifest = read_dataset(root, "archive_trade_manifest")

    klines = _exclude_symbols(raw_klines, cfg.exclude_symbols)
    funding = _exclude_symbols(funding, cfg.exclude_symbols)
    archive_manifest = _exclude_symbols(archive_manifest, cfg.exclude_symbols)

    full_pit_universe_pass = _full_pit_universe_pass(klines, archive_manifest)
    if cfg.require_full_pit_universe and not full_pit_universe_pass:
        raise RuntimeError(_full_pit_universe_error(klines, archive_manifest))

    signals_config = _signals_config_from(cfg)
    features = build_momentum_features(klines, funding=funding, config=signals_config)
    features = _filter_signal_window(features, start=cfg.start_date, end=cfg.end_date)
    if features.is_empty():
        raise RuntimeError("No features generated; check data root coverage and config window.")

    events_config = _events_config_from(cfg)
    entry_events = detect_entry_events(features, config=events_config)

    bars_by_symbol = _price_bars_by_symbol(klines)
    funding_lookup = _funding_lookup(funding) if funding is not None and not funding.is_empty() else None

    trades, lifecycle_stats = _run_pipeline(
        features=features,
        entry_events=entry_events,
        bars_by_symbol=bars_by_symbol,
        funding_lookup=funding_lookup,
        config=cfg,
        events_config=events_config,
        costs=costs,
    )

    bt_config = TradeLifecycleConfig(
        score="momentum_rank_norm",
        hold_days=30,  # informational only; momentum exits are event-driven
        rebalance_days=30,
        gross_exposure=cfg.gross_exposure,
        entry_delay_hours=cfg.entry_delay_hours,
        stop_mode="trailing_atr",
        cost_multiplier=cfg.cost_multiplier,
        side_mode="long_high_short_low",
    )
    baskets = summarize_baskets(trades, config=bt_config)
    equity = build_equity_curve(baskets)
    summary_metrics = summarize_trade_backtest(trades, baskets, equity, config=bt_config)
    monthly = _monthly_returns(baskets)
    split_rows = _split_rows(baskets, config=bt_config)
    funding_mode = summary_metrics.get("funding_mode", "missing")

    promotion = _evaluate_promotion(
        split_rows=split_rows,
        summary=summary_metrics,
        funding_mode=funding_mode,
        full_pit_universe_pass=full_pit_universe_pass,
        config=cfg,
    )

    if not trades.is_empty():
        trades.write_csv(output_dir / "cross_sectional_momentum_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "cross_sectional_momentum_baskets.csv")
    if not equity.is_empty():
        equity.write_csv(output_dir / "cross_sectional_momentum_equity.csv")
    if not monthly.is_empty():
        monthly.write_csv(output_dir / "cross_sectional_momentum_monthly.csv")

    metadata = {
        "config": asdict(cfg),
        "rows": {
            "features": features.height,
            "entry_candidates": entry_events.height,
            "trades": trades.height,
            "baskets": baskets.height,
        },
        "date_range": _date_range(features),
        "pit_manifest": _pit_manifest_metadata(archive_manifest, features, klines),
        "cost_model": {
            **asdict(costs),
            "base_round_trip_cost_bps": costs.base_entry_exit_cost_bps,
            "cost_multiplier": cfg.cost_multiplier,
            "effective_round_trip_cost_bps": costs.base_entry_exit_cost_bps * cfg.cost_multiplier,
        },
        "summary": summary_metrics,
        "lifecycle": lifecycle_stats,
        "splits": split_rows,
        "promotion": promotion,
        "run_label": _run_label(
            config=cfg,
            archive_manifest=archive_manifest,
            full_pit_universe_pass=full_pit_universe_pass,
            funding_mode=funding_mode,
        ),
        "promotion_note": _promotion_note(
            archive_manifest=archive_manifest,
            full_pit_universe_pass=full_pit_universe_pass,
            funding_mode=funding_mode,
        ),
    }
    (output_dir / "cross_sectional_momentum_research_report.json").write_text(
        json.dumps(metadata, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "cross_sectional_momentum_research_report.md").write_text(
        format_momentum_report(metadata),
        encoding="utf-8",
    )
    return {**metadata, "report_dir": str(output_dir)}


def _validate_config(cfg: CrossSectionalMomentumConfig) -> None:
    if cfg.ranker not in RANKERS:
        raise ValueError(f"ranker must be one of {RANKERS}")
    if cfg.position_sizing not in POSITION_SIZINGS:
        raise ValueError(f"position_sizing must be one of {POSITION_SIZINGS}")
    if cfg.max_concurrent_positions <= 0:
        raise ValueError("max_concurrent_positions must be positive")
    if cfg.gross_exposure <= 0.0:
        raise ValueError("gross_exposure must be positive")
    if cfg.entry_delay_hours < 0:
        raise ValueError("entry_delay_hours must be non-negative")
    if cfg.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    if not 0.0 < cfg.rank_entry_min_norm <= 1.0:
        raise ValueError("rank_entry_min_norm must be in (0, 1]")
    if not 0.0 <= cfg.rank_exit_max_norm < cfg.rank_entry_min_norm:
        raise ValueError("rank_exit_max_norm must be in [0, rank_entry_min_norm)")
    if cfg.position_weight_min <= 0.0 or cfg.position_weight_max < cfg.position_weight_min:
        raise ValueError("position_weight_min/max must satisfy 0 < min <= max")
    if cfg.cost_multiplier < 0.0:
        raise ValueError("cost_multiplier must be non-negative")


def _signals_config_from(cfg: CrossSectionalMomentumConfig) -> MomentumSignalsConfig:
    return MomentumSignalsConfig(
        liquidity_tier_size=cfg.liquidity_tier_size,
        liquidity_volume_window_days=cfg.liquidity_volume_window_days,
        min_listing_history_days=cfg.min_listing_history_days,
        ranker=cfg.ranker,
        ranker_lookback_days=cfg.ranker_lookback_days,
        vol_short_window_days=cfg.vol_short_window_days,
        vol_long_window_days=cfg.vol_long_window_days,
        atr_window_days=cfg.atr_window_days,
        breakout_window_days=cfg.breakout_window_days,
        sma_trend_break_days=cfg.sma_trend_break_days,
        sma_regime_days=cfg.sma_regime_days,
        regime_symbol=cfg.regime_symbol,
        vol_floor_annual=cfg.vol_floor_annual,
        coil_release_min_compress_days=cfg.coil_release_min_compress_days,
        funding_overheat_window_days=cfg.funding_overheat_window_days,
        funding_overheat_percentile=cfg.funding_overheat_percentile,
        rank_threshold_quantile=cfg.rank_entry_min_norm,
        rank_exit_quantile=cfg.rank_exit_max_norm,
        vol_shock_multiple=cfg.vol_shock_multiple,
    )


def _events_config_from(cfg: CrossSectionalMomentumConfig) -> MomentumEventsConfig:
    return MomentumEventsConfig(
        rank_entry_min_norm=cfg.rank_entry_min_norm,
        rank_exit_max_norm=cfg.rank_exit_max_norm,
        breakout_window_days=cfg.breakout_window_days,
        sma_trend_break_days=cfg.sma_trend_break_days,
        atr_window_days=cfg.atr_window_days,
        vol_shock_multiple=cfg.vol_shock_multiple,
        trailing_atr_multiple=cfg.trailing_atr_multiple,
        require_regime_on_entry=cfg.require_regime_on_entry,
        require_funding_not_overheated=cfg.require_funding_not_overheated,
        require_coil_release=cfg.require_coil_release,
        require_breakout=cfg.require_breakout,
    )


def _filter_signal_window(features: pl.DataFrame, *, start: str, end: str) -> pl.DataFrame:
    if features.is_empty():
        return features
    if start:
        features = features.filter(pl.col("ts_ms") >= date_ms(start))
    if end:
        features = features.filter(pl.col("ts_ms") < date_ms(end))
    return features


def _run_pipeline(
    *,
    features: pl.DataFrame,
    entry_events: pl.DataFrame,
    bars_by_symbol: dict[str, dict[str, Any]],
    funding_lookup: dict[str, dict[str, Any]] | None,
    config: CrossSectionalMomentumConfig,
    events_config: MomentumEventsConfig,
    costs: CostConfig,
) -> tuple[pl.DataFrame, dict[str, int]]:
    bars_index: dict[str, dict[str, Any]] = {}
    for symbol, arrays in bars_by_symbol.items():
        ends = arrays["bar_end_ts_ms"].tolist()
        bars_index[symbol] = {
            **arrays,
            "ends": ends,
            "by_end": {end_ts: idx for idx, end_ts in enumerate(ends)},
        }

    dates = sorted(int(ts) for ts in features["ts_ms"].unique().to_list())
    features_by_date: dict[int, list[dict[str, Any]]] = {}
    for part in features.partition_by("ts_ms", maintain_order=True):
        ts = int(part["ts_ms"][0])
        features_by_date[ts] = part.to_dicts()

    entries_by_date: dict[int, list[dict[str, Any]]] = {}
    if not entry_events.is_empty():
        for part in entry_events.partition_by("ts_ms", maintain_order=True):
            ts = int(part["ts_ms"][0])
            entries_by_date[ts] = (
                part.sort("rank_norm", descending=True).to_dicts()
            )

    open_positions: dict[str, dict[str, Any]] = {}
    cooldown_until: dict[str, int] = {}
    trade_rows: list[dict[str, Any]] = []
    stats = {
        "skipped_no_entry_bar": 0,
        "skipped_capacity": 0,
        "skipped_cooldown": 0,
        "skipped_already_held": 0,
        "force_closed_at_end": 0,
        "force_closed_missing_data": 0,
    }
    notional_weight = config.gross_exposure / max(config.max_concurrent_positions, 1)
    round_trip_cost_bps = costs.base_entry_exit_cost_bps * config.cost_multiplier

    for ts in dates:
        today_rows = {row["symbol"]: row for row in features_by_date.get(ts, [])}

        # Exits first.
        for symbol in list(open_positions.keys()):
            pos = open_positions[symbol]
            today = today_rows.get(symbol)
            if today is None:
                continue  # No data today; hold.
            close = today.get("close")
            if close is not None and math.isfinite(float(close)):
                pos["high_water_close"] = max(pos["high_water_close"], float(close))
            reason = exit_reason_for_position(today, high_water_close=pos["high_water_close"], config=events_config)
            if reason is not None:
                trade = _finalize_trade(
                    pos=pos,
                    exit_day_ts_ms=ts,
                    exit_price=float(close) if close is not None else pos["entry_price"],
                    reason=reason,
                    notional_weight=notional_weight,
                    round_trip_cost_bps=round_trip_cost_bps,
                    funding_lookup=funding_lookup,
                )
                trade_rows.append(trade)
                cooldown_until[symbol] = ts + config.cooldown_days * MS_PER_DAY
                del open_positions[symbol]

        # Entries (subject to capacity).
        for candidate in entries_by_date.get(ts, []):
            symbol = str(candidate["symbol"])
            if symbol in open_positions:
                stats["skipped_already_held"] += 1
                continue
            if cooldown_until.get(symbol, 0) > ts:
                stats["skipped_cooldown"] += 1
                continue
            if len(open_positions) >= config.max_concurrent_positions:
                stats["skipped_capacity"] += 1
                continue
            entry_ts_ms = ts + config.entry_delay_hours * MS_PER_HOUR
            entry_idx, entry_price = _entry_bar(bars_index.get(symbol), entry_ts_ms)
            if entry_idx is None or entry_price is None:
                stats["skipped_no_entry_bar"] += 1
                continue
            position_weight = _position_weight(candidate, config=config)
            open_positions[symbol] = {
                "symbol": symbol,
                "entry_signal_ts_ms": int(ts),
                "entry_ts_ms": int(entry_ts_ms),
                "entry_price": float(entry_price),
                "entry_bar_idx": int(entry_idx),
                "high_water_close": float(entry_price),
                "position_weight": float(position_weight),
                "score": float(candidate.get("clenow_score") or candidate.get("sharpe_score") or 0.0),
                "rank_norm": float(candidate.get("rank_norm") or 0.0),
                "vol_estimate": float(candidate.get(f"realized_vol_{config.vol_long_window_days}d") or 0.0),
                "basket_id": f"momentum-{_iso_date(int(ts))}-{symbol}",
            }

    # Force-close any still-open positions at the last feature date.
    if open_positions:
        last_ts = dates[-1] if dates else 0
        last_row_by_symbol = {row["symbol"]: row for row in features_by_date.get(last_ts, [])}
        for symbol, pos in list(open_positions.items()):
            today = last_row_by_symbol.get(symbol)
            if today is None:
                # Pull the last known close from the symbol's bars.
                bars = bars_index.get(symbol)
                if bars is None or len(bars["close"]) == 0:
                    stats["force_closed_missing_data"] += 1
                    del open_positions[symbol]
                    continue
                exit_price = float(bars["close"][-1])
                exit_ts = int(bars["bar_end_ts_ms"][-1])
            else:
                exit_price = float(today.get("close") or pos["entry_price"])
                exit_ts = int(today["ts_ms"])
            trade = _finalize_trade(
                pos=pos,
                exit_day_ts_ms=exit_ts,
                exit_price=exit_price,
                reason=EXIT_DATA_END,
                notional_weight=notional_weight,
                round_trip_cost_bps=round_trip_cost_bps,
                funding_lookup=funding_lookup,
            )
            trade_rows.append(trade)
            stats["force_closed_at_end"] += 1
            del open_positions[symbol]

    trades = (
        pl.DataFrame(trade_rows, infer_schema_length=None).sort(["entry_ts_ms", "symbol"])
        if trade_rows
        else _empty_momentum_trades()
    )
    return trades, stats


def _entry_bar(
    bars: dict[str, Any] | None, entry_ts_ms: int
) -> tuple[int | None, float | None]:
    if bars is None:
        return None, None
    idx = bars["by_end"].get(int(entry_ts_ms))
    if idx is None:
        return None, None
    price = float(bars["close"][idx])
    if not math.isfinite(price) or price <= 0.0:
        return None, None
    return int(idx), price


def _position_weight(candidate: dict[str, Any], *, config: CrossSectionalMomentumConfig) -> float:
    if config.position_sizing == POSITION_SIZING_EQUAL:
        return 1.0
    # inverse_vol
    vol_col = f"realized_vol_{config.vol_long_window_days}d"
    vol_estimate = candidate.get(vol_col)
    if vol_estimate is None or not math.isfinite(float(vol_estimate)) or float(vol_estimate) <= 0.0:
        return 1.0
    vol_used = max(float(vol_estimate), config.vol_floor_annual)
    # 1.0 corresponds to a coin at exactly `vol_floor`; lower vol → larger weight, capped.
    raw = config.vol_floor_annual / vol_used
    return float(min(max(raw, config.position_weight_min), config.position_weight_max))


def _finalize_trade(
    *,
    pos: dict[str, Any],
    exit_day_ts_ms: int,
    exit_price: float,
    reason: str,
    notional_weight: float,
    round_trip_cost_bps: float,
    funding_lookup: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    side = "long"
    entry_price = float(pos["entry_price"])
    gross_trade_return = exit_price / entry_price - 1.0 if entry_price > 0.0 else 0.0
    raw_funding_return, funding_mode, funding_event_count = _perp_funding_return(
        funding_lookup,
        symbol=pos["symbol"],
        side=side,
        entry_ts_ms=int(pos["entry_ts_ms"]),
        exit_ts_ms=int(exit_day_ts_ms),
    )
    effective_weight = notional_weight * float(pos["position_weight"])
    funding_return = abs(effective_weight) * raw_funding_return
    cost_return = -abs(effective_weight) * round_trip_cost_bps / 10_000.0
    gross_return = abs(effective_weight) * gross_trade_return
    net_return = gross_return + cost_return + funding_return
    bars_held = max(
        int(round((int(exit_day_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR)),
        0,
    )
    return {
        "trade_id": f"{pos['basket_id']}-l-{pos['symbol']}",
        "basket_id": pos["basket_id"],
        "entry_signal_ts_ms": int(pos["entry_signal_ts_ms"]),
        "entry_ts_ms": int(pos["entry_ts_ms"]),
        "exit_ts_ms": int(exit_day_ts_ms),
        "entry_date": _iso_date(int(pos["entry_ts_ms"])),
        "exit_date": _iso_date(int(exit_day_ts_ms)),
        "exit_month": _iso_month(int(exit_day_ts_ms)),
        "symbol": pos["symbol"],
        "side": side,
        "score": float(pos.get("score", 0.0)),
        "rank": 0,
        "entry_price": entry_price,
        "exit_price": float(exit_price),
        "exit_reason": reason,
        "planned_exit_ts_ms": 0,
        "stop_price": None,
        "take_profit_price": None,
        "notional_weight": abs(effective_weight),
        "position_weight": float(pos["position_weight"]),
        "gross_trade_return": gross_trade_return,
        "gross_return": gross_return,
        "cost_return": cost_return,
        "funding_return": funding_return,
        "funding_mode": funding_mode,
        "funding_event_count": int(funding_event_count),
        "net_return": net_return,
        "mae": 0.0,
        "mfe": 0.0,
        "bars_held": bars_held,
        "hold_hours": (int(exit_day_ts_ms) - int(pos["entry_ts_ms"])) / MS_PER_HOUR,
        "actual_entry_delay_hours": (int(pos["entry_ts_ms"]) - int(pos["entry_signal_ts_ms"])) / MS_PER_HOUR,
        "rank_norm": float(pos.get("rank_norm", 0.0)),
        "vol_estimate": float(pos.get("vol_estimate", 0.0)),
    }


def _empty_momentum_trades() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "trade_id": pl.Series([], dtype=pl.String),
            "basket_id": pl.Series([], dtype=pl.String),
            "entry_signal_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_ts_ms": pl.Series([], dtype=pl.Int64),
            "exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "entry_date": pl.Series([], dtype=pl.String),
            "exit_date": pl.Series([], dtype=pl.String),
            "exit_month": pl.Series([], dtype=pl.String),
            "symbol": pl.Series([], dtype=pl.String),
            "side": pl.Series([], dtype=pl.String),
            "score": pl.Series([], dtype=pl.Float64),
            "rank": pl.Series([], dtype=pl.Int64),
            "entry_price": pl.Series([], dtype=pl.Float64),
            "exit_price": pl.Series([], dtype=pl.Float64),
            "exit_reason": pl.Series([], dtype=pl.String),
            "planned_exit_ts_ms": pl.Series([], dtype=pl.Int64),
            "stop_price": pl.Series([], dtype=pl.Float64),
            "take_profit_price": pl.Series([], dtype=pl.Float64),
            "notional_weight": pl.Series([], dtype=pl.Float64),
            "position_weight": pl.Series([], dtype=pl.Float64),
            "gross_trade_return": pl.Series([], dtype=pl.Float64),
            "gross_return": pl.Series([], dtype=pl.Float64),
            "cost_return": pl.Series([], dtype=pl.Float64),
            "funding_return": pl.Series([], dtype=pl.Float64),
            "funding_mode": pl.Series([], dtype=pl.String),
            "funding_event_count": pl.Series([], dtype=pl.Int64),
            "net_return": pl.Series([], dtype=pl.Float64),
            "mae": pl.Series([], dtype=pl.Float64),
            "mfe": pl.Series([], dtype=pl.Float64),
            "bars_held": pl.Series([], dtype=pl.Int64),
            "hold_hours": pl.Series([], dtype=pl.Float64),
            "actual_entry_delay_hours": pl.Series([], dtype=pl.Float64),
            "rank_norm": pl.Series([], dtype=pl.Float64),
            "vol_estimate": pl.Series([], dtype=pl.Float64),
        }
    )


def _split_rows(baskets: pl.DataFrame, *, config: TradeLifecycleConfig) -> list[dict[str, Any]]:
    rows = []
    for name, start, end in SPLITS:
        start_ms = date_ms(start)
        end_ms = date_ms(end)
        part = (
            baskets.filter((pl.col("entry_signal_ts_ms") >= start_ms) & (pl.col("entry_signal_ts_ms") < end_ms))
            if not baskets.is_empty()
            else baskets
        )
        rows.append(_summarize_split(part, name=name, config=config))
    return rows


def _summarize_split(part: pl.DataFrame, *, name: str, config: TradeLifecycleConfig) -> dict[str, Any]:
    if part.is_empty():
        return {
            "name": name,
            "basket_count": 0,
            "total_return": 0.0,
            "sharpe_like": 0.0,
            "max_drawdown": 0.0,
        }
    returns = np.asarray(part.sort("entry_signal_ts_ms")["basket_return"].to_list(), dtype=float)
    equity_curve = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(equity_curve)
    drawdowns = equity_curve / peaks - 1.0
    stdev = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    mean = float(np.mean(returns)) if returns.size else 0.0
    annual_periods = 365.0 / max(config.hold_days, 1)
    return {
        "name": name,
        "basket_count": int(returns.size),
        "total_return": float(equity_curve[-1] - 1.0),
        "sharpe_like": float(mean / stdev * math.sqrt(annual_periods)) if stdev > 1e-12 else 0.0,
        "max_drawdown": float(drawdowns.min()),
    }


def _evaluate_promotion(
    *,
    split_rows: list[dict[str, Any]],
    summary: dict[str, Any],
    funding_mode: str,
    full_pit_universe_pass: bool,
    config: CrossSectionalMomentumConfig,
) -> dict[str, Any]:
    splits_with_baskets = [row for row in split_rows if row["basket_count"] > 0]
    all_splits_positive = bool(splits_with_baskets) and all(
        row["total_return"] > 0.0 for row in splits_with_baskets
    )
    avg_sharpe = (
        float(np.mean([row["sharpe_like"] for row in splits_with_baskets]))
        if splits_with_baskets
        else 0.0
    )
    max_dd_ok = summary.get("max_drawdown", 0.0) >= config.promotion_max_drawdown
    sharpe_ok = avg_sharpe >= config.promotion_min_avg_sharpe
    funding_ok = funding_mode != "missing"
    reasons = []
    if not full_pit_universe_pass:
        reasons.append("full_pit_universe_missing")
    if not all_splits_positive:
        reasons.append("not_all_splits_positive")
    if not max_dd_ok:
        reasons.append("max_drawdown_too_deep")
    if not sharpe_ok:
        reasons.append("avg_split_sharpe_below_threshold")
    if not funding_ok:
        reasons.append("funding_missing")
    return {
        "promotion_gate_pass": bool(
            full_pit_universe_pass
            and all_splits_positive
            and max_dd_ok
            and sharpe_ok
            and funding_ok
        ),
        "all_splits_positive": all_splits_positive,
        "splits_with_baskets": len(splits_with_baskets),
        "avg_split_sharpe": avg_sharpe,
        "max_drawdown": float(summary.get("max_drawdown", 0.0)),
        "funding_mode": funding_mode,
        "promotion_reasons": reasons,
    }


def _monthly_returns(baskets: pl.DataFrame) -> pl.DataFrame:
    if baskets.is_empty():
        return pl.DataFrame(
            {
                "month": pl.Series([], dtype=pl.String),
                "strategy_return": pl.Series([], dtype=pl.Float64),
                "long_return": pl.Series([], dtype=pl.Float64),
                "cost_return": pl.Series([], dtype=pl.Float64),
                "funding_return": pl.Series([], dtype=pl.Float64),
                "baskets": pl.Series([], dtype=pl.Int64),
            }
        )
    return (
        baskets.with_columns(pl.from_epoch(pl.col("exit_ts_ms"), time_unit="ms").dt.strftime("%Y-%m").alias("month"))
        .group_by("month")
        .agg(
            [
                ((pl.col("basket_return") + 1.0).product() - 1.0).alias("strategy_return"),
                pl.col("long_return").sum().alias("long_return"),
                pl.col("cost_return").sum().alias("cost_return"),
                pl.col("funding_return").sum().alias("funding_return"),
                pl.len().alias("baskets"),
            ]
        )
        .sort("month")
    )


def _run_label(
    *,
    config: CrossSectionalMomentumConfig,
    archive_manifest: pl.DataFrame,
    full_pit_universe_pass: bool,
    funding_mode: str,
) -> str:
    if archive_manifest.is_empty():
        return "pit_required_missing_manifest" if config.require_pit_membership else "biased_benchmark"
    if not full_pit_universe_pass:
        return "pit_membership_filtered_current_universe"
    if funding_mode == "missing":
        return "full_pit_universe_funding_missing"
    if funding_mode == "partial":
        return "full_pit_universe_funding_partial"
    return "full_pit_universe"


def _promotion_note(
    *,
    archive_manifest: pl.DataFrame,
    full_pit_universe_pass: bool,
    funding_mode: str,
) -> str:
    if archive_manifest.is_empty():
        return (
            "No archive_trade_manifest is present; momentum sleeve is event-shape research only "
            "and cannot be promoted."
        )
    if not full_pit_universe_pass:
        return (
            "Archive membership is present, but the feature universe does not cover every manifest "
            "symbol/date. PIT-membership-filtered current-universe research only."
        )
    if funding_mode == "missing":
        return (
            "Full PIT universe passes, but funding data is not present on this root. Long-perp "
            "funding cost is unmodeled — treat returns as gross of funding."
        )
    if funding_mode == "partial":
        return (
            "Full PIT universe passes; funding data is partial on this root. Some trades carry "
            "funding cost, others don't — interpret with care."
        )
    return "Full PIT universe and funding present; suitable for split/promotion review."


def _config_hash(cfg: CrossSectionalMomentumConfig) -> str:
    payload = json.dumps(asdict(cfg), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def format_momentum_report(metadata: dict[str, Any]) -> str:
    cfg = metadata.get("config", {})
    pit = metadata.get("pit_manifest", {})
    summary = metadata.get("summary", {})
    promotion = metadata.get("promotion", {})
    splits = metadata.get("splits", [])
    lifecycle = metadata.get("lifecycle", {})
    date_range = metadata.get("date_range", {})

    lines: list[str] = [
        "# Cross-Sectional Momentum Research",
        "",
        "Long-only event-triggered cross-sectional momentum sleeve (Clenow-style "
        "slope×R² ranker, crypto-adapted). Counterpart to the event-driven short.",
        "",
        "## Inputs",
        "",
        f"- Run label: `{metadata.get('run_label', 'unknown')}`",
        f"- Date range: {date_range.get('start')} to {date_range.get('end')}",
        f"- Feature rows: {metadata.get('rows', {}).get('features', 0)}",
        f"- Entry candidates: {metadata.get('rows', {}).get('entry_candidates', 0)}",
        f"- Trades: {metadata.get('rows', {}).get('trades', 0)}",
        f"- Ranker: `{cfg.get('ranker')}` lookback {cfg.get('ranker_lookback_days')}d",
        f"- Universe: top {cfg.get('liquidity_tier_size')} by {cfg.get('liquidity_volume_window_days')}d median turnover",
        f"- Max concurrent positions: {cfg.get('max_concurrent_positions')}",
        f"- Cost multiplier: {cfg.get('cost_multiplier')}x",
        f"- Position sizing: `{cfg.get('position_sizing')}`",
        f"- PIT manifest rows: {pit.get('rows', 0)} | symbols: {pit.get('symbols', 0)}",
        f"- Feature symbols: {pit.get('feature_symbols', 0)} | full PIT: {pit.get('full_pit_universe_pass', False)}",
        f"- Promotion note: {metadata.get('promotion_note', '')}",
        "",
        "## Headline metrics",
        "",
        f"- Total return: {pct(summary.get('total_return'))}",
        f"- Sharpe-like: {summary.get('sharpe_like', 0.0):.2f}",
        f"- Max drawdown: {pct(summary.get('max_drawdown'))}",
        f"- Max underwater days: {summary.get('max_underwater_days', 0)}",
        f"- Worst 30d / 60d / 90d / 120d: {pct(summary.get('worst_30d_return'))} / "
        f"{pct(summary.get('worst_60d_return'))} / {pct(summary.get('worst_90d_return'))} / "
        f"{pct(summary.get('worst_120d_return'))}",
        f"- Trade win rate: {pct(summary.get('trade_win_rate'))}",
        f"- Profit factor: {summary.get('profit_factor', 0.0):.2f}",
        f"- Gross return: {pct(summary.get('gross_return'))} | cost: {pct(summary.get('cost_return'))} | "
        f"funding: {pct(summary.get('funding_return'))} ({summary.get('funding_mode', 'missing')})",
        "",
        "## Promotion gate",
        "",
        f"- Pass: **{promotion.get('promotion_gate_pass', False)}**",
        f"- All splits positive: {promotion.get('all_splits_positive', False)} "
        f"({promotion.get('splits_with_baskets', 0)}/{len(SPLITS)} with baskets)",
        f"- Avg split sharpe: {promotion.get('avg_split_sharpe', 0.0):.2f}",
        f"- Funding mode: `{promotion.get('funding_mode', 'missing')}`",
        f"- Block reasons: {', '.join(promotion.get('promotion_reasons', [])) or 'none'}",
        "",
        "## Splits",
        "",
        "| Split | Baskets | Return | Sharpe | Max DD |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in splits:
        lines.append(
            f"| {row.get('name')} | {row.get('basket_count', 0)} | "
            f"{pct(row.get('total_return'))} | {row.get('sharpe_like', 0.0):.2f} | "
            f"{pct(row.get('max_drawdown'))} |"
        )

    lines.extend(
        [
            "",
            "## Lifecycle counters",
            "",
            f"- Skipped: capacity={lifecycle.get('skipped_capacity', 0)}, "
            f"cooldown={lifecycle.get('skipped_cooldown', 0)}, "
            f"already_held={lifecycle.get('skipped_already_held', 0)}, "
            f"no_entry_bar={lifecycle.get('skipped_no_entry_bar', 0)}",
            f"- Force closed at end: {lifecycle.get('force_closed_at_end', 0)} | "
            f"missing data: {lifecycle.get('force_closed_missing_data', 0)}",
            "",
            "## Output files",
            "",
            "```text",
            "cross_sectional_momentum_trades.csv",
            "cross_sectional_momentum_baskets.csv",
            "cross_sectional_momentum_equity.csv",
            "cross_sectional_momentum_monthly.csv",
            "cross_sectional_momentum_research_report.json",
            "cross_sectional_momentum_research_report.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)
