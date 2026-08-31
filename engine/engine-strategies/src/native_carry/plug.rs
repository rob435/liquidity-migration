//! Engine adapter for the native CARRY scorer and lifecycle reducer.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    MarketEvent, OrderUpdate, SignalObservation, Strategy, StrategyCheckpoint,
    StrategyCheckpointIdentity, StrategyCtx, StrategyId, StrategyImportContext,
    StrategyImportSource, Subscription, SymbolId, TimerId, TranslatedStrategyState, WorkPolicy,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use serde::Deserialize;

use super::plan::{
    reduce_lifecycle, reduce_signal, CarrySignalBatch, DataRejection, MarketMark,
    PresettlementObservation, ReducerInput, ReducerOutput, SettledFundingObservation, SleeveState,
    StrategyConfig,
};
use super::scorer::{CarryDecision, CarryFeatureRow};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, checkpoint_payload, emit_effects,
    flatten_execution, owned_order_state, planner_facts, validate_signal_identity, Effect,
    FlattenExecutionInput, SignalConfigIdentity, UniverseIdentity,
    DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};
use crate::params::Params;
use crate::position_plan::Skipped;
use crate::BuildError;

pub const NAME: &str = "carry_native";
const TIMER: TimerId = TimerId(0x4341_5259);
const ENTRY_RETRY_MS: i64 = 1_000;

pub fn config_from_params(params: &toml::Value) -> Result<StrategyConfig, BuildError> {
    let p = Params::new(NAME, params)?;
    let raw = p.string("config_json")?;
    p.reject_unknown(&["config_json"])?;
    let config: StrategyConfig =
        serde_json::from_str(&raw).map_err(|error| p.invalid("config_json", error.to_string()))?;
    config
        .validate()
        .map_err(|error| p.invalid("config_json", error))?;
    Ok(config)
}

pub fn decision_fingerprint_from_params(params: &toml::Value) -> Result<String, BuildError> {
    Ok(config_from_params(params)?.fingerprint())
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SignalEnvelope {
    schema_version: u32,
    config: SignalConfigIdentity,
    universe: Option<UniverseIdentity>,
    payload: SignalPayload,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
enum SignalPayload {
    CarryFeatureBatch {
        decision_ts_ms: i64,
        rows: Vec<CarryFeatureRow>,
        upcoming_rows: Vec<CarryFeatureRow>,
        settled_funding: Vec<SettledFundingObservation>,
        presettlement: Vec<PresettlementObservation>,
        marks: Vec<MarketMark>,
        rejections: Vec<DataRejection>,
    },
    MarketSnapshot {
        tickers: Vec<TickerObservation>,
        marks: Vec<MarketMark>,
        presettlement: Vec<PresettlementObservation>,
    },
    FundingUpdate {
        settled_funding: Vec<SettledFundingObservation>,
    },
    UniverseChanged,
    Readiness {
        readiness: Readiness,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TickerObservation {
    symbol: String,
    observed_ts_ms: i64,
    available_at_ms: i64,
    last_price: Option<f64>,
    mark_price: Option<f64>,
    index_price: Option<f64>,
    bid1_price: Option<f64>,
    ask1_price: Option<f64>,
    bid1_size: Option<f64>,
    ask1_size: Option<f64>,
    open_interest: Option<f64>,
    open_interest_value: Option<f64>,
    turnover_24h: Option<f64>,
    volume_24h: Option<f64>,
    funding_rate: Option<f64>,
    next_funding_time_ms: Option<i64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Readiness {
    long_ready: bool,
    carry_ready: bool,
    universe_ready: bool,
    reason: String,
    long_feature_ts_ms: Option<i64>,
    carry_feature_ts_ms: Option<i64>,
    rejected_symbols: Vec<DataRejection>,
}

pub struct NativeCarry {
    id: StrategyId,
    pub config: StrategyConfig,
    pub state: SleeveState,
    restored: bool,
    checkpoint_fingerprint: Option<String>,
    blockers: BTreeMap<String, String>,
    last_error: Option<String>,
    flatten_request_id: Option<String>,
}

impl NativeCarry {
    pub fn new(config: StrategyConfig, state: SleeveState) -> Result<Self, &'static str> {
        config.validate()?;
        let mut state = state;
        if state.schema_version == 0 {
            state.schema_version = DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION;
        }
        state.validate()?;
        Ok(Self {
            id: StrategyId(0),
            config,
            state,
            restored: true,
            checkpoint_fingerprint: None,
            blockers: BTreeMap::new(),
            last_error: None,
            flatten_request_id: None,
        })
    }

    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let config = config_from_params(params)?;
        Ok(Self {
            id,
            config,
            state: SleeveState {
                schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
                ..SleeveState::default()
            },
            restored: false,
            checkpoint_fingerprint: None,
            blockers: BTreeMap::new(),
            last_error: None,
            flatten_request_id: None,
        })
    }

    pub fn reduce(&mut self, input: ReducerInput) -> Result<ReducerOutput, &'static str> {
        let output = reduce_lifecycle(input, self.state.clone(), &self.config)?;
        self.state = output.next_state.clone();
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        Ok(output)
    }

    fn ensure_restored(&mut self, ctx: &dyn StrategyCtx) {
        if self.restored {
            return;
        }
        self.restored = true;
        let Some(checkpoint) = ctx.strategy_global_checkpoint() else {
            return;
        };
        self.checkpoint_fingerprint = Some(checkpoint.decision_fingerprint.clone());
        if checkpoint.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION
            || checkpoint.decision_fingerprint != self.config.fingerprint()
        {
            self.last_error = Some("CARRY checkpoint identity mismatch".to_owned());
            return;
        }
        match serde_json::from_slice::<SleeveState>(&checkpoint.payload)
            .map_err(|error| error.to_string())
            .and_then(|state| {
                state.validate().map_err(str::to_owned)?;
                Ok(state)
            }) {
            Ok(state) => self.state = state,
            Err(error) => {
                self.last_error = Some(format!("CARRY checkpoint refused: {error}"));
                self.checkpoint_fingerprint = Some("invalid-checkpoint".to_owned());
            }
        }
    }

    fn entry_work(&self) -> Option<WorkPolicy> {
        self.config.rest_entries.then_some(WorkPolicy {
            hold_decision_px: self.config.hold_decision_price,
            give_up_instead_of_crossing: self.config.give_up_instead_of_crossing,
            ..WorkPolicy::default()
        })
    }

    fn effective_config(&self, ctx: &dyn StrategyCtx) -> StrategyConfig {
        let mut config = self.config.clone();
        config.entries_enabled = ctx.entries_enabled(self.config.entries_enabled);
        config
    }

    fn account(&self, ctx: &dyn StrategyCtx) -> (bool, f64) {
        let account = ctx.account_summary();
        let healthy = account.equity_usdt.is_finite()
            && account.equity_usdt > 0.0
            && account.available_margin_usdt.is_finite()
            && account.available_margin_usdt >= 0.0;
        (healthy, if healthy { account.equity_usdt } else { 0.0 })
    }

    fn known_symbols(
        &self,
        extra: impl IntoIterator<Item = String>,
        ctx: &dyn StrategyCtx,
    ) -> BTreeSet<String> {
        let mut symbols = attributed_symbols(ctx);
        symbols.extend(self.state.desired_targets.keys().cloned());
        if let Some(decision) = &self.state.current_decision {
            symbols.extend(decision.weights.keys().cloned());
        }
        symbols.extend(extra);
        symbols
    }

    fn durable_fires(
        &self,
        ctx: &dyn StrategyCtx,
    ) -> Result<Vec<crate::native_common::CarryPresettlementFire>, String> {
        let mut events = Vec::new();
        ctx.strategy_events(&mut events);
        let mut fires = Vec::new();
        for event in events {
            if event.source != self.id {
                continue;
            }
            if event.kind != "carry_presettlement_fire" {
                return Err("CARRY owns a pending event outside its fire contract".to_owned());
            }
            let fire: crate::native_common::CarryPresettlementFire =
                serde_json::from_slice(&event.payload).map_err(|error| error.to_string())?;
            if fire.event_id != event.event_id {
                return Err("CARRY event envelope and payload ids disagree".to_owned());
            }
            fires.push(fire);
        }
        fires.sort_by(|left, right| {
            left.fired_ts_ms
                .cmp(&right.fired_ts_ms)
                .then_with(|| left.event_id.cmp(&right.event_id))
        });
        Ok(fires)
    }

    fn base_input(
        &self,
        now_ms: i64,
        decision: CarryDecision,
        extra_symbols: impl IntoIterator<Item = String>,
        ctx: &dyn StrategyCtx,
    ) -> Result<ReducerInput, String> {
        let (healthy, equity) = self.account(ctx);
        let (working, opening) = owned_order_state(ctx);
        let mut symbols = self.known_symbols(extra_symbols, ctx);
        symbols.extend(working.iter().cloned());
        Ok(ReducerInput {
            now_ms,
            decision,
            upcoming_decision: None,
            settled_funding: Vec::new(),
            presettlement: Vec::new(),
            durable_fires: self.durable_fires(ctx)?,
            trail_by_symbol: BTreeMap::new(),
            entry_blockers: self.blockers.clone(),
            account_healthy: healthy,
            equity_usdt: equity,
            upcoming_sizing_equity_usdt: None,
            facts: planner_facts(ctx, &symbols),
            owned_working_symbols: working,
            owned_opening_order_ids: opening,
            checkpoint_fingerprint: self.checkpoint_fingerprint.clone(),
            signal_receipt: None,
        })
    }

    fn apply(&mut self, output: ReducerOutput, ctx: &mut dyn StrategyCtx) {
        self.state = output.next_state;
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        self.blockers.clear();
        for skipped in output.execution.skipped {
            let (symbol, reason) = match skipped {
                Skipped::TooSmallToBother { symbol, .. } => (symbol, "inside_resize_band"),
                Skipped::BelowEntryFloor { symbol, .. } => (symbol, "below_entry_floor"),
                Skipped::BelowVenueMinimum { symbol } => (symbol, "below_venue_minimum"),
                Skipped::EntryWindowClosed { symbol } => (symbol, "entry_window_closed"),
                Skipped::NoPrice { symbol } => (symbol, "no_price"),
                Skipped::NoInstrumentRule { symbol } => (symbol, "no_instrument_rule"),
            };
            self.blockers.insert(symbol, reason.to_owned());
        }
        for symbol in &self.state.refused_entries {
            self.blockers
                .entry(symbol.clone())
                .or_insert_with(|| "entry_refused".to_owned());
        }
        if let Err(error) = emit_effects(
            output.execution.effects,
            self.id,
            Some(&self.config.exodus_sleeve_name),
            self.entry_work(),
            ctx,
        ) {
            self.last_error = Some(error.to_owned());
        } else {
            self.last_error = None;
        }
        self.arm_next(ctx);
    }

    fn replan(&mut self, ctx: &mut dyn StrategyCtx) {
        self.ensure_restored(ctx);
        if self.flatten_request_id.is_some() {
            self.flatten_now(ctx);
            return;
        }
        let Some(decision) = self.state.current_decision.clone() else {
            return;
        };
        let input = match self.base_input(ctx.wall_ms().max(1), decision, Vec::new(), ctx) {
            Ok(input) => input,
            Err(error) => {
                self.last_error = Some(error);
                return;
            }
        };
        let config = self.effective_config(ctx);
        match reduce_lifecycle(input, self.state.clone(), &config) {
            Ok(output) => self.apply(output, ctx),
            Err(error) => self.last_error = Some(error.to_owned()),
        }
    }

    fn flatten_now(&mut self, ctx: &mut dyn StrategyCtx) {
        self.ensure_restored(ctx);
        let Some(request_id) = self.flatten_request_id.clone() else {
            return;
        };
        let mut symbols = self.known_symbols(Vec::new(), ctx);
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working);
        let facts = planner_facts(ctx, &symbols);
        let conclusively_flat = attributed_exposure_is_flat(ctx, &symbols);
        self.state.desired_targets.clear();
        self.state.refused_entries.clear();
        self.state.entry_retry_after_ms.clear();
        let (execution, flat) = flatten_execution(
            &self.state,
            FlattenExecutionInput {
                config_fingerprint: self.config.fingerprint(),
                facts: &facts,
                owned_opening_order_ids: &opening,
                now_ms: ctx.wall_ms().max(1),
                request_id: &request_id,
                tag: "carry_native_flatten",
                conclusively_flat,
            },
        );
        if flat {
            self.flatten_request_id = None;
        }
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        if let Err(error) = emit_effects(
            execution.effects,
            self.id,
            Some(&self.config.exodus_sleeve_name),
            self.entry_work(),
            ctx,
        ) {
            self.last_error = Some(error.to_owned());
        }
    }

    fn arm_next(&self, ctx: &mut dyn StrategyCtx) {
        let Some(decision) = &self.state.current_decision else {
            return;
        };
        let now_ms = ctx.wall_ms().max(1);
        let cutoff = decision.decision_ts_ms + self.config.execution.signal_validity_ms
            - self.config.execution.engine_entry_cutoff_ms;
        if cutoff > now_ms {
            let delay_ms = u64::try_from(cutoff - now_ms).unwrap_or(u64::MAX);
            ctx.arm_timer(TIMER, delay_ms.saturating_mul(1_000_000).max(1));
        }
    }

    fn validate_observation(
        &self,
        observation: &SignalObservation,
        ctx: &dyn StrategyCtx,
        envelope: &SignalEnvelope,
    ) -> Result<(), String> {
        if observation.schema_version != SIGNAL_OBSERVATION_SCHEMA_VERSION
            || observation.destination != self.id
            || observation.decision_fingerprint != self.config.fingerprint()
            || observation.source.is_empty()
            || observation.sequence == 0
            || observation.observation_id.is_empty()
            || observation.observed_wall_ts_ms <= 0
            || observation.available_wall_ts_ms < observation.observed_wall_ts_ms
            || observation.available_wall_ts_ms > ctx.wall_ms()
            || envelope.schema_version != 1
        {
            return Err("CARRY signal envelope identity is invalid".to_owned());
        }
        validate_signal_identity(
            &envelope.config,
            envelope.universe.as_ref(),
            &self.config.environment,
        )
        .map_err(str::to_owned)?;
        if envelope.config.carry_config_id != self.config.rule.config_id
            || envelope.config.carry_rule_sha256 != self.config.rule_sha256
            || envelope.config.carry_feature_contract_sha256 != self.config.feature_contract_sha256
            || envelope.config.carry_decision_fingerprint != self.config.fingerprint()
        {
            return Err("CARRY signal config does not bind this reducer".to_owned());
        }
        Ok(())
    }

    fn consume_only(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        let effects = vec![
            Effect::PersistCheckpoint {
                symbol: String::new(),
                config_fingerprint: self.config.fingerprint(),
                payload: checkpoint_payload(&self.state),
            },
            Effect::ConsumeSignal {
                source: observation.source.clone(),
                sequence: observation.sequence,
                observation_id: observation.observation_id.clone(),
            },
        ];
        if let Err(error) = emit_effects(effects, self.id, None, None, ctx) {
            self.last_error = Some(error.to_owned());
        }
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
    }

    fn accept_signal(
        &mut self,
        observation: &SignalObservation,
        ctx: &mut dyn StrategyCtx,
    ) -> Result<(), String> {
        self.ensure_restored(ctx);
        let envelope: SignalEnvelope =
            serde_json::from_slice(&observation.payload).map_err(|error| error.to_string())?;
        self.validate_observation(observation, ctx, &envelope)?;
        let kind = match &envelope.payload {
            SignalPayload::CarryFeatureBatch { .. } => "carry_feature_batch",
            SignalPayload::MarketSnapshot { .. } => "market_snapshot",
            SignalPayload::FundingUpdate { .. } => "funding_update",
            SignalPayload::UniverseChanged => "universe_changed",
            SignalPayload::Readiness { .. } => "readiness",
        };
        if observation.kind != kind {
            return Err("CARRY outer and inner signal kinds disagree".to_owned());
        }
        let receipt = Some((
            observation.source.clone(),
            observation.sequence,
            observation.observation_id.clone(),
        ));
        let effective_config = self.effective_config(ctx);
        match envelope.payload {
            SignalPayload::CarryFeatureBatch {
                decision_ts_ms,
                rows,
                upcoming_rows,
                settled_funding,
                presettlement,
                marks,
                rejections,
            } => {
                let extra = rows
                    .iter()
                    .chain(upcoming_rows.iter())
                    .map(|row| row.symbol.clone())
                    .chain(marks.iter().map(|mark| mark.symbol.clone()))
                    .collect::<Vec<_>>();
                let placeholder = CarryDecision {
                    schema_version: 1,
                    decision_ts_ms,
                    weights: BTreeMap::new(),
                    universe_size: 1,
                    replay_days: 0,
                    gross: 0.0,
                };
                let mut input = self.base_input(
                    ctx.wall_ms().max(observation.available_wall_ts_ms),
                    placeholder,
                    extra,
                    ctx,
                )?;
                input.trail_by_symbol = rows
                    .iter()
                    .filter(|row| row.bar_ts_ms == decision_ts_ms)
                    .filter_map(|row| {
                        row.trail_fund_24h
                            .filter(|value| value.is_finite())
                            .map(|value| (row.symbol.clone(), value))
                    })
                    .collect();
                if !upcoming_rows.is_empty() && input.account_healthy {
                    input.upcoming_sizing_equity_usdt = Some(input.equity_usdt);
                }
                input.signal_receipt = receipt;
                for mark in &marks {
                    validate_mark(mark, observation.observed_wall_ts_ms)?;
                    input.facts.prices.insert(mark.symbol.clone(), mark.mark_px);
                }
                let batch = CarrySignalBatch {
                    schema_version: 1,
                    decision_ts_ms,
                    rows,
                    upcoming_rows,
                    settled_funding,
                    presettlement,
                    marks,
                    rejections,
                };
                let output = reduce_signal(batch, input, self.state.clone(), &effective_config)
                    .map_err(str::to_owned)?;
                self.apply(output, ctx);
            }
            SignalPayload::MarketSnapshot {
                tickers,
                marks,
                presettlement,
            } => {
                validate_tickers(&tickers, observation.observed_wall_ts_ms)?;
                let Some(decision) = self.state.current_decision.clone() else {
                    self.consume_only(observation, ctx);
                    return Ok(());
                };
                let extra = marks
                    .iter()
                    .map(|mark| mark.symbol.clone())
                    .collect::<Vec<_>>();
                let mut input = self.base_input(
                    ctx.wall_ms().max(observation.available_wall_ts_ms),
                    decision,
                    extra,
                    ctx,
                )?;
                input.presettlement = presettlement;
                input.signal_receipt = receipt;
                for mark in &marks {
                    validate_mark(mark, observation.observed_wall_ts_ms)?;
                    input.facts.prices.insert(mark.symbol.clone(), mark.mark_px);
                }
                let output = reduce_lifecycle(input, self.state.clone(), &effective_config)
                    .map_err(str::to_owned)?;
                self.apply(output, ctx);
            }
            SignalPayload::FundingUpdate { settled_funding } => {
                let Some(decision) = self.state.current_decision.clone() else {
                    self.consume_only(observation, ctx);
                    return Ok(());
                };
                let extra = settled_funding
                    .iter()
                    .map(|row| row.symbol.clone())
                    .collect::<Vec<_>>();
                let mut input = self.base_input(
                    ctx.wall_ms().max(observation.available_wall_ts_ms),
                    decision,
                    extra,
                    ctx,
                )?;
                input.settled_funding = settled_funding;
                input.signal_receipt = receipt;
                let output = reduce_lifecycle(input, self.state.clone(), &effective_config)
                    .map_err(str::to_owned)?;
                self.apply(output, ctx);
            }
            SignalPayload::UniverseChanged => self.consume_only(observation, ctx),
            SignalPayload::Readiness { readiness } => {
                let _ = (
                    readiness.long_ready,
                    readiness.carry_ready,
                    readiness.universe_ready,
                    readiness.reason,
                    readiness.long_feature_ts_ms,
                    readiness.carry_feature_ts_ms,
                    readiness.rejected_symbols,
                );
                self.consume_only(observation, ctx);
            }
        }
        if self.flatten_request_id.is_some() {
            self.flatten_now(ctx);
        }
        Ok(())
    }
}

fn validate_mark(mark: &MarketMark, observed_ts_ms: i64) -> Result<(), String> {
    if !crate::native_common::valid_symbol(&mark.symbol)
        || mark.observed_ts_ms <= 0
        || mark.observed_ts_ms > observed_ts_ms
        || !mark.mark_px.is_finite()
        || mark.mark_px <= 0.0
    {
        return Err("CARRY market mark is invalid".to_owned());
    }
    Ok(())
}

fn validate_tickers(rows: &[TickerObservation], observed_ts_ms: i64) -> Result<(), String> {
    let mut seen = BTreeSet::new();
    for row in rows {
        let finite = [
            row.last_price,
            row.mark_price,
            row.index_price,
            row.bid1_price,
            row.ask1_price,
            row.bid1_size,
            row.ask1_size,
            row.open_interest,
            row.open_interest_value,
            row.turnover_24h,
            row.volume_24h,
            row.funding_rate,
        ]
        .into_iter()
        .flatten()
        .all(f64::is_finite);
        if !seen.insert(&row.symbol)
            || !crate::native_common::valid_symbol(&row.symbol)
            || row.observed_ts_ms <= 0
            || row.observed_ts_ms > observed_ts_ms
            || row.available_at_ms < row.observed_ts_ms
            || !finite
            || row.next_funding_time_ms.is_some_and(|value| value <= 0)
        {
            return Err("CARRY ticker observation is invalid".to_owned());
        }
    }
    Ok(())
}

impl Strategy for NativeCarry {
    fn name(&self) -> &str {
        NAME
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }

    fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
        Some(StrategyCheckpointIdentity {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            decision_fingerprint: self.config.fingerprint(),
        })
    }

    fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        Some(StrategyCheckpoint {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            decision_fingerprint: self.config.fingerprint(),
            payload: checkpoint_payload(&state),
        })
    }

    fn validate_checkpoint(&self, checkpoint: &StrategyCheckpoint) -> Result<(), String> {
        if checkpoint.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION
            || checkpoint.decision_fingerprint != self.config.fingerprint()
        {
            return Err("CARRY checkpoint identity mismatch".to_owned());
        }
        let state: SleeveState =
            serde_json::from_slice(&checkpoint.payload).map_err(|error| error.to_string())?;
        state.validate().map_err(str::to_owned)
    }

    fn translate_checkpoint(
        &self,
        _context: &StrategyImportContext,
        source_format: &str,
        sources: &[StrategyImportSource],
    ) -> Result<TranslatedStrategyState, String> {
        super::state_import::translate(&self.config, source_format, sources)
    }

    fn requires_signal_feed(&self) -> bool {
        true
    }

    fn configured_entries_enabled(&self) -> bool {
        self.config.entries_enabled
    }

    fn on_boot(&mut self, ctx: &mut dyn StrategyCtx) {
        self.replan(ctx);
    }

    fn on_entry_permission(
        &mut self,
        _request_id: &str,
        _entries_enabled: bool,
        ctx: &mut dyn StrategyCtx,
    ) {
        self.replan(ctx);
    }

    fn on_flatten_directional(&mut self, request_id: &str, ctx: &mut dyn StrategyCtx) {
        self.flatten_request_id = Some(request_id.to_owned());
        self.flatten_now(ctx);
    }

    fn on_signal(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        if let Err(error) = self.accept_signal(observation, ctx) {
            self.last_error = Some(error);
        }
    }

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let symbol = match event {
            MarketEvent::Quote { symbol, .. }
            | MarketEvent::Ticker { symbol, .. }
            | MarketEvent::Depth { symbol, .. }
            | MarketEvent::Trades { symbol, .. } => Some(*symbol),
            MarketEvent::FeedReset { .. } => None,
        };
        if symbol
            .and_then(|id| ctx.symbol_name(id))
            .is_some_and(|name| self.state.desired_targets.contains_key(name))
        {
            self.replan(ctx);
        }
    }

    fn on_timer(&mut self, id: TimerId, _now_ns: u64, ctx: &mut dyn StrategyCtx) {
        if id == TIMER {
            self.replan(ctx);
        }
    }

    fn on_order(&mut self, _update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        self.replan(ctx);
    }

    fn on_intent_refused(
        &mut self,
        symbol: SymbolId,
        reduce_only: bool,
        reason: &str,
        ctx: &mut dyn StrategyCtx,
    ) {
        self.ensure_restored(ctx);
        let Some(name) = ctx.symbol_name(symbol).map(str::to_owned) else {
            return;
        };
        self.blockers.insert(name.clone(), reason.to_owned());
        if reduce_only {
            ctx.arm_timer(TIMER, 1_000_000_000);
            return;
        }
        self.state.refused_entries.insert(name.clone());
        self.state
            .entry_retry_after_ms
            .insert(name, ctx.wall_ms().max(1).saturating_add(ENTRY_RETRY_MS));
        self.replan(ctx);
        ctx.arm_timer(
            TIMER,
            u64::try_from(ENTRY_RETRY_MS).unwrap_or(1) * 1_000_000,
        );
    }

    fn entry_blockers(&self) -> Vec<(String, String)> {
        self.blockers
            .iter()
            .map(|(symbol, reason)| (symbol.clone(), reason.clone()))
            .collect()
    }

    fn health_error(&self) -> Option<&str> {
        self.last_error.as_deref()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn worker_payload_rejects_an_unknown_field() {
        let raw = br#"{"schema_version":1,"config":{},"universe":null,"payload":{"kind":"universe_changed","extra":1}}"#;
        assert!(serde_json::from_slice::<SignalEnvelope>(raw).is_err());
    }
}
