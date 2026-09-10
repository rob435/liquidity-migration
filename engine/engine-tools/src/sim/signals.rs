//! A synthetic signal producer in the worker's own envelope contract, so the
//! native reducers accept its rows through every check they apply live.
//!
//! Two legacy sources, `sim.long` and `sim.carry`, addressed at the deployed
//! template's `long_native` and `carry_native` blocks. Identity comes from the
//! blocks themselves — `config_from_params` on the same `config_json` the
//! engine reduces with — so a rendered config and its producer cannot drift.
//! Feature values are on the worker's grids: LONG features close at UTC
//! midnight (`signal-worker::features::daily_bars` stamps a daily bar at
//! `day_start + DAY_MS`) and are republished hourly, which is what puts the
//! sniper deadline six hours after the close rather than six hours into the
//! future forever.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    Feed, SignalLane, SignalObservation, StrategyId, Subscription,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use serde_json::json;
use sha2::{Digest, Sha256};

use super::market::{MarketPlan, TapeSummary};
use super::rng::Rng;
use crate::backtest::signals::ReplayLifecycle;
use crate::config::StrategyConfig;

/// The producer name and the generation it asks the engine to grant. The
/// engine mints epoch 1 under this generation, so the source names below are
/// the ones it will accept (`backtest::signals::ReplayLifecycle`).
pub const PRODUCER: &str = "sim";
pub const GENERATION: &str = "51d05a1c51d05a1c51d05a1c51d05a1c";
pub const EPOCH: u64 = 1;

pub fn lifecycle(producer: &Producer) -> Option<ReplayLifecycle> {
    let mut routes = Vec::new();
    if let Some(long) = &producer.long {
        routes.push((SignalLane::Long, long.id));
    }
    if let Some(carry) = &producer.carry {
        routes.push((SignalLane::Carry, carry.id));
    }
    (!routes.is_empty()).then(|| ReplayLifecycle {
        producer: PRODUCER.to_owned(),
        generation: GENERATION.to_owned(),
        routes,
    })
}

fn source(lane: SignalLane) -> String {
    engine_types::ManagedSignalSource {
        producer: PRODUCER,
        epoch: EPOCH,
        generation: GENERATION,
        lane,
    }
    .encode()
    .expect("the producer's own source identity is valid")
}

const HOUR_MS: i64 = 3_600_000;
const DAY_MS: i64 = 86_400_000;
/// Availability lags the decision clock by this many seconds, drawn per row.
const AVAILABLE_LAG_S: (f64, f64) = (5.0, 90.0);
/// Days of daily history a `carry_feature_batch` carries, one over the
/// scorer's `MIN_REPLAY_DAYS` floor.
const CARRY_REPLAY_DAYS: i64 = 46;

/// A hash-shaped value for an artifact this producer has none of. Every
/// reducer checks the shape of these fields and resolves none of them.
fn placeholder_sha256() -> String {
    "5".repeat(64)
}

/// One sleeve's identity, read from its own rendered block.
#[derive(Clone, Debug)]
pub struct Binding {
    pub sleeve: String,
    pub id: StrategyId,
    pub fingerprint: String,
    pub environment: String,
    pub operational_profile_sha256: String,
    pub rule_sha256: String,
    pub feature_contract_sha256: String,
    /// LONG's `profile_name`, CARRY's `rule.config_id`.
    pub label: String,
    /// LONG's `rule.execution_strategy_id`; empty for CARRY.
    pub execution_strategy_id: String,
}

/// What the producer publishes for one seed.
#[derive(Clone, Debug, Default)]
pub struct Producer {
    pub long: Option<Binding>,
    pub carry: Option<Binding>,
    pub pump_probability: f64,
    pub gate: bool,
}

impl Producer {
    /// Bind to the rendered blocks. A config with neither sleeve produces
    /// nothing, which is what quoter mode wants.
    pub fn bind(strategies: &[StrategyConfig]) -> Result<Self, String> {
        let mut producer = Producer {
            pump_probability: 0.5,
            ..Producer::default()
        };
        for (index, block) in strategies.iter().enumerate() {
            let id = StrategyId(u16::try_from(index).map_err(|_| "too many strategy blocks")?);
            let params = toml::Value::Table(block.params.clone());
            if block.name == engine_strategies::native_long::plug::NAME {
                let config = engine_strategies::native_long::plug::config_from_params(&params)
                    .map_err(|error| error.to_string())?;
                producer.long = Some(Binding {
                    sleeve: block.sleeve_name().to_owned(),
                    id,
                    fingerprint: config.fingerprint(),
                    environment: config.environment.clone(),
                    operational_profile_sha256: config.operational_profile_sha256.clone(),
                    rule_sha256: config.rule_sha256.clone(),
                    feature_contract_sha256: config.feature_contract_sha256.clone(),
                    label: config.profile_name.clone(),
                    execution_strategy_id: config.rule.execution_strategy_id.clone(),
                });
            } else if block.name == engine_strategies::native_carry::plug::NAME {
                let config = engine_strategies::native_carry::plug::config_from_params(&params)
                    .map_err(|error| error.to_string())?;
                producer.carry = Some(Binding {
                    sleeve: block.sleeve_name().to_owned(),
                    id,
                    fingerprint: config.fingerprint(),
                    environment: config.environment.clone(),
                    operational_profile_sha256: config.operational_profile_sha256.clone(),
                    rule_sha256: config.rule_sha256.clone(),
                    feature_contract_sha256: config.feature_contract_sha256.clone(),
                    label: config.rule.config_id.clone(),
                    execution_strategy_id: String::new(),
                });
            }
        }
        if let (Some(long), Some(carry)) = (&producer.long, &producer.carry) {
            if long.environment != carry.environment {
                return Err(format!(
                    "the rendered sleeves disagree about the environment: {} and {}",
                    long.environment, carry.environment
                ));
            }
        }
        Ok(producer)
    }

    pub fn is_empty(&self) -> bool {
        self.long.is_none() && self.carry.is_none()
    }

    fn environment(&self) -> String {
        self.long
            .as_ref()
            .or(self.carry.as_ref())
            .map(|binding| binding.environment.clone())
            .unwrap_or_default()
    }

    fn identity(&self) -> serde_json::Value {
        let long = self.long.as_ref();
        let carry = self.carry.as_ref();
        let profile = long
            .or(carry)
            .map(|binding| binding.operational_profile_sha256.clone())
            .unwrap_or_else(placeholder_sha256);
        json!({
            "schema_version": 1,
            "signal_config_id": "sim",
            "long_profile": long.map(|b| b.label.clone()).unwrap_or_default(),
            "long_execution_strategy_id": long.map(|b| b.execution_strategy_id.clone()).unwrap_or_default(),
            "long_rule_sha256": long.map(|b| b.rule_sha256.clone()).unwrap_or_else(placeholder_sha256),
            "long_feature_contract_sha256": long.map(|b| b.feature_contract_sha256.clone()).unwrap_or_else(placeholder_sha256),
            "signal_config_sha256": placeholder_sha256(),
            "carry_config_id": carry.map(|b| b.label.clone()).unwrap_or_default(),
            "carry_rule_sha256": carry.map(|b| b.rule_sha256.clone()).unwrap_or_else(placeholder_sha256),
            "carry_feature_contract_sha256": carry.map(|b| b.feature_contract_sha256.clone()).unwrap_or_else(placeholder_sha256),
            "operational_profile_sha256": profile,
            "engine_config_sha256": placeholder_sha256(),
            "long_decision_fingerprint": long.map(|b| b.fingerprint.clone()).unwrap_or_else(placeholder_sha256),
            "carry_decision_fingerprint": carry.map(|b| b.fingerprint.clone()).unwrap_or_else(placeholder_sha256),
        })
    }

    fn universe(&self, plan: &MarketPlan) -> serde_json::Value {
        let names = plan.names();
        json!({
            "mode": "current",
            "environment": self.environment(),
            "endpoint": "sim",
            "snapshot_ts_ms": plan.t0_ms(),
            "available_at_ms": plan.t0_ms(),
            "artifact_sha256": placeholder_sha256(),
            "file_sha256": placeholder_sha256(),
            "symbols": names,
            "long_symbols": names,
            "carry_symbols": names,
        })
    }
}

/// The worker's own observation id: length-prefixed source, kind and payload,
/// then the destination, sequence and both clocks, little-endian
/// (`signal-worker::worker::semantic_id`).
fn semantic_id(
    source: &str,
    destination: StrategyId,
    sequence: u64,
    kind: &str,
    observed: i64,
    available: i64,
    payload: &[u8],
) -> String {
    let mut hasher = Sha256::new();
    for value in [source.as_bytes(), kind.as_bytes(), payload] {
        hasher.update((value.len() as u64).to_le_bytes());
        hasher.update(value);
    }
    hasher.update(destination.0.to_le_bytes());
    hasher.update(sequence.to_le_bytes());
    hasher.update(observed.to_le_bytes());
    hasher.update(available.to_le_bytes());
    hex::encode(hasher.finalize())
}

/// Quote and ticker per symbol, the pair `signal-worker::market_subscriptions`
/// publishes. A native sleeve declares no subscriptions of its own: the
/// engine's market feed learns a symbol from the durable observation that
/// names it, and with none the tape is filtered down to nothing.
fn market_subscriptions(symbols: &[String]) -> Vec<Subscription> {
    symbols
        .iter()
        .flat_map(|symbol| {
            [Feed::Quote, Feed::Ticker].map(|feed| Subscription {
                symbol: symbol.clone(),
                feed,
            })
        })
        .collect()
}

struct Pen {
    source: String,
    binding: Binding,
    sequence: u64,
    subscriptions: Vec<Subscription>,
    /// Availability must not go backwards inside one source: the engine reads
    /// the spool in availability order and a lower sequence arriving second is
    /// a hole in the source's prefix.
    last_available: i64,
}

impl Pen {
    fn write(
        &mut self,
        kind: &str,
        observed: i64,
        available: i64,
        envelope: serde_json::Value,
    ) -> SignalObservation {
        self.sequence += 1;
        let available = available.max(self.last_available + 1);
        self.last_available = available;
        let payload = serde_json::to_vec(&envelope).expect("the envelope serializes");
        let observation_id = semantic_id(
            &self.source,
            self.binding.id,
            self.sequence,
            kind,
            observed,
            available,
            &payload,
        );
        let mut observation = SignalObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: self.binding.fingerprint.clone(),
            destination: self.binding.id,
            source: self.source.clone(),
            sequence: self.sequence,
            observation_id,
            kind: kind.to_owned(),
            observed_wall_ts_ms: observed,
            available_wall_ts_ms: available,
            subscriptions: self.subscriptions.clone(),
            payload,
            content_sha256: String::new(),
        };
        observation.content_sha256 = crate::signals::content_sha256(&observation);
        observation
    }
}

/// The hour boundaries the tape covers, first one at or after its start.
fn hour_boundaries(plan: &MarketPlan) -> Vec<i64> {
    let first = plan.t0_ms().div_euclid(HOUR_MS) * HOUR_MS + HOUR_MS;
    let end = plan.end_ms();
    let mut out = Vec::new();
    let mut at = first;
    while at <= end {
        out.push(at);
        at += HOUR_MS;
    }
    out
}

fn daily_close(at_ms: i64) -> i64 {
    at_ms.div_euclid(DAY_MS) * DAY_MS
}

/// Rank by displayed notional, largest first, one-based: the worker's
/// `today_volume_rank`.
fn volume_ranks(plan: &MarketPlan) -> BTreeMap<String, f64> {
    let mut rows: Vec<(String, f64)> = plan
        .symbols
        .iter()
        .map(|spec| (spec.name.to_owned(), spec.level_qty * spec.px0))
        .collect();
    rows.sort_by(|a, b| b.1.total_cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    rows.into_iter()
        .enumerate()
        .map(|(index, (name, _))| (name, index as f64 + 1.0))
        .collect()
}

/// Every row the producer publishes, ordered by availability then sequence.
///
/// Feature values are internally consistent and sized so a LONG entry clears
/// the risk kernel's symbol cap: `btc_rv_30` 0.6 leaves the volatility scale
/// at one, and `realized_vol` 0.9 against `vol_floor_annual` 0.3 puts the
/// target at a fifth of equity, under the profile's half-of-equity cap.
pub fn publish(
    mut rng: Rng,
    plan: &MarketPlan,
    tape: &TapeSummary,
    producer: &Producer,
) -> Vec<SignalObservation> {
    let mut out = Vec::new();
    if producer.is_empty() {
        return out;
    }
    let identity = producer.identity();
    let universe = producer.universe(plan);
    let hours = hour_boundaries(plan);
    let ranks = volume_ranks(plan);
    let names = plan.names();

    let days: BTreeSet<i64> = hours.iter().copied().map(daily_close).collect();
    let mut pumped: BTreeSet<(i64, String)> = BTreeSet::new();
    for day in &days {
        for name in &names {
            if rng.chance(producer.pump_probability) {
                pumped.insert((*day, name.clone()));
            }
        }
    }
    let lag = |rng: &mut Rng| {
        (rng.between(AVAILABLE_LAG_S.0, AVAILABLE_LAG_S.1) * 1_000.0).round() as i64
    };

    if let Some(binding) = producer.long.clone() {
        let mut pen = Pen {
            source: source(SignalLane::Long),
            binding,
            sequence: 0,
            subscriptions: market_subscriptions(&names),
            last_available: 0,
        };
        for hour in &hours {
            let feature_ts = daily_close(*hour);
            let available = hour + lag(&mut rng);
            let rows: Vec<serde_json::Value> = names
                .iter()
                .map(|name| {
                    let close = tape.mid_at_ms(name, feature_ts).unwrap_or(0.0);
                    let pump = pumped.contains(&(feature_ts, name.clone()));
                    json!({
                        "symbol": name,
                        "ts_ms": feature_ts,
                        "close": close,
                        "turnover_quote": close * 1_000.0,
                        "log_return": if pump { 0.20 } else { 0.0 },
                        "realized_vol": 0.9,
                        "sigma_daily_30d": 0.05,
                        "turnover_median_90d": close * 1_000.0,
                        "today_volume_rank": ranks.get(name).copied().unwrap_or(1.0),
                        "universe_rank": ranks.get(name).copied().unwrap_or(1.0),
                        "in_universe": true,
                        "close_location": if pump { 0.9 } else { 0.5 },
                        "atr_14d_pct": 0.05,
                        "regime_on": true,
                        "btc_rv_30": 0.6,
                        "eth_regime_on": true,
                        "symbol_age_days": 200,
                    })
                })
                .collect();
            let marks: Vec<serde_json::Value> = names
                .iter()
                .filter_map(|name| {
                    tape.mid_at_ms(name, *hour).map(
                        |mid| json!({ "symbol": name, "observed_ts_ms": hour, "mark_px": mid }),
                    )
                })
                .collect();
            out.push(pen.write(
                "long_feature_batch",
                *hour,
                available,
                json!({
                    "schema_version": 1,
                    "config": identity.clone(),
                    "universe": universe.clone(),
                    "payload": {
                        "kind": "long_feature_batch",
                        "decision_ts_ms": hour,
                        "feature_ts_ms": feature_ts,
                        "rows": rows,
                        "marks": marks,
                        "cold_start_fallback_count": 0,
                        "rejections": [],
                    },
                }),
            ));
        }
        if producer.gate {
            if let (Some(hour), Some(name)) = (hours.first(), names.first()) {
                let available = hour + lag(&mut rng);
                let trigger = tape.mid_at_ms(name, *hour).unwrap_or(1.0);
                out.push(pen.write(
                    "llm_gate_candidates",
                    *hour,
                    available,
                    json!({
                        "schema_version": 1,
                        "config": &identity,
                        "universe": &universe,
                        "payload": {
                            "kind": "llm_gate_candidates",
                            "decision_ts_ms": hour,
                            "valid_until_ms": hour + HOUR_MS,
                            "btc_rv_30": 0.6,
                            "rows": [{
                                "symbol": name,
                                "score": 0.9,
                                "band": "high",
                                "trigger_ts_ms": hour,
                                "trigger_price": trigger,
                                "atr_pct": 0.05,
                                "sigma_daily_30d": 0.05,
                                "turnover_rank": 1.0,
                                "trigger_window_h": 24,
                            }],
                        },
                    }),
                ));
            }
        }
    }

    if let Some(binding) = producer.carry.clone() {
        let mut pen = Pen {
            source: source(SignalLane::Carry),
            binding,
            sequence: 0,
            subscriptions: market_subscriptions(&names),
            last_available: 0,
        };
        let envelope = |payload: serde_json::Value| {
            json!({
                "schema_version": 1,
                "config": identity.clone(),
                "universe": universe.clone(),
                "payload": payload,
            })
        };
        if let Some(first) = hours.first() {
            let available = first + lag(&mut rng);
            out.push(pen.write(
                "readiness",
                *first,
                available,
                envelope(json!({
                    "kind": "readiness",
                    "readiness": {
                        "long_ready": true,
                        "carry_ready": true,
                        "universe_ready": true,
                        "reason": "sim",
                        "long_feature_ts_ms": daily_close(*first),
                        "carry_feature_ts_ms": daily_close(*first),
                        "rejected_symbols": [],
                    },
                })),
            ));
        }
        for hour in &hours {
            let available = hour + lag(&mut rng);
            let marks: Vec<serde_json::Value> = names
                .iter()
                .filter_map(|name| {
                    tape.mid_at_ms(name, *hour).map(
                        |mid| json!({ "symbol": name, "observed_ts_ms": hour, "mark_px": mid }),
                    )
                })
                .collect();
            out.push(pen.write(
                "market_snapshot",
                *hour,
                available,
                envelope(json!({
                    "kind": "market_snapshot",
                    "expires_at_ms": hour + 2 * HOUR_MS,
                    "tickers": [],
                    "marks": marks,
                    "presettlement": [],
                })),
            ));
            if hour % (8 * HOUR_MS) == 0 {
                let available = hour + lag(&mut rng);
                let settled: Vec<serde_json::Value> = names
                    .iter()
                    .map(|name| {
                        json!({
                            "symbol": name,
                            "settlement_ts_ms": hour,
                            "available_at_ms": available,
                            "rate": 0.0001,
                            "funding_interval_min": 480,
                        })
                    })
                    .collect();
                out.push(pen.write(
                    "funding_update",
                    *hour,
                    available,
                    envelope(json!({
                        "kind": "funding_update",
                        "decision_ts_ms": hour,
                        "settled_funding": settled,
                    })),
                ));
            }
            if hour % DAY_MS == 0 {
                let available = hour + lag(&mut rng);
                // The scorer refuses a first decision with less than
                // MIN_REPLAY_DAYS of daily history in the batch itself, so the
                // batch carries the days before the tape at the opening price.
                let rows: Vec<serde_json::Value> = names
                    .iter()
                    .flat_map(|name| {
                        let rank = ranks.get(name).copied().unwrap_or(1.0) as u32;
                        (0..=CARRY_REPLAY_DAYS).rev().map(move |back| {
                            let bar_ts_ms = hour - back * DAY_MS;
                            let close = tape.mid_at_ms(name, bar_ts_ms).unwrap_or(0.0);
                            json!({
                            "symbol": name,
                            "bar_ts_ms": bar_ts_ms,
                            "by_close": close,
                            "by_turnover_quote": close * 1_000.0,
                            "by_funding": 0.0001,
                            "by_funding_age_h": 1.0,
                            "adv24": close * 1_000.0,
                            "trail_fund_24h": 0.0003,
                            "momentum": 0.0,
                            "ret_3d": 0.0,
                            "vol_30d_daily": 0.05,
                            "dtrail_2d": 0.0,
                            "crowd_persistence": 0.0,
                            "turn_growth_3d": 0.0,
                            "d_tt_ls_3d": 0.0,
                            "adv_rank": rank,
                            "in_universe": true,
                            })
                        })
                    })
                    .collect();
                let marks: Vec<serde_json::Value> = names
                    .iter()
                    .filter_map(|name| {
                        tape.mid_at_ms(name, *hour).map(
                            |mid| json!({ "symbol": name, "observed_ts_ms": hour, "mark_px": mid }),
                        )
                    })
                    .collect();
                out.push(pen.write(
                    "carry_feature_batch",
                    *hour,
                    available,
                    envelope(json!({
                        "kind": "carry_feature_batch",
                        "decision_ts_ms": hour,
                        "rows": rows,
                        "upcoming_rows": [],
                        "settled_funding": [],
                        "presettlement": [],
                        "marks": marks,
                        "rejections": [],
                    })),
                ));
            }
        }
    }

    out.sort_by_key(|row| (row.available_wall_ts_ms, row.source.clone(), row.sequence));
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sim::market::{Realm, SimStrategies};

    fn realm_config(realm: Realm) -> crate::config::LoadedConfig {
        let directory = crate::testpath::temp_path("sim-producer");
        std::fs::create_dir_all(directory.path()).unwrap();
        let path = directory.path().join("engine.toml");
        let profile = directory.path().join("operational-profile.json");
        std::fs::write(&profile, crate::sim::market::OPERATIONAL_PROFILE).unwrap();
        crate::sim::market::write_engine_config(
            &path,
            &MarketPlan::new(3, 12 * 3_600),
            SimStrategies::Realm(realm),
            &profile,
        )
        .unwrap();
        let loaded = crate::config::load(&path).unwrap();
        std::fs::remove_dir_all(directory.path()).unwrap();
        loaded
    }

    #[test]
    fn every_published_row_is_a_row_the_engine_would_accept() {
        let loaded = realm_config(Realm::Mexc);
        let producer = Producer::bind(&loaded.config.strategies).expect("the blocks bind");
        let long = producer.long.as_ref().expect("mexc renders LONG");
        let carry = producer.carry.as_ref().expect("mexc renders CARRY");
        assert_eq!(long.id, StrategyId(1));
        assert_eq!(carry.id, StrategyId(0));
        assert_eq!(long.environment, "mexc");
        engine_strategies::native_common::validate_signal_identity(
            &serde_json::from_value(producer.identity()).expect("the identity block parses"),
            Some(&serde_json::from_value(producer.universe(&MarketPlan::new(3, 600))).unwrap()),
            "mexc",
        )
        .expect("the producer's identity binds the mexc reducers");

        let mut plan = MarketPlan::new(3, 12 * 3_600);
        plan.step_s = 10;
        let directory = crate::testpath::temp_path("sim-producer-tape");
        std::fs::create_dir_all(directory.path()).unwrap();
        let tape_path = directory.path().join("tape.jsonl");
        let tape = crate::sim::market::write_tape(&tape_path, &plan, Rng::new(1)).unwrap();
        std::fs::remove_dir_all(directory.path()).unwrap();

        let rows = publish(Rng::new(1), &plan, &tape, &producer);
        assert!(rows.len() > 12, "{}", rows.len());
        let mut kinds = BTreeSet::new();
        for row in &rows {
            crate::signals::validate(row).expect("the engine's own row validator accepts it");
            assert!(engine_types::ManagedSignalSource::parse(&row.source).is_some());
            assert!(row.available_wall_ts_ms >= row.observed_wall_ts_ms);
            kinds.insert(row.kind.clone());
        }
        assert!(kinds.contains("long_feature_batch"));
        assert!(kinds.contains("carry_feature_batch"));
        assert!(kinds.contains("readiness"));
        let mut previous = (0i64, String::new(), 0u64);
        for row in &rows {
            let key = (row.available_wall_ts_ms, row.source.clone(), row.sequence);
            assert!(key > previous, "{key:?} after {previous:?}");
            previous = key;
        }
    }

    #[test]
    fn one_seed_publishes_one_set() {
        let loaded = realm_config(Realm::Mexc);
        let producer = Producer::bind(&loaded.config.strategies).unwrap();
        let mut plan = MarketPlan::new(2, 12 * 3_600);
        plan.step_s = 10;
        let directory = crate::testpath::temp_path("sim-producer-twice");
        std::fs::create_dir_all(directory.path()).unwrap();
        let tape_path = directory.path().join("tape.jsonl");
        let tape = crate::sim::market::write_tape(&tape_path, &plan, Rng::new(3)).unwrap();
        std::fs::remove_dir_all(directory.path()).unwrap();
        let first = publish(Rng::new(3), &plan, &tape, &producer);
        let second = publish(Rng::new(3), &plan, &tape, &producer);
        assert_eq!(first, second);
    }

    #[test]
    fn a_pumped_day_carries_the_rule_s_own_trigger() {
        let loaded = realm_config(Realm::Mexc);
        let mut producer = Producer::bind(&loaded.config.strategies).unwrap();
        producer.pump_probability = 1.0;
        producer.carry = None;
        let mut plan = MarketPlan::new(1, 12 * 3_600);
        plan.step_s = 10;
        let directory = crate::testpath::temp_path("sim-producer-pump");
        std::fs::create_dir_all(directory.path()).unwrap();
        let tape_path = directory.path().join("tape.jsonl");
        let tape = crate::sim::market::write_tape(&tape_path, &plan, Rng::new(5)).unwrap();
        std::fs::remove_dir_all(directory.path()).unwrap();
        let rows = publish(Rng::new(5), &plan, &tape, &producer);
        let config = engine_strategies::native_long::plug::config_from_params(&toml::Value::Table(
            loaded.config.strategies[1].params.clone(),
        ))
        .unwrap();
        let envelope: serde_json::Value = serde_json::from_slice(&rows[0].payload).unwrap();
        let row: engine_strategies::native_long::plan::FeatureRow =
            serde_json::from_value(envelope["payload"]["rows"][0].clone()).unwrap();
        let classified = engine_strategies::native_long::plan::classify_feature(&row, &config.rule);
        assert!(classified.trigger_1d, "{classified:?}");
        assert!(classified.pattern.is_some(), "{classified:?}");
    }
}
