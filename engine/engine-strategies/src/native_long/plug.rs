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
    reduce_batch, reduce_batch_with_mode, BatchInput, BatchOutput, DataRejection, DecisionInput,
    FeatureRow, LongSignalBatch, MarketMark, ReplanMode, SleeveState, StrategyConfig,
};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, checkpoint_payload,
    directional_account_is_healthy, emit_effects, flatten_execution, owned_order_state,
    planner_facts, validate_exact_symbol_coverage, validate_signal_identity, Effect,
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
        config.entries_enabled =
            ctx.entries_enabled(self.config.entries_enabled) && Self::account(ctx).0;
        config
    }

    fn account(ctx: &dyn StrategyCtx) -> (bool, f64) {
        let account = ctx.account_summary();
        let healthy = directional_account_is_healthy(account);
        (healthy, if healthy { account.equity_usdt } else { 1.0 })
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
        let (_, equity) = Self::account(ctx);
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
            now_ms: ctx.wall_ms().max(1),
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
        } else {
            self.last_error = None;
        }
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        self.arm_next(ctx);
    }

    fn replan_with_mode(&mut self, replan_mode: ReplanMode, ctx: &mut dyn StrategyCtx) {
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
        match reduce_batch_with_mode(input, self.state.clone(), &config, replan_mode) {
            Ok(output) => self.apply(output, ctx),
            Err(error) => self.last_error = Some(error.to_owned()),
        }
    }

    fn replan(&mut self, ctx: &mut dyn StrategyCtx) {
        self.replan_with_mode(ReplanMode::Ordinary, ctx);
    }

    fn defer_opening(&mut self, name: String, reason: &str, ctx: &mut dyn StrategyCtx) {
        let now_ms = ctx.wall_ms().max(1);
        if self.state.entry_cycle_started_ms == 0
            || now_ms
                >= self
                    .state
                    .entry_cycle_started_ms
                    .saturating_add(super::plan::ENTRY_CYCLE_MS)
        {
            self.state.entry_cycle_started_ms = now_ms;
            self.state.entry_cycle_selected_symbols.clear();
            self.state.refused_entries.clear();
        }
        self.blockers.insert(name.clone(), reason.to_owned());
        self.state.refused_entries.insert(name);
        let effect = Effect::PersistCheckpoint {
            symbol: String::new(),
            config_fingerprint: self.config.fingerprint(),
            payload: checkpoint_payload(&self.state),
        };
        if let Err(error) = emit_effects(vec![effect], self.id, None, None, ctx) {
            self.last_error = Some(error.to_owned());
        }
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        self.arm_next(ctx);
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
            if prior.attempted_signal_ts_ms > now_ms {
                wakes.push(prior.attempted_signal_ts_ms);
            }
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
        if !self.state.pending_signals.is_empty()
            && !Self::account(ctx).0
            && ctx.entries_enabled(self.config.entries_enabled)
        {
            wakes.push(now_ms.saturating_add(1_000));
        }
        let next_entry_cycle = self
            .state
            .entry_cycle_started_ms
            .saturating_add(super::plan::ENTRY_CYCLE_MS);
        let unresolved_entry = self
            .state
            .symbols
            .values()
            .any(|prior| prior.requested && !prior.filled);
        if (unresolved_entry
            || !self.state.pending_signals.is_empty()
            || !self.state.refused_entries.is_empty())
            && next_entry_cycle > now_ms
        {
            wakes.push(next_entry_cycle);
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
        if observation.schema_version != SIGNAL_OBSERVATION_SCHEMA_VERSION
            || observation.destination != self.id
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
        if observation.decision_fingerprint != envelope.config.long_decision_fingerprint {
            return Err("LONG outer and inner decision fingerprints disagree".to_owned());
        }
        let kind = match &envelope.payload {
            SignalPayload::LongFeatureBatch { .. } => "long_feature_batch",
        };
        if observation.kind != kind {
            return Err("LONG outer and inner signal kinds disagree".to_owned());
        }
        self.ensure_restored(ctx);
        if observation.decision_fingerprint != self.config.fingerprint() {
            emit_effects(
                vec![Effect::ConsumeSignal {
                    source: observation.source.clone(),
                    sequence: observation.sequence,
                    observation_id: observation.observation_id.clone(),
                }],
                self.id,
                None,
                None,
                ctx,
            )
            .map_err(str::to_owned)?;
            self.last_error = None;
            return Ok(());
        }
        let eligible = envelope
            .universe
            .as_ref()
            .expect("validated LONG universe identity")
            .long_symbols
            .clone();
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
        {
            return Err("LONG feature batch timing is invalid".to_owned());
        }
        validate_feature_coverage(&eligible, &batch.rows, &batch.rejections)?;
        if batch.rejections.iter().any(|row| {
            row.reason.trim().is_empty()
                || row
                    .first_missing_ts_ms
                    .is_some_and(|timestamp| timestamp <= 0 || timestamp > batch.decision_ts_ms)
        }) {
            return Err("LONG data rejection is invalid".to_owned());
        }
        let eligible = eligible.into_iter().collect::<BTreeSet<_>>();
        let mut mark_by_symbol = BTreeMap::new();
        for mark in &batch.marks {
            if !crate::native_common::valid_symbol(&mark.symbol)
                || !eligible.contains(&mark.symbol)
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
        if entry_window_is_closed(
            ctx.wall_ms(),
            batch.decision_ts_ms,
            self.config.book_validity_ms,
            self.config.engine_entry_cutoff_ms,
        ) {
            self.consume_only(observation, ctx);
            return Ok(());
        }
        let (_, equity) = Self::account(ctx);
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

fn entry_window_is_closed(
    now_ms: i64,
    decision_ts_ms: i64,
    book_validity_ms: i64,
    entry_cutoff_ms: i64,
) -> bool {
    now_ms
        >= decision_ts_ms
            .saturating_add(book_validity_ms)
            .saturating_sub(entry_cutoff_ms)
}

fn validate_feature_coverage(
    eligible: &[String],
    rows: &[FeatureRow],
    rejections: &[DataRejection],
) -> Result<(), String> {
    let accepted = rows
        .iter()
        .map(|row| row.symbol.clone())
        .collect::<Vec<_>>();
    let rejected = rejections
        .iter()
        .map(|row| row.symbol.clone())
        .collect::<Vec<_>>();
    validate_exact_symbol_coverage(eligible, &accepted, &rejected)
        .map_err(|error| format!("LONG feature coverage is invalid: {error}"))
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
        self.replan_with_mode(ReplanMode::BootRecovery, ctx);
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

    fn on_order(&mut self, update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        self.ensure_restored(ctx);
        let terminal = match update {
            OrderUpdate::Reject {
                client_order_id,
                reason,
                ..
            } => Some((client_order_id.as_str(), reason.as_str())),
            OrderUpdate::Cancelled {
                client_order_id, ..
            } => Some((client_order_id.as_str(), "opening_order_cancelled")),
            _ => None,
        };
        if let Some((client_order_id, reason)) = terminal {
            if let Some(facts) = ctx.order_facts(client_order_id) {
                if facts.reduce_only {
                    if let Some(name) = ctx.symbol_name(facts.symbol).map(str::to_owned) {
                        self.blockers.insert(name, reason.to_owned());
                    }
                    ctx.arm_timer(TIMER, 1_000_000_000);
                    return;
                }
                if let Some(name) = ctx.symbol_name(facts.symbol).map(str::to_owned) {
                    if self
                        .state
                        .symbols
                        .get(&name)
                        .is_some_and(|prior| prior.requested)
                        && !self.state.exit_pending.contains(&name)
                    {
                        self.defer_opening(name, reason, ctx);
                        return;
                    }
                }
            }
        }
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
        if reduce_only {
            self.blockers.insert(name, reason.to_owned());
            ctx.arm_timer(TIMER, 1_000_000_000);
            return;
        }
        self.defer_opening(name, reason, ctx);
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
    use crate::mock_ctx::{MockCtx, RestingSeed};
    use engine_types::{Action, OrderKind, Side};
    use serde_json::json;

    const NOW_MS: i64 = 1_700_000_000_000;

    fn config() -> StrategyConfig {
        serde_json::from_value(json!({
            "schema_version": 1,
            "profile_name": "v12",
            "environment": "demo",
            "rule_sha256": "1".repeat(64),
            "feature_contract_sha256": "2".repeat(64),
            "operational_profile_sha256": "3".repeat(64),
            "entries_enabled": true,
            "rule": {
                "execution_strategy_id": "long_native_v12_wide_stop",
                "entry_delay_hours": 1,
                "fc_min_day_return": 0.15,
                "fc_top_volume_rank_max": 10.0,
                "fc_min_close_location": 0.7,
                "fc_max_hold_days": 3,
                "fc_max_atr_pct": 0.12,
                "fc_atr_stop_mult": 3.0,
                "fc_sigma_mult": 2.5,
                "fc_sniper_retrace_pct": 0.01,
                "fc_sniper_deadline_hours": 6,
                "weekend_size_mult": 1.5,
                "fc_close_loc_multi_day": 0.6,
                "fc_stop_time_decay_hours": 48,
                "fc_stop_time_decay_atr_mult": 1.5,
                "max_concurrent_positions": 10,
                "cooldown_days": 7,
                "gross_exposure": 1.0,
                "vol_floor_annual": 0.3,
                "max_position_weight": 0.3,
                "vol_target_annual": 0.6,
                "vol_target_min_scale": 0.3,
                "vol_target_max_scale": 1.25
            },
            "notional_multiplier": 6.0,
            "entry_leverage": 5.0,
            "order_notional_pct_equity": 0.0,
            "wallet_balance_fraction": 1.0,
            "max_new_entries_per_cycle": 5,
            "signal_freshness_ms": 86_400_000,
            "book_validity_ms": 3_600_000,
            "entry_floor_usdt": 6.0,
            "resize_floor_usdt": 1.0,
            "resize_floor_fraction": 0.05,
            "engine_entry_cutoff_ms": 900_000,
            "rest_entries": false,
            "hold_decision_price": false,
            "give_up_instead_of_crossing": false
        }))
        .expect("LONG config")
    }

    fn identity(config: &StrategyConfig, fingerprint: String) -> SignalConfigIdentity {
        SignalConfigIdentity {
            schema_version: 1,
            signal_config_id: "signal-test".into(),
            long_profile: config.profile_name.clone(),
            long_execution_strategy_id: config.rule.execution_strategy_id.clone(),
            long_rule_sha256: config.rule_sha256.clone(),
            long_feature_contract_sha256: config.feature_contract_sha256.clone(),
            signal_config_sha256: "4".repeat(64),
            carry_config_id: "carry-test".into(),
            carry_rule_sha256: "5".repeat(64),
            carry_feature_contract_sha256: "6".repeat(64),
            operational_profile_sha256: config.operational_profile_sha256.clone(),
            engine_config_sha256: "7".repeat(64),
            long_decision_fingerprint: fingerprint,
            carry_decision_fingerprint: "8".repeat(64),
        }
    }

    fn universe(environment: &str) -> UniverseIdentity {
        UniverseIdentity {
            mode: crate::native_common::UniverseMode::Current,
            environment: environment.into(),
            endpoint: "candidate-universe.json".into(),
            snapshot_ts_ms: NOW_MS - 1,
            available_at_ms: NOW_MS,
            artifact_sha256: "9".repeat(64),
            file_sha256: "a".repeat(64),
            symbols: Vec::new(),
            long_symbols: Vec::new(),
            carry_symbols: Vec::new(),
        }
    }

    fn observation(
        config: &StrategyConfig,
        fingerprint: String,
        sequence: u64,
    ) -> SignalObservation {
        let payload = json!({
            "schema_version": 1,
            "config": identity(config, fingerprint.clone()),
            "universe": universe(&config.environment),
            "payload": {
                "kind": "long_feature_batch",
                "decision_ts_ms": NOW_MS,
                "feature_ts_ms": NOW_MS - 86_400_000,
                "rows": [],
                "marks": [],
                "cold_start_fallback_count": 0,
                "rejections": []
            }
        });
        SignalObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: fingerprint,
            destination: StrategyId(0),
            source: "signal.generation.long".into(),
            sequence,
            observation_id: format!("long-{sequence}"),
            kind: "long_feature_batch".into(),
            observed_wall_ts_ms: NOW_MS,
            available_wall_ts_ms: NOW_MS,
            subscriptions: Vec::new(),
            payload: serde_json::to_vec(&payload).expect("LONG payload"),
            content_sha256: "b".repeat(64),
        }
    }

    fn params(config: &StrategyConfig) -> toml::Value {
        toml::Value::Table(
            [(
                "config_json".into(),
                toml::Value::String(serde_json::to_string(config).expect("LONG config JSON")),
            )]
            .into_iter()
            .collect(),
        )
    }

    fn row(symbol: &str) -> FeatureRow {
        FeatureRow {
            symbol: symbol.to_owned(),
            ..FeatureRow::default()
        }
    }

    fn rejection(symbol: &str) -> DataRejection {
        DataRejection {
            symbol: symbol.to_owned(),
            reason: "missing".to_owned(),
            first_missing_ts_ms: None,
        }
    }

    #[test]
    fn params_are_one_strict_config_blob() {
        let params: toml::Value = toml::from_str("config_json = '{}'").expect("toml");
        assert!(NativeLong::from_params(StrategyId(0), &params).is_err());
    }

    #[test]
    fn account_snapshot_without_an_engine_observation_is_unhealthy() {
        let mut ctx = MockCtx::new();
        assert!(NativeLong::account(&ctx).0);
        ctx.set_account_summary(f64::NAN, 1_000.0);
        assert!(!NativeLong::account(&ctx).0);
        ctx.set_account_summary(1_000.0, 1_000.0);
        ctx.set_now(0);
        assert!(!NativeLong::account(&ctx).0);
    }

    #[test]
    fn backward_wall_clock_arms_the_attempted_signal_time() {
        let attempted_signal_ts_ms = NOW_MS + 5_000;
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            symbols: BTreeMap::from([(
                "AUSDT".into(),
                super::super::plan::PriorState {
                    requested: true,
                    filled: false,
                    target_notional_usdt: 100.0,
                    stop_loss_fraction: 0.2,
                    max_hold_duration_ms: 2 * 86_400_000,
                    entry_valid_until_ms: NOW_MS + 10_000,
                    attempted_signal_ts_ms,
                    ..super::super::plan::PriorState::default()
                },
            )]),
            ..SleeveState::default()
        };
        let strategy = NativeLong::new(config(), state).expect("strategy");
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(NOW_MS);

        strategy.arm_next(&mut ctx);

        assert_eq!(ctx.arm_calls.len(), 1);
        assert_eq!(
            ctx.arm_calls[0].due_ns - ctx.arm_calls[0].armed_ns,
            5_000_000_000
        );
    }

    #[test]
    fn terminal_reduction_waits_for_the_retry_timer() {
        for update in [
            OrderUpdate::Reject {
                client_order_id: "exit-order".into(),
                code: 10001,
                reason: "venue_reject".into(),
            },
            OrderUpdate::Cancelled {
                client_order_id: "exit-order".into(),
                recv_ns: 2_000,
            },
        ] {
            let mut strategy = NativeLong::new(config(), SleeveState::default()).expect("strategy");
            let mut ctx = MockCtx::new();
            ctx.set_wall_ms(NOW_MS);
            ctx.set_position("AUSDT", Side::Buy, 1.0, 100.0);
            let symbol = ctx.id_of("AUSDT");

            strategy.on_flatten_directional("flatten-long", &mut ctx);
            assert!(
                ctx.emitted
                    .iter()
                    .any(|action| matches!(action, Action::Place(intent) if intent.reduce_only)),
                "initial flatten actions: {:?}",
                ctx.emitted
            );
            ctx.emitted.clear();
            ctx.resting.push(RestingSeed {
                client_order_id: "exit-order".into(),
                symbol,
                side: Side::Sell,
                kind: OrderKind::Market,
                qty: 1.0,
                filled_qty: 0.0,
                reduce_only: true,
                acked: true,
            });

            strategy.on_order(&update, &mut ctx);

            assert!(ctx.emitted.is_empty(), "terminal callback must stay quiet");
            let timer = *ctx.arm_calls.last().expect("reduction retry timer");
            assert_eq!(timer.id, TIMER);
            assert_eq!(timer.due_ns - timer.armed_ns, 1_000_000_000);

            ctx.resting.clear();
            ctx.set_now(timer.due_ns);
            strategy.on_timer(timer.id, timer.due_ns, &mut ctx);
            let retries = ctx
                .emitted
                .iter()
                .filter_map(|action| match action {
                    Action::Place(intent) => Some(intent),
                    _ => None,
                })
                .collect::<Vec<_>>();
            assert_eq!(retries.len(), 1, "timer must produce one retry");
            assert!(retries[0].reduce_only);
        }
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

    #[test]
    fn downstream_outage_cannot_replay_an_expired_long_entry() {
        assert!(!entry_window_is_closed(84_999, 0, 100_000, 15_000));
        assert!(entry_window_is_closed(85_000, 0, 100_000, 15_000));
        assert!(entry_window_is_closed(i64::MAX, i64::MAX - 10, 100, 15));
    }

    #[test]
    fn delivery_requires_an_exact_long_population_partition() {
        let eligible = vec!["AUSDT".to_owned(), "BUSDT".to_owned()];
        assert!(
            validate_feature_coverage(&eligible, &[row("AUSDT")], &[rejection("BUSDT")]).is_ok()
        );
        assert!(validate_feature_coverage(&eligible, &[row("AUSDT")], &[]).is_err());
        assert!(
            validate_feature_coverage(&eligible, &[row("AUSDT")], &[rejection("AUSDT")]).is_err()
        );
        assert!(validate_feature_coverage(
            &eligible,
            &[row("AUSDT"), row("CUSDT")],
            &[rejection("BUSDT")]
        )
        .is_err());
    }

    #[test]
    fn old_contract_is_consumed_once_before_current_contract_and_restart() {
        let config = config();
        let current_fingerprint = config.fingerprint();
        let old_fingerprint = "c".repeat(64);
        let mut strategy = NativeLong::new(config.clone(), SleeveState::default()).unwrap();
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(NOW_MS);

        strategy
            .accept_signal(&observation(&config, old_fingerprint.clone(), 1), &mut ctx)
            .expect("old self-consistent contract is terminal");
        assert_eq!(ctx.emitted.len(), 1);
        assert!(matches!(
            &ctx.emitted[0],
            Action::ConsumeSignalObservation { sequence: 1, .. }
        ));

        strategy
            .accept_signal(
                &observation(&config, current_fingerprint.clone(), 2),
                &mut ctx,
            )
            .expect("current contract");
        let checkpoint = ctx
            .emitted
            .iter()
            .find_map(|action| match action {
                Action::SetStrategyGlobalCheckpoint { checkpoint, .. } => Some(checkpoint.clone()),
                _ => None,
            })
            .expect("current checkpoint");
        assert_eq!(
            ctx.emitted
                .iter()
                .filter(|action| matches!(
                    action,
                    Action::ConsumeSignalObservation { sequence: 1, .. }
                ))
                .count(),
            1
        );
        assert!(ctx
            .emitted
            .iter()
            .any(|action| matches!(action, Action::ConsumeSignalObservation { sequence: 2, .. })));

        let mut restarted = NativeLong::from_params(StrategyId(0), &params(&config)).unwrap();
        let mut restart_ctx = MockCtx::new();
        restart_ctx.set_wall_ms(NOW_MS);
        restart_ctx.set_global_checkpoint(checkpoint);
        restarted
            .accept_signal(
                &observation(&config, current_fingerprint, 3),
                &mut restart_ctx,
            )
            .expect("current contract after restart");
        assert!(restart_ctx
            .emitted
            .iter()
            .any(|action| matches!(action, Action::ConsumeSignalObservation { sequence: 3, .. })));

        let mut mismatched = observation(&config, old_fingerprint, 4);
        mismatched.decision_fingerprint = "d".repeat(64);
        let emitted_before = restart_ctx.emitted.len();
        assert!(restarted
            .accept_signal(&mismatched, &mut restart_ctx)
            .unwrap_err()
            .contains("outer and inner"));
        assert_eq!(restart_ctx.emitted.len(), emitted_before);

        let mut kind_mismatched = observation(&config, "e".repeat(64), 5);
        kind_mismatched.kind = "market_snapshot".into();
        assert!(restarted
            .accept_signal(&kind_mismatched, &mut restart_ctx)
            .unwrap_err()
            .contains("outer and inner signal kinds"));
        assert_eq!(restart_ctx.emitted.len(), emitted_before);
    }
}
