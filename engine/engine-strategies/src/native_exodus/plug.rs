//! Engine adapter for the native Exodus event consumer.

use std::collections::BTreeMap;

use engine_types::{
    MarketEvent, OrderUpdate, Strategy, StrategyCheckpoint, StrategyCheckpointIdentity,
    StrategyCtx, StrategyEvent, StrategyId, StrategyImportContext, StrategyImportSource,
    Subscription, SymbolId, TimerId, TranslatedStrategyState, WorkPolicy,
};

use super::plan::{reduce, ReducerInput, ReducerOutput, SleeveState, StrategyConfig};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, checkpoint_payload, emit_effects,
    flatten_execution, owned_order_state, planner_facts, CarryPresettlementFire, Effect,
    FlattenExecutionInput, DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
};
use crate::params::Params;
use crate::position_plan::Skipped;
use crate::BuildError;

pub const NAME: &str = "exodus_native";
const TIMER: TimerId = TimerId(0x4558_4f44);
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

pub struct NativeExodus {
    id: StrategyId,
    pub config: StrategyConfig,
    pub state: SleeveState,
    restored: bool,
    checkpoint_fingerprint: Option<String>,
    blockers: BTreeMap<String, String>,
    last_error: Option<String>,
    flatten_request_id: Option<String>,
}

impl NativeExodus {
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
        Ok(Self {
            id,
            config: config_from_params(params)?,
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
        let output = reduce(input, self.state.clone(), &self.config)?;
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
            self.last_error = Some("Exodus checkpoint identity mismatch".to_owned());
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
                self.last_error = Some(format!("Exodus checkpoint refused: {error}"));
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

    fn pending_events(&self, ctx: &dyn StrategyCtx) -> Result<Vec<CarryPresettlementFire>, String> {
        let source = ctx
            .strategy_id(&self.config.carry_sleeve_name)
            .ok_or_else(|| "configured CARRY source strategy is absent".to_owned())?;
        let mut pending = Vec::new();
        ctx.strategy_events(&mut pending);
        let mut decoded = Vec::new();
        for event in pending {
            if event.destination != self.id {
                continue;
            }
            if event.source != source || event.kind != "carry_presettlement_fire" {
                return Err("Exodus received an event outside its CARRY contract".to_owned());
            }
            let payload: CarryPresettlementFire =
                serde_json::from_slice(&event.payload).map_err(|error| error.to_string())?;
            if payload.event_id != event.event_id {
                return Err("Exodus event envelope and payload ids disagree".to_owned());
            }
            decoded.push(payload);
        }
        decoded.sort_by(|left, right| {
            left.fired_ts_ms
                .cmp(&right.fired_ts_ms)
                .then_with(|| left.event_id.cmp(&right.event_id))
        });
        Ok(decoded)
    }

    fn base_input(
        &self,
        now_ms: i64,
        events: Vec<CarryPresettlementFire>,
        ctx: &dyn StrategyCtx,
    ) -> ReducerInput {
        let (working, opening) = owned_order_state(ctx);
        let mut symbols = attributed_symbols(ctx);
        symbols.extend(self.state.open.keys().cloned());
        symbols.extend(events.iter().map(|event| event.symbol.clone()));
        symbols.extend(working.iter().cloned());
        let account = ctx.account_summary();
        let account_healthy = account.equity_usdt.is_finite()
            && account.equity_usdt > 0.0
            && account.available_margin_usdt.is_finite()
            && account.available_margin_usdt >= 0.0;
        ReducerInput {
            now_ms,
            events,
            facts: planner_facts(ctx, &symbols),
            owned_working_symbols: working,
            owned_opening_order_ids: opening,
            account_healthy,
            checkpoint_fingerprint: self.checkpoint_fingerprint.clone(),
        }
    }

    fn apply(&mut self, output: ReducerOutput, ctx: &mut dyn StrategyCtx) {
        self.state = output.next_state;
        self.checkpoint_fingerprint = Some(self.config.fingerprint());
        self.blockers.clear();
        for (event_id, reason) in output.summary.blocked_events {
            self.blockers.insert(event_id, reason);
        }
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
        let events = match self.pending_events(ctx) {
            Ok(events) => events,
            Err(error) => {
                self.last_error = Some(error);
                return;
            }
        };
        if events.is_empty() && self.state.open.is_empty() && self.checkpoint_fingerprint.is_none()
        {
            return;
        }
        let input = self.base_input(ctx.wall_ms().max(1), events, ctx);
        let config = self.effective_config(ctx);
        match reduce(input, self.state.clone(), &config) {
            Ok(output) => self.apply(output, ctx),
            Err(error) => self.last_error = Some(error.to_owned()),
        }
    }

    fn flatten_now(&mut self, ctx: &mut dyn StrategyCtx) {
        self.ensure_restored(ctx);
        let Some(request_id) = self.flatten_request_id.clone() else {
            return;
        };
        let mut symbols = attributed_symbols(ctx);
        symbols.extend(self.state.open.keys().cloned());
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working);
        let facts = planner_facts(ctx, &symbols);
        let conclusively_flat = attributed_exposure_is_flat(ctx, &symbols);
        if conclusively_flat && opening.values().all(Vec::is_empty) {
            self.state.open.clear();
            self.state.entry_closed_ts_ms_by_symbol.clear();
            self.state.refused_entries.clear();
            self.state.entry_retry_after_ms.clear();
        }
        let (execution, flat) = flatten_execution(
            &self.state,
            FlattenExecutionInput {
                config_fingerprint: self.config.fingerprint(),
                facts: &facts,
                owned_opening_order_ids: &opening,
                now_ms: ctx.wall_ms().max(1),
                request_id: &request_id,
                tag: "exodus_native_flatten",
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
        let next_cover = self
            .state
            .open
            .values()
            .map(|record| {
                record.settlement_ts_ms + self.config.rule.cover_minutes_after_settlement * 60_000
            })
            .filter(|wake| *wake > now_ms)
            .chain(
                self.state
                    .entry_retry_after_ms
                    .values()
                    .copied()
                    .filter(|wake| *wake > now_ms),
            )
            .min();
        if let Some(next) = next_cover {
            let delay_ms = u64::try_from(next - now_ms).unwrap_or(u64::MAX);
            ctx.arm_timer(TIMER, delay_ms.saturating_mul(1_000_000).max(1));
        }
    }
}

impl Strategy for NativeExodus {
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
            return Err("Exodus checkpoint identity mismatch".to_owned());
        }
        let state: SleeveState =
            serde_json::from_slice(&checkpoint.payload).map_err(|error| error.to_string())?;
        state.validate().map_err(str::to_owned)
    }

    fn translate_checkpoint(
        &self,
        context: &StrategyImportContext,
        source_format: &str,
        sources: &[StrategyImportSource],
    ) -> Result<TranslatedStrategyState, String> {
        super::state_import::translate(
            &self.config,
            super::state_import::LegacyImportIdentity {
                venue: &context.venue,
                realm: &context.realm,
                account_user_id: &context.account_user_id,
            },
            source_format,
            sources,
        )
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

    fn on_strategy_event(&mut self, _event: &StrategyEvent, ctx: &mut dyn StrategyCtx) {
        self.replan(ctx);
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
            .is_some_and(|name| self.state.open.contains_key(name))
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
        if !reduce_only && self.state.open.contains_key(&name) {
            self.state.refused_entries.insert(name.clone());
            self.state
                .entry_retry_after_ms
                .insert(name, ctx.wall_ms().max(1).saturating_add(ENTRY_RETRY_MS));
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
    use crate::mock_ctx::Harness;
    use crate::native_exodus::plan::{OpenRecord, RuleConfig};

    #[test]
    fn params_refuse_an_untyped_config() {
        let params: toml::Value = toml::from_str("config_json = '{}'").expect("toml");
        assert!(NativeExodus::from_params(StrategyId(0), &params).is_err());
    }

    #[test]
    fn boot_restores_open_cover_and_arms_its_clock_without_market_news() {
        let now_ms = 1_700_000_000_000;
        let config = StrategyConfig {
            schema_version: 1,
            profile_name: "v1".into(),
            environment: "demo".into(),
            rule_sha256: "1".repeat(64),
            operational_profile_sha256: "2".repeat(64),
            entries_enabled: true,
            carry_sleeve_name: "carry".into(),
            entry_leverage: 5.0,
            rest_entries: false,
            hold_decision_price: false,
            give_up_instead_of_crossing: false,
            rule: RuleConfig {
                config_id: "lane2_exodus_short_v1".into(),
                accepted_source_profile: "carry_hold_v7_live_v1".into(),
                accepted_source_config_id: "lane2_carry_hold_v7".into(),
                cover_minutes_after_settlement: 60,
                entry_valid_minutes_after_settlement: 20,
                stop_loss_fraction: 0.35,
            },
        };
        let mut restored = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        restored.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 10.0,
                settlement_ts_ms: now_ms + 60_000,
                fired_ts_ms: now_ms - 600_000,
                target_qty: Some(1.0),
            },
        );
        let checkpoint = StrategyCheckpoint {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            decision_fingerprint: config.fingerprint(),
            payload: checkpoint_payload(&restored),
        };
        let strategy = NativeExodus {
            id: StrategyId(1),
            config,
            state: SleeveState::default(),
            restored: false,
            checkpoint_fingerprint: None,
            blockers: BTreeMap::new(),
            last_error: None,
            flatten_request_id: None,
        };
        let mut harness = Harness::new(Box::new(strategy));
        harness.ctx.set_wall_ms(now_ms);
        harness.ctx.add_symbol("AUSDT");
        harness.ctx.set_strategy_id("carry", StrategyId(0));
        harness.ctx.set_global_checkpoint(checkpoint);

        harness.boot();

        assert_eq!(harness.ctx.arm_calls.len(), 1);
        assert_eq!(harness.ctx.arm_calls[0].id, TIMER);
        assert!(harness.ctx.arm_calls[0].due_ns > harness.ctx.arm_calls[0].armed_ns);
    }
}
