use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::config::ConfigIdentity;
pub use engine_strategies::native_common::{TickerObservation, UniverseIdentity, UniverseMode};
pub use engine_types::SignalObservation as NormalizedObservation;

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct HourlyKline {
    pub symbol: String,
    /// Bybit's bar-open stamp.
    pub open_ts_ms: i64,
    /// First instant the closed bar was available to the worker.
    pub available_at_ms: i64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume_base: f64,
    pub turnover_quote: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SettledFunding {
    pub symbol: String,
    pub settlement_ts_ms: i64,
    pub available_at_ms: i64,
    pub rate: f64,
    pub funding_interval_min: i64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct BinanceWhaleObservation {
    pub symbol: String,
    /// End of the complete UTC metrics day.
    pub day_end_ms: i64,
    pub available_at_ms: i64,
    pub long_short_ratio: Option<f64>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct InstrumentObservation {
    pub symbol: String,
    pub observed_ts_ms: i64,
    pub available_at_ms: i64,
    pub contract_type: Option<String>,
    pub symbol_type: Option<String>,
    pub status: Option<String>,
    pub base_coin: Option<String>,
    pub quote_coin: Option<String>,
    pub settle_coin: Option<String>,
    pub launch_time_ms: Option<i64>,
    pub delivery_time_ms: Option<i64>,
    pub tick_size: Option<f64>,
    pub qty_step: Option<f64>,
    pub min_order_qty: Option<f64>,
    pub min_notional_value: Option<f64>,
    pub max_order_qty: Option<f64>,
    pub max_market_order_qty: Option<f64>,
    pub funding_interval_min: Option<i64>,
    pub is_prelisting: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct InstrumentTradingInterval {
    pub trading_from_ms: i64,
    pub trading_through_ms: Option<i64>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct BootstrapCoverage {
    pub completed_at_ms: i64,
    pub kline_end_ms: i64,
    pub funding_end_ms: i64,
    pub whale_end_ms: i64,
    pub source_contract_sha256: String,
    pub long_feature_sha256: String,
    pub carry_feature_sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct SourceCoverage {
    pub symbol: String,
    pub checked_from_ms: i64,
    pub checked_through_ms: i64,
    #[serde(default)]
    pub replace_coverage: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct CoverageInterval {
    pub checked_from_ms: i64,
    pub checked_through_ms: i64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum WireEvent {
    BybitKlineBatch {
        schema_version: u32,
        sequence: u64,
        symbol: String,
        available_at_ms: i64,
        #[serde(default)]
        checked_from_ms: Option<i64>,
        #[serde(default)]
        checked_through_ms: Option<i64>,
        #[serde(default)]
        replace_coverage: bool,
        rows: Vec<Vec<Value>>,
    },
    BybitFundingBatch {
        schema_version: u32,
        sequence: u64,
        symbol: String,
        available_at_ms: i64,
        #[serde(default)]
        checked_from_ms: Option<i64>,
        #[serde(default)]
        checked_through_ms: Option<i64>,
        #[serde(default)]
        replace_coverage: bool,
        #[serde(default)]
        emit_lifecycle: bool,
        rows: Vec<BybitFundingWire>,
    },
    BybitInstrumentSnapshot {
        schema_version: u32,
        sequence: u64,
        observed_ts_ms: i64,
        available_at_ms: i64,
        rows: Vec<BybitInstrumentWire>,
    },
    BybitTickerSnapshot {
        schema_version: u32,
        sequence: u64,
        observed_ts_ms: i64,
        available_at_ms: i64,
        rows: Vec<BybitTickerWire>,
    },
    BinanceWhaleBatch {
        schema_version: u32,
        sequence: u64,
        available_at_ms: i64,
        #[serde(default)]
        coverage: Vec<SourceCoverage>,
        rows: Vec<BinanceWhaleWire>,
    },
    UniverseSnapshot {
        schema_version: u32,
        sequence: u64,
        universe: UniverseIdentity,
    },
    BootstrapComplete {
        schema_version: u32,
        sequence: u64,
        coverage: BootstrapCoverage,
    },
    Watermark {
        schema_version: u32,
        sequence: u64,
        observed_ts_ms: i64,
    },
    LongWatermark {
        schema_version: u32,
        sequence: u64,
        observed_ts_ms: i64,
        data_through_ms: i64,
        #[serde(default)]
        gap_symbols: Vec<String>,
    },
    CarryWatermark {
        schema_version: u32,
        sequence: u64,
        observed_ts_ms: i64,
        data_through_ms: i64,
        #[serde(default)]
        gap_symbols: Vec<String>,
    },
    CarryScorerCatchupWatermark {
        schema_version: u32,
        sequence: u64,
        observed_ts_ms: i64,
        decision_through_ms: i64,
        #[serde(default)]
        gap_symbols: Vec<String>,
    },
}

impl WireEvent {
    pub fn schema_version(&self) -> u32 {
        match self {
            Self::BybitKlineBatch { schema_version, .. }
            | Self::BybitFundingBatch { schema_version, .. }
            | Self::BybitInstrumentSnapshot { schema_version, .. }
            | Self::BybitTickerSnapshot { schema_version, .. }
            | Self::BinanceWhaleBatch { schema_version, .. }
            | Self::UniverseSnapshot { schema_version, .. }
            | Self::BootstrapComplete { schema_version, .. }
            | Self::Watermark { schema_version, .. } => *schema_version,
            Self::LongWatermark { schema_version, .. }
            | Self::CarryWatermark { schema_version, .. }
            | Self::CarryScorerCatchupWatermark { schema_version, .. } => *schema_version,
        }
    }

    pub fn sequence(&self) -> u64 {
        match self {
            Self::BybitKlineBatch { sequence, .. }
            | Self::BybitFundingBatch { sequence, .. }
            | Self::BybitInstrumentSnapshot { sequence, .. }
            | Self::BybitTickerSnapshot { sequence, .. }
            | Self::BinanceWhaleBatch { sequence, .. }
            | Self::UniverseSnapshot { sequence, .. }
            | Self::BootstrapComplete { sequence, .. }
            | Self::Watermark { sequence, .. } => *sequence,
            Self::LongWatermark { sequence, .. }
            | Self::CarryWatermark { sequence, .. }
            | Self::CarryScorerCatchupWatermark { sequence, .. } => *sequence,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BybitFundingWire {
    pub funding_rate_timestamp: Value,
    pub funding_rate: Value,
    #[serde(default)]
    pub funding_interval_hour: Option<Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BybitInstrumentWire {
    pub symbol: String,
    #[serde(default)]
    pub contract_type: Option<String>,
    #[serde(default)]
    pub symbol_type: Option<String>,
    #[serde(default)]
    pub status: Option<String>,
    #[serde(default)]
    pub base_coin: Option<String>,
    #[serde(default)]
    pub quote_coin: Option<String>,
    #[serde(default)]
    pub settle_coin: Option<String>,
    #[serde(default)]
    pub launch_time: Option<Value>,
    #[serde(default)]
    pub delivery_time: Option<Value>,
    #[serde(default)]
    pub price_filter: BTreeMap<String, Value>,
    #[serde(default)]
    pub lot_size_filter: BTreeMap<String, Value>,
    #[serde(default)]
    pub funding_interval: Option<Value>,
    #[serde(default)]
    pub is_pre_listing: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BybitTickerWire {
    pub symbol: String,
    #[serde(default)]
    pub mark_observed_ts_ms: Option<i64>,
    #[serde(default)]
    pub funding_observed_ts_ms: Option<i64>,
    #[serde(default)]
    pub schedule_observed_ts_ms: Option<i64>,
    #[serde(default)]
    pub last_price: Option<Value>,
    #[serde(default)]
    pub mark_price: Option<Value>,
    #[serde(default)]
    pub index_price: Option<Value>,
    #[serde(default)]
    pub bid1_price: Option<Value>,
    #[serde(default)]
    pub ask1_price: Option<Value>,
    #[serde(default)]
    pub bid1_size: Option<Value>,
    #[serde(default)]
    pub ask1_size: Option<Value>,
    #[serde(default)]
    pub open_interest: Option<Value>,
    #[serde(default)]
    pub open_interest_value: Option<Value>,
    #[serde(default)]
    pub turnover24h: Option<Value>,
    #[serde(default)]
    pub volume24h: Option<Value>,
    #[serde(default)]
    pub funding_rate: Option<Value>,
    #[serde(default)]
    pub next_funding_time: Option<Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct BinanceWhaleWire {
    pub symbol: String,
    pub day_end_ms: Value,
    #[serde(default)]
    pub long_short_ratio: Option<Value>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LongFeatureRow {
    pub symbol: String,
    pub ts_ms: i64,
    pub close: f64,
    pub turnover_quote: f64,
    pub log_return: Option<f64>,
    pub realized_vol: Option<f64>,
    pub sigma_daily_30d: Option<f64>,
    pub turnover_median_90d: Option<f64>,
    pub today_volume_rank: u32,
    pub universe_rank: Option<u32>,
    pub in_universe: bool,
    pub pump_3d_log: Option<f64>,
    pub pump_7d_log: Option<f64>,
    pub close_location: f64,
    pub close_loc_3d: Option<f64>,
    pub close_loc_7d: Option<f64>,
    pub atr_14d_pct: Option<f64>,
    pub regime_on: bool,
    pub btc_rv_30: f64,
    pub eth_regime_on: bool,
    pub symbol_age_days: i64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct CarryFeatureRow {
    pub symbol: String,
    /// Knowledge-time bar close, not Bybit's bar-open stamp.
    pub bar_ts_ms: i64,
    pub by_close: f64,
    pub by_turnover_quote: f64,
    pub by_funding: Option<f64>,
    pub by_funding_age_h: Option<f64>,
    pub adv24: Option<f64>,
    pub trail_fund_24h: Option<f64>,
    pub momentum: Option<f64>,
    pub ret_3d: Option<f64>,
    pub vol_30d_daily: Option<f64>,
    pub dtrail_2d: Option<f64>,
    pub crowd_persistence: Option<f64>,
    pub turn_growth_3d: Option<f64>,
    pub d_tt_ls_3d: Option<f64>,
    pub adv_rank: Option<u32>,
    pub in_universe: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct MarketMark {
    pub symbol: String,
    pub observed_ts_ms: i64,
    pub mark_px: f64,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct PresettlementPublicObservation {
    pub symbol: String,
    pub observed_ts_ms: i64,
    pub settlement_ts_ms: i64,
    pub running_rate: f64,
    pub mark_px: Option<f64>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DataRejection {
    pub symbol: String,
    pub reason: String,
    pub first_missing_ts_ms: Option<i64>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Readiness {
    pub long_ready: bool,
    pub carry_ready: bool,
    pub universe_ready: bool,
    pub reason: String,
    pub long_feature_ts_ms: Option<i64>,
    pub carry_feature_ts_ms: Option<i64>,
    pub rejected_symbols: Vec<DataRejection>,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ObservationPayload {
    LongFeatureBatch {
        decision_ts_ms: i64,
        feature_ts_ms: i64,
        rows: Vec<LongFeatureRow>,
        marks: Vec<MarketMark>,
        cold_start_fallback_count: usize,
        rejections: Vec<DataRejection>,
    },
    CarryFeatureBatch {
        decision_ts_ms: i64,
        rows: Vec<CarryFeatureRow>,
        upcoming_rows: Vec<CarryFeatureRow>,
        settled_funding: Vec<SettledFunding>,
        presettlement: Vec<PresettlementPublicObservation>,
        marks: Vec<MarketMark>,
        rejections: Vec<DataRejection>,
    },
    CarryScorerCatchup {
        decision_ts_ms: i64,
        rows: Vec<CarryFeatureRow>,
        rejections: Vec<DataRejection>,
    },
    MarketSnapshot {
        expires_at_ms: i64,
        tickers: Vec<TickerObservation>,
        marks: Vec<MarketMark>,
        presettlement: Vec<PresettlementPublicObservation>,
    },
    FundingUpdate {
        decision_ts_ms: i64,
        settled_funding: Vec<SettledFunding>,
    },
    UniverseChanged,
    Readiness {
        readiness: Readiness,
    },
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct SignalPayloadEnvelope {
    pub schema_version: u32,
    pub config: ConfigIdentity,
    pub universe: Option<UniverseIdentity>,
    pub payload: ObservationPayload,
}
