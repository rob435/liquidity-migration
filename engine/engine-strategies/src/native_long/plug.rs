//! Engine adapter for the native LONG reducer.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    MarketEvent, OrderUpdate, SignalObservation, Strategy, StrategyCheckpoint,
    StrategyCheckpointIdentity, StrategyCtx, StrategyId, Subscription, SymbolId, TimerId,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use serde::Deserialize;

use super::plan::{
    reduce_batch, reduce_batch_with_mode, BatchInput, BatchOutput, DataRejection, DecisionInput,
    FeatureRow, GateSignal, LongSignalBatch, MarketMark, ReplanMode, SleeveState, StrategyConfig,
    GATE_TRIGGER_MAX_AGE_MS,
};
use crate::native_common::sleeve::{SleeveConfig, SleeveCore, SleeveState as SleeveStateContract};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, checkpoint_payload,
    directional_account_is_healthy, emit_effects, flatten_execution, owned_order_state,
    planner_facts, validate_exact_symbol_coverage, validate_signal_identity, Effect,
    FlattenExecutionInput, SignalConfigIdentity, UniverseIdentity,
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
    /// One publication of the LLM entry gate, byte-compatible with the
    /// worker's `llm_gate_candidates` payload.
    LlmGateCandidates {
        decision_ts_ms: i64,
        valid_until_ms: i64,
        btc_rv_30: Option<f64>,
        rows: Vec<GateCandidateRow>,
    },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct GateCandidateRow {
    symbol: String,
    score: f64,
    band: String,
    trigger_ts_ms: i64,
    trigger_price: f64,
    atr_pct: f64,
    sigma_daily_30d: Option<f64>,
    turnover_rank: Option<f64>,
    trigger_window_h: Option<i64>,
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct NativeLong {
    pub core: SleeveCore<StrategyConfig, SleeveState>,
}

impl NativeLong {
    /// Reducer-facing constructor used by contract tests.
    pub fn new(config: StrategyConfig, state: SleeveState) -> Result<Self, &'static str> {
        Ok(Self {
            core: SleeveCore::new(config, state)?,
        })
    }

    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        Ok(Self {
            core: SleeveCore::from_config(id, config_from_params(params)?),
        })
    }

    pub fn reduce(&mut self, input: BatchInput) -> Result<BatchOutput, &'static str> {
        let output = reduce_batch(input, self.core.state.clone(), &self.core.config)?;
        self.core.state = output.next_state.clone();
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        Ok(output)
    }

    fn effective_config(&self, ctx: &dyn StrategyCtx) -> StrategyConfig {
        let mut config = self.core.config.clone();
        config.entries_enabled =
            ctx.entries_enabled(self.core.config.entries_enabled) && Self::account(ctx).0;
        config
    }

    fn account(ctx: &dyn StrategyCtx) -> (bool, f64) {
        let account = ctx.account_summary();
        let healthy = directional_account_is_healthy(account);
        (healthy, if healthy { account.equity_usdt } else { 1.0 })
    }

    fn known_symbols(&self, ctx: &dyn StrategyCtx) -> BTreeSet<String> {
        let mut symbols = attributed_symbols(ctx);
        symbols.extend(self.core.state.symbols.keys().cloned());
        symbols.extend(self.core.state.pending_signals.keys().cloned());
        symbols.extend(self.core.state.exit_pending.iter().cloned());
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
        for (symbol, prior) in &self.core.state.symbols {
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
                    gate: None,
                },
            );
        }
        for (symbol, pending) in &self.core.state.pending_signals {
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
                    feature_row: pending.feature_row.clone(),
                    gate: pending.gate.clone(),
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
    ) -> Result<BatchInput, &'static str> {
        let mut symbols = self.known_symbols(ctx);
        symbols.extend(decisions.iter().map(|row| row.symbol.clone()));
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working.iter().cloned());
        let mut executed_positions = BTreeMap::new();
        for name in &symbols {
            let Some(symbol) = ctx.symbol_id(name) else {
                continue;
            };
            let quantity = ctx
                .my_position_exact(symbol)
                .map_err(|_| "LONG executed quantity is invalid")?;
            if quantity.is_zero() {
                continue;
            }
            let facts = ctx.my_position_facts(symbol);
            let basis = match facts.as_ref().and_then(|facts| facts.allocated.as_ref()) {
                Some(allocated) => allocated.entry_px,
                None => ctx.position(symbol).map(|position| position.entry_px),
            }
            .filter(|price| price.is_finite() && *price > 0.0);
            executed_positions.insert(name.clone(), basis);
        }
        Ok(BatchInput {
            now_ms: ctx.wall_ms().max(1),
            decisions,
            facts: planner_facts(ctx, &symbols),
            executed_positions,
            owned_working_symbols: working,
            owned_opening_order_ids: opening,
            checkpoint_fingerprint: self.core.checkpoint_fingerprint.clone(),
            signal_receipt,
            replace_gate_pending: false,
        })
    }

    fn apply(&mut self, output: BatchOutput, ctx: &mut dyn StrategyCtx) {
        self.core.state = output.next_state;
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        self.core.blockers.clear();
        for skipped in output.execution.skipped {
            let (symbol, reason) = match skipped {
                Skipped::TooSmallToBother { symbol, .. } => (symbol, "inside_resize_band"),
                Skipped::BelowEntryFloor { symbol, .. } => (symbol, "below_entry_floor"),
                Skipped::BelowVenueMinimum { symbol } => (symbol, "below_venue_minimum"),
                Skipped::EntryWindowClosed { symbol } => (symbol, "entry_window_closed"),
                Skipped::NoPrice { symbol } => (symbol, "no_price"),
                Skipped::NoInstrumentRule { symbol } => (symbol, "no_instrument_rule"),
                Skipped::ForeignOwner { symbol } => (symbol, "foreign_strategy_owner"),
            };
            self.core.blockers.insert(symbol, reason.to_owned());
        }
        for symbol in &self.core.state.refused_entries {
            self.core
                .blockers
                .entry(symbol.clone())
                .or_insert_with(|| "entry_refused".to_owned());
        }
        if let Err(error) = emit_effects(
            output.execution.effects,
            self.core.id,
            None,
            self.core.entry_work(),
            ctx,
        ) {
            self.core.last_error = Some(error.to_owned());
        } else {
            self.core.last_error = None;
        }
        self.arm_next(ctx);
    }

    fn consume_only(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        let effects = vec![
            Effect::PersistCheckpoint {
                symbol: String::new(),
                config_fingerprint: self.core.config.fingerprint(),
                payload: checkpoint_payload(&self.core.state),
            },
            Effect::ConsumeSignal {
                source: observation.source.clone(),
                sequence: observation.sequence,
                observation_id: observation.observation_id.clone(),
            },
        ];
        if let Err(error) = emit_effects(effects, self.core.id, None, None, ctx) {
            self.core.last_error = Some(error.to_owned());
        } else {
            self.core.last_error = None;
        }
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        self.arm_next(ctx);
    }

    fn replan_with_mode(&mut self, replan_mode: ReplanMode, ctx: &mut dyn StrategyCtx) {
        self.core.ensure_restored("LONG", ctx);
        if self.core.flatten_request_id.is_some() {
            self.flatten_now(ctx);
            return;
        }
        let now_ms = ctx.wall_ms().max(1);
        let decisions = self.current_decisions(now_ms, ctx);
        if decisions.is_empty() && self.core.checkpoint_fingerprint.is_none() {
            return;
        }
        let input = match self.make_input(decisions, None, ctx) {
            Ok(input) => input,
            Err(error) => {
                self.core.last_error = Some(error.to_owned());
                return;
            }
        };
        let config = self.effective_config(ctx);
        match reduce_batch_with_mode(input, self.core.state.clone(), &config, replan_mode) {
            Ok(output) => self.apply(output, ctx),
            Err(error) => self.core.last_error = Some(error.to_owned()),
        }
    }

    fn replan(&mut self, ctx: &mut dyn StrategyCtx) {
        self.replan_with_mode(ReplanMode::Ordinary, ctx);
    }

    fn defer_opening(&mut self, name: String, reason: &str, ctx: &mut dyn StrategyCtx) {
        let now_ms = ctx.wall_ms().max(1);
        if self.core.state.entry_cycle_started_ms == 0
            || now_ms
                >= self
                    .core
                    .state
                    .entry_cycle_started_ms
                    .saturating_add(super::plan::ENTRY_CYCLE_MS)
        {
            self.core.state.entry_cycle_started_ms = now_ms;
            self.core.state.entry_cycle_selected_symbols.clear();
            self.core.state.refused_entries.clear();
        }
        self.core.blockers.insert(name.clone(), reason.to_owned());
        self.core.state.refused_entries.insert(name);
        let effect = Effect::PersistCheckpoint {
            symbol: String::new(),
            config_fingerprint: self.core.config.fingerprint(),
            payload: checkpoint_payload(&self.core.state),
        };
        if let Err(error) = emit_effects(vec![effect], self.core.id, None, None, ctx) {
            self.core.last_error = Some(error.to_owned());
        }
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        self.arm_next(ctx);
    }

    fn flatten_now(&mut self, ctx: &mut dyn StrategyCtx) {
        self.core.ensure_restored("LONG", ctx);
        let Some(request_id) = self.core.flatten_request_id.clone() else {
            return;
        };
        let mut symbols = self.known_symbols(ctx);
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working);
        let facts = planner_facts(ctx, &symbols);
        let conclusively_flat = attributed_exposure_is_flat(ctx, &symbols);
        self.core.state.pending_signals.clear();
        if conclusively_flat && opening.values().all(Vec::is_empty) {
            self.core.state.symbols.clear();
            self.core.state.exit_pending.clear();
            self.core.state.refused_entries.clear();
        } else {
            self.core.state.exit_pending.extend(facts.held_symbols());
        }
        let (execution, flat) = flatten_execution(
            &self.core.state,
            FlattenExecutionInput {
                config_fingerprint: self.core.config.fingerprint(),
                facts: &facts,
                owned_opening_order_ids: &opening,
                now_ms: ctx.wall_ms().max(1),
                request_id: &request_id,
                tag: "long_native_flatten",
                conclusively_flat,
            },
        );
        if flat {
            self.core.flatten_request_id = None;
        }
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        if let Err(error) = emit_effects(
            execution.effects,
            self.core.id,
            None,
            self.core.entry_work(),
            ctx,
        ) {
            self.core.last_error = Some(error.to_owned());
        }
    }

    fn arm_next(&self, ctx: &mut dyn StrategyCtx) {
        let now_ms = ctx.wall_ms().max(1);
        let mut wakes = Vec::new();
        for prior in self.core.state.symbols.values() {
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
        for pending in self.core.state.pending_signals.values() {
            let clocks = if let Some(gate) = pending.gate.as_ref() {
                [
                    pending.signal_ts_ms + GATE_TRIGGER_MAX_AGE_MS,
                    gate.valid_until_ms,
                    pending.signal_ts_ms + self.core.config.signal_freshness_ms,
                ]
            } else {
                [
                    pending.signal_ts_ms
                        + self.core.config.rule.entry_delay_hours.max(1) * 3_600_000,
                    pending.signal_ts_ms
                        + self.core.config.rule.fc_sniper_deadline_hours * 3_600_000,
                    pending.signal_ts_ms + self.core.config.signal_freshness_ms,
                ]
            };
            for wake in clocks {
                if wake > now_ms {
                    wakes.push(wake);
                }
            }
        }
        if !self.core.state.pending_signals.is_empty()
            && !Self::account(ctx).0
            && ctx.entries_enabled(self.core.config.entries_enabled)
        {
            wakes.push(now_ms.saturating_add(1_000));
        }
        let next_entry_cycle = self
            .core
            .state
            .entry_cycle_started_ms
            .saturating_add(super::plan::ENTRY_CYCLE_MS);
        let unresolved_entry = self
            .core
            .state
            .symbols
            .values()
            .any(|prior| prior.requested && !prior.filled);
        if (unresolved_entry
            || !self.core.state.pending_signals.is_empty()
            || !self.core.state.refused_entries.is_empty())
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
            || observation.destination != self.core.id
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
            &self.core.config.environment,
        )
        .map_err(str::to_owned)?;
        if observation.decision_fingerprint != envelope.config.long_decision_fingerprint {
            return Err("LONG outer and inner decision fingerprints disagree".to_owned());
        }
        let kind = match &envelope.payload {
            SignalPayload::LongFeatureBatch { .. } => "long_feature_batch",
            SignalPayload::LlmGateCandidates { .. } => "llm_gate_candidates",
        };
        if observation.kind != kind {
            return Err("LONG outer and inner signal kinds disagree".to_owned());
        }
        self.core.ensure_restored("LONG", ctx);
        if observation.decision_fingerprint != self.core.config.fingerprint() {
            emit_effects(
                vec![Effect::ConsumeSignal {
                    source: observation.source.clone(),
                    sequence: observation.sequence,
                    observation_id: observation.observation_id.clone(),
                }],
                self.core.id,
                None,
                None,
                ctx,
            )
            .map_err(str::to_owned)?;
            self.core.last_error = None;
            return Ok(());
        }
        let eligible = envelope
            .universe
            .as_ref()
            .expect("validated LONG universe identity")
            .long_symbols
            .clone();
        if envelope.config.long_profile != self.core.config.profile_name
            || envelope.config.long_execution_strategy_id
                != self.core.config.rule.execution_strategy_id
            || envelope.config.long_rule_sha256 != self.core.config.rule_sha256
            || envelope.config.long_feature_contract_sha256
                != self.core.config.feature_contract_sha256
            || envelope.config.long_decision_fingerprint != self.core.config.fingerprint()
        {
            return Err("LONG signal config does not bind this reducer".to_owned());
        }
        let (decision_ts_ms, feature_ts_ms, rows, marks, cold_start_fallback_count, rejections) =
            match envelope.payload {
                SignalPayload::LlmGateCandidates {
                    decision_ts_ms,
                    valid_until_ms,
                    btc_rv_30,
                    rows,
                } => {
                    return self.accept_gate_candidates(
                        observation,
                        decision_ts_ms,
                        valid_until_ms,
                        btc_rv_30,
                        rows,
                        ctx,
                    );
                }
                SignalPayload::LongFeatureBatch {
                    decision_ts_ms,
                    feature_ts_ms,
                    rows,
                    marks,
                    cold_start_fallback_count,
                    rejections,
                } => (
                    decision_ts_ms,
                    feature_ts_ms,
                    rows,
                    marks,
                    cold_start_fallback_count,
                    rejections,
                ),
            };
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
            self.core.config.book_validity_ms,
            self.core.config.engine_entry_cutoff_ms,
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
                gate: None,
            });
        }
        let receipt = Some((
            observation.source.clone(),
            observation.sequence,
            observation.observation_id.clone(),
        ));
        let mut input = self
            .make_input(decisions, receipt, ctx)
            .map_err(str::to_owned)?;
        for (symbol, mark) in mark_by_symbol {
            input.facts.prices.insert(symbol, mark);
        }
        let config = self.effective_config(ctx);
        let output =
            reduce_batch(input, self.core.state.clone(), &config).map_err(str::to_owned)?;
        self.apply(output, ctx);
        if self.core.flatten_request_id.is_some() {
            self.flatten_now(ctx);
        }
        Ok(())
    }
}

impl NativeLong {
    /// The gate's judged events become entry inputs: each row is one
    /// candidate priced at its trigger, entered at market through the native
    /// sizing and exits. The publication replaces every gate candidate still
    /// waiting, so an empty one withdraws them.
    fn accept_gate_candidates(
        &mut self,
        observation: &SignalObservation,
        decision_ts_ms: i64,
        valid_until_ms: i64,
        btc_rv_30: Option<f64>,
        rows: Vec<GateCandidateRow>,
        ctx: &mut dyn StrategyCtx,
    ) -> Result<(), String> {
        if decision_ts_ms <= 0
            || decision_ts_ms > observation.available_wall_ts_ms
            || valid_until_ms <= decision_ts_ms
            || btc_rv_30.is_some_and(|value| !value.is_finite() || value <= 0.0)
        {
            return Err("LLM gate publication timing is invalid".to_owned());
        }
        let received_ts_ms = observation.available_wall_ts_ms;
        if entry_window_is_closed(
            ctx.wall_ms(),
            received_ts_ms,
            self.core.config.book_validity_ms,
            self.core.config.engine_entry_cutoff_ms,
        ) {
            self.consume_only(observation, ctx);
            return Ok(());
        }
        let (_, equity) = Self::account(ctx);
        let mut seen = BTreeSet::new();
        let mut decisions = Vec::with_capacity(rows.len());
        for row in rows {
            if !crate::native_common::valid_symbol(&row.symbol) || !seen.insert(row.symbol.clone())
            {
                return Err("LLM gate candidates repeat or malform a symbol".to_owned());
            }
            if row.trigger_ts_ms <= 0
                || row.trigger_ts_ms > received_ts_ms
                || !row.trigger_price.is_finite()
                || row.trigger_price <= 0.0
            {
                return Err("LLM gate candidate trigger is invalid".to_owned());
            }
            decisions.push(DecisionInput {
                decision_ts_ms: received_ts_ms,
                symbol: row.symbol.clone(),
                signal_ts_ms: row.trigger_ts_ms,
                signal_close: row.trigger_price,
                market_price: Self::mark(ctx, &row.symbol),
                observed_low: None,
                equity_usdt: equity,
                feature_row: None,
                gate: Some(GateSignal {
                    score: row.score,
                    band: row.band,
                    atr_pct: row.atr_pct,
                    sigma_daily_30d: row.sigma_daily_30d.unwrap_or(0.0),
                    turnover_rank: row.turnover_rank,
                    trigger_window_h: row.trigger_window_h,
                    btc_rv_30,
                    valid_until_ms,
                }),
            });
        }
        let receipt = Some((
            observation.source.clone(),
            observation.sequence,
            observation.observation_id.clone(),
        ));
        let mut input = self
            .make_input(decisions, receipt, ctx)
            .map_err(str::to_owned)?;
        input.replace_gate_pending = true;
        let config = self.effective_config(ctx);
        let output =
            reduce_batch(input, self.core.state.clone(), &config).map_err(str::to_owned)?;
        self.apply(output, ctx);
        if self.core.flatten_request_id.is_some() {
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
    fn runtime_state(
        &self,
    ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String> {
        crate::runtime::snapshot(NAME, self, &(self.core.id, &self.core.config)).map(Some)
    }
    fn name(&self) -> &str {
        NAME
    }

    fn retained_signal_subscriptions(&self) -> Option<Vec<Subscription>> {
        if !self.core.restored {
            return None;
        }
        let mut symbols = self
            .core
            .state
            .symbols
            .keys()
            .cloned()
            .collect::<BTreeSet<_>>();
        symbols.extend(self.core.state.pending_signals.keys().cloned());
        symbols.extend(self.core.state.exit_pending.iter().cloned());
        symbols.extend(self.core.state.refused_entries.iter().cloned());
        symbols.extend(self.core.state.entry_cycle_selected_symbols.iter().cloned());
        Some(crate::native_common::retained_market_subscriptions(symbols))
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        Vec::new()
    }

    fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
        self.core.checkpoint_identity()
    }

    fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
        self.core.initial_checkpoint()
    }

    fn validate_checkpoint(&self, checkpoint: &StrategyCheckpoint) -> Result<(), String> {
        self.core.validate_checkpoint("LONG", checkpoint)
    }

    fn requires_signal_feed(&self) -> bool {
        true
    }

    fn configured_entries_enabled(&self) -> bool {
        self.core.config.entries_enabled
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
        self.core.flatten_request_id = Some(request_id.to_owned());
        self.flatten_now(ctx);
    }

    fn requires_signal_readiness(&self) -> bool {
        true
    }

    fn on_signal(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        if let Err(error) = self.accept_signal(observation, ctx) {
            ctx.emit(engine_types::Action::RejectSignalObservation {
                strategy: observation.destination,
                source: observation.source.clone(),
                sequence: observation.sequence,
                observation_id: observation.observation_id.clone(),
                reason: error.clone(),
            });
            self.core.last_error = Some(error);
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
                self.core.state.symbols.contains_key(name)
                    || self.core.state.pending_signals.contains_key(name)
                    || self.core.state.exit_pending.contains(name)
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
        self.core.ensure_restored("LONG", ctx);
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
                        self.core.blockers.insert(name, reason.to_owned());
                    }
                    ctx.arm_timer(TIMER, 1_000_000_000);
                    return;
                }
                if let Some(name) = ctx.symbol_name(facts.symbol).map(str::to_owned) {
                    if self
                        .core
                        .state
                        .symbols
                        .get(&name)
                        .is_some_and(|prior| prior.requested)
                        && !self.core.state.exit_pending.contains(&name)
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
        self.core.ensure_restored("LONG", ctx);
        let Some(name) = ctx.symbol_name(symbol).map(str::to_owned) else {
            return;
        };
        if reduce_only {
            self.core.blockers.insert(name, reason.to_owned());
            ctx.arm_timer(TIMER, 1_000_000_000);
            return;
        }
        self.defer_opening(name, reason, ctx);
    }

    fn entry_blockers(&self) -> Vec<(String, String)> {
        self.core.entry_blockers()
    }

    fn health_error(&self) -> Option<&str> {
        self.core.health_error()
    }
}

impl SleeveConfig for StrategyConfig {
    fn validate(&self) -> Result<(), &'static str> {
        StrategyConfig::validate(self)
    }

    fn fingerprint(&self) -> String {
        StrategyConfig::fingerprint(self)
    }

    fn rest_entries(&self) -> bool {
        self.rest_entries
    }

    fn hold_decision_price(&self) -> bool {
        self.hold_decision_price
    }

    fn give_up_instead_of_crossing(&self) -> bool {
        self.give_up_instead_of_crossing
    }
}

impl SleeveStateContract for SleeveState {
    fn schema_version(&self) -> u16 {
        self.schema_version
    }

    fn set_schema_version(&mut self, version: u16) {
        self.schema_version = version;
    }

    fn validate(&self) -> Result<(), &'static str> {
        SleeveState::validate(self)
    }
}
#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::mock_ctx::{MockCtx, RestingSeed};
    use crate::native_common::DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION;
    use engine_types::{Action, OrderKind, Side};
    use serde_json::json;

    const NOW_MS: i64 = 1_700_000_000_000;

    #[test]
    fn runtime_restore_rejects_semantically_invalid_native_state_and_configuration() {
        let mut plug = NativeLong::new(config(), SleeveState::default()).unwrap();
        plug.core
            .state
            .cooldown_until_ms
            .insert("BTCUSDT".into(), -1);
        let invalid_state = plug.runtime_state().unwrap().unwrap();
        assert!(
            crate::runtime::restore(&invalid_state).is_err(),
            "serialized state with a negative cooldown was restored"
        );
        plug.core.state.cooldown_until_ms.clear();
        plug.core.config.notional_multiplier = -1.0;
        let invalid_config = plug.runtime_state().unwrap().unwrap();
        assert!(
            crate::runtime::restore(&invalid_config).is_err(),
            "self-consistent configuration hash bypassed semantic config validation"
        );
    }

    #[test]
    fn retained_routes_follow_live_retry_state_and_release_history() {
        let mut plug = NativeLong::new(config(), SleeveState::default()).unwrap();
        plug.core.state.exit_pending.insert("EXITUSDT".into());
        plug.core.state.entry_cycle_started_ms = 10;
        plug.core
            .state
            .entry_cycle_selected_symbols
            .insert("RETRYUSDT".into());
        plug.core
            .state
            .cooldown_until_ms
            .insert("HISTORYUSDT".into(), 10);
        plug.core
            .state
            .attempted_signal_ts_ms
            .insert("HISTORYUSDT".into(), 10);
        let routes = plug
            .retained_signal_subscriptions()
            .expect("native consumer declares retained routes");
        assert_eq!(routes.len(), 4);
        assert!(routes.iter().all(|route| route.symbol != "HISTORYUSDT"));
        for symbol in ["EXITUSDT", "RETRYUSDT"] {
            for feed in [engine_types::Feed::Quote, engine_types::Feed::Ticker] {
                assert!(routes.contains(&Subscription {
                    symbol: symbol.into(),
                    feed
                }));
            }
        }
        let runtime = plug.runtime_state().unwrap().unwrap();
        let restored = crate::runtime::restore(&runtime).unwrap();
        assert_eq!(restored.retained_signal_subscriptions(), Some(routes));
        plug.core.state = SleeveState::default();
        assert_eq!(plug.retained_signal_subscriptions(), Some(Vec::new()));
    }

    pub(crate) fn config() -> StrategyConfig {
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

    fn requested_tao() -> SleeveState {
        SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            symbols: BTreeMap::from([(
                "TAOUSDT".into(),
                super::super::plan::PriorState {
                    requested: true,
                    entry_price: 257.57,
                    target_notional_usdt: 3.191 * 257.57,
                    stop_loss_fraction: 0.2151,
                    max_hold_duration_ms: 2 * 86_400_000,
                    entry_valid_until_ms: NOW_MS + 3_600_000,
                    attempted_signal_ts_ms: NOW_MS - 1_000,
                    ..super::super::plan::PriorState::default()
                },
            )]),
            ..SleeveState::default()
        }
    }

    fn pending_tao_context() -> MockCtx {
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(NOW_MS);
        ctx.set_allocated_position("TAOUSDT", 0.0, None, None);
        ctx.set_in_flight("TAOUSDT", 3.191);
        ctx.set_rule(
            "TAOUSDT",
            engine_types::InstrumentRule {
                tick_size: 0.01,
                qty_step: 0.001,
                min_qty: 0.001,
                min_notional: 5.0,
            },
        );
        ctx.resting.push(RestingSeed {
            client_order_id: "pending-tao".into(),
            symbol: ctx.id_of("TAOUSDT"),
            side: Side::Buy,
            kind: OrderKind::Market,
            qty: 3.191,
            filled_qty: 0.0,
            reduce_only: false,
            acked: true,
        });
        ctx
    }

    #[test]
    fn an_acknowledged_opening_reservation_does_not_become_a_long_fill() {
        let mut strategy = NativeLong::new(config(), requested_tao()).unwrap();
        let mut ctx = pending_tao_context();
        strategy.on_order(
            &OrderUpdate::Ack(engine_types::OrderAck {
                client_order_id: "pending-tao".into(),
                venue_order_id: "venue-tao".into(),
                sent_ns: 1,
                ack_ns: 2,
            }),
            &mut ctx,
        );
        assert!(
            strategy.core.state.validate().is_ok(),
            "{:?}",
            strategy.core.state
        );
        assert!(
            !strategy.core.state.symbols["TAOUSDT"].filled,
            "a reservation with zero executed inventory is not a fill"
        );
        assert_eq!(strategy.core.state.symbols["TAOUSDT"].entry_ts_ms, 0);
        assert!(
            !ctx.emitted.iter().any(|a| matches!(a, Action::Place(_))),
            "the original opening remains reserved"
        );
        let mut restarted = NativeLong::new(config(), strategy.core.state.clone()).unwrap();
        restarted.core.checkpoint_fingerprint = Some(config().fingerprint());
        restarted.on_boot(&mut ctx);
        assert!(restarted.core.state.validate().is_ok());
        assert!(!restarted.core.state.symbols["TAOUSDT"].filled);

        ctx.set_wall_ms(NOW_MS + 2_000);
        ctx.set_allocated_position("TAOUSDT", 1.0, Some(257.57), Some(202.13));
        ctx.set_in_flight("TAOUSDT", 2.191);
        ctx.resting[0].filled_qty = 1.0;
        restarted.replan(&mut ctx);
        let prior = &restarted.core.state.symbols["TAOUSDT"];
        assert!(prior.filled);
        assert_eq!(prior.entry_ts_ms, NOW_MS + 2_000);
        assert_eq!(prior.entry_price, 257.57);
        assert!(restarted.core.state.validate().is_ok());
    }

    #[test]
    fn a_pending_full_reduction_keeps_longs_executed_position_and_basis() {
        let mut state = requested_tao();
        let prior = state.symbols.get_mut("TAOUSDT").unwrap();
        prior.filled = true;
        prior.entry_ts_ms = NOW_MS - 5_000;
        prior.max_hold_deadline_ts_ms = NOW_MS + 86_400_000;
        let mut strategy = NativeLong::new(config(), state).unwrap();
        let mut ctx = pending_tao_context();
        ctx.set_allocated_position("TAOUSDT", 3.191, Some(258.0), Some(202.13));
        ctx.set_in_flight("TAOUSDT", -3.191);
        ctx.resting[0].side = Side::Sell;
        ctx.resting[0].reduce_only = true;
        strategy.replan(&mut ctx);
        assert_eq!(
            strategy.core.state.symbols["TAOUSDT"].entry_price, 258.0,
            "a reserved full reduction must not hide actual inventory updates"
        );
        assert_eq!(
            strategy.core.state.symbols["TAOUSDT"].entry_ts_ms,
            NOW_MS - 5_000
        );
        assert!(strategy.core.state.validate().is_ok());
        assert!(!ctx.emitted.iter().any(|a| matches!(a, Action::Place(_))));
    }

    #[test]
    fn an_unknown_sleeve_basis_does_not_borrow_the_venue_basis() {
        let mut strategy = NativeLong::new(config(), requested_tao()).unwrap();
        let mut ctx = pending_tao_context();
        ctx.set_position("TAOUSDT", Side::Buy, 10.0, 300.0);
        ctx.set_allocated_position("TAOUSDT", 1.0, None, Some(202.13));
        ctx.set_in_flight("TAOUSDT", 2.191);
        let input = strategy.make_input(Vec::new(), None, &ctx).unwrap();
        assert_eq!(input.executed_positions["TAOUSDT"], None);
        strategy.replan(&mut ctx);
        assert!(!strategy.core.state.symbols["TAOUSDT"].filled);
        assert_eq!(strategy.core.state.symbols["TAOUSDT"].entry_ts_ms, 0);
        assert!(strategy.core.state.validate().is_ok());
        assert!(!ctx.emitted.iter().any(|a| matches!(a, Action::Place(_))));
    }

    #[test]
    fn a_completed_long_reduction_retires_inventory_after_its_order_is_gone() {
        let mut state = requested_tao();
        let prior = state.symbols.get_mut("TAOUSDT").unwrap();
        prior.filled = true;
        prior.entry_ts_ms = NOW_MS - 5_000;
        prior.max_hold_deadline_ts_ms = NOW_MS + 86_400_000;
        state.exit_pending.insert("TAOUSDT".into());
        let mut strategy = NativeLong::new(config(), state).unwrap();
        let mut ctx = pending_tao_context();
        ctx.set_in_flight("TAOUSDT", 0.0);
        ctx.resting[0].side = Side::Sell;
        ctx.resting[0].reduce_only = true;
        strategy.replan(&mut ctx);
        assert!(strategy.core.state.symbols.contains_key("TAOUSDT"));
        assert!(strategy.core.state.exit_pending.contains("TAOUSDT"));
        ctx.resting.clear();
        strategy.replan(&mut ctx);
        assert!(!strategy.core.state.symbols.contains_key("TAOUSDT"));
        assert!(!strategy.core.state.exit_pending.contains("TAOUSDT"));
        assert!(strategy.core.state.cooldown_until_ms["TAOUSDT"] > NOW_MS);
        assert!(!ctx.emitted.iter().any(|a| matches!(a, Action::Place(_))));
        assert!(strategy.core.state.validate().is_ok());
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
    fn malformed_input_emits_an_explicit_terminal_rejection() {
        let config = config();
        let mut strategy = NativeLong::from_params(StrategyId(7), &params(&config)).unwrap();
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(NOW_MS);
        let mut row = observation(&config, config.fingerprint(), 37);
        row.destination = StrategyId(7);
        row.payload = b"invalid JSON".to_vec();
        strategy.on_signal(&row, &mut ctx);
        assert_eq!(
            strategy.core.last_error.as_deref(),
            Some("expected value at line 1 column 1")
        );
        assert!(!ctx
            .emitted
            .iter()
            .any(|action| matches!(action, Action::ConsumeSignalObservation { .. })));
        assert_eq!(
            ctx.emitted,
            vec![Action::RejectSignalObservation {
                strategy: StrategyId(7),
                source: "signal.generation.long".into(),
                sequence: 37,
                observation_id: "long-37".into(),
                reason: "expected value at line 1 column 1".into(),
            }],
            "malformed input must emit exactly one terminal rejection with its original identity"
        );
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
