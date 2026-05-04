from __future__ import annotations

import json
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from .bybit import BybitMarketData
from .config import DailyCloseFadeConfig, ForwardTestConfig, ResearchConfig
from .daily_close_fade import (
    MS_PER_DAY,
    MS_PER_MINUTE,
    attach_close_fade_coin_market_context,
    _close_fade_position_weight,
    _mfe_giveback_stop_price,
    _short_return,
    _vwap_reversion_exit_price,
    select_close_fade_candidates,
)
from .storage import dataset_path, read_dataset, write_dataset
from .telegram import send_telegram_message


EPSILON = 1e-12


def run_forward_scan(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig | None = None,
    forward_config: ForwardTestConfig | None = None,
    now: datetime | None = None,
    client: Any | None = None,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    fade = fade_config or config.daily_close_fade
    forward = forward_config or config.forward_test
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    now_ms = _floor_minute_ms(_ms_from_dt(now_dt))
    signal_ts_ms = _signal_ts_ms(now_dt, fade.signal_minute)
    scan_end_ms = min(now_ms, signal_ts_ms)
    day_start_ms = _day_start_ms(now_dt)
    scan_id = _scan_id(scan_end_ms, fade.signal_minute)

    market = client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    universe = build_forward_universe(
        market.get_instruments_info(),
        market.get_tickers(),
        now_ms=now_ms,
        fade_config=fade,
        forward_config=forward,
        settle_coin=config.exchange.settle_coin,
    )
    if forward.max_symbols > 0 and not universe.is_empty():
        universe = universe.head(forward.max_symbols)

    klines_1m, daily_klines, failures = _fetch_forward_bars(
        universe["symbol"].to_list() if not universe.is_empty() else [],
        config=config,
        client=market if client is not None else None,
        day_start_ms=day_start_ms,
        scan_end_ms=scan_end_ms,
        daily_start_ms=day_start_ms - max(fade.vol_lookback_days + 5, 7) * MS_PER_DAY,
        daily_end_ms=day_start_ms - 1,
        workers=forward.workers,
    )
    features = build_forward_scan_features(
        klines_1m,
        daily_klines,
        universe,
        fade_config=fade,
        signal_minute=fade.signal_minute,
        scan_id=scan_id,
    )
    candidates = select_close_fade_candidates(features, fade) if not features.is_empty() else pl.DataFrame()

    payload = {
        "scan_id": scan_id,
        "now": now_dt.isoformat(),
        "scan_end": _dt_from_ms(scan_end_ms).isoformat(),
        "signal_time": _dt_from_ms(signal_ts_ms).isoformat(),
        "status": _scan_status(now_ms, signal_ts_ms, features, candidates),
        "config": {"fade": asdict(fade), "forward": asdict(forward)},
        "rows": {
            "universe": universe.height,
            "features": features.height,
            "candidates": candidates.height,
            "fetch_failures": len(failures),
        },
        "candidates": candidates.to_dicts() if not candidates.is_empty() else [],
        "fetch_failures": failures,
    }
    _write_forward_scan_outputs(data_root, payload, features, candidates, report_dir=report_dir)
    return payload


def run_forward_once(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig | None = None,
    forward_config: ForwardTestConfig | None = None,
    now: datetime | None = None,
    client: Any | None = None,
    report_dir: str | Path | None = None,
    send_telegram: bool | None = None,
) -> dict[str, Any]:
    fade = fade_config or config.daily_close_fade
    forward = forward_config or config.forward_test
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    now_ms = _floor_minute_ms(_ms_from_dt(now_dt))
    market = client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
    round_trip_cost_bps = config.costs.base_entry_exit_cost_bps * fade.cost_multiplier

    existing = read_dataset(data_root, "forward_paper_trades")
    marked_existing = mark_forward_open_trades(
        existing,
        config=config,
        fade_config=fade,
        forward_config=forward,
        now=now_dt,
        client=market,
    )

    scan = run_forward_scan(
        data_root,
        config=config,
        fade_config=fade,
        forward_config=forward,
        now=now_dt,
        client=market,
        report_dir=report_dir,
    )

    candidates = pl.DataFrame(scan["candidates"], infer_schema_length=None) if scan["candidates"] else pl.DataFrame()
    new_trades = open_forward_paper_trades(
        candidates,
        existing_trades=marked_existing,
        config=config,
        fade_config=fade,
        forward_config=forward,
        now=now_dt,
        client=market,
        round_trip_cost_bps=round_trip_cost_bps,
    )

    all_trades = _merge_trade_frames(marked_existing, new_trades)
    baskets = summarize_forward_paper_baskets(all_trades)
    payload = {
        "now": now_dt.isoformat(),
        "scan": scan,
        "rows": {
            "existing_trades": existing.height,
            "new_trades": new_trades.height,
            "total_trades": all_trades.height,
            "open_trades": _count_status(all_trades, "open"),
            "closed_trades": _count_status(all_trades, "closed"),
            "baskets": baskets.height,
        },
        "new_trades": new_trades.to_dicts() if not new_trades.is_empty() else [],
        "open_trades": all_trades.filter(pl.col("status") == "open").to_dicts() if not all_trades.is_empty() else [],
    }
    _write_forward_trade_outputs(data_root, payload, all_trades, baskets, report_dir=report_dir)

    telegram_enabled = forward.send_telegram if send_telegram is None else send_telegram
    send_telegram_message(format_forward_run_message(payload), enabled=telegram_enabled)
    return payload


def run_forward_sleeves(
    data_root: str | Path,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig | None = None,
    forward_config: ForwardTestConfig | None = None,
    now: datetime | None = None,
    client: Any | None = None,
    report_dir: str | Path | None = None,
    send_telegram: bool | None = None,
    sleeves: dict[str, DailyCloseFadeConfig] | None = None,
) -> dict[str, Any]:
    fade = fade_config or config.daily_close_fade
    forward = forward_config or config.forward_test
    now_dt = _as_utc(now or datetime.now(tz=UTC))
    sleeve_configs = sleeves or default_forward_sleeves(fade)
    output_dir = Path(report_dir or Path(data_root) / "reports")
    sleeve_root_base = Path(data_root) / "forward_sleeves"
    results = []

    for name, sleeve_fade in sleeve_configs.items():
        sleeve_root = sleeve_root_base / _safe_sleeve_name(name)
        sleeve_report_dir = output_dir / "forward_sleeves" / _safe_sleeve_name(name)
        payload = run_forward_once(
            sleeve_root,
            config=config,
            fade_config=sleeve_fade,
            forward_config=forward,
            now=now_dt,
            client=client,
            report_dir=sleeve_report_dir,
            send_telegram=False,
        )
        summary = payload.get("summary") or summarize_forward_paper_trades(read_dataset(sleeve_root, "forward_paper_trades"))
        results.append(
            {
                "sleeve": name,
                "data_root": str(sleeve_root),
                "report_dir": str(sleeve_report_dir),
                "signal_minute": sleeve_fade.signal_minute,
                "top_n": sleeve_fade.top_n,
                "gross_exposure": sleeve_fade.gross_exposure,
                "liquidity_rank_min": sleeve_fade.liquidity_rank_min,
                "liquidity_rank_max": sleeve_fade.liquidity_rank_max,
                "min_baseline_turnover": sleeve_fade.min_baseline_turnover,
                "min_day_turnover": sleeve_fade.min_day_turnover,
                "min_last_60m_turnover": sleeve_fade.min_last_60m_turnover,
                "max_position_weight": sleeve_fade.max_position_weight,
                "max_trade_notional_pct_of_day_turnover": sleeve_fade.max_trade_notional_pct_of_day_turnover,
                "max_trade_notional_pct_of_baseline_turnover": sleeve_fade.max_trade_notional_pct_of_baseline_turnover,
                "scan_status": payload.get("scan", {}).get("status"),
                "universe": payload.get("scan", {}).get("rows", {}).get("universe", 0),
                "candidates": payload.get("scan", {}).get("rows", {}).get("candidates", 0),
                "new_trades": payload.get("rows", {}).get("new_trades", 0),
                "open_trades": payload.get("rows", {}).get("open_trades", 0),
                "closed_trades": payload.get("rows", {}).get("closed_trades", 0),
                "total_trades": payload.get("rows", {}).get("total_trades", 0),
                "marked_net_return": summary.get("marked_net_return", 0.0),
                "closed_net_return": summary.get("closed_net_return", 0.0),
                "capacity_limited_trades": summary.get("capacity_limited_trades", 0),
                "stop_loss_exits": summary.get("stop_loss_exits", 0),
                "take_profit_exits": summary.get("take_profit_exits", 0),
                "max_hold_exits": summary.get("max_hold_exits", 0),
                "trailing_stop_exits": summary.get("trailing_stop_exits", 0),
                "vol_trailing_stop_exits": summary.get("vol_trailing_stop_exits", 0),
                "mfe_giveback_exits": summary.get("mfe_giveback_exits", 0),
                "vwap_reversion_exits": summary.get("vwap_reversion_exits", 0),
            }
        )

    payload = {
        "now": now_dt.isoformat(),
        "rows": {"sleeves": len(results)},
        "config": {"forward": asdict(forward)},
        "results": results,
    }
    _write_forward_sleeves_outputs(payload, output_dir=output_dir)
    telegram_enabled = forward.send_telegram if send_telegram is None else send_telegram
    send_telegram_message(format_forward_sleeves_message(payload), enabled=telegram_enabled)
    return payload


def default_forward_sleeves(base: DailyCloseFadeConfig) -> dict[str, DailyCloseFadeConfig]:
    return {
        "control_top_1_30": replace(
            base,
            liquidity_rank_min=1,
            liquidity_rank_max=30,
            exclude_symbols=(),
            top_n=5,
            gross_exposure=min(base.gross_exposure, 0.25),
            max_position_weight=base.max_position_weight,
            max_trade_notional_pct_of_day_turnover=0.0,
            max_trade_notional_pct_of_baseline_turnover=0.0,
        ),
        "core_31_150": replace(
            base,
            liquidity_rank_min=31,
            liquidity_rank_max=150,
            top_n=5,
            gross_exposure=base.gross_exposure,
            max_position_weight=base.max_position_weight,
            max_trade_notional_pct_of_day_turnover=0.0,
            max_trade_notional_pct_of_baseline_turnover=0.0,
        ),
        "microcap_151_plus": replace(
            base,
            liquidity_rank_min=151,
            liquidity_rank_max=0,
            top_n=3,
            gross_exposure=min(base.gross_exposure, 0.5),
            min_baseline_turnover=max(base.min_baseline_turnover, 250_000.0),
            min_day_turnover=max(base.min_day_turnover, 750_000.0),
            min_last_60m_turnover=max(base.min_last_60m_turnover, 75_000.0),
            max_position_weight=base.max_position_weight or 0.20,
            max_trade_notional_pct_of_day_turnover=base.max_trade_notional_pct_of_day_turnover or 0.002,
            max_trade_notional_pct_of_baseline_turnover=base.max_trade_notional_pct_of_baseline_turnover or 0.005,
        ),
    }


def run_forward_report(
    data_root: str | Path,
    *,
    report_dir: str | Path | None = None,
) -> dict[str, Any]:
    trades = read_dataset(data_root, "forward_paper_trades")
    baskets = summarize_forward_paper_baskets(trades)
    payload = {
        "rows": {
            "trades": trades.height,
            "open_trades": _count_status(trades, "open"),
            "closed_trades": _count_status(trades, "closed"),
            "baskets": baskets.height,
        },
        "summary": summarize_forward_paper_trades(trades),
    }
    _write_forward_trade_outputs(data_root, payload, trades, baskets, report_dir=report_dir)
    return payload


def build_forward_universe(
    instruments_rows: list[dict[str, Any]],
    ticker_rows: list[dict[str, Any]],
    *,
    now_ms: int,
    fade_config: DailyCloseFadeConfig,
    forward_config: ForwardTestConfig,
    settle_coin: str = "USDT",
) -> pl.DataFrame:
    instruments = _normalize_live_instruments(instruments_rows)
    tickers = _normalize_live_tickers(ticker_rows)
    if instruments.is_empty() or tickers.is_empty():
        return _empty_universe()
    exclude = {symbol.upper() for symbol in fade_config.exclude_symbols}
    table = (
        instruments.join(tickers, on="symbol", how="inner")
        .with_columns(
            [
                ((now_ms - pl.col("launch_time_ms")) / MS_PER_DAY).alias("age_days"),
                ((pl.col("ask1_price") / pl.col("bid1_price") - 1.0) * 10_000.0).alias("spread_bps"),
            ]
        )
        .filter(
            (pl.col("status") == "Trading")
            & (pl.col("contract_type") == "LinearPerpetual")
            & (pl.col("settle_coin") == settle_coin)
            & (~pl.col("is_prelisting"))
            & (~pl.col("symbol").is_in(exclude))
            & (pl.col("bid1_price").fill_null(0.0) > 0.0)
            & (pl.col("ask1_price").fill_null(0.0) > 0.0)
            & (pl.col("launch_time_ms").is_not_null())
            & (pl.col("age_days") >= float(fade_config.min_age_days))
            & (pl.col("turnover_24h").fill_null(0.0) >= forward_config.min_turnover_24h)
            & (pl.col("spread_bps").fill_null(float("inf")) <= forward_config.max_spread_bps)
            & (pl.col("open_interest_value").fill_null(0.0) >= forward_config.min_open_interest_value)
        )
        .sort("turnover_24h", descending=True)
        .with_columns(pl.int_range(1, pl.len() + 1).alias("live_liquidity_rank"))
    )
    return table


def build_forward_scan_features(
    klines_1m: pl.DataFrame,
    daily_klines: pl.DataFrame,
    universe: pl.DataFrame,
    *,
    fade_config: DailyCloseFadeConfig,
    signal_minute: int,
    scan_id: str,
) -> pl.DataFrame:
    if klines_1m.is_empty() or universe.is_empty():
        return pl.DataFrame()
    base = _minute_frame(klines_1m)
    features = _signal_feature_frame(base, signal_minute)
    if features.is_empty():
        return features

    daily_vol = _latest_realized_vol(daily_klines, lookback_days=fade_config.vol_lookback_days)
    features = features.join(daily_vol, on="symbol", how="left")
    baseline_liquidity = _latest_baseline_liquidity(
        daily_klines,
        lookback_days=fade_config.liquidity_lookback_days,
    )
    features = features.join(baseline_liquidity, on="symbol", how="left")
    features = features.join(
        universe.select(
            [
                "symbol",
                "launch_time_ms",
                "age_days",
                "turnover_24h",
                "open_interest_value",
                "spread_bps",
                "live_liquidity_rank",
                "bid1_price",
                "ask1_price",
            ]
        ),
        on="symbol",
        how="inner",
    )

    median_vol = features.select(pl.col("realized_vol").median()).item()
    fallback_vol = float(median_vol) if median_vol is not None and median_vol > EPSILON else 0.03
    return (
        features.with_columns(
            [
                pl.lit(scan_id).alias("scan_id"),
                pl.col("realized_vol").fill_null(fallback_vol).clip(lower_bound=0.002).alias("realized_vol"),
                (pl.col("day_return") / pl.col("realized_vol").fill_null(fallback_vol).clip(lower_bound=0.002)).alias(
                    "vol_adjusted_day_return"
                ),
                pl.when(pl.col("day_volume_base") > 0.0)
                .then(pl.col("day_turnover") / pl.col("day_volume_base"))
                .otherwise(None)
                .alias("intraday_vwap"),
                (pl.col("bar_count") / (signal_minute + 1)).alias("bar_coverage"),
            ]
        )
        .with_columns(
            [
                (pl.col("signal_close") / pl.col("intraday_vwap") - 1.0).fill_null(0.0).alias("vwap_extension"),
                (pl.col("last_60m_turnover") / (pl.col("day_turnover") / ((signal_minute + 1) / 60.0)).clip(EPSILON))
                .fill_null(0.0)
                .alias("late_volume_ratio"),
                (pl.col("signal_close") >= pl.col("day_high") * 0.995).alias("fresh_day_high"),
            ]
        )
        .with_columns(
            [
                (
                    (pl.col("day_return") >= 0.03).cast(pl.Int8)
                    + (pl.col("vol_adjusted_day_return") >= 1.5).cast(pl.Int8)
                    + (pl.col("late_volume_ratio") >= 1.5).cast(pl.Int8)
                    + (pl.col("vwap_extension") >= 0.015).cast(pl.Int8)
                    + (pl.col("last_60m_return") >= 0.01).fill_null(False).cast(pl.Int8)
                    + pl.col("fresh_day_high").cast(pl.Int8)
                ).alias("pump_score")
            ]
        )
        .with_columns((pl.col("pump_score") >= 3).alias("pump_like"))
        .with_columns(
            (
                (pl.col("age_days").fill_null(-1.0) >= float(fade_config.min_age_days))
                & (pl.col("bar_coverage") >= 0.95)
            ).alias("eligible")
        )
        .sort(["signal_ts_ms", "symbol"])
    )
    return attach_close_fade_coin_market_context(features, fade_config)


def open_forward_paper_trades(
    candidates: pl.DataFrame,
    *,
    existing_trades: pl.DataFrame,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig,
    forward_config: ForwardTestConfig,
    now: datetime,
    client: Any,
    round_trip_cost_bps: float,
) -> pl.DataFrame:
    if candidates.is_empty():
        return pl.DataFrame()
    if fade_config.entry_twap_minutes > 0:
        # Do not fake a TWAP position as one paper fill. The order/ledger layer
        # needs explicit slice accounting before forward/demo can trade this.
        return pl.DataFrame()
    now_ms = _floor_minute_ms(_ms_from_dt(_as_utc(now)))
    signal_ts_ms = int(candidates["signal_ts_ms"].head(1).item())
    entry_due_ts_ms = signal_ts_ms + fade_config.entry_delay_minutes * MS_PER_MINUTE
    if now_ms < entry_due_ts_ms:
        return pl.DataFrame()
    if now_ms > entry_due_ts_ms + forward_config.max_entry_lag_minutes * MS_PER_MINUTE:
        return pl.DataFrame()
    basket_id = f"{_dt_from_ms(signal_ts_ms).date().isoformat()}-{fade_config.signal_minute}"
    if _basket_already_opened(existing_trades, basket_id):
        return pl.DataFrame()

    selected_count = max(candidates.height, 1)
    rows: list[dict[str, Any]] = []
    for row in candidates.sort("entry_rank").to_dicts():
        symbol = str(row["symbol"])
        try:
            bars = _fetch_symbol_klines(client, symbol, "1", entry_due_ts_ms, now_ms)
        except Exception:
            continue
        frame = pl.DataFrame(bars, infer_schema_length=None)
        if frame.is_empty():
            continue
        entry_candidates = frame.filter(pl.col("ts_ms") >= entry_due_ts_ms).sort("ts_ms")
        if entry_candidates.is_empty():
            continue
        entry_bar = entry_candidates.row(0, named=True)
        trade = _new_paper_trade(
            row,
            basket_id=basket_id,
            entry_ts_ms=int(entry_bar["ts_ms"]),
            entry_price=float(entry_bar["open"]),
            selected_count=selected_count,
            fade_config=fade_config,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        if float(trade["weight"]) <= EPSILON:
            continue
        rows.append(_mark_trade_from_bars(trade, entry_candidates, now_ms=now_ms, fade_config=fade_config, round_trip_cost_bps=round_trip_cost_bps))
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def mark_forward_open_trades(
    trades: pl.DataFrame,
    *,
    config: ResearchConfig,
    fade_config: DailyCloseFadeConfig,
    forward_config: ForwardTestConfig,
    now: datetime,
    client: Any,
) -> pl.DataFrame:
    del config, forward_config
    if trades.is_empty() or "status" not in trades.columns:
        return trades
    now_ms = _floor_minute_ms(_ms_from_dt(_as_utc(now)))
    rows: list[dict[str, Any]] = []
    default_round_trip_cost_bps = (
        float(trades["round_trip_cost_bps"].drop_nulls().head(1).item())
        if "round_trip_cost_bps" in trades.columns and trades["round_trip_cost_bps"].drop_nulls().len()
        else 0.0
    )
    for row in trades.sort(["entry_ts_ms", "symbol"]).to_dicts():
        if row.get("status") != "open":
            rows.append(row)
            continue
        symbol = str(row["symbol"])
        start_ms = int(row["entry_ts_ms"])
        target_exit_ms = min(int(row["target_exit_ts_ms"]), now_ms)
        try:
            bars = _fetch_symbol_klines(client, symbol, "1", start_ms, target_exit_ms)
        except Exception as exc:  # noqa: BLE001 - keep the paper ledger alive for other symbols
            updated = dict(row)
            updated["last_error"] = str(exc)
            rows.append(updated)
            continue
        frame = pl.DataFrame(bars, infer_schema_length=None)
        row_round_trip_cost_bps = _trade_number(row, "round_trip_cost_bps", default_round_trip_cost_bps)
        rows.append(
            _mark_trade_from_bars(
                row,
                frame,
                now_ms=now_ms,
                fade_config=fade_config,
                round_trip_cost_bps=row_round_trip_cost_bps,
            )
        )
    return pl.DataFrame(rows, infer_schema_length=None) if rows else trades


def summarize_forward_paper_baskets(trades: pl.DataFrame) -> pl.DataFrame:
    if trades.is_empty():
        return pl.DataFrame()
    return (
        trades.group_by(["basket_id", "signal_ts_ms", "date"], maintain_order=True)
        .agg(
            [
                pl.len().alias("trade_count"),
                (pl.col("status") == "open").sum().alias("open_count"),
                (pl.col("status") == "closed").sum().alias("closed_count"),
                pl.col("weighted_net_return").sum().alias("marked_basket_return"),
                pl.col("weighted_gross_return").sum().alias("marked_gross_return"),
                pl.col("weighted_cost_return").sum().alias("marked_cost_return"),
                pl.col("target_weight").sum().alias("target_gross_exposure"),
                pl.col("weight").sum().alias("basket_gross_exposure"),
                pl.col("capacity_limited").sum().alias("capacity_limited_count"),
                pl.col("mae").min().alias("worst_mae"),
                pl.col("mfe").max().alias("best_mfe"),
            ]
        )
        .sort("signal_ts_ms")
    )


def summarize_forward_paper_trades(trades: pl.DataFrame) -> dict[str, Any]:
    if trades.is_empty():
        return {
            "trades": 0,
            "open_trades": 0,
            "closed_trades": 0,
            "closed_net_return": 0.0,
            "marked_net_return": 0.0,
        }
    closed = trades.filter(pl.col("status") == "closed")
    return {
        "trades": trades.height,
        "open_trades": _count_status(trades, "open"),
        "closed_trades": closed.height,
        "closed_net_return": float(closed["weighted_net_return"].sum()) if not closed.is_empty() else 0.0,
        "marked_net_return": float(trades["weighted_net_return"].sum()),
        "avg_basket_gross_exposure": float(
            summarize_forward_paper_baskets(trades)["basket_gross_exposure"].mean()
        )
        if "weight" in trades.columns
        else 0.0,
        "capacity_limited_trades": int(trades["capacity_limited"].sum()) if "capacity_limited" in trades.columns else 0,
        "stop_loss_exits": _count_reason(trades, "stop_loss"),
        "take_profit_exits": _count_reason(trades, "take_profit"),
        "max_hold_exits": _count_reason(trades, "max_hold"),
        "trailing_stop_exits": _count_reason(trades, "trailing_stop"),
        "vol_trailing_stop_exits": _count_reason(trades, "vol_trailing_stop"),
        "mfe_giveback_exits": _count_reason(trades, "mfe_giveback"),
        "vwap_reversion_exits": _count_reason(trades, "vwap_reversion"),
    }


def format_forward_scan_report(payload: dict[str, Any]) -> str:
    score_name = payload.get("config", {}).get("fade", {}).get("score", "score")
    lines = [
        "# Forward Scan Report",
        "",
        f"Scan ID: `{payload['scan_id']}`",
        f"Now: {payload['now']}",
        f"Scan end: {payload['scan_end']}",
        f"Signal time: {payload['signal_time']}",
        f"Status: `{payload['status']}`",
        "",
        "## Rows",
        "",
        "| Dataset | Rows |",
        "|---|---:|",
        f"| Universe | {payload['rows']['universe']} |",
        f"| Features | {payload['rows']['features']} |",
        f"| Candidates | {payload['rows']['candidates']} |",
        f"| Fetch failures | {payload['rows']['fetch_failures']} |",
        "",
        "## Candidates",
        "",
        "| Rank | Symbol | Score | Day Ret | Pump | Age | Base Liq Rank | DTD Turnover | Spread bps |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("candidates", [])[:25]:
        score_value = float(row.get(score_name, row.get("score", 0.0)) or 0.0)
        lines.append(
            f"| {int(row.get('entry_rank', 0))} | {row.get('symbol')} | {score_value:.4f} | "
            f"{float(row.get('day_return') or 0.0):.2%} | {int(row.get('pump_score') or 0)} | "
            f"{float(row.get('age_days') or 0.0):.1f} | {int(row.get('baseline_liquidity_rank') or 0)} | "
            f"{float(row.get('day_turnover') or 0.0):,.0f} | "
            f"{float(row.get('spread_bps') or 0.0):.1f} |"
        )
    lines.append("")
    return "\n".join(lines)


def format_forward_run_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    rows = payload.get("rows", {})
    lines = [
        "# Forward Paper Report",
        "",
        f"Now: {payload.get('now')}",
        "",
        "## Rows",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total trades | {rows.get('total_trades', rows.get('trades', 0))} |",
        f"| New trades | {rows.get('new_trades', 0)} |",
        f"| Open trades | {rows.get('open_trades', 0)} |",
        f"| Closed trades | {rows.get('closed_trades', 0)} |",
        f"| Baskets | {rows.get('baskets', 0)} |",
        "",
    ]
    if summary:
        lines.extend(
            [
                "## Summary",
                "",
                "| Metric | Value |",
                "|---|---:|",
                f"| Closed weighted net | {summary.get('closed_net_return', 0.0):.2%} |",
                f"| Marked weighted net | {summary.get('marked_net_return', 0.0):.2%} |",
                f"| Avg basket gross exposure | {summary.get('avg_basket_gross_exposure', 0.0):.2%} |",
                f"| Capacity-limited trades | {summary.get('capacity_limited_trades', 0)} |",
                f"| Stop exits | {summary.get('stop_loss_exits', 0)} |",
                f"| Take-profit exits | {summary.get('take_profit_exits', 0)} |",
                f"| Max hold exits | {summary.get('max_hold_exits', 0)} |",
                f"| Trailing exits | {summary.get('trailing_stop_exits', 0)} |",
                f"| Vol trailing exits | {summary.get('vol_trailing_stop_exits', 0)} |",
                f"| MFE giveback exits | {summary.get('mfe_giveback_exits', 0)} |",
                f"| VWAP reversion exits | {summary.get('vwap_reversion_exits', 0)} |",
                "",
            ]
        )
    return "\n".join(lines)


def format_forward_run_message(payload: dict[str, Any]) -> str:
    rows = payload.get("rows", {})
    scan = payload.get("scan", {})
    candidates = scan.get("candidates", [])
    symbols = ", ".join(str(row.get("symbol")) for row in candidates[:10]) or "none"
    return (
        "Forward paper update\n"
        f"now: {payload.get('now')}\n"
        f"scan: {scan.get('status')} candidates={scan.get('rows', {}).get('candidates', 0)}\n"
        f"new_trades: {rows.get('new_trades', 0)} open={rows.get('open_trades', 0)} closed={rows.get('closed_trades', 0)}\n"
        f"candidates: {symbols}"
    )


def format_forward_sleeves_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Forward Paper Sleeves",
        "",
        f"Now: {payload.get('now')}",
        "",
        "| Sleeve | Scan | Universe | Candidates | New | Open | Closed | Marked Net | Closed Net | Capacity Capped | Stop | Max Hold | Vol Trail | MFE Giveback |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("results", []):
        lines.append(
            f"| {row['sleeve']} | {row.get('scan_status')} | {row.get('universe', 0)} | "
            f"{row.get('candidates', 0)} | {row.get('new_trades', 0)} | {row.get('open_trades', 0)} | "
            f"{row.get('closed_trades', 0)} | {row.get('marked_net_return', 0.0):.2%} | "
            f"{row.get('closed_net_return', 0.0):.2%} | {row.get('capacity_limited_trades', 0)} | "
            f"{row.get('stop_loss_exits', 0)} | {row.get('max_hold_exits', 0)} | "
            f"{row.get('vol_trailing_stop_exits', 0)} | {row.get('mfe_giveback_exits', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Sleeve Configs",
            "",
            "| Sleeve | Ranks | Top N | Gross | Min Baseline Turnover | Min DTD Turnover | Min 60m Turnover | Max Weight | Day Cap | Baseline Cap |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload.get("results", []):
        rank_max = int(row.get("liquidity_rank_max") or 0)
        ranks = f"{row.get('liquidity_rank_min')}-{'all' if rank_max <= 0 else rank_max}"
        lines.append(
            f"| {row['sleeve']} | {ranks} | {row.get('top_n')} | {row.get('gross_exposure', 0.0):.2f}x | "
            f"{row.get('min_baseline_turnover', 0.0):,.0f} | {row.get('min_day_turnover', 0.0):,.0f} | "
            f"{row.get('min_last_60m_turnover', 0.0):,.0f} | {row.get('max_position_weight', 0.0):.2%} | "
            f"{row.get('max_trade_notional_pct_of_day_turnover', 0.0):.3%} | "
            f"{row.get('max_trade_notional_pct_of_baseline_turnover', 0.0):.3%} |"
        )
    lines.extend(
        [
            "",
            "Each sleeve keeps its own ledger under `forward_sleeves/<sleeve>/` and report files under `reports/forward_sleeves/<sleeve>/`.",
            "",
        ]
    )
    return "\n".join(lines)


def format_forward_sleeves_message(payload: dict[str, Any]) -> str:
    lines = [f"Forward sleeves update\nnow: {payload.get('now')}"]
    for row in payload.get("results", []):
        lines.append(
            f"{row['sleeve']}: {row.get('scan_status')} cand={row.get('candidates', 0)} "
            f"new={row.get('new_trades', 0)} open={row.get('open_trades', 0)} "
            f"closed={row.get('closed_trades', 0)} marked={row.get('marked_net_return', 0.0):.2%}"
        )
    return "\n".join(lines)


def _fetch_forward_bars(
    symbols: list[str],
    *,
    config: ResearchConfig,
    client: Any | None,
    day_start_ms: int,
    scan_end_ms: int,
    daily_start_ms: int,
    daily_end_ms: int,
    workers: int,
) -> tuple[pl.DataFrame, pl.DataFrame, list[dict[str, Any]]]:
    if not symbols:
        return pl.DataFrame(), pl.DataFrame(), []
    frames_1m: list[pl.DataFrame] = []
    frames_daily: list[pl.DataFrame] = []
    failures: list[dict[str, Any]] = []

    def fetch_with(local_client: Any, symbol: str) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], str | None]:
        try:
            return (
                symbol,
                _fetch_symbol_klines(local_client, symbol, "1", day_start_ms, scan_end_ms),
                _fetch_symbol_klines(local_client, symbol, "D", daily_start_ms, daily_end_ms),
                None,
            )
        except Exception as exc:  # noqa: BLE001 - forward recorder should keep scanning other symbols
            return symbol, [], [], str(exc)

    if client is not None or workers <= 1:
        local_client = client or BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet)
        results = [fetch_with(local_client, symbol) for symbol in symbols]
    else:
        max_workers = max(1, min(workers, len(symbols)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    fetch_with,
                    BybitMarketData(category=config.exchange.category, testnet=config.exchange.testnet),
                    symbol,
                ): symbol
                for symbol in symbols
            }
            results = [future.result() for future in as_completed(futures)]

    for symbol, rows_1m, rows_daily, error in results:
        if error:
            failures.append({"symbol": symbol, "error": error})
            continue
        if rows_1m:
            frames_1m.append(pl.DataFrame(rows_1m, infer_schema_length=None))
        if rows_daily:
            frames_daily.append(pl.DataFrame(rows_daily, infer_schema_length=None))

    return _concat(frames_1m), _concat(frames_daily), failures


def _fetch_symbol_klines(client: Any, symbol: str, interval: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    if end_ms < start_ms:
        return []
    rows = client.get_klines(symbol, interval, start_ms, end_ms)
    return _normalize_klines(symbol, rows, source="bybit_rest_forward")


def _normalize_live_instruments(rows: list[dict[str, Any]]) -> pl.DataFrame:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "contract_type": row.get("contractType") or row.get("contract_type"),
                "status": row.get("status"),
                "settle_coin": row.get("settleCoin") or row.get("settle_coin"),
                "launch_time_ms": _int_or_none(row.get("launchTime", row.get("launch_time_ms"))),
                "is_prelisting": _bool_value(row.get("isPreListing", row.get("is_prelisting", False))),
            }
        )
    return pl.DataFrame(normalized, infer_schema_length=None) if normalized else _empty_universe()


def _normalize_live_tickers(rows: list[dict[str, Any]]) -> pl.DataFrame:
    normalized = []
    for row in rows:
        normalized.append(
            {
                "symbol": str(row.get("symbol", "")).upper(),
                "last_price": _float_or_none(row.get("lastPrice", row.get("last_price"))),
                "bid1_price": _float_or_none(row.get("bid1Price", row.get("bid1_price"))),
                "ask1_price": _float_or_none(row.get("ask1Price", row.get("ask1_price"))),
                "turnover_24h": _float_or_none(row.get("turnover24h", row.get("turnover_24h"))),
                "open_interest_value": _float_or_none(row.get("openInterestValue", row.get("open_interest_value"))),
            }
        )
    return pl.DataFrame(normalized, infer_schema_length=None) if normalized else pl.DataFrame()


def _normalize_klines(symbol: str, rows: list, *, source: str) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append(
            {
                "ts_ms": int(row[0]),
                "symbol": symbol,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume_base": float(row[5]),
                "turnover_quote": float(row[6]),
                "source": source,
            }
        )
    return sorted(output, key=lambda item: item["ts_ms"])


def _signal_feature_frame(base: pl.DataFrame, signal_minute: int) -> pl.DataFrame:
    subset = base.filter(pl.col("minute_of_day") <= signal_minute).sort(["symbol", "date", "ts_ms"])
    if subset.is_empty():
        return pl.DataFrame()
    avg_hours = max((signal_minute + 1) / 60.0, 1.0)
    return (
        subset.group_by(["symbol", "date"], maintain_order=True)
        .agg(
            [
                pl.col("ts_ms").last().alias("signal_ts_ms"),
                pl.col("open").first().alias("day_open"),
                pl.col("close").last().alias("signal_close"),
                pl.col("high").max().alias("day_high"),
                pl.col("low").min().alias("day_low"),
                pl.col("turnover_quote").sum().alias("day_turnover"),
                pl.col("volume_base").sum().alias("day_volume_base"),
                pl.len().alias("bar_count"),
                pl.col("turnover_quote").filter(pl.col("minute_of_day") > signal_minute - 60).sum().alias("last_60m_turnover"),
                pl.col("close").filter(pl.col("minute_of_day") <= signal_minute - 15).last().alias("close_15m_ago"),
                pl.col("close").filter(pl.col("minute_of_day") <= signal_minute - 60).last().alias("close_60m_ago"),
            ]
        )
        .with_columns(
            [
                pl.lit(signal_minute).alias("signal_minute"),
                (pl.col("signal_close") / pl.col("day_open") - 1.0).alias("day_return"),
                (pl.col("signal_close").log() - pl.col("day_open").log()).alias("day_log_return"),
                (pl.col("signal_close") / pl.col("close_15m_ago") - 1.0).alias("last_15m_return"),
                (pl.col("signal_close") / pl.col("close_60m_ago") - 1.0).alias("last_60m_return"),
                (pl.col("day_turnover") / avg_hours).alias("avg_hourly_turnover_so_far"),
            ]
        )
    )


def _latest_realized_vol(daily_klines: pl.DataFrame, *, lookback_days: int) -> pl.DataFrame:
    if daily_klines.is_empty():
        return pl.DataFrame({"symbol": pl.Series([], dtype=pl.String), "realized_vol": pl.Series([], dtype=pl.Float64)})
    daily = (
        daily_klines.sort(["symbol", "ts_ms"])
        .group_by(["symbol", "ts_ms"], maintain_order=True)
        .agg(pl.col("close").last().alias("day_close"))
        .sort(["symbol", "ts_ms"])
    )
    return (
        daily.with_columns((pl.col("day_close").log() - pl.col("day_close").shift(1).over("symbol").log()).alias("daily_log_ret"))
        .with_columns(
            pl.col("daily_log_ret")
            .rolling_std(window_size=max(lookback_days, 2), min_samples=2)
            .over("symbol")
            .alias("realized_vol")
        )
        .group_by("symbol")
        .agg(pl.col("realized_vol").drop_nulls().last().alias("realized_vol"))
    )


def _latest_baseline_liquidity(daily_klines: pl.DataFrame, *, lookback_days: int) -> pl.DataFrame:
    if daily_klines.is_empty():
        return pl.DataFrame(
            {
                "symbol": pl.Series([], dtype=pl.String),
                "baseline_liquidity_turnover": pl.Series([], dtype=pl.Float64),
                "baseline_liquidity_rank": pl.Series([], dtype=pl.Int64),
            }
        )
    liquidity = (
        daily_klines.sort(["symbol", "ts_ms"])
        .group_by("symbol", maintain_order=True)
        .agg(pl.col("turnover_quote").tail(max(lookback_days, 1)).mean().alias("baseline_liquidity_turnover"))
    )
    return liquidity.with_columns(
        pl.col("baseline_liquidity_turnover")
        .rank("ordinal", descending=True)
        .cast(pl.Int64)
        .alias("baseline_liquidity_rank")
    )


def _minute_frame(df: pl.DataFrame) -> pl.DataFrame:
    dt = pl.from_epoch(pl.col("ts_ms"), time_unit="ms")
    return df.with_columns(
        [
            dt.dt.strftime("%Y-%m-%d").alias("date"),
            (dt.dt.hour().cast(pl.Int16) * 60 + dt.dt.minute().cast(pl.Int16)).alias("minute_of_day"),
        ]
    )


def _new_paper_trade(
    row: dict[str, Any],
    *,
    basket_id: str,
    entry_ts_ms: int,
    entry_price: float,
    selected_count: int,
    fade_config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
) -> dict[str, Any]:
    signal_ts_ms = int(row["signal_ts_ms"])
    target_exit_ts_ms = entry_ts_ms + fade_config.hold_minutes * MS_PER_MINUTE
    weight_info = _close_fade_position_weight(row, config=fade_config, selected_count=selected_count)
    weight = weight_info["weight"]
    trade_id = f"paper-{signal_ts_ms}-{row['symbol']}-{int(row.get('entry_rank', 0))}"
    signal_dt = _dt_from_ms(signal_ts_ms)
    entry_dt = _dt_from_ms(entry_ts_ms)
    return {
        "trade_id": trade_id,
        "basket_id": basket_id,
        "status": "open",
        "symbol": str(row["symbol"]),
        "side": "short",
        "date": signal_dt.date().isoformat(),
        "signal_ts_ms": signal_ts_ms,
        "signal_time": signal_dt.isoformat(),
        "signal_minute": fade_config.signal_minute,
        "entry_due_ts_ms": signal_ts_ms + fade_config.entry_delay_minutes * MS_PER_MINUTE,
        "entry_ts_ms": entry_ts_ms,
        "entry_time": entry_dt.isoformat(),
        "entry_price": entry_price,
        "target_exit_ts_ms": target_exit_ts_ms,
        "target_exit_time": _dt_from_ms(target_exit_ts_ms).isoformat(),
        "exit_ts_ms": None,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": "open",
        "mark_ts_ms": entry_ts_ms,
        "mark_time": entry_dt.isoformat(),
        "mark_price": entry_price,
        "hold_minutes": 0.0,
        "entry_rank": int(row.get("entry_rank") or 0),
        "score": float(row.get(fade_config.score) or row.get("score") or 0.0),
        "score_name": fade_config.score,
        "day_return": float(row.get("day_return") or 0.0),
        "vol_adjusted_day_return": float(row.get("vol_adjusted_day_return") or 0.0),
        "pump_score": int(row.get("pump_score") or 0),
        "pump_like": bool(row.get("pump_like")),
        "late_volume_ratio": float(row.get("late_volume_ratio") or 0.0),
        "vwap_extension": float(row.get("vwap_extension") or 0.0),
        "market_median_day_return": float(row.get("market_median_day_return") or 0.0),
        "coin_excess_vs_market": float(row.get("coin_excess_vs_market") or 0.0),
        "intraday_vwap": float(row.get("intraday_vwap") or 0.0),
        "realized_vol": float(row.get("realized_vol") or 0.0),
        "baseline_liquidity_turnover": float(row.get("baseline_liquidity_turnover") or 0.0),
        "baseline_liquidity_rank": int(row.get("baseline_liquidity_rank") or 0),
        "age_days": float(row.get("age_days") or 0.0),
        "day_turnover": float(row.get("day_turnover") or 0.0),
        "last_60m_turnover": float(row.get("last_60m_turnover") or 0.0),
        "target_weight": weight_info["target_weight"],
        "weight": weight,
        "position_weight_cap": weight_info["position_weight_cap"],
        "capacity_limited": weight_info["capacity_limited"],
        "capacity_cap_reason": weight_info["capacity_cap_reason"],
        "account_equity": fade_config.account_equity,
        "target_notional": weight_info["target_notional"],
        "actual_notional": weight_info["actual_notional"],
        "gross_return": 0.0,
        "cost_return": round_trip_cost_bps / 10_000.0,
        "net_return": -(round_trip_cost_bps / 10_000.0),
        "weighted_gross_return": 0.0,
        "weighted_cost_return": (round_trip_cost_bps / 10_000.0) * weight,
        "weighted_net_return": -(round_trip_cost_bps / 10_000.0) * weight,
        "mae": 0.0,
        "mfe": 0.0,
        "stop_loss_pct": fade_config.stop_loss_pct,
        "take_profit_pct": fade_config.take_profit_pct,
        "basket_stop_loss_pct": fade_config.basket_stop_loss_pct,
        "trailing_stop_pct": fade_config.trailing_stop_pct,
        "trailing_activation_pct": fade_config.trailing_activation_pct,
        "vol_trailing_stop_mult": fade_config.vol_trailing_stop_mult,
        "vol_trailing_activation_mult": fade_config.vol_trailing_activation_mult,
        "mfe_giveback_activation_pct": fade_config.mfe_giveback_activation_pct,
        "mfe_giveback_pct": fade_config.mfe_giveback_pct,
        "vwap_reversion_pct": fade_config.vwap_reversion_pct,
        "stop_delay_minutes": fade_config.stop_delay_minutes,
        "cost_multiplier": fade_config.cost_multiplier,
        "liquidity_lookback_days": fade_config.liquidity_lookback_days,
        "liquidity_rank_min": fade_config.liquidity_rank_min,
        "liquidity_rank_max": fade_config.liquidity_rank_max,
        "min_baseline_turnover": fade_config.min_baseline_turnover,
        "position_sizing": fade_config.position_sizing,
        "score_weight_power": fade_config.score_weight_power,
        "coin_excess_vs_market_min": fade_config.coin_excess_vs_market_min,
        "coin_vwap_extension_min": fade_config.coin_vwap_extension_min,
        "coin_late_volume_ratio_min": fade_config.coin_late_volume_ratio_min,
        "round_trip_cost_bps": round_trip_cost_bps,
        "last_error": None,
    }


def _mark_trade_from_bars(
    trade: dict[str, Any],
    bars: pl.DataFrame,
    *,
    now_ms: int,
    fade_config: DailyCloseFadeConfig,
    round_trip_cost_bps: float,
) -> dict[str, Any]:
    updated = dict(trade)
    entry_price = float(updated["entry_price"])
    entry_ts_ms = int(updated["entry_ts_ms"])
    target_exit_ts_ms = int(updated["target_exit_ts_ms"])
    stop_delay_minutes = int(_trade_number(updated, "stop_delay_minutes", float(fade_config.stop_delay_minutes)))
    stop_loss_pct = _trade_number(updated, "stop_loss_pct", fade_config.stop_loss_pct)
    take_profit_pct = _trade_number(updated, "take_profit_pct", fade_config.take_profit_pct)
    trailing_stop_pct = _trade_number(updated, "trailing_stop_pct", fade_config.trailing_stop_pct)
    trailing_activation_pct = _trade_number(updated, "trailing_activation_pct", fade_config.trailing_activation_pct)
    vol_trailing_stop_mult = _trade_number(updated, "vol_trailing_stop_mult", fade_config.vol_trailing_stop_mult)
    vol_trailing_activation_mult = _trade_number(
        updated,
        "vol_trailing_activation_mult",
        fade_config.vol_trailing_activation_mult,
    )
    mfe_giveback_activation_pct = _trade_number(
        updated,
        "mfe_giveback_activation_pct",
        fade_config.mfe_giveback_activation_pct,
    )
    mfe_giveback_pct = _trade_number(updated, "mfe_giveback_pct", fade_config.mfe_giveback_pct)
    vwap_reversion_pct = _trade_number(updated, "vwap_reversion_pct", fade_config.vwap_reversion_pct)
    realized_vol = max(_trade_number(updated, "realized_vol", 0.0), 0.0)
    stop_active_ts_ms = entry_ts_ms + stop_delay_minutes * MS_PER_MINUTE
    hard_stop = entry_price * (1.0 + stop_loss_pct) if stop_loss_pct > 0.0 else None
    take_profit = entry_price * (1.0 - take_profit_pct) if take_profit_pct > 0.0 else None
    vwap_exit = _vwap_reversion_exit_price(entry_price, updated, replace(fade_config, vwap_reversion_pct=vwap_reversion_pct))
    vol_trailing_stop_pct = realized_vol * vol_trailing_stop_mult if vol_trailing_stop_mult > 0.0 else 0.0
    vol_trailing_activation_pct = (
        realized_vol * vol_trailing_activation_mult if vol_trailing_stop_mult > 0.0 else 0.0
    )
    best_price = entry_price
    trailing_active = False
    vol_trailing_active = False
    mfe_giveback_active = False
    max_favorable = 0.0
    max_adverse = 0.0
    mark_ts_ms = entry_ts_ms
    mark_price = entry_price
    exit_ts_ms: int | None = None
    exit_price: float | None = None
    exit_reason = "open"

    if not bars.is_empty():
        for bar in bars.filter(pl.col("ts_ms") >= entry_ts_ms).sort("ts_ms").to_dicts():
            ts_ms = int(bar["ts_ms"])
            high = float(bar["high"])
            low = float(bar["low"])
            close = float(bar["close"])
            mark_ts_ms = ts_ms
            mark_price = close
            max_favorable = max(max_favorable, _short_return(entry_price, low))
            max_adverse = min(max_adverse, _short_return(entry_price, high))

            if ts_ms >= stop_active_ts_ms and hard_stop is not None and high >= hard_stop:
                exit_ts_ms = ts_ms
                exit_price = hard_stop
                exit_reason = "stop_loss"
                break

            if ts_ms >= stop_active_ts_ms:
                protective_exits: list[tuple[float, str]] = []
                if trailing_active and trailing_stop_pct > 0.0:
                    trailing_stop = best_price * (1.0 + trailing_stop_pct)
                    if high >= trailing_stop:
                        protective_exits.append((trailing_stop, "trailing_stop"))
                if vol_trailing_active and vol_trailing_stop_pct > 0.0:
                    vol_trailing_stop = best_price * (1.0 + vol_trailing_stop_pct)
                    if high >= vol_trailing_stop:
                        protective_exits.append((vol_trailing_stop, "vol_trailing_stop"))
                if mfe_giveback_active and mfe_giveback_pct > 0.0:
                    mfe_stop = _mfe_giveback_stop_price(entry_price, best_price, mfe_giveback_pct)
                    if high >= mfe_stop:
                        protective_exits.append((mfe_stop, "mfe_giveback"))
                if protective_exits:
                    exit_price, exit_reason = max(protective_exits, key=lambda item: item[0])
                    exit_ts_ms = ts_ms
                    break

            if take_profit is not None and low <= take_profit:
                exit_ts_ms = ts_ms
                exit_price = take_profit
                exit_reason = "take_profit"
                break

            if vwap_exit is not None and low <= vwap_exit:
                exit_ts_ms = ts_ms
                exit_price = vwap_exit
                exit_reason = "vwap_reversion"
                break

            if low < best_price:
                best_price = low

            if ts_ms >= stop_active_ts_ms:
                best_return = _short_return(entry_price, best_price)
                if trailing_stop_pct > 0.0 and not trailing_active and best_return >= trailing_activation_pct:
                    trailing_active = True
                if vol_trailing_stop_pct > 0.0 and not vol_trailing_active and best_return >= vol_trailing_activation_pct:
                    vol_trailing_active = True
                if (
                    mfe_giveback_pct > 0.0
                    and not mfe_giveback_active
                    and best_return >= mfe_giveback_activation_pct
                ):
                    mfe_giveback_active = True

            if ts_ms >= target_exit_ts_ms:
                exit_ts_ms = ts_ms
                exit_price = close
                exit_reason = "max_hold"
                break

    if exit_ts_ms is None and now_ms >= target_exit_ts_ms and mark_ts_ms >= target_exit_ts_ms:
        exit_ts_ms = mark_ts_ms
        exit_price = mark_price
        exit_reason = "max_hold"

    cost_return = round_trip_cost_bps / 10_000.0
    weight = float(updated["weight"])
    if exit_ts_ms is not None:
        gross_return = _short_return(entry_price, float(exit_price))
        mark_dt = _dt_from_ms(exit_ts_ms)
        updated.update(
            {
                "status": "closed",
                "exit_ts_ms": exit_ts_ms,
                "exit_time": mark_dt.isoformat(),
                "exit_price": float(exit_price),
                "exit_reason": exit_reason,
                "mark_ts_ms": exit_ts_ms,
                "mark_time": mark_dt.isoformat(),
                "mark_price": float(exit_price),
                "hold_minutes": (exit_ts_ms - entry_ts_ms) / MS_PER_MINUTE,
            }
        )
    else:
        gross_return = _short_return(entry_price, mark_price)
        mark_dt = _dt_from_ms(mark_ts_ms)
        updated.update(
            {
                "status": "open",
                "exit_reason": "open",
                "mark_ts_ms": mark_ts_ms,
                "mark_time": mark_dt.isoformat(),
                "mark_price": mark_price,
                "hold_minutes": (mark_ts_ms - entry_ts_ms) / MS_PER_MINUTE,
            }
        )

    updated.update(
        {
            "gross_return": gross_return,
            "cost_return": cost_return,
            "net_return": gross_return - cost_return,
            "weighted_gross_return": gross_return * weight,
            "weighted_cost_return": cost_return * weight,
            "weighted_net_return": (gross_return - cost_return) * weight,
            "mae": min(float(updated.get("mae") or 0.0), max_adverse),
            "mfe": max(float(updated.get("mfe") or 0.0), max_favorable),
            "round_trip_cost_bps": round_trip_cost_bps,
        }
    )
    return updated


def _trade_number(row: dict[str, Any], key: str, default: float) -> float:
    value = row.get(key)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _write_forward_scan_outputs(
    data_root: str | Path,
    payload: dict[str, Any],
    features: pl.DataFrame,
    candidates: pl.DataFrame,
    *,
    report_dir: str | Path | None,
) -> None:
    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "forward_scan_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "forward_scan_report.md").write_text(format_forward_scan_report(payload), encoding="utf-8")
    if not candidates.is_empty():
        candidates.write_csv(output_dir / "forward_scan_candidates.csv")
    _replace_dataset(features, data_root, "forward_scan_features", partition_by=("date", "scan_id"))


def _write_forward_trade_outputs(
    data_root: str | Path,
    payload: dict[str, Any],
    trades: pl.DataFrame,
    baskets: pl.DataFrame,
    *,
    report_dir: str | Path | None,
) -> None:
    payload = dict(payload)
    payload.setdefault("summary", summarize_forward_paper_trades(trades))
    output_dir = Path(report_dir or Path(data_root) / "reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "forward_paper_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "forward_paper_report.md").write_text(format_forward_run_report(payload), encoding="utf-8")
    if not trades.is_empty():
        trades.write_csv(output_dir / "forward_paper_trades.csv")
    if not baskets.is_empty():
        baskets.write_csv(output_dir / "forward_paper_baskets.csv")
    _replace_dataset(trades, data_root, "forward_paper_trades", partition_by=("date", "symbol"))
    _replace_dataset(baskets, data_root, "forward_paper_baskets", partition_by=("date",))


def _write_forward_sleeves_outputs(payload: dict[str, Any], *, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "forward_sleeves_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (output_dir / "forward_sleeves_report.md").write_text(format_forward_sleeves_report(payload), encoding="utf-8")
    results = pl.DataFrame(payload.get("results", []), infer_schema_length=None)
    if not results.is_empty():
        results.write_csv(output_dir / "forward_sleeves_results.csv")


def _replace_dataset(df: pl.DataFrame, data_root: str | Path, dataset: str, *, partition_by: tuple[str, ...]) -> None:
    path = dataset_path(data_root, dataset)
    if path.exists():
        shutil.rmtree(path)
    write_dataset(df, data_root, dataset, partition_by=partition_by, append=False)


def _merge_trade_frames(left: pl.DataFrame, right: pl.DataFrame) -> pl.DataFrame:
    frames = [frame for frame in (left, right) if not frame.is_empty()]
    if not frames:
        return pl.DataFrame()
    return pl.concat(frames, how="diagonal_relaxed").unique(subset=["trade_id"], keep="last").sort(["entry_ts_ms", "symbol"])


def _basket_already_opened(trades: pl.DataFrame, basket_id: str) -> bool:
    return not trades.is_empty() and "basket_id" in trades.columns and trades.filter(pl.col("basket_id") == basket_id).height > 0


def _count_status(trades: pl.DataFrame, status: str) -> int:
    return 0 if trades.is_empty() or "status" not in trades.columns else int((trades["status"] == status).sum())


def _count_reason(trades: pl.DataFrame, reason: str) -> int:
    return 0 if trades.is_empty() or "exit_reason" not in trades.columns else int((trades["exit_reason"] == reason).sum())


def _scan_status(now_ms: int, signal_ts_ms: int, features: pl.DataFrame, candidates: pl.DataFrame) -> str:
    if now_ms < signal_ts_ms:
        return "pre_signal"
    if features.is_empty():
        return "no_features"
    if candidates.is_empty():
        return "no_candidates"
    return "candidates_ready"


def _scan_id(scan_end_ms: int, signal_minute: int) -> str:
    return f"{_dt_from_ms(scan_end_ms).date().isoformat()}-{signal_minute}"


def _safe_sleeve_name(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_")
    return safe or "sleeve"


def _signal_ts_ms(now: datetime, signal_minute: int) -> int:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return _ms_from_dt(day) + signal_minute * MS_PER_MINUTE


def _day_start_ms(now: datetime) -> int:
    day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return _ms_from_dt(day)


def _floor_minute_ms(ts_ms: int) -> int:
    return ts_ms - (ts_ms % MS_PER_MINUTE)


def _ms_from_dt(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _dt_from_ms(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _concat(frames: list[pl.DataFrame]) -> pl.DataFrame:
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()


def _empty_universe() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "symbol": pl.Series([], dtype=pl.String),
            "contract_type": pl.Series([], dtype=pl.String),
            "status": pl.Series([], dtype=pl.String),
            "settle_coin": pl.Series([], dtype=pl.String),
            "launch_time_ms": pl.Series([], dtype=pl.Int64),
            "is_prelisting": pl.Series([], dtype=pl.Boolean),
        }
    )


def _float_or_none(value: Any) -> float | None:
    return float(value) if value not in (None, "") else None


def _int_or_none(value: Any) -> int | None:
    return int(value) if value not in (None, "") else None


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}
