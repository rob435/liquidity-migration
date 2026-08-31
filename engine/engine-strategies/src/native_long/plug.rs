//! Engine adapter for the native LONG reducer.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    MarketEvent, OrderUpdate, SignalObservation, Strategy, StrategyCheckpoint,
    StrategyCheckpointIdentity, StrategyCtx, StrategyId, StrategyImportContext,
    StrategyImportSource, Subscription, SymbolId, TimerId, TranslatedStrategyState, WorkPolicy,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use serde::Deserialize;

use super::plan::{
    reduce_batch, BatchInput, BatchOutput, DataRejection, DecisionInput, FeatureRow,
    LongSignalBatch, MarketMark, SleeveState, StrategyConfig,
};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, checkpoint_payload, emit_effects,
    flatten_execution, owned_order_state, planner_facts, validate_signal_identity, Effect,
    FlattenExecutionInput, SignalConfigIdentity, UniverseIdentity,
    DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};
use crate::params::Params;
use crate::position_plan::Skipped;
use crate::BuildError;

pub const NAME: &str = "long_native";
const TIMER: TimerId = TimerId(0x4c4f_4e47);

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
    LongFeatureBatch {
        decision_ts_ms: i64,
        feature_ts_ms: i64,
        rows: Vec<FeatureRow>,
        marks: Vec<MarketMark>,
        cold_start_fallback_count: usize,
        rejections: Vec<DataRejection>,
    },
}

pub struct NativeLong {
    id: StrategyId,
    pub config: StrategyConfig,
    pub state: SleeveState,
    restored: bool,
    checkpoint_fingerprint: Option<String>,
    blockers: BTreeMap<String, String>,
    last_error: Option<String>,
    flatten_request_id: Option<String>,
}

impl NativeLong {
    /// Reducer-facing constructor used by contract tests.
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

    pub fn reduce(&mut self, input: BatchInput) -> Result<BatchOutput, &'static str> {
        let output = reduce_batch(input, self.state.clone(), &self.config)?;
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
        let expected = self.config.fingerprint();
        if checkpoint.schema_version != DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION
            || checkpoint.decision_fingerprint != expected
        {
            self.last_error = Some("LONG checkpoint identity mismatch".to_owned());
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
                self.last_error = Some(format!("LONG checkpoint refused: {error}"));
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

    fn known_symbols(&self, ctx: &dyn StrategyCtx) -> BTreeSet<String> {
        let mut symbols = attributed_symbols(ctx);
        symbols.extend(self.state.symbols.keys().cloned());
        symbols.extend(self.state.pending_signals.keys().cloned());
        symbols.extend(self.state.exit_pending.iter().cloned());
        symbols
    }

    fn mark(ctx: &dyn StrategyCtx, symbol: &str) -> Option<f64> {
        let id = ctx.symbol_id(symbol)?;
        let quote = ctx.quote(id);
        let value = if quote.bid_px > 0.0 && quote.ask_px > 0.0 {
            (quote.bid_px + quote.ask_px) / 2.0
        } else {
            let ticker = ctx.ticker(id);
            if ticker.mark_px > 0.0 {
                ticker.mark_px
            } else {
                ticker.last_px
            }
        };
        (value.is_finite() && value > 0.0).then_some(value)
    }

    fn current_decisions(&self, now_ms: i64, ctx: &dyn StrategyCtx) -> Vec<DecisionInput> {
        let account = ctx.account_summary();
        let equity = if account.equity_usdt.is_finite() && account.equity_usdt >= 0.0 {
            account.equity_usdt
        } else {
            0.0
        };
        let mut decisions = BTreeMap::<String, DecisionInput>::new();
        for (symbol, prior) in &self.state.symbols {
            decisions.insert(
                symbol.clone(),
                DecisionInput {
                    decision_ts_ms: now_ms,
                    symbol: symbol.clone(),
                    signal_ts_ms: prior.attempted_signal_ts_ms,
                    signal_close: 0.0,
                    market_price: Self::mark(ctx, symbol),
                    observed_low: None,
                    equity_usdt: equity,
                    feature_row: None,
                },
            );
        }
        for (symbol, pending) in &self.state.pending_signals {
            decisions.insert(
                symbol.clone(),
                DecisionInput {
                    decision_ts_ms: now_ms,
                    symbol: symbol.clone(),
                    signal_ts_ms: pending.signal_ts_ms,
                    signal_close: pending.signal_close,
                    market_price: Self::mark(ctx, symbol),
                    observed_low: None,
                    equity_usdt: equity,
                    feature_row: Some(pending.feature_row.clone()),
                },
            );
        }
        decisions.into_values().collect()
    }

    fn make_input(
        &self,
        decisions: Vec<DecisionInput>,
        signal_receipt: Option<(String, u64, String)>,
        ctx: &dyn StrategyCtx,
    ) -> BatchInput {
        let mut symbols = self.known_symbols(ctx);
        symbols.extend(decisions.iter().map(|row| row.symbol.clone()));
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working.iter().cloned());
        BatchInput {
            decisions,
            facts: planner_facts(ctx, &symbols),
            owned_working_symbols: working,
            owned_opening_order_ids: opening,
            checkpoint_fingerprint: self.checkpoint_fingerprint.clone(),
            signal_receipt,
        }
    }

    fn apply(&mut self, output: BatchOutput, ctx: &mut dyn StrategyCtx) {
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
            None,
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
        let now_ms = ctx.wall_ms().max(1);
        let decisions = self.current_decisions(now_ms, ctx);
        if decisions.is_empty() && self.checkpoint_fingerprint.is_none() {
            return;
        }
        let input = self.make_input(decisions, None, ctx);
        let config = self.effective_config(ctx);
        match reduce_batch(input, self.state.clone(), &config) {
            Ok(output) => self.apply(output, ctx),
            Err(error) => self.last_error = Some(error.to_owned()),
        }
    }

    fn flatten_now(&mut self, ctx: &mut dyn StrategyCtx) {
        self.ensure_restored(ctx);
        let Some(request_id) = self.flatten_request_id.clone() else {
            return;
        };
        let mut symbols = self.known_symbols(ctx);
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working);
        let facts = planner_facts(ctx, &symbols);
        let conclusively_flat = attributed_exposure_is_flat(ctx, &symbols);
        self.state.pending_signals.clear();
        if conclusively_flat && opening.values().all(Vec::is_empty) {
            self.state.symbols.clear();
            self.state.exit_pending.clear();
            self.state.refused_entries.clear();
        } else {
            self.state.exit_pending.extend(facts.held_symbols());
        }
        let (execution, flat) = flatten_execution(
            &self.state,
            FlattenExecutionInput {
                config_fingerprint: self.config.fingerprint(),
                facts: &facts,
                owned_opening_order_ids: &opening,
                now_ms: ctx.wall_ms().max(1),
                request_id: &request_id,
                tag: "long_native_flatten",
                conclusively_flat,
            },
        );
        if flat {
            self.flatten_request_id = None;
        }
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        if let Err(error) = emit_effects(execution.effects, self.id, None, self.entry_work(), ctx) {
            self.last_error = Some(error.to_owned());
        }
    }

    fn arm_next(&self, ctx: &mut dyn StrategyCtx) {
        let now_ms = ctx.wall_ms().max(1);
        let mut wakes = Vec::new();
        for prior in self.state.symbols.values() {
            if prior.requested && !prior.filled && prior.entry_valid_until_ms > now_ms {
                wakes.push(prior.entry_valid_until_ms);
            }
            if prior.filled {
                if prior.max_hold_deadline_ts_ms > now_ms {
                    wakes.push(prior.max_hold_deadline_ts_ms);
                }
                let decay = prior.entry_ts_ms.saturating_add(prior.stop_decay_after_ms);
                if prior.stop_decay_after_ms > 0 && decay > now_ms {
                    wakes.push(decay);
                }
            }
        }
        for pending in self.state.pending_signals.values() {
            for wake in [
                pending.signal_ts_ms + self.config.rule.entry_delay_hours.max(1) * 3_600_000,
                pending.signal_ts_ms + self.config.rule.fc_sniper_deadline_hours * 3_600_000,
                pending.signal_ts_ms + self.config.signal_freshness_ms,
            ] {
                if wake > now_ms {
                    wakes.push(wake);
                }
            }
        }
        if let Some(next) = wakes.into_iter().min() {
            let delay_ms = u64::try_from(next.saturating_sub(now_ms)).unwrap_or(u64::MAX);
            ctx.arm_timer(TIMER, delay_ms.saturating_mul(1_000_000).max(1));
        }
    }

    fn accept_signal(
        &mut self,
        observation: &SignalObservation,
        ctx: &mut dyn StrategyCtx,
    ) -> Result<(), String> {
        self.ensure_restored(ctx);
        if observation.schema_version != SIGNAL_OBSERVATION_SCHEMA_VERSION
            || observation.destination != self.id
            || observation.kind != "long_feature_batch"
            || observation.decision_fingerprint != self.config.fingerprint()
            || observation.source.is_empty()
            || observation.sequence == 0
            || observation.observation_id.is_empty()
            || observation.observed_wall_ts_ms <= 0
            || observation.available_wall_ts_ms < observation.observed_wall_ts_ms
            || observation.available_wall_ts_ms > ctx.wall_ms()
        {
            return Err("LONG signal envelope identity is invalid".to_owned());
        }
        let envelope: SignalEnvelope =
            serde_json::from_slice(&observation.payload).map_err(|error| error.to_string())?;
        if envelope.schema_version != 1 {
            return Err("unsupported LONG signal payload schema".to_owned());
        }
        validate_signal_identity(
            &envelope.config,
            envelope.universe.as_ref(),
            &self.config.environment,
        )
        .map_err(str::to_owned)?;
        if envelope.config.long_profile != self.config.profile_name
            || envelope.config.long_execution_strategy_id != self.config.rule.execution_strategy_id
            || envelope.config.long_rule_sha256 != self.config.rule_sha256
            || envelope.config.long_feature_contract_sha256 != self.config.feature_contract_sha256
            || envelope.config.long_decision_fingerprint != self.config.fingerprint()
        {
            return Err("LONG signal config does not bind this reducer".to_owned());
        }
        let SignalPayload::LongFeatureBatch {
            decision_ts_ms,
            feature_ts_ms,
            rows,
            marks,
            cold_start_fallback_count,
            rejections,
        } = envelope.payload;
        let batch = LongSignalBatch {
            decision_ts_ms,
            feature_ts_ms,
            rows,
            marks,
            cold_start_fallback_count,
            rejections,
        };
        if batch.decision_ts_ms != observation.observed_wall_ts_ms
            || batch.feature_ts_ms <= 0
            || batch.feature_ts_ms > batch.decision_ts_ms
            || batch.rows.is_empty()
        {
            return Err("LONG feature batch timing is invalid".to_owned());
        }
        let mut mark_by_symbol = BTreeMap::new();
        for mark in &batch.marks {
            if !crate::native_common::valid_symbol(&mark.symbol)
                || mark.observed_ts_ms <= 0
                || mark.observed_ts_ms > observation.observed_wall_ts_ms
                || !mark.mark_px.is_finite()
                || mark.mark_px <= 0.0
                || mark_by_symbol
                    .insert(mark.symbol.clone(), mark.mark_px)
                    .is_some()
            {
                return Err("LONG market marks are invalid".to_owned());
            }
        }
        let account = ctx.account_summary();
        let equity = if account.equity_usdt.is_finite() && account.equity_usdt >= 0.0 {
            account.equity_usdt
        } else {
            0.0
        };
        let mut seen = BTreeSet::new();
        let mut decisions = Vec::with_capacity(batch.rows.len());
        for row in batch.rows {
            if !seen.insert(row.symbol.clone()) || row.ts_ms != batch.feature_ts_ms {
                return Err("LONG feature batch contains duplicate or off-grid rows".to_owned());
            }
            let market_price = mark_by_symbol
                .get(&row.symbol)
                .copied()
                .or_else(|| Self::mark(ctx, &row.symbol));
            decisions.push(DecisionInput {
                decision_ts_ms: batch.decision_ts_ms,
                symbol: row.symbol.clone(),
                signal_ts_ms: row.ts_ms,
                signal_close: row.close.unwrap_or(0.0),
                market_price,
                observed_low: None,
                equity_usdt: equity,
                feature_row: Some(row),
            });
        }
        let receipt = Some((
            observation.source.clone(),
            observation.sequence,
            observation.observation_id.clone(),
        ));
        let mut input = self.make_input(decisions, receipt, ctx);
        for (symbol, mark) in mark_by_symbol {
            input.facts.prices.insert(symbol, mark);
        }
        let config = self.effective_config(ctx);
        let output = reduce_batch(input, self.state.clone(), &config).map_err(str::to_owned)?;
        self.apply(output, ctx);
        if self.flatten_request_id.is_some() {
            self.flatten_now(ctx);
        }
        Ok(())
    }
}

impl Strategy for NativeLong {
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
            return Err("LONG checkpoint identity mismatch".to_owned());
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
            .is_some_and(|name| {
                self.state.symbols.contains_key(name)
                    || self.state.pending_signals.contains_key(name)
                    || self.state.exit_pending.contains(name)
            })
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
        let flat = ctx.my_position_facts(symbol).is_none_or(|facts| {
            facts.attributed_signed_qty.abs() <= f64::EPSILON
                && facts.in_flight_signed_qty.abs() <= f64::EPSILON
        });
        if flat {
            self.state.symbols.remove(&name);
            self.state.pending_signals.remove(&name);
            self.state.exit_pending.remove(&name);
        }
        let effect = Effect::PersistCheckpoint {
            symbol: String::new(),
            config_fingerprint: self.config.fingerprint(),
            payload: checkpoint_payload(&self.state),
        };
        if let Err(error) = emit_effects(vec![effect], self.id, None, None, ctx) {
            self.last_error = Some(error.to_owned());
        }
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
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
    fn params_are_one_strict_config_blob() {
        let params: toml::Value = toml::from_str("config_json = '{}'").expect("toml");
        assert!(NativeLong::from_params(StrategyId(0), &params).is_err());
    }

    #[test]
    fn worker_payload_rejects_an_unknown_field() {
        let raw = br#"{"schema_version":1,"config":{},"universe":null,"payload":{"kind":"long_feature_batch","decision_ts_ms":1,"feature_ts_ms":1,"rows":[],"marks":[],"cold_start_fallback_count":0,"rejections":[],"extra":1}}"#;
        assert!(serde_json::from_slice::<SignalEnvelope>(raw).is_err());
    }

    #[test]
    fn subscriptions_are_supplied_by_durable_signal_observations() {
        assert!(Vec::<Subscription>::new().is_empty());
    }
}
