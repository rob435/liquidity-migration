use std::collections::{BTreeMap, BTreeSet};

use crate::config::{CarryFeatureConfig, LongFeatureConfig};
use crate::model::{
    BinanceWhaleObservation, CarryFeatureRow, DataRejection, HourlyKline, LongFeatureRow,
    SettledFunding,
};
use crate::{DAY_MS, HOUR_MS};

pub type KlineHistory = BTreeMap<String, BTreeMap<i64, HourlyKline>>;
pub type FundingHistory = BTreeMap<String, BTreeMap<i64, SettledFunding>>;
pub type WhaleHistory = BTreeMap<String, BTreeMap<i64, BinanceWhaleObservation>>;

#[derive(Clone, Debug, PartialEq)]
pub struct LongFeatureBuild {
    pub feature_ts_ms: Option<i64>,
    pub rows: Vec<LongFeatureRow>,
    pub fallback_count: usize,
    pub rejections: Vec<DataRejection>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct CarryFeatureBuild {
    pub decision_ts_ms: Option<i64>,
    pub rows: Vec<CarryFeatureRow>,
    pub rejections: Vec<DataRejection>,
}

#[derive(Clone, Debug)]
struct DailyBar {
    ts_ms: i64,
    high: f64,
    low: f64,
    close: f64,
    turnover_quote: f64,
}

pub fn build_long_features(
    klines: &KlineHistory,
    symbols: &[String],
    observed_ts_ms: i64,
    cfg: &LongFeatureConfig,
) -> LongFeatureBuild {
    let excluded: BTreeSet<&str> = cfg.exclude_symbols.iter().map(String::as_str).collect();
    let wanted: BTreeSet<&str> = symbols
        .iter()
        .map(String::as_str)
        .filter(|symbol| !excluded.contains(symbol))
        .chain([cfg.regime_symbol.as_str(), "ETHUSDT"])
        .collect();
    let current: BTreeSet<&str> = symbols
        .iter()
        .map(String::as_str)
        .filter(|symbol| !excluded.contains(symbol))
        .collect();
    let mut by_symbol: BTreeMap<String, Vec<DailyBar>> = BTreeMap::new();
    let mut rejections: Vec<DataRejection> = symbols
        .iter()
        .filter(|symbol| excluded.contains(symbol.as_str()))
        .map(|symbol| DataRejection {
            symbol: symbol.clone(),
            reason: "registered_exclusion".to_owned(),
            first_missing_ts_ms: None,
        })
        .collect();
    for symbol in wanted {
        let Some(history) = klines.get(symbol) else {
            if current.contains(symbol) {
                rejections.push(DataRejection {
                    symbol: symbol.to_owned(),
                    reason: "cold_start_no_klines".to_owned(),
                    first_missing_ts_ms: None,
                });
            }
            continue;
        };
        if let Some(missing) =
            first_internal_gap(history, observed_ts_ms, cfg.cold_start_lookback_days)
        {
            if current.contains(symbol) {
                rejections.push(DataRejection {
                    symbol: symbol.to_owned(),
                    reason: "hourly_kline_gap".to_owned(),
                    first_missing_ts_ms: Some(missing),
                });
            }
            continue;
        }
        by_symbol.insert(
            symbol.to_owned(),
            daily_bars(history, observed_ts_ms, cfg.daily_min_hourly_bars),
        );
    }
    let feature_ts_ms = by_symbol
        .values()
        .filter_map(|bars| bars.last().map(|row| row.ts_ms))
        .filter(|ts| *ts <= observed_ts_ms)
        .max();
    let Some(feature_ts_ms) = feature_ts_ms else {
        return LongFeatureBuild {
            feature_ts_ms: None,
            rows: Vec::new(),
            fallback_count: 0,
            rejections,
        };
    };

    let btc_regime = regime_features(
        by_symbol.get(&cfg.regime_symbol),
        feature_ts_ms,
        cfg.regime_sma_days,
        cfg.btc_rv_window_days,
        cfg.btc_rv_min_samples,
        cfg.btc_rv_null_value,
    );
    let eth_regime_on = sma_regime(by_symbol.get("ETHUSDT"), feature_ts_ms, cfg.regime_sma_days)
        .unwrap_or(cfg.regime_missing_is_on);

    let mut rows = Vec::new();
    for (symbol, bars) in &by_symbol {
        if !current.contains(symbol.as_str()) {
            continue;
        }
        if let Some(row) = long_row(symbol, bars, feature_ts_ms, cfg, btc_regime, eth_regime_on) {
            rows.push(row);
        } else {
            rejections.push(DataRejection {
                symbol: symbol.clone(),
                reason: "feature_row_unavailable".to_owned(),
                first_missing_ts_ms: None,
            });
        }
    }
    rows.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    assign_long_ranks(&mut rows, cfg);
    let fallback_count = apply_long_median_membership(&mut rows, cfg.universe_size);
    LongFeatureBuild {
        feature_ts_ms: Some(feature_ts_ms),
        rows,
        fallback_count,
        rejections,
    }
}

pub fn build_carry_features(
    klines: &KlineHistory,
    funding: &FundingHistory,
    whales: &WhaleHistory,
    symbols: &[String],
    observed_ts_ms: i64,
    cfg: &CarryFeatureConfig,
) -> CarryFeatureBuild {
    let day = observed_ts_ms - observed_ts_ms.rem_euclid(DAY_MS);
    let mut decision_ts_ms = day + cfg.decision_phase_ms;
    if observed_ts_ms < decision_ts_ms + cfg.decision_kline_lag_ms {
        decision_ts_ms -= DAY_MS;
    }
    if decision_ts_ms <= 0 {
        return CarryFeatureBuild {
            decision_ts_ms: None,
            rows: Vec::new(),
            rejections: Vec::new(),
        };
    }
    build_carry_features_at(
        klines,
        funding,
        whales,
        symbols,
        decision_ts_ms,
        observed_ts_ms,
        cfg,
    )
}

pub fn build_carry_features_at(
    klines: &KlineHistory,
    funding: &FundingHistory,
    whales: &WhaleHistory,
    symbols: &[String],
    decision_ts_ms: i64,
    observed_ts_ms: i64,
    cfg: &CarryFeatureConfig,
) -> CarryFeatureBuild {
    if decision_ts_ms <= 0
        || decision_ts_ms % DAY_MS != cfg.decision_phase_ms
        || decision_ts_ms > observed_ts_ms
    {
        return CarryFeatureBuild {
            decision_ts_ms: None,
            rows: Vec::new(),
            rejections: Vec::new(),
        };
    }
    let mut rows = Vec::new();
    let mut rejections = Vec::new();
    for symbol in symbols {
        let Some(history) = klines.get(symbol) else {
            rejections.push(DataRejection {
                symbol: symbol.clone(),
                reason: "cold_start_no_klines".to_owned(),
                first_missing_ts_ms: None,
            });
            continue;
        };
        let needed_hours = cfg
            .vol_window_hours
            .saturating_add(cfg.vol_return_lag_hours)
            .max(cfg.momentum_lookback_hours)
            .max(cfg.turn_growth_lookback_hours + cfg.adv_window_hours);
        if let Some(missing) = first_internal_hourly_gap(
            history,
            decision_ts_ms,
            usize::try_from(needed_hours).unwrap_or(usize::MAX),
        ) {
            rejections.push(DataRejection {
                symbol: symbol.clone(),
                reason: "hourly_kline_gap".to_owned(),
                first_missing_ts_ms: Some(missing),
            });
            continue;
        }
        let Some(row) = carry_row(
            symbol,
            history,
            funding.get(symbol),
            whales.get(symbol),
            decision_ts_ms,
            observed_ts_ms,
            cfg,
        ) else {
            rejections.push(DataRejection {
                symbol: symbol.clone(),
                reason: "momentum_not_mature_or_decision_bar_missing".to_owned(),
                first_missing_ts_ms: None,
            });
            continue;
        };
        rows.push(row);
    }
    rows.sort_by(|a, b| a.symbol.cmp(&b.symbol));
    assign_carry_ranks(&mut rows, cfg.universe_top_n);
    CarryFeatureBuild {
        decision_ts_ms: Some(decision_ts_ms),
        rows,
        rejections,
    }
}

pub fn build_carry_replay_features(
    klines: &KlineHistory,
    funding: &FundingHistory,
    whales: &WhaleHistory,
    symbols: &[String],
    decision_ts_ms: i64,
    observed_ts_ms: i64,
    cfg: &CarryFeatureConfig,
) -> CarryFeatureBuild {
    let mut rows = Vec::new();
    let mut latest_rejections = Vec::new();
    for days_back in (0..=cfg.minimum_replay_days).rev() {
        let ts = decision_ts_ms - days_back as i64 * DAY_MS;
        let built =
            build_carry_features_at(klines, funding, whales, symbols, ts, observed_ts_ms, cfg);
        if days_back == 0 {
            latest_rejections = built.rejections;
        }
        rows.extend(built.rows);
    }
    rows.sort_by(|left, right| {
        (&left.symbol, left.bar_ts_ms).cmp(&(&right.symbol, right.bar_ts_ms))
    });
    CarryFeatureBuild {
        decision_ts_ms: Some(decision_ts_ms),
        rows,
        rejections: latest_rejections,
    }
}

fn daily_bars(
    history: &BTreeMap<i64, HourlyKline>,
    observed_ts_ms: i64,
    min_hourly_bars: usize,
) -> Vec<DailyBar> {
    let mut groups: BTreeMap<i64, Vec<&HourlyKline>> = BTreeMap::new();
    for row in history.values() {
        if row.available_at_ms > observed_ts_ms || row.open_ts_ms + HOUR_MS > observed_ts_ms {
            continue;
        }
        let day_start = row.open_ts_ms - row.open_ts_ms.rem_euclid(DAY_MS);
        groups.entry(day_start).or_default().push(row);
    }
    let mut out = Vec::new();
    for (day_start, mut rows) in groups {
        rows.sort_by_key(|row| row.open_ts_ms);
        if rows.len() < min_hourly_bars {
            continue;
        }
        let last = rows[rows.len() - 1];
        out.push(DailyBar {
            ts_ms: day_start + DAY_MS,
            high: rows
                .iter()
                .map(|row| row.high)
                .fold(f64::NEG_INFINITY, f64::max),
            low: rows.iter().map(|row| row.low).fold(f64::INFINITY, f64::min),
            close: last.close,
            turnover_quote: rows.iter().map(|row| row.turnover_quote).sum(),
        });
    }
    out
}

fn long_row(
    symbol: &str,
    bars: &[DailyBar],
    ts_ms: i64,
    cfg: &LongFeatureConfig,
    btc_regime: (bool, f64),
    eth_regime_on: bool,
) -> Option<LongFeatureRow> {
    let bar = exact_daily(bars, ts_ms)?;
    let previous = exact_daily(bars, ts_ms - DAY_MS);
    let log_return = previous.map(|row| (bar.close / row.close).ln());
    let vol_values = rolling_daily_values(ts_ms, cfg.vol_estimate_window_days, |row_ts| {
        let current = exact_daily(bars, row_ts)?;
        let prior = exact_daily(bars, row_ts - DAY_MS)?;
        Some((current.close / prior.close).ln())
    });
    let realized_vol = (vol_values.len() >= cfg.vol_estimate_window_days)
        .then(|| sample_std(&vol_values))
        .flatten()
        .map(|value| value * 365.0_f64.sqrt());
    let turnover = rolling_daily_values(ts_ms, cfg.universe_volume_window_days, |row_ts| {
        exact_daily(bars, row_ts).map(|row| row.turnover_quote)
    });
    let turnover_median_90d = (turnover.len() >= cfg.universe_volume_window_days)
        .then(|| median(&turnover))
        .flatten();
    let pump_3d_log = exact_daily(bars, ts_ms - cfg.pump_lookback_days[0] as i64 * DAY_MS)
        .map(|prior| (bar.close / prior.close).ln());
    let pump_7d_log = exact_daily(bars, ts_ms - cfg.pump_lookback_days[1] as i64 * DAY_MS)
        .map(|prior| (bar.close / prior.close).ln());
    let close_location = range_location(bar.close, bar.low, bar.high);
    let range3 = rolling_range(bars, ts_ms, cfg.pump_lookback_days[0]);
    let range7 = rolling_range(bars, ts_ms, cfg.pump_lookback_days[1]);
    let true_ranges = rolling_daily_values(ts_ms, cfg.atr_window_days, |row_ts| {
        let current = exact_daily(bars, row_ts)?;
        let range = current.high - current.low;
        let value = exact_daily(bars, row_ts - DAY_MS).map_or(range, |prior| {
            range
                .max((current.high - prior.close).abs())
                .max((current.low - prior.close).abs())
        });
        Some(value)
    });
    let atr = (true_ranges.len() >= cfg.atr_min_samples)
        .then(|| mean(&true_ranges))
        .flatten();
    let first_ts = bars.first()?.ts_ms;
    let symbol_age_days = (ts_ms - first_ts) / DAY_MS + 1;
    Some(LongFeatureRow {
        symbol: symbol.to_owned(),
        ts_ms,
        close: bar.close,
        turnover_quote: bar.turnover_quote,
        log_return,
        realized_vol,
        sigma_daily_30d: realized_vol.map(|value| value / 365.0_f64.sqrt()),
        turnover_median_90d,
        today_volume_rank: 0,
        universe_rank: None,
        in_universe: false,
        pump_3d_log,
        pump_7d_log,
        close_location,
        close_loc_3d: range3.map(|(low, high)| range_location(bar.close, low, high)),
        close_loc_7d: range7.map(|(low, high)| range_location(bar.close, low, high)),
        atr_14d_pct: atr.map(|value| value / bar.close),
        regime_on: btc_regime.0,
        btc_rv_30: btc_regime.1,
        eth_regime_on,
        symbol_age_days,
    })
}

fn regime_features(
    bars: Option<&Vec<DailyBar>>,
    ts_ms: i64,
    sma_days: usize,
    rv_days: usize,
    rv_min_samples: usize,
    rv_null: f64,
) -> (bool, f64) {
    let Some(bars) = bars else {
        return (false, rv_null);
    };
    let Some(current) = exact_daily(bars, ts_ms) else {
        return (false, rv_null);
    };
    let closes = rolling_daily_values(ts_ms, sma_days, |row_ts| {
        exact_daily(bars, row_ts).map(|row| row.close)
    });
    let regime =
        closes.len() >= sma_days && mean(&closes).is_some_and(|average| current.close > average);
    let returns = rolling_daily_values(ts_ms, rv_days, |row_ts| {
        let row = exact_daily(bars, row_ts)?;
        let previous = exact_daily(bars, row_ts - DAY_MS)?;
        Some((row.close / previous.close).ln())
    });
    let rv = if returns.len() >= rv_min_samples {
        sample_std(&returns)
            .map(|value| value * 365.0_f64.sqrt())
            .unwrap_or(rv_null)
    } else {
        rv_null
    };
    (regime, rv)
}

fn sma_regime(bars: Option<&Vec<DailyBar>>, ts_ms: i64, days: usize) -> Option<bool> {
    let bars = bars?;
    let current = exact_daily(bars, ts_ms)?;
    let closes = rolling_daily_values(ts_ms, days, |row_ts| {
        exact_daily(bars, row_ts).map(|row| row.close)
    });
    (closes.len() >= days)
        .then(|| mean(&closes))
        .flatten()
        .map(|average| current.close > average)
}

fn assign_long_ranks(rows: &mut [LongFeatureRow], cfg: &LongFeatureConfig) {
    let mut today: Vec<usize> = (0..rows.len()).collect();
    today.sort_by(|a, b| {
        rows[*b]
            .turnover_quote
            .total_cmp(&rows[*a].turnover_quote)
            .then_with(|| rows[*a].symbol.cmp(&rows[*b].symbol))
    });
    for (rank, index) in today.into_iter().enumerate() {
        rows[index].today_volume_rank = u32::try_from(rank + 1).unwrap_or(u32::MAX);
    }
    let mut median_rows: Vec<usize> = rows
        .iter()
        .enumerate()
        .filter_map(|(index, row)| row.turnover_median_90d.map(|_| index))
        .collect();
    median_rows.sort_by(|a, b| {
        rows[*b]
            .turnover_median_90d
            .unwrap_or_default()
            .total_cmp(&rows[*a].turnover_median_90d.unwrap_or_default())
            .then_with(|| rows[*a].symbol.cmp(&rows[*b].symbol))
    });
    for (rank, index) in median_rows.into_iter().enumerate() {
        let rank = u32::try_from(rank + 1).unwrap_or(u32::MAX);
        rows[index].universe_rank = Some(rank);
        rows[index].in_universe = rank as usize <= cfg.universe_size
            && rows[index].symbol_age_days >= cfg.min_listing_history_days as i64;
    }
}

fn apply_long_median_membership(rows: &mut [LongFeatureRow], universe_size: usize) -> usize {
    let mut finite: Vec<usize> = rows
        .iter()
        .enumerate()
        .filter_map(|(index, row)| row.turnover_median_90d.map(|_| index))
        .collect();
    finite.sort_by(|a, b| {
        rows[*b]
            .turnover_median_90d
            .unwrap_or_default()
            .total_cmp(&rows[*a].turnover_median_90d.unwrap_or_default())
            .then_with(|| rows[*a].symbol.cmp(&rows[*b].symbol))
    });
    let mut members: BTreeSet<usize> = finite.into_iter().take(universe_size).collect();
    let before = members.len();
    if members.len() < universe_size {
        let mut cold: Vec<usize> = (0..rows.len())
            .filter(|index| !members.contains(index))
            .collect();
        cold.sort_by(|a, b| {
            rows[*b]
                .turnover_quote
                .total_cmp(&rows[*a].turnover_quote)
                .then_with(|| rows[*a].symbol.cmp(&rows[*b].symbol))
        });
        members.extend(cold.into_iter().take(universe_size - members.len()));
    }
    for (index, row) in rows.iter_mut().enumerate() {
        row.in_universe = members.contains(&index);
    }
    members.len() - before
}

fn carry_row(
    symbol: &str,
    history: &BTreeMap<i64, HourlyKline>,
    funding: Option<&BTreeMap<i64, SettledFunding>>,
    whales: Option<&BTreeMap<i64, BinanceWhaleObservation>>,
    decision_ts_ms: i64,
    observed_ts_ms: i64,
    cfg: &CarryFeatureConfig,
) -> Option<CarryFeatureRow> {
    let current = history.get(&(decision_ts_ms - HOUR_MS))?;
    let by_funding = funding.and_then(|rows| {
        rows.range(..=decision_ts_ms)
            .rev()
            .find_map(|(_, row)| (row.available_at_ms <= observed_ts_ms).then_some(row))
    });
    let by_funding_age_h =
        by_funding.map(|row| (decision_ts_ms - row.settlement_ts_ms) as f64 / HOUR_MS as f64);
    let adv24 = hourly_sum(history, decision_ts_ms, cfg.adv_window_hours, |row| {
        row.turnover_quote
    });
    let trail_fund_24h = trail_funding_at(
        history,
        funding,
        decision_ts_ms,
        cfg.trail_window_hours,
        observed_ts_ms,
    );
    let shifted_close = close_at_end(
        history,
        decision_ts_ms - cfg.momentum_lookback_hours * HOUR_MS,
    )?;
    let momentum = Some(current.close / shifted_close - 1.0);
    let ret_3d = close_at_end(
        history,
        decision_ts_ms - cfg.return_lookback_hours * HOUR_MS,
    )
    .map(|value| current.close / value - 1.0);
    let mut r24 = Vec::new();
    let start = decision_ts_ms - cfg.vol_window_hours * HOUR_MS;
    for offset in 0..cfg.vol_window_hours {
        let ts = start + offset * HOUR_MS;
        if let (Some(now), Some(prior)) = (
            close_at_end(history, ts),
            close_at_end(history, ts - cfg.vol_return_lag_hours * HOUR_MS),
        ) {
            r24.push(now / prior - 1.0);
        }
    }
    let vol_30d_daily = (r24.len() == cfg.vol_required_finite_samples)
        .then(|| sample_std(&r24))
        .flatten();
    let dtrail_2d = trail_fund_24h.and_then(|now| {
        trail_funding_at(
            history,
            funding,
            decision_ts_ms - cfg.trail_change_lookback_hours * HOUR_MS,
            cfg.trail_window_hours,
            observed_ts_ms,
        )
        .map(|prior| now - prior)
    });
    let turn_growth_3d = adv24.and_then(|now| {
        hourly_sum(
            history,
            decision_ts_ms - cfg.turn_growth_lookback_hours * HOUR_MS,
            cfg.adv_window_hours,
            |row| row.turnover_quote,
        )
        .and_then(|prior| (prior > 0.0).then_some(now / prior - 1.0))
    });
    let crowd_persistence = crowd_persistence(
        funding,
        decision_ts_ms,
        observed_ts_ms,
        cfg.persistence_window_settlements,
        cfg.enter_bp,
    );
    let d_tt_ls_3d = whales.and_then(|rows| {
        let now = fresh_whale(
            rows,
            decision_ts_ms,
            observed_ts_ms,
            cfg.whale_freshness_hours,
        )?;
        let prior = fresh_whale(
            rows,
            decision_ts_ms - cfg.whale_change_lookback_hours * HOUR_MS,
            observed_ts_ms,
            cfg.whale_freshness_hours,
        )?;
        Some(now - prior)
    });
    Some(CarryFeatureRow {
        symbol: symbol.to_owned(),
        bar_ts_ms: decision_ts_ms,
        by_close: current.close,
        by_turnover_quote: current.turnover_quote,
        by_funding: by_funding.map(|row| row.rate),
        by_funding_age_h,
        adv24,
        trail_fund_24h,
        momentum,
        ret_3d,
        vol_30d_daily,
        dtrail_2d,
        crowd_persistence,
        turn_growth_3d,
        d_tt_ls_3d,
        adv_rank: None,
        in_universe: false,
    })
}

fn assign_carry_ranks(rows: &mut [CarryFeatureRow], top_n: usize) {
    let mut ranked: Vec<usize> = rows
        .iter()
        .enumerate()
        .filter_map(|(index, row)| row.adv24.map(|_| index))
        .collect();
    ranked.sort_by(|a, b| {
        rows[*b]
            .adv24
            .unwrap_or_default()
            .total_cmp(&rows[*a].adv24.unwrap_or_default())
            .then_with(|| rows[*a].symbol.cmp(&rows[*b].symbol))
    });
    for (rank, index) in ranked.into_iter().enumerate() {
        let rank = u32::try_from(rank + 1).unwrap_or(u32::MAX);
        rows[index].adv_rank = Some(rank);
        rows[index].in_universe = rank as usize <= top_n;
    }
}

fn crowd_persistence(
    funding: Option<&BTreeMap<i64, SettledFunding>>,
    ts_ms: i64,
    observed_ts_ms: i64,
    window: Option<usize>,
    deep_bp: f64,
) -> Option<f64> {
    let window = window?;
    let rows = funding?;
    let latest = rows
        .range(..=ts_ms)
        .rev()
        .find_map(|(_, row)| (row.available_at_ms <= observed_ts_ms).then_some(row))?;
    let interval_ms = latest.funding_interval_min.checked_mul(60_000)?;
    if interval_ms <= 0 || ts_ms.saturating_sub(latest.settlement_ts_ms) >= interval_ms {
        return None;
    }
    let mut deep = 0;
    for offset in 1..=window {
        let offset = i64::try_from(offset).ok()?;
        let expected_ts_ms = latest
            .settlement_ts_ms
            .checked_sub(offset.checked_mul(interval_ms)?)?;
        let row = rows.get(&expected_ts_ms)?;
        if row.available_at_ms > observed_ts_ms
            || row.funding_interval_min != latest.funding_interval_min
        {
            return None;
        }
        deep += usize::from(row.rate < -deep_bp / 1e4);
    }
    Some(deep as f64 / window as f64)
}

fn fresh_whale(
    rows: &BTreeMap<i64, BinanceWhaleObservation>,
    ts_ms: i64,
    observed_ts_ms: i64,
    freshness_hours: i64,
) -> Option<f64> {
    let row = rows
        .range(..=ts_ms)
        .rev()
        .find_map(|(_, row)| (row.available_at_ms <= observed_ts_ms).then_some(row))?;
    if ts_ms - row.day_end_ms > freshness_hours * HOUR_MS {
        return None;
    }
    row.long_short_ratio
}

fn trail_funding_at(
    history: &BTreeMap<i64, HourlyKline>,
    funding: Option<&BTreeMap<i64, SettledFunding>>,
    ts_ms: i64,
    hours: i64,
    observed_ts_ms: i64,
) -> Option<f64> {
    if !contiguous_window(history, ts_ms, hours) {
        return None;
    }
    let rows = funding?;
    let latest = rows
        .range(..=ts_ms)
        .rev()
        .find_map(|(_, row)| (row.available_at_ms <= observed_ts_ms).then_some(row))?;
    let interval_ms = latest.funding_interval_min.checked_mul(60_000)?;
    if interval_ms <= 0 || ts_ms.saturating_sub(latest.settlement_ts_ms) >= interval_ms {
        return None;
    }
    let start_ms = ts_ms.saturating_sub(hours.saturating_mul(HOUR_MS));
    let mut settlement_ts_ms = latest.settlement_ts_ms;
    let mut total = 0.0;
    while settlement_ts_ms > start_ms {
        let row = rows.get(&settlement_ts_ms)?;
        if row.available_at_ms > observed_ts_ms
            || row.funding_interval_min != latest.funding_interval_min
        {
            return None;
        }
        total += row.rate;
        settlement_ts_ms = settlement_ts_ms.checked_sub(interval_ms)?;
    }
    Some(total)
}

fn hourly_sum(
    history: &BTreeMap<i64, HourlyKline>,
    end_ts_ms: i64,
    hours: i64,
    value: impl Fn(&HourlyKline) -> f64,
) -> Option<f64> {
    if !contiguous_window(history, end_ts_ms, hours) {
        return None;
    }
    Some(
        (0..hours)
            .filter_map(|offset| history.get(&(end_ts_ms - (offset + 1) * HOUR_MS)))
            .map(value)
            .sum(),
    )
}

fn contiguous_window(history: &BTreeMap<i64, HourlyKline>, end_ts_ms: i64, hours: i64) -> bool {
    (0..hours).all(|offset| history.contains_key(&(end_ts_ms - (offset + 1) * HOUR_MS)))
}

fn close_at_end(history: &BTreeMap<i64, HourlyKline>, end_ts_ms: i64) -> Option<f64> {
    history.get(&(end_ts_ms - HOUR_MS)).map(|row| row.close)
}

fn exact_daily(bars: &[DailyBar], ts_ms: i64) -> Option<&DailyBar> {
    bars.binary_search_by_key(&ts_ms, |row| row.ts_ms)
        .ok()
        .map(|index| &bars[index])
}

fn rolling_daily_values(ts_ms: i64, days: usize, value: impl Fn(i64) -> Option<f64>) -> Vec<f64> {
    (0..days)
        .filter_map(|offset| value(ts_ms - offset as i64 * DAY_MS))
        .collect()
}

fn rolling_range(bars: &[DailyBar], ts_ms: i64, days: usize) -> Option<(f64, f64)> {
    let mut low = f64::INFINITY;
    let mut high = f64::NEG_INFINITY;
    for offset in 0..days {
        let row = exact_daily(bars, ts_ms - offset as i64 * DAY_MS)?;
        low = low.min(row.low);
        high = high.max(row.high);
    }
    Some((low, high))
}

fn range_location(close: f64, low: f64, high: f64) -> f64 {
    if high - low > 1e-12 {
        (close - low) / (high - low)
    } else {
        0.5
    }
}

fn mean(values: &[f64]) -> Option<f64> {
    (!values.is_empty()).then(|| values.iter().sum::<f64>() / values.len() as f64)
}

fn sample_std(values: &[f64]) -> Option<f64> {
    if values.len() < 2 {
        return None;
    }
    let mean = values.iter().sum::<f64>() / values.len() as f64;
    let variance = values
        .iter()
        .map(|value| (value - mean).powi(2))
        .sum::<f64>()
        / (values.len() - 1) as f64;
    Some(variance.sqrt())
}

fn median(values: &[f64]) -> Option<f64> {
    if values.is_empty() {
        return None;
    }
    let mut values = values.to_vec();
    values.sort_by(f64::total_cmp);
    let middle = values.len() / 2;
    if values.len().is_multiple_of(2) {
        Some((values[middle - 1] + values[middle]) / 2.0)
    } else {
        Some(values[middle])
    }
}

fn first_internal_gap(
    history: &BTreeMap<i64, HourlyKline>,
    observed_ts_ms: i64,
    lookback_days: usize,
) -> Option<i64> {
    first_internal_hourly_gap(history, observed_ts_ms, lookback_days.saturating_mul(24))
}

fn first_internal_hourly_gap(
    history: &BTreeMap<i64, HourlyKline>,
    end_ts_ms: i64,
    lookback_hours: usize,
) -> Option<i64> {
    let start = end_ts_ms - lookback_hours as i64 * HOUR_MS;
    let mut times = history.range(start..end_ts_ms).map(|(time, _)| *time);
    let mut previous = times.next()?;
    for current in times {
        if current != previous + HOUR_MS {
            return Some(previous + HOUR_MS);
        }
        previous = current;
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Deserialize)]
    struct LongGolden {
        base_ts_ms: i64,
        days: usize,
        observed_ts_ms: i64,
        rows: Vec<LongFeatureRow>,
    }

    #[derive(Deserialize)]
    struct CarryGolden {
        base_ts_ms: i64,
        days: usize,
        decision_ts_ms: i64,
        rows: Vec<CarryFeatureRow>,
    }

    #[test]
    fn sample_std_matches_ddof_one() {
        assert!((sample_std(&[1.0, 2.0, 3.0]).unwrap() - 1.0).abs() < 1e-12);
    }

    #[test]
    fn daily_bars_accept_the_configured_minimum_without_a_contiguity_gate() {
        let day = 100 * DAY_MS;
        let history: BTreeMap<_, _> = (0..24)
            .filter(|hour| *hour != 7)
            .map(|hour| {
                let open_ts_ms = day + hour * HOUR_MS;
                (
                    open_ts_ms,
                    HourlyKline {
                        symbol: "BTCUSDT".into(),
                        open_ts_ms,
                        available_at_ms: day + DAY_MS,
                        open: 1.0,
                        high: 1.0,
                        low: 1.0,
                        close: 1.0,
                        volume_base: 1.0,
                        turnover_quote: 1.0,
                    },
                )
            })
            .collect();
        let bars = daily_bars(&history, day + DAY_MS, 20);
        assert_eq!(bars.len(), 1);
        assert_eq!(bars[0].turnover_quote, 23.0);
    }

    #[test]
    fn whale_is_nulled_before_the_three_day_shift() {
        let day = 100 * DAY_MS;
        let mut rows = BTreeMap::new();
        rows.insert(
            day - 6 * DAY_MS,
            BinanceWhaleObservation {
                symbol: "BTCUSDT".into(),
                day_end_ms: day - 6 * DAY_MS,
                available_at_ms: day,
                long_short_ratio: Some(2.0),
            },
        );
        rows.insert(
            day,
            BinanceWhaleObservation {
                symbol: "BTCUSDT".into(),
                day_end_ms: day,
                available_at_ms: day,
                long_short_ratio: Some(1.0),
            },
        );
        assert_eq!(fresh_whale(&rows, day, day, 48), Some(1.0));
        assert_eq!(fresh_whale(&rows, day - 3 * DAY_MS, day, 48), None);
    }

    #[test]
    fn future_available_funding_and_whales_are_invisible() {
        let day = 100 * DAY_MS;
        let mut funding = BTreeMap::new();
        funding.insert(
            day,
            SettledFunding {
                symbol: "BTCUSDT".into(),
                settlement_ts_ms: day,
                available_at_ms: day + 10,
                rate: -0.01,
                funding_interval_min: 480,
            },
        );
        assert_eq!(
            crowd_persistence(Some(&funding), day, day, Some(1), 10.0),
            None
        );

        let mut whales = BTreeMap::new();
        whales.insert(
            day,
            BinanceWhaleObservation {
                symbol: "BTCUSDT".into(),
                day_end_ms: day,
                available_at_ms: day + 10,
                long_short_ratio: Some(1.2),
            },
        );
        assert_eq!(fresh_whale(&whales, day, day, 48), None);
    }

    #[test]
    fn funding_trails_require_each_expected_settlement_in_both_windows() {
        let decision = 100 * DAY_MS;
        let mut history = BTreeMap::new();
        for open_ts_ms in (decision - 96 * HOUR_MS..decision).step_by(HOUR_MS as usize) {
            history.insert(
                open_ts_ms,
                HourlyKline {
                    symbol: "BTCUSDT".into(),
                    open_ts_ms,
                    available_at_ms: decision,
                    open: 1.0,
                    high: 1.0,
                    low: 1.0,
                    close: 1.0,
                    volume_base: 1.0,
                    turnover_quote: 1.0,
                },
            );
        }
        let mut funding = BTreeMap::new();
        for settlement_ts_ms in (decision - 96 * HOUR_MS..=decision).step_by(HOUR_MS as usize) {
            funding.insert(
                settlement_ts_ms,
                SettledFunding {
                    symbol: "BTCUSDT".into(),
                    settlement_ts_ms,
                    available_at_ms: decision,
                    rate: -0.001,
                    funding_interval_min: 60,
                },
            );
        }
        close_option(
            trail_funding_at(&history, Some(&funding), decision, 24, decision),
            Some(-0.024),
        );
        funding.remove(&(decision - 22 * HOUR_MS));
        assert_eq!(
            trail_funding_at(&history, Some(&funding), decision, 24, decision),
            None
        );
        funding.insert(
            decision - 22 * HOUR_MS,
            SettledFunding {
                symbol: "BTCUSDT".into(),
                settlement_ts_ms: decision - 22 * HOUR_MS,
                available_at_ms: decision,
                rate: -0.001,
                funding_interval_min: 60,
            },
        );
        let shifted = decision - 48 * HOUR_MS;
        funding.remove(&(shifted - 22 * HOUR_MS));
        assert_eq!(
            trail_funding_at(&history, Some(&funding), shifted, 24, decision),
            None
        );
    }

    #[test]
    fn long_features_match_recorded_golden_with_identical_nulls() {
        let golden: LongGolden =
            serde_json::from_str(include_str!("../tests/fixtures/long_feature_golden.json"))
                .unwrap();
        let mut history = KlineHistory::new();
        let symbols = ["AAAUSDT", "BTCUSDT", "ETHUSDT"];
        for (symbol_index, symbol) in symbols.iter().enumerate() {
            let mut prior = 100.0 * (symbol_index + 1) as f64;
            for hour in 0..golden.days * 24 {
                let h = hour as f64;
                let close = 100.0
                    * (symbol_index + 1) as f64
                    * ((0.00008 + symbol_index as f64 * 0.00001) * h
                        + (0.004 + symbol_index as f64 * 0.001) * (h / 19.0).sin())
                    .exp();
                let open_ts_ms = golden.base_ts_ms + hour as i64 * HOUR_MS;
                history.entry((*symbol).to_owned()).or_default().insert(
                    open_ts_ms,
                    HourlyKline {
                        symbol: (*symbol).to_owned(),
                        open_ts_ms,
                        available_at_ms: golden.observed_ts_ms,
                        open: prior,
                        high: prior.max(close) * 1.01,
                        low: prior.min(close) * 0.99,
                        close,
                        volume_base: 10.0 + symbol_index as f64,
                        turnover_quote: (symbol_index + 1) as f64 * 1000.0
                            + (hour % 24) as f64 * 10.0,
                    },
                );
                prior = close;
            }
        }
        let config = LongFeatureConfig {
            profile_name: "v12".into(),
            execution_strategy_id: "long_native_v12_wide_stop".into(),
            exclude_symbols: Vec::new(),
            universe_size: 50,
            universe_volume_window_days: 90,
            min_listing_history_days: 30,
            regime_symbol: "BTCUSDT".into(),
            regime_sma_days: 30,
            vol_estimate_window_days: 30,
            daily_min_hourly_bars: 20,
            cold_start_lookback_days: 100,
            pump_lookback_days: [3, 7],
            atr_window_days: 14,
            atr_min_samples: 7,
            btc_rv_window_days: 30,
            btc_rv_min_samples: 20,
            btc_rv_null_value: 0.8,
            regime_missing_is_on: false,
            median_fallback_to_daily_turnover: true,
        };
        let actual = build_long_features(
            &history,
            &symbols
                .iter()
                .map(|value| (*value).to_owned())
                .collect::<Vec<_>>(),
            golden.observed_ts_ms,
            &config,
        );
        assert_eq!(actual.rows.len(), golden.rows.len());
        for (actual, expected) in actual.rows.iter().zip(&golden.rows) {
            assert_eq!(actual.symbol, expected.symbol);
            assert_eq!(actual.ts_ms, expected.ts_ms);
            assert_eq!(actual.today_volume_rank, expected.today_volume_rank);
            assert_eq!(actual.universe_rank, expected.universe_rank);
            assert_eq!(actual.in_universe, expected.in_universe);
            assert_eq!(actual.regime_on, expected.regime_on);
            assert_eq!(actual.eth_regime_on, expected.eth_regime_on);
            assert_eq!(actual.symbol_age_days, expected.symbol_age_days);
            close(actual.close, expected.close);
            close(actual.turnover_quote, expected.turnover_quote);
            close_option(actual.log_return, expected.log_return);
            close_option(actual.realized_vol, expected.realized_vol);
            close_option(actual.sigma_daily_30d, expected.sigma_daily_30d);
            close_option(actual.turnover_median_90d, expected.turnover_median_90d);
            close_option(actual.pump_3d_log, expected.pump_3d_log);
            close_option(actual.pump_7d_log, expected.pump_7d_log);
            close(actual.close_location, expected.close_location);
            close_option(actual.close_loc_3d, expected.close_loc_3d);
            close_option(actual.close_loc_7d, expected.close_loc_7d);
            close_option(actual.atr_14d_pct, expected.atr_14d_pct);
            close(actual.btc_rv_30, expected.btc_rv_30);
        }
        engine_strategies::native_common::validate_exact_symbol_coverage(
            &symbols
                .iter()
                .map(|symbol| (*symbol).to_owned())
                .collect::<Vec<_>>(),
            &actual
                .rows
                .iter()
                .map(|row| row.symbol.clone())
                .collect::<Vec<_>>(),
            &actual
                .rejections
                .iter()
                .map(|row| row.symbol.clone())
                .collect::<Vec<_>>(),
        )
        .expect("LONG producer emits an exact sleeve partition");

        let mut gapped = history.clone();
        let missing = golden.observed_ts_ms - 10 * HOUR_MS;
        gapped.get_mut("AAAUSDT").unwrap().remove(&missing);
        let gapped = build_long_features(
            &gapped,
            &symbols
                .iter()
                .map(|value| (*value).to_owned())
                .collect::<Vec<_>>(),
            golden.observed_ts_ms,
            &config,
        );
        assert!(gapped.rows.iter().all(|row| row.symbol != "AAAUSDT"));
        assert_eq!(
            gapped
                .rejections
                .iter()
                .filter(|row| row.symbol == "AAAUSDT")
                .count(),
            1,
            "a gapped symbol is rejected instead of accepted and rejected"
        );

        let cold = build_long_features(
            &KlineHistory::new(),
            &["AAAUSDT".to_owned()],
            golden.observed_ts_ms,
            &config,
        );
        assert_eq!(
            cold.rejections
                .iter()
                .map(|row| row.symbol.as_str())
                .collect::<Vec<_>>(),
            vec!["AAAUSDT"],
            "support-only regime symbols never escape into sleeve rejections"
        );
    }

    #[test]
    fn carry_features_match_recorded_golden_with_identical_nulls() {
        let golden: CarryGolden =
            serde_json::from_str(include_str!("../tests/fixtures/carry_feature_golden.json"))
                .unwrap();
        let symbols = ["AAAUSDT", "BBBUSDT"];
        let mut klines = KlineHistory::new();
        let mut funding = FundingHistory::new();
        let mut whales = WhaleHistory::new();
        for (symbol_index, symbol) in symbols.iter().enumerate() {
            for hour in 0..golden.days * 24 {
                let h = (hour + 1) as f64;
                let close = 100.0
                    * (symbol_index + 1) as f64
                    * ((0.00005 + symbol_index as f64 * 0.00001) * h
                        + (0.003 + symbol_index as f64 * 0.001) * (h / 23.0).sin())
                    .exp();
                let open_ts_ms = golden.base_ts_ms + hour as i64 * HOUR_MS;
                klines.entry((*symbol).to_owned()).or_default().insert(
                    open_ts_ms,
                    HourlyKline {
                        symbol: (*symbol).to_owned(),
                        open_ts_ms,
                        available_at_ms: golden.decision_ts_ms,
                        open: close,
                        high: close,
                        low: close,
                        close,
                        volume_base: 1.0,
                        turnover_quote: (symbol_index + 1) as f64 * 1000.0
                            + (hour % 24) as f64 * 17.0,
                    },
                );
            }
            for settlement in 0..=golden.days * 3 {
                let settlement_ts_ms = golden.base_ts_ms + settlement as i64 * 8 * HOUR_MS;
                let rate = -0.0015 + 0.0003 * (settlement as f64 / 5.0 + symbol_index as f64).sin();
                funding.entry((*symbol).to_owned()).or_default().insert(
                    settlement_ts_ms,
                    SettledFunding {
                        symbol: (*symbol).to_owned(),
                        settlement_ts_ms,
                        available_at_ms: golden.decision_ts_ms,
                        rate,
                        funding_interval_min: 480,
                    },
                );
            }
            for day in 0..=golden.days {
                let day_end_ms = golden.base_ts_ms + day as i64 * DAY_MS;
                whales.entry((*symbol).to_owned()).or_default().insert(
                    day_end_ms,
                    BinanceWhaleObservation {
                        symbol: (*symbol).to_owned(),
                        day_end_ms,
                        available_at_ms: golden.decision_ts_ms,
                        long_short_ratio: Some(
                            1.5 + symbol_index as f64 * 0.1
                                + 0.02 * (day as f64 / 3.0 + symbol_index as f64).sin(),
                        ),
                    },
                );
            }
        }
        let config = CarryFeatureConfig {
            config_id: "lane2_carry_hold_v7".into(),
            universe_top_n: 100,
            enter_bp: 10.0,
            persistence_window_settlements: Some(20),
            momentum_lookback_hours: 168,
            adv_window_hours: 24,
            return_lookback_hours: 72,
            vol_window_hours: 720,
            vol_return_lag_hours: 24,
            vol_required_finite_samples: 720,
            trail_window_hours: 24,
            trail_change_lookback_hours: 48,
            turn_growth_lookback_hours: 72,
            whale_change_lookback_hours: 72,
            whale_freshness_hours: 48,
            whale_feed_days: 6,
            settlement_age_reset_threshold_hours: 0.5,
            decision_phase_ms: 0,
            decision_kline_lag_ms: 1_200_000,
            minimum_replay_days: 90,
            minimum_decision_symbols: 50,
            minimum_funding_coverage: 0.5,
            standing_funding_max_age_hours: 25.0,
            presettlement_window_ms: 900_000,
            missing_conditioning: "fail_open".into(),
            missing_depth: "floor".into(),
            stale_whale: "null_fail_open".into(),
        };
        let actual = build_carry_features_at(
            &klines,
            &funding,
            &whales,
            &symbols
                .iter()
                .map(|value| (*value).to_owned())
                .collect::<Vec<_>>(),
            golden.decision_ts_ms,
            golden.decision_ts_ms,
            &config,
        );
        assert_eq!(actual.rows.len(), golden.rows.len());
        for (actual, expected) in actual.rows.iter().zip(&golden.rows) {
            assert_eq!(actual.symbol, expected.symbol);
            assert_eq!(actual.bar_ts_ms, expected.bar_ts_ms);
            assert_eq!(actual.adv_rank, expected.adv_rank);
            assert_eq!(actual.in_universe, expected.in_universe);
            close(actual.by_close, expected.by_close);
            close(actual.by_turnover_quote, expected.by_turnover_quote);
            close_option(actual.by_funding, expected.by_funding);
            close_option(actual.by_funding_age_h, expected.by_funding_age_h);
            close_option(actual.adv24, expected.adv24);
            close_option(actual.trail_fund_24h, expected.trail_fund_24h);
            close_option(actual.momentum, expected.momentum);
            close_option(actual.ret_3d, expected.ret_3d);
            close_option(actual.vol_30d_daily, expected.vol_30d_daily);
            close_option(actual.dtrail_2d, expected.dtrail_2d);
            close_option(actual.crowd_persistence, expected.crowd_persistence);
            close_option(actual.turn_growth_3d, expected.turn_growth_3d);
            close_option(actual.d_tt_ls_3d, expected.d_tt_ls_3d);
        }
        engine_strategies::native_common::validate_exact_symbol_coverage(
            &symbols
                .iter()
                .map(|symbol| (*symbol).to_owned())
                .collect::<Vec<_>>(),
            &actual
                .rows
                .iter()
                .filter(|row| row.bar_ts_ms == golden.decision_ts_ms)
                .map(|row| row.symbol.clone())
                .collect::<Vec<_>>(),
            &actual
                .rejections
                .iter()
                .map(|row| row.symbol.clone())
                .collect::<Vec<_>>(),
        )
        .expect("CARRY producer emits an exact sleeve partition");

        let mut gapped = klines.clone();
        let missing = golden.decision_ts_ms - 10 * HOUR_MS;
        gapped.get_mut("AAAUSDT").unwrap().remove(&missing);
        let gapped = build_carry_features_at(
            &gapped,
            &funding,
            &whales,
            &symbols
                .iter()
                .map(|value| (*value).to_owned())
                .collect::<Vec<_>>(),
            golden.decision_ts_ms,
            golden.decision_ts_ms,
            &config,
        );
        assert!(gapped.rows.iter().all(|row| row.symbol != "AAAUSDT"));
        assert_eq!(
            gapped
                .rejections
                .iter()
                .filter(|row| row.symbol == "AAAUSDT")
                .count(),
            1,
            "a gapped symbol is rejected instead of accepted and rejected"
        );
    }

    #[test]
    fn realistic_150_symbol_90_day_payload_fits_cap_and_replays_native() {
        use engine_strategies::native_carry::scorer::{
            score_decision, CarryFeatureRow as NativeCarryFeatureRow, CarryRuleConfig, ScorerState,
        };

        let first_ts_ms = 10 * DAY_MS;
        let decision_ts_ms = first_ts_ms + 90 * DAY_MS;
        let mut rows = Vec::new();
        for symbol_index in 0..150 {
            let symbol = format!("S{symbol_index:03}USDT");
            for day in 0..=90 {
                rows.push(CarryFeatureRow {
                    symbol: symbol.clone(),
                    bar_ts_ms: first_ts_ms + day * DAY_MS,
                    by_close: 100.0 + symbol_index as f64,
                    by_turnover_quote: 1_000_000.0 + symbol_index as f64,
                    by_funding: Some(-0.002),
                    by_funding_age_h: Some(0.0),
                    adv24: Some(10_000_000.0 + symbol_index as f64),
                    trail_fund_24h: Some(-0.012),
                    momentum: Some(0.1),
                    ret_3d: Some(0.1),
                    vol_30d_daily: Some(0.1),
                    dtrail_2d: Some(0.0),
                    crowd_persistence: Some(1.0),
                    turn_growth_3d: Some(1.0),
                    d_tt_ls_3d: Some(1.0),
                    adv_rank: Some(symbol_index + 1),
                    in_universe: true,
                });
            }
        }
        rows.sort_by(|left, right| {
            (&left.symbol, left.bar_ts_ms).cmp(&(&right.symbol, right.bar_ts_ms))
        });
        let encoded_rows = serde_json::to_vec(&rows).unwrap();
        assert_eq!(rows.len(), 150 * 91);
        assert!(encoded_rows.len() < engine_types::MAX_SIGNAL_OBSERVATION_BYTES);
        let native_rows: Vec<NativeCarryFeatureRow> =
            serde_json::from_slice(&encoded_rows).unwrap();
        let rule = CarryRuleConfig {
            config_id: "lane2_carry_hold_v7".into(),
            universe_top_n: 100,
            enter_bp: 10.0,
            exit_bp: 3.0,
            per_name_cap: 0.1,
            gross_cap: 1.0,
            depth_ref_bp_per_day: 120.0,
            depth_floor: 0.25,
            depth_exponent: 1.5,
            toxic_band_ret3d_lo: -0.30,
            toxic_band_ret3d_hi: 0.0,
            min_vol30_daily: 0.05,
            trail_recovery_exit_bp_2d: 30.0,
            persistence_cut: 0.10,
            persistence_lo: 0.0,
            flow_cut: 0.40,
            flow_lo: 0.5,
            whale_cut: -0.26,
            whale_lo: 0.5,
        };
        let (decision, state) =
            score_decision(&native_rows, decision_ts_ms, &ScorerState::default(), &rule)
                .expect("worker cold replay must satisfy the native 45-day floor");
        assert_eq!(decision.replay_days, 90);
        assert_eq!(decision.universe_size, 100);
        assert_eq!(state.last_decision_ts_ms, decision_ts_ms);

        let upcoming_worker_rows: Vec<CarryFeatureRow> = rows
            .iter()
            .filter(|row| row.bar_ts_ms == decision_ts_ms)
            .cloned()
            .map(|mut row| {
                row.bar_ts_ms += DAY_MS;
                row
            })
            .collect();
        let symbols = (0..150)
            .map(|symbol_index| format!("S{symbol_index:03}USDT"))
            .collect::<Vec<_>>();
        let envelope = crate::model::SignalPayloadEnvelope {
            schema_version: crate::SCHEMA_VERSION,
            config: crate::config::ConfigIdentity {
                schema_version: crate::SCHEMA_VERSION,
                signal_config_id: "realistic_150_symbol_replay".into(),
                long_profile: "v12".into(),
                long_execution_strategy_id: "long_native_v12_wide_stop".into(),
                signal_config_sha256: "1".repeat(64),
                long_rule_sha256: "2".repeat(64),
                long_feature_contract_sha256: "8".repeat(64),
                carry_config_id: "lane2_carry_hold_v7".into(),
                carry_rule_sha256: "3".repeat(64),
                carry_feature_contract_sha256: "9".repeat(64),
                operational_profile_sha256: "4".repeat(64),
                engine_config_sha256: "5".repeat(64),
                long_decision_fingerprint: "6".repeat(64),
                carry_decision_fingerprint: "7".repeat(64),
            },
            universe: Some(crate::model::UniverseIdentity {
                mode: crate::model::UniverseMode::Pit,
                environment: "demo".into(),
                endpoint: "api-demo.bybit.com".into(),
                snapshot_ts_ms: first_ts_ms,
                available_at_ms: first_ts_ms,
                artifact_sha256: "8".repeat(64),
                file_sha256: "9".repeat(64),
                symbols: symbols.clone(),
                long_symbols: symbols.clone(),
                carry_symbols: symbols,
            }),
            payload: crate::model::ObservationPayload::CarryFeatureBatch {
                decision_ts_ms,
                rows: rows.clone(),
                upcoming_rows: upcoming_worker_rows.clone(),
                settled_funding: Vec::new(),
                presettlement: Vec::new(),
                marks: Vec::new(),
                rejections: Vec::new(),
            },
        };
        let full_payload = serde_json::to_vec(&envelope).unwrap();
        assert!(full_payload.len() < engine_types::MAX_SIGNAL_OBSERVATION_BYTES);
        let upcoming_native_rows: Vec<NativeCarryFeatureRow> =
            serde_json::from_value(serde_json::to_value(&upcoming_worker_rows).unwrap()).unwrap();
        let (upcoming, _) = score_decision(
            &upcoming_native_rows,
            decision_ts_ms + DAY_MS,
            &state,
            &rule,
        )
        .expect("causally frozen upcoming worker rows must advance the native scorer");
        assert_eq!(upcoming.decision_ts_ms, decision_ts_ms + DAY_MS);
    }

    fn close(actual: f64, expected: f64) {
        let tolerance = 1e-12_f64.max(expected.abs() * 1e-12);
        assert!(
            (actual - expected).abs() <= tolerance,
            "{actual} differs from {expected} by more than {tolerance}"
        );
    }

    fn close_option(actual: Option<f64>, expected: Option<f64>) {
        assert_eq!(
            actual.is_none(),
            expected.is_none(),
            "null position differs"
        );
        if let (Some(actual), Some(expected)) = (actual, expected) {
            close(actual, expected);
        }
    }
}
