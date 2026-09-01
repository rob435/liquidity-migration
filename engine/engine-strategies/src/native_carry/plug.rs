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
    reduce_lifecycle, reduce_lifecycle_with_mode, reduce_scorer_catchup, reduce_signal,
    CarrySignalBatch, DataRejection, MarketMark, PresettlementObservation, ReducerInput,
    ReducerOutput, ReplanMode, ScorerCatchupInput, ScorerCatchupOutput, SettledFundingObservation,
    SleeveState, StrategyConfig,
};
use super::scorer::{CarryDecision, CarryFeatureRow, DAY_MS};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, checkpoint_payload,
    directional_account_is_healthy, emit_effects, flatten_execution, owned_order_state,
    planner_facts, validate_exact_symbol_coverage, validate_signal_identity, Effect,
    FlattenExecutionInput, SignalConfigIdentity, TickerObservation, UniverseIdentity,
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
    CarryScorerCatchup {
        decision_ts_ms: i64,
        rows: Vec<CarryFeatureRow>,
        rejections: Vec<DataRejection>,
    },
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
        expires_at_ms: i64,
        tickers: Vec<TickerObservation>,
        marks: Vec<MarketMark>,
        presettlement: Vec<PresettlementObservation>,
    },
    FundingUpdate {
        decision_ts_ms: i64,
        settled_funding: Vec<SettledFundingObservation>,
    },
    UniverseChanged,
    Readiness {
        readiness: Readiness,
    },
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
        let healthy = directional_account_is_healthy(account);
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

    fn apply_scorer_catchup(&mut self, output: ScorerCatchupOutput, ctx: &mut dyn StrategyCtx) {
        self.state = output.next_state;
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        if let Err(error) = emit_effects(output.execution.effects, self.id, None, None, ctx) {
            self.last_error = Some(error.to_owned());
        } else {
            self.last_error = None;
        }
    }

    fn replan_with_mode(&mut self, replan_mode: ReplanMode, ctx: &mut dyn StrategyCtx) {
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
        match reduce_lifecycle_with_mode(input, self.state.clone(), &config, replan_mode) {
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
            self.state.entry_retry_after_ms.clear();
        }
        self.blockers.insert(name.clone(), reason.to_owned());
        self.state.entry_retry_after_ms.remove(&name);
        self.state.refused_entries.insert(name);
        self.replan(ctx);
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
        let decision_available = decision.decision_ts_ms <= now_ms;
        let account_retry = (decision_available
            && !self.account(ctx).0
            && ctx.entries_enabled(self.config.entries_enabled)
            && self.has_deferred_growth(ctx)
            && cutoff > now_ms)
            .then(|| now_ms.saturating_add(ENTRY_RETRY_MS));
        let next_entry_cycle = self
            .state
            .entry_cycle_started_ms
            .saturating_add(super::plan::ENTRY_CYCLE_MS);
        let entry_cycle = (decision_available
            && (!self.state.entry_cycle_selected_symbols.is_empty()
                || !self.state.refused_entries.is_empty())
            && next_entry_cycle > now_ms
            && cutoff > now_ms)
            .then_some(next_entry_cycle);
        let next = self
            .state
            .current_decision
            .as_ref()
            .and_then(|decision| {
                decision
                    .weights
                    .keys()
                    .any(|symbol| !self.state.desired_targets.contains_key(symbol))
                    .then_some(next_entry_cycle)
            })
            .filter(|wake| decision_available && *wake > now_ms && cutoff > now_ms)
            .into_iter()
            .chain((!decision_available).then_some(decision.decision_ts_ms))
            .chain((cutoff > now_ms).then_some(cutoff))
            .chain(account_retry)
            .chain(entry_cycle)
            .min();
        if let Some(next) = next {
            let delay_ms = u64::try_from(next - now_ms).unwrap_or(u64::MAX);
            ctx.arm_timer(TIMER, delay_ms.saturating_mul(1_000_000).max(1));
        }
    }

    fn has_deferred_growth(&self, ctx: &dyn StrategyCtx) -> bool {
        let Some(decision) = &self.state.current_decision else {
            return false;
        };
        if decision
            .weights
            .keys()
            .any(|symbol| !self.state.desired_targets.contains_key(symbol))
            || !self.state.refused_entries.is_empty()
        {
            return true;
        }
        let symbols = self.known_symbols(Vec::new(), ctx);
        let facts = planner_facts(ctx, &symbols);
        let (working, _) = owned_order_state(ctx);
        self.state.desired_targets.iter().any(|(symbol, target)| {
            if target.notional_usdt <= 0.0 || working.contains(symbol) {
                return false;
            }
            let Some(held) = facts.held.get(symbol) else {
                return true;
            };
            let held_notional = held.notional();
            if held_notional <= 0.0 {
                return false;
            }
            let growth = target.notional_usdt - held_notional;
            growth > self.config.execution.resize_floor_usdt
                && growth > held_notional * self.config.execution.resize_floor_fraction
        })
    }

    fn validate_observation(
        &self,
        observation: &SignalObservation,
        ctx: &dyn StrategyCtx,
        envelope: &SignalEnvelope,
    ) -> Result<(), String> {
        if observation.schema_version != SIGNAL_OBSERVATION_SCHEMA_VERSION
            || observation.destination != self.id
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
        if observation.decision_fingerprint != envelope.config.carry_decision_fingerprint {
            return Err("CARRY outer and inner decision fingerprints disagree".to_owned());
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
        let envelope: SignalEnvelope =
            serde_json::from_slice(&observation.payload).map_err(|error| error.to_string())?;
        self.validate_observation(observation, ctx, &envelope)?;
        let kind = match &envelope.payload {
            SignalPayload::CarryScorerCatchup { .. } => "carry_scorer_catchup",
            SignalPayload::CarryFeatureBatch { .. } => "carry_feature_batch",
            SignalPayload::MarketSnapshot { .. } => "market_snapshot",
            SignalPayload::FundingUpdate { .. } => "funding_update",
            SignalPayload::UniverseChanged => "universe_changed",
            SignalPayload::Readiness { .. } => "readiness",
        };
        if observation.kind != kind {
            return Err("CARRY outer and inner signal kinds disagree".to_owned());
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
        if envelope.config.carry_config_id != self.config.rule.config_id
            || envelope.config.carry_rule_sha256 != self.config.rule_sha256
            || envelope.config.carry_feature_contract_sha256 != self.config.feature_contract_sha256
        {
            return Err("CARRY signal config does not bind this reducer".to_owned());
        }
        let eligible = envelope
            .universe
            .as_ref()
            .expect("validated CARRY universe identity")
            .carry_symbols
            .clone();
        let receipt = (
            observation.source.clone(),
            observation.sequence,
            observation.observation_id.clone(),
        );
        let effective_config = self.effective_config(ctx);
        match envelope.payload {
            SignalPayload::CarryScorerCatchup {
                decision_ts_ms,
                rows,
                rejections,
            } => {
                if observation.observed_wall_ts_ms < decision_ts_ms {
                    return Err("CARRY scorer catch-up predates its UTC generation".to_owned());
                }
                validate_feature_coverage(
                    decision_ts_ms,
                    observation.observed_wall_ts_ms,
                    &eligible,
                    &rows,
                    &[],
                    &[],
                    &[],
                    &[],
                    &rejections,
                )?;
                let output = reduce_scorer_catchup(
                    ScorerCatchupInput {
                        decision_ts_ms,
                        rows,
                        signal_receipt: receipt,
                    },
                    self.state.clone(),
                    &effective_config,
                )
                .map_err(str::to_owned)?;
                self.apply_scorer_catchup(output, ctx);
                return Ok(());
            }
            SignalPayload::CarryFeatureBatch {
                decision_ts_ms,
                rows,
                upcoming_rows,
                settled_funding,
                presettlement,
                marks,
                rejections,
            } => {
                if decision_ts_ms > observation.observed_wall_ts_ms
                    || decision_ts_ms > ctx.wall_ms()
                {
                    return Err("CARRY feature batch predates its UTC generation".to_owned());
                }
                validate_feature_coverage(
                    decision_ts_ms,
                    observation.observed_wall_ts_ms,
                    &eligible,
                    &rows,
                    &upcoming_rows,
                    &marks,
                    &settled_funding,
                    &presettlement,
                    &rejections,
                )?;
                for mark in &marks {
                    validate_mark(mark, observation.observed_wall_ts_ms)?;
                }
                if signal_is_stale(
                    ctx.wall_ms(),
                    decision_ts_ms,
                    self.config.execution.book_validity_ms,
                ) {
                    let output = reduce_scorer_catchup(
                        ScorerCatchupInput {
                            decision_ts_ms,
                            rows,
                            signal_receipt: receipt,
                        },
                        self.state.clone(),
                        &effective_config,
                    )
                    .map_err(str::to_owned)?;
                    self.apply_scorer_catchup(output, ctx);
                    return Ok(());
                }
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
                input.signal_receipt = Some(receipt);
                for mark in &marks {
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
                expires_at_ms,
                tickers,
                marks,
                presettlement,
            } => {
                if expires_at_ms < observation.available_wall_ts_ms {
                    return Err("CARRY market snapshot expiry predates availability".to_owned());
                }
                if ctx.wall_ms() > expires_at_ms {
                    self.consume_only(observation, ctx);
                    return Ok(());
                }
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
                input.signal_receipt = Some(receipt);
                for mark in &marks {
                    validate_mark(mark, observation.observed_wall_ts_ms)?;
                    input.facts.prices.insert(mark.symbol.clone(), mark.mark_px);
                }
                let output = reduce_lifecycle(input, self.state.clone(), &effective_config)
                    .map_err(str::to_owned)?;
                self.apply(output, ctx);
            }
            SignalPayload::FundingUpdate {
                decision_ts_ms,
                settled_funding,
            } => {
                if decision_ts_ms <= 0 || decision_ts_ms > observation.observed_wall_ts_ms {
                    return Err("CARRY funding update decision clock is invalid".to_owned());
                }
                let Some(decision) = self.state.current_decision.clone() else {
                    self.consume_only(observation, ctx);
                    return Ok(());
                };
                if decision.decision_ts_ms != decision_ts_ms
                    || signal_is_stale(
                        ctx.wall_ms(),
                        decision_ts_ms,
                        self.config.execution.book_validity_ms,
                    )
                {
                    self.consume_only(observation, ctx);
                    return Ok(());
                }
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
                input.signal_receipt = Some(receipt);
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

fn signal_is_stale(now_ms: i64, decision_ts_ms: i64, validity_ms: i64) -> bool {
    now_ms >= decision_ts_ms.saturating_add(validity_ms)
}

#[allow(clippy::too_many_arguments)]
fn validate_feature_coverage(
    decision_ts_ms: i64,
    observed_ts_ms: i64,
    eligible: &[String],
    rows: &[CarryFeatureRow],
    upcoming_rows: &[CarryFeatureRow],
    marks: &[MarketMark],
    settled_funding: &[SettledFundingObservation],
    presettlement: &[PresettlementObservation],
    rejections: &[DataRejection],
) -> Result<(), String> {
    let eligible_set = eligible.iter().collect::<BTreeSet<_>>();
    let escaped = rows
        .iter()
        .chain(upcoming_rows)
        .map(|row| &row.symbol)
        .chain(marks.iter().map(|row| &row.symbol))
        .chain(settled_funding.iter().map(|row| &row.symbol))
        .chain(presettlement.iter().map(|row| &row.symbol))
        .any(|symbol| !eligible_set.contains(symbol));
    if escaped {
        return Err("CARRY feature batch escapes declared eligible population".to_owned());
    }
    if rows.iter().any(|row| row.bar_ts_ms > decision_ts_ms) {
        return Err("CARRY current feature history contains a future generation".to_owned());
    }
    let accepted = rows
        .iter()
        .filter(|row| row.bar_ts_ms == decision_ts_ms)
        .map(|row| row.symbol.clone())
        .collect::<Vec<_>>();
    let rejected = rejections
        .iter()
        .map(|row| row.symbol.clone())
        .collect::<Vec<_>>();
    validate_exact_symbol_coverage(eligible, &accepted, &rejected)
        .map_err(|error| format!("CARRY feature coverage is invalid: {error}"))?;
    if rejections.iter().any(|row| {
        row.reason.trim().is_empty()
            || row
                .first_missing_ts_ms
                .is_some_and(|timestamp| timestamp <= 0 || timestamp > observed_ts_ms)
    }) {
        return Err("CARRY data rejection is invalid".to_owned());
    }
    if !upcoming_rows.is_empty() {
        let upcoming_ts_ms = decision_ts_ms.saturating_add(DAY_MS);
        if upcoming_rows
            .iter()
            .any(|row| row.bar_ts_ms != upcoming_ts_ms)
        {
            return Err("CARRY upcoming feature generation is invalid".to_owned());
        }
        let upcoming = upcoming_rows
            .iter()
            .map(|row| row.symbol.clone())
            .collect::<Vec<_>>();
        validate_exact_symbol_coverage(eligible, &upcoming, &[])
            .map_err(|error| format!("CARRY upcoming feature coverage is invalid: {error}"))?;
    }
    Ok(())
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
            || [
                row.mark_observed_ts_ms,
                row.funding_observed_ts_ms,
                row.schedule_observed_ts_ms,
            ]
            .into_iter()
            .flatten()
            .any(|clock| clock <= 0 || clock > row.observed_ts_ms)
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
                    if self.state.desired_targets.contains_key(&name) {
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
            "profile_name": "carry_hold_v7_live_v1",
            "environment": "demo",
            "rule_sha256": "1".repeat(64),
            "feature_contract_sha256": "2".repeat(64),
            "operational_profile_sha256": "3".repeat(64),
            "entries_enabled": true,
            "exodus_sleeve_name": "exodus",
            "rule": {
                "config_id": "lane2_carry_hold_v7",
                "universe_top_n": 100,
                "enter_bp": 10.0,
                "exit_bp": 3.0,
                "per_name_cap": 0.1,
                "gross_cap": 1.0,
                "depth_ref_bp_per_day": 120.0,
                "depth_floor": 0.25,
                "depth_exponent": 1.5,
                "toxic_band_ret3d_lo": -0.3,
                "toxic_band_ret3d_hi": 0.0,
                "min_vol30_daily": 0.05,
                "trail_recovery_exit_bp_2d": 30.0,
                "persistence_cut": 0.1,
                "persistence_lo": 0.0,
                "flow_cut": 0.4,
                "flow_lo": 0.5,
                "whale_cut": -0.26,
                "whale_lo": 0.5
            },
            "exit_bp": 3.0,
            "early_exit_enabled": true,
            "presettlement_exit_enabled": true,
            "notional_multiplier": 1.0,
            "entry_leverage": 2.0,
            "stop_loss_fraction": 0.35,
            "max_new_entries_per_cycle": 2,
            "capital_reference_usdt": 0.0,
            "rest_entries": true,
            "hold_decision_price": false,
            "give_up_instead_of_crossing": false,
            "execution": {
                "entry_floor_usdt": 6.0,
                "resize_floor_usdt": 1.0,
                "resize_floor_fraction": 0.05,
                "engine_entry_cutoff_ms": 900_000,
                "signal_validity_ms": 21_600_000,
                "book_validity_ms": 108_000_000,
                "presettlement_window_ms": 900_000
            }
        }))
        .expect("CARRY config")
    }

    fn identity(config: &StrategyConfig, fingerprint: String) -> SignalConfigIdentity {
        SignalConfigIdentity {
            schema_version: 1,
            signal_config_id: "signal-test".into(),
            long_profile: "long-test".into(),
            long_execution_strategy_id: "long-test".into(),
            long_rule_sha256: "4".repeat(64),
            long_feature_contract_sha256: "5".repeat(64),
            signal_config_sha256: "6".repeat(64),
            carry_config_id: config.rule.config_id.clone(),
            carry_rule_sha256: config.rule_sha256.clone(),
            carry_feature_contract_sha256: config.feature_contract_sha256.clone(),
            operational_profile_sha256: config.operational_profile_sha256.clone(),
            engine_config_sha256: "7".repeat(64),
            long_decision_fingerprint: "8".repeat(64),
            carry_decision_fingerprint: fingerprint,
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
                "kind": "readiness",
                "readiness": {
                    "long_ready": true,
                    "carry_ready": true,
                    "universe_ready": true,
                    "reason": "ready_no_new_decision",
                    "long_feature_ts_ms": null,
                    "carry_feature_ts_ms": null,
                    "rejected_symbols": []
                }
            }
        });
        SignalObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: fingerprint,
            destination: StrategyId(0),
            source: "signal.generation.carry".into(),
            sequence,
            observation_id: format!("carry-{sequence}"),
            kind: "readiness".into(),
            observed_wall_ts_ms: NOW_MS,
            available_wall_ts_ms: NOW_MS,
            subscriptions: Vec::new(),
            payload: serde_json::to_vec(&payload).expect("CARRY payload"),
            content_sha256: "b".repeat(64),
        }
    }

    fn params(config: &StrategyConfig) -> toml::Value {
        toml::Value::Table(
            [(
                "config_json".into(),
                toml::Value::String(serde_json::to_string(config).expect("CARRY config JSON")),
            )]
            .into_iter()
            .collect(),
        )
    }

    fn row(symbol: &str, bar_ts_ms: i64) -> CarryFeatureRow {
        CarryFeatureRow {
            symbol: symbol.to_owned(),
            bar_ts_ms,
            ..CarryFeatureRow::default()
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
    fn worker_payload_rejects_an_unknown_field() {
        let raw = br#"{"schema_version":1,"config":{},"universe":null,"payload":{"kind":"universe_changed","extra":1}}"#;
        assert!(serde_json::from_slice::<SignalEnvelope>(raw).is_err());
    }

    #[test]
    fn worker_ticker_accepts_per_field_clocks() {
        let raw = br#"{"symbol":"BTCUSDT","observed_ts_ms":3000,"available_at_ms":3001,"mark_observed_ts_ms":2900,"funding_observed_ts_ms":2800,"schedule_observed_ts_ms":2700,"last_price":1.0,"mark_price":1.0,"index_price":1.0,"bid1_price":1.0,"ask1_price":1.0,"bid1_size":1.0,"ask1_size":1.0,"open_interest":1.0,"open_interest_value":1.0,"turnover_24h":1.0,"volume_24h":1.0,"funding_rate":0.0,"next_funding_time_ms":6000}"#;
        let ticker: TickerObservation = serde_json::from_slice(raw).expect("worker ticker");
        validate_tickers(&[ticker], 3_000).expect("per-field clocks");
    }

    #[test]
    fn market_snapshot_requires_the_worker_expiry_contract() {
        let valid = br#"{"kind":"market_snapshot","expires_at_ms":4000,"tickers":[],"marks":[],"presettlement":[]}"#;
        let payload: SignalPayload = serde_json::from_slice(valid).expect("snapshot expiry");
        assert!(matches!(
            payload,
            SignalPayload::MarketSnapshot {
                expires_at_ms: 4_000,
                ..
            }
        ));

        let missing = br#"{"kind":"market_snapshot","tickers":[],"marks":[],"presettlement":[]}"#;
        assert!(serde_json::from_slice::<SignalPayload>(missing).is_err());
    }

    #[test]
    fn funding_update_requires_the_bound_carry_generation() {
        let valid = br#"{"kind":"funding_update","decision_ts_ms":4000,"settled_funding":[]}"#;
        let payload: SignalPayload = serde_json::from_slice(valid).expect("funding generation");
        assert!(matches!(
            payload,
            SignalPayload::FundingUpdate {
                decision_ts_ms: 4_000,
                ..
            }
        ));

        let missing = br#"{"kind":"funding_update","settled_funding":[]}"#;
        assert!(serde_json::from_slice::<SignalPayload>(missing).is_err());
    }

    #[test]
    fn downstream_outage_reduces_expired_carry_batches_as_scorer_only() {
        assert!(!signal_is_stale(108_999, 1_000, 108_000));
        assert!(signal_is_stale(109_000, 1_000, 108_000));
        assert!(signal_is_stale(i64::MAX, i64::MAX - 10, 100));
    }

    #[test]
    fn delivery_requires_exact_current_and_upcoming_carry_populations() {
        let decision_ts_ms = 100 * DAY_MS;
        let eligible = vec!["AUSDT".to_owned(), "BUSDT".to_owned()];
        let validate = |rows: &[CarryFeatureRow],
                        upcoming_rows: &[CarryFeatureRow],
                        rejections: &[DataRejection]| {
            validate_feature_coverage(
                decision_ts_ms,
                decision_ts_ms + DAY_MS,
                &eligible,
                rows,
                upcoming_rows,
                &[],
                &[],
                &[],
                rejections,
            )
        };

        assert!(validate(&[row("AUSDT", decision_ts_ms)], &[], &[rejection("BUSDT")]).is_ok());
        assert!(validate(&[row("AUSDT", decision_ts_ms)], &[], &[]).is_err());
        assert!(validate(&[row("AUSDT", decision_ts_ms)], &[], &[rejection("AUSDT")]).is_err());
        assert!(validate(
            &[
                row("AUSDT", decision_ts_ms),
                row("BUSDT", decision_ts_ms),
                row("CUSDT", decision_ts_ms - DAY_MS),
            ],
            &[],
            &[]
        )
        .is_err());
        assert!(validate(
            &[row("AUSDT", decision_ts_ms), row("BUSDT", decision_ts_ms)],
            &[row("AUSDT", decision_ts_ms + DAY_MS)],
            &[]
        )
        .is_err());
    }

    #[test]
    fn full_feature_batch_cannot_arrive_before_its_generation() {
        let config = config();
        let fingerprint = config.fingerprint();
        let envelope = json!({
            "schema_version": 1,
            "config": identity(&config, fingerprint.clone()),
            "universe": universe(&config.environment),
            "payload": {
                "kind": "carry_feature_batch",
                "decision_ts_ms": NOW_MS + 1,
                "rows": [],
                "upcoming_rows": [],
                "settled_funding": [],
                "presettlement": [],
                "marks": [],
                "rejections": []
            }
        });
        let mut observation = observation(&config, fingerprint, 1);
        observation.kind = "carry_feature_batch".into();
        observation.payload = serde_json::to_vec(&envelope).expect("future payload");
        let mut strategy = NativeCarry::new(config, SleeveState::default()).expect("strategy");
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(NOW_MS);

        let error = strategy
            .accept_signal(&observation, &mut ctx)
            .expect_err("future generation must fail closed");
        assert!(error.contains("predates its UTC generation"));
        assert!(ctx.emitted.is_empty());
        assert_eq!(
            strategy.state,
            SleeveState {
                schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
                ..SleeveState::default()
            }
        );
    }

    #[test]
    fn backward_wall_clock_arms_the_durable_decision_time() {
        let decision_ts_ms = NOW_MS + 5_000;
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms,
            weights: BTreeMap::from([("AUSDT".into(), 0.1)]),
            universe_size: 100,
            replay_days: 45,
            gross: 0.1,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(decision_ts_ms, 1_000.0)]),
            last_publication_decision_ts_ms: decision_ts_ms,
            current_decision: Some(decision),
            ..SleeveState::default()
        };
        let strategy = NativeCarry::new(config(), state).expect("strategy");
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
    fn unobserved_account_retries_only_when_growth_is_deferred() {
        let config = config();
        let decision = CarryDecision {
            schema_version: 1,
            decision_ts_ms: NOW_MS,
            weights: BTreeMap::from([("AUSDT".into(), 0.1)]),
            universe_size: 100,
            replay_days: 45,
            gross: 0.1,
        };
        let state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            sizing_anchors: BTreeMap::from([(NOW_MS, 1_000.0)]),
            desired_targets: BTreeMap::from([(
                "AUSDT".into(),
                super::super::plan::StoredTarget {
                    notional_usdt: 100.0,
                    stop_loss_fraction: config.stop_loss_fraction,
                    leverage: config.entry_leverage,
                    entry_valid_until_ms: NOW_MS + config.execution.signal_validity_ms,
                },
            )]),
            last_publication_decision_ts_ms: NOW_MS,
            current_decision: Some(decision),
            entry_cycle_started_ms: NOW_MS,
            ..SleeveState::default()
        };
        let run = |qty: Option<f64>| {
            let strategy = NativeCarry::new(config.clone(), state.clone()).expect("strategy");
            let mut ctx = MockCtx::new();
            ctx.set_wall_ms(NOW_MS);
            ctx.set_now(0);
            ctx.add_symbol("AUSDT");
            if let Some(qty) = qty {
                ctx.set_position("AUSDT", engine_types::Side::Buy, qty, 100.0);
            }
            strategy.arm_next(&mut ctx);
            ctx.arm_calls[0].due_ns - ctx.arm_calls[0].armed_ns
        };

        assert!(run(Some(1.0)) > u64::try_from(ENTRY_RETRY_MS).unwrap() * 1_000_000);
        assert_eq!(
            run(Some(0.5)),
            u64::try_from(ENTRY_RETRY_MS).unwrap() * 1_000_000
        );
        assert_eq!(
            run(None),
            u64::try_from(ENTRY_RETRY_MS).unwrap() * 1_000_000
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
            let mut strategy =
                NativeCarry::new(config(), SleeveState::default()).expect("strategy");
            let mut ctx = MockCtx::new();
            ctx.set_wall_ms(NOW_MS);
            ctx.set_position("AUSDT", Side::Buy, 1.0, 100.0);
            let symbol = ctx.id_of("AUSDT");

            strategy.on_flatten_directional("flatten-carry", &mut ctx);
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
    fn old_contract_is_consumed_once_before_current_contract_and_restart() {
        let config = config();
        let current_fingerprint = config.fingerprint();
        let old_fingerprint = "c".repeat(64);
        let mut strategy = NativeCarry::new(config.clone(), SleeveState::default()).unwrap();
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

        let mut restarted = NativeCarry::from_params(StrategyId(0), &params(&config)).unwrap();
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
    }
}
