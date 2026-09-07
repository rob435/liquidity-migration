//! Engine adapter for the native Exodus event consumer.

use engine_types::{
    MarketEvent, OrderUpdate, Strategy, StrategyCheckpoint, StrategyCheckpointIdentity,
    StrategyCtx, StrategyEvent, StrategyId, StrategyImportContext, StrategyImportSource,
    Subscription, SymbolId, TimerId, TranslatedStrategyState,
};

use super::plan::{reduce, ReducerInput, ReducerOutput, SleeveState, StrategyConfig};
use crate::native_common::sleeve::{SleeveConfig, SleeveCore, SleeveState as SleeveStateContract};
use crate::native_common::{
    attributed_exposure_is_flat, attributed_symbols, directional_account_is_healthy, emit_effects,
    flatten_execution, owned_order_state, planner_facts, CarryPresettlementFire,
    FlattenExecutionInput,
};
use crate::params::Params;
use crate::position_plan::Skipped;
use crate::BuildError;

pub const NAME: &str = "exodus_native";
const TIMER: TimerId = TimerId(0x4558_4f44);
const FAST_RETRY_MS: i64 = 1_000;
const TERMINAL_ENTRY_RETRY_MS: i64 = 30_000;
const ENGINE_ENTRY_CUTOFF_MS: i64 = 15 * 60_000;

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

#[derive(serde::Serialize, serde::Deserialize)]
pub struct NativeExodus {
    pub core: SleeveCore<StrategyConfig, SleeveState>,
}

impl NativeExodus {
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

    pub fn reduce(&mut self, input: ReducerInput) -> Result<ReducerOutput, &'static str> {
        let output = reduce(input, self.core.state.clone(), &self.core.config)?;
        self.core.state = output.next_state.clone();
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        Ok(output)
    }

    fn effective_config(&self, ctx: &dyn StrategyCtx) -> StrategyConfig {
        let mut config = self.core.config.clone();
        config.entries_enabled = ctx.entries_enabled(self.core.config.entries_enabled);
        config
    }

    fn pending_events(&self, ctx: &dyn StrategyCtx) -> Result<Vec<CarryPresettlementFire>, String> {
        let source = ctx
            .strategy_id(&self.core.config.carry_sleeve_name)
            .ok_or_else(|| "configured CARRY source strategy is absent".to_owned())?;
        let mut pending = Vec::new();
        ctx.strategy_events(&mut pending);
        let mut decoded = Vec::new();
        for event in pending {
            if event.destination != self.core.id {
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
        symbols.extend(self.core.state.open.keys().cloned());
        symbols.extend(events.iter().map(|event| event.symbol.clone()));
        symbols.extend(working.iter().cloned());
        let account = ctx.account_summary();
        let account_healthy = directional_account_is_healthy(account);
        ReducerInput {
            now_ms,
            events,
            facts: planner_facts(ctx, &symbols),
            owned_working_symbols: working,
            owned_opening_order_ids: opening,
            account_healthy,
            checkpoint_fingerprint: self.core.checkpoint_fingerprint.clone(),
        }
    }

    fn apply(&mut self, output: ReducerOutput, ctx: &mut dyn StrategyCtx) {
        self.core.state = output.next_state;
        self.core.checkpoint_fingerprint = Some(self.core.config.fingerprint());
        self.core.blockers.clear();
        for (event_id, reason) in output.summary.blocked_events {
            self.core.blockers.insert(event_id, reason);
        }
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

    fn replan(&mut self, ctx: &mut dyn StrategyCtx) {
        self.core.ensure_restored("Exodus", ctx);
        if self.core.flatten_request_id.is_some() {
            self.flatten_now(ctx);
            return;
        }
        let events = match self.pending_events(ctx) {
            Ok(events) => events,
            Err(error) => {
                self.core.last_error = Some(error);
                return;
            }
        };
        if events.is_empty()
            && self.core.state.open.is_empty()
            && self.core.checkpoint_fingerprint.is_none()
        {
            return;
        }
        let input = self.base_input(ctx.wall_ms().max(1), events, ctx);
        let config = self.effective_config(ctx);
        match reduce(input, self.core.state.clone(), &config) {
            Ok(output) => self.apply(output, ctx),
            Err(error) => self.core.last_error = Some(error.to_owned()),
        }
    }

    fn defer_opening(&mut self, name: String, reason: &str, ctx: &mut dyn StrategyCtx) {
        self.core.blockers.insert(name.clone(), reason.to_owned());
        self.core.state.refused_entries.insert(name.clone());
        let now_ms = ctx.wall_ms().max(1);
        let retry_at = self.core.state.open.get(&name).map(|record| {
            now_ms.saturating_add(TERMINAL_ENTRY_RETRY_MS).min(
                record
                    .settlement_ts_ms
                    .saturating_add(
                        self.core.config.rule.entry_valid_minutes_after_settlement * 60_000,
                    )
                    .saturating_sub(ENGINE_ENTRY_CUTOFF_MS),
            )
        });
        if let Some(retry_at) = retry_at.filter(|retry_at| *retry_at > now_ms) {
            self.core.state.entry_retry_after_ms.insert(name, retry_at);
        } else {
            self.core.state.entry_retry_after_ms.remove(&name);
        }
        self.replan(ctx);
    }

    fn flatten_now(&mut self, ctx: &mut dyn StrategyCtx) {
        self.core.ensure_restored("Exodus", ctx);
        let Some(request_id) = self.core.flatten_request_id.clone() else {
            return;
        };
        let mut symbols = attributed_symbols(ctx);
        symbols.extend(self.core.state.open.keys().cloned());
        let (working, opening) = owned_order_state(ctx);
        symbols.extend(working);
        let facts = planner_facts(ctx, &symbols);
        let conclusively_flat = attributed_exposure_is_flat(ctx, &symbols);
        if conclusively_flat && opening.values().all(Vec::is_empty) {
            self.core.state.open.clear();
            self.core.state.entry_closed_ts_ms_by_symbol.clear();
            self.core.state.refused_entries.clear();
            self.core.state.entry_retry_after_ms.clear();
        }
        let (execution, flat) = flatten_execution(
            &self.core.state,
            FlattenExecutionInput {
                config_fingerprint: self.core.config.fingerprint(),
                facts: &facts,
                owned_opening_order_ids: &opening,
                now_ms: ctx.wall_ms().max(1),
                request_id: &request_id,
                tag: "exodus_native_flatten",
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
        let account = ctx.account_summary();
        let account_healthy = directional_account_is_healthy(account);
        let mut wakes = self
            .core
            .state
            .open
            .values()
            .flat_map(|record| {
                [
                    record.fired_ts_ms,
                    record.settlement_ts_ms
                        + self.core.config.rule.cover_minutes_after_settlement * 60_000,
                ]
            })
            .filter(|wake| *wake > now_ms)
            .chain(
                self.core
                    .state
                    .entry_retry_after_ms
                    .values()
                    .copied()
                    .filter(|wake| *wake > now_ms),
            )
            .collect::<Vec<_>>();
        if !account_healthy
            && ctx.entries_enabled(self.core.config.entries_enabled)
            && self.core.state.open.values().any(|record| {
                let entry_deadline = record.settlement_ts_ms
                    + (self.core.config.rule.entry_valid_minutes_after_settlement - 15) * 60_000;
                now_ms < entry_deadline
                    && !self
                        .core
                        .state
                        .entry_closed_ts_ms_by_symbol
                        .contains_key(&record.symbol)
            })
        {
            wakes.push(now_ms.saturating_add(FAST_RETRY_MS));
        }
        if let Ok(events) = self.pending_events(ctx) {
            for event in events {
                if self.core.state.consumed_event_ids.contains(&event.event_id) {
                    continue;
                }
                if event.fired_ts_ms > now_ms {
                    wakes.push(event.fired_ts_ms);
                }
                let deadline = event.settlement_ts_ms
                    + (self.core.config.rule.entry_valid_minutes_after_settlement - 15) * 60_000;
                if deadline > now_ms {
                    wakes.push(deadline);
                    let matches_source = event.environment == self.core.config.environment
                        && event.source_profile == self.core.config.rule.accepted_source_profile
                        && event.source_config_id
                            == self.core.config.rule.accepted_source_config_id;
                    if !account_healthy
                        && ctx.entries_enabled(self.core.config.entries_enabled)
                        && event.fired_ts_ms <= now_ms
                        && matches_source
                    {
                        wakes.push(now_ms.saturating_add(FAST_RETRY_MS));
                    }
                }
            }
        }
        if let Some(next) = wakes.into_iter().filter(|wake| *wake > now_ms).min() {
            let delay_ms = u64::try_from(next - now_ms).unwrap_or(u64::MAX);
            ctx.arm_timer(TIMER, delay_ms.saturating_mul(1_000_000).max(1));
        }
    }
}

impl Strategy for NativeExodus {
    fn runtime_state(
        &self,
    ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String> {
        crate::runtime::snapshot(NAME, self, &(self.core.id, &self.core.config)).map(Some)
    }
    fn name(&self) -> &str {
        NAME
    }

    fn input_dependencies(&self) -> Vec<String> {
        vec![self.core.config.carry_sleeve_name.clone()]
    }

    fn retained_signal_subscriptions(&self) -> Option<Vec<Subscription>> {
        if !self.core.restored {
            return None;
        }
        let mut symbols = self
            .core
            .state
            .open
            .keys()
            .cloned()
            .collect::<std::collections::BTreeSet<_>>();
        symbols.extend(self.core.state.refused_entries.iter().cloned());
        symbols.extend(self.core.state.entry_retry_after_ms.keys().cloned());
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
        self.core.validate_checkpoint("Exodus", checkpoint)
    }

    fn translate_checkpoint(
        &self,
        context: &StrategyImportContext,
        source_format: &str,
        sources: &[StrategyImportSource],
    ) -> Result<TranslatedStrategyState, String> {
        super::state_import::translate(
            &self.core.config,
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
        self.core.config.entries_enabled
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
        self.core.flatten_request_id = Some(request_id.to_owned());
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
            .is_some_and(|name| self.core.state.open.contains_key(name))
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
        self.core.ensure_restored("Exodus", ctx);
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
                    ctx.arm_timer(TIMER, u64::try_from(FAST_RETRY_MS).unwrap_or(1) * 1_000_000);
                    return;
                }
                if let Some(name) = ctx.symbol_name(facts.symbol).map(str::to_owned) {
                    if self.core.state.open.contains_key(&name) {
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
        self.core.ensure_restored("Exodus", ctx);
        let Some(name) = ctx.symbol_name(symbol).map(str::to_owned) else {
            return;
        };
        if !reduce_only && self.core.state.open.contains_key(&name) {
            self.defer_opening(name, reason, ctx);
            return;
        }
        self.core.blockers.insert(name, reason.to_owned());
        ctx.arm_timer(TIMER, u64::try_from(FAST_RETRY_MS).unwrap_or(1) * 1_000_000);
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
    use std::collections::{BTreeMap, BTreeSet};

    use super::*;
    use crate::mock_ctx::{Harness, MockCtx, RestingSeed};
    use crate::native_common::{checkpoint_payload, DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION};
    use crate::native_exodus::plan::{OpenRecord, RuleConfig};
    use engine_types::{Action, OrderKind, Side};

    #[test]
    fn retained_routes_follow_live_retry_state_and_release_history() {
        let mut plug = NativeExodus::new(config(), SleeveState::default()).unwrap();
        plug.core.state.refused_entries.insert("EXITUSDT".into());
        plug.core.state.refused_entries.insert("RETRYUSDT".into());
        plug.core
            .state
            .entry_retry_after_ms
            .insert("RETRYUSDT".into(), 10);
        for symbol in ["EXITUSDT", "RETRYUSDT"] {
            plug.core.state.open.insert(
                symbol.into(),
                OpenRecord {
                    symbol: symbol.into(),
                    notional_usdt: 10.0,
                    settlement_ts_ms: 120_000,
                    fired_ts_ms: 60_000,
                    target_qty: Some(1.0),
                },
            );
        }
        plug.core
            .state
            .consumed_event_ids
            .insert("carry-presettlement-HISTORYUSDT".into());
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
        StrategyConfig {
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
        }
    }

    #[test]
    fn params_refuse_an_untyped_config() {
        let params: toml::Value = toml::from_str("config_json = '{}'").expect("toml");
        assert!(NativeExodus::from_params(StrategyId(0), &params).is_err());
    }

    #[test]
    fn boot_restores_open_cover_and_arms_its_clock_without_market_news() {
        let now_ms = 1_700_000_000_000;
        let config = config();
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
            core: SleeveCore {
                id: StrategyId(1),
                config,
                state: SleeveState::default(),
                restored: false,
                checkpoint_fingerprint: None,
                blockers: std::collections::BTreeMap::new(),
                last_error: None,
                flatten_request_id: None,
            },
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

    #[test]
    fn backward_wall_clock_arms_the_durable_fire_time() {
        let now_ms = 1_700_000_000_000;
        let fired_ts_ms = now_ms + 5_000;
        let mut state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        state.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 10.0,
                settlement_ts_ms: fired_ts_ms + 20 * 60_000,
                fired_ts_ms,
                target_qty: Some(1.0),
            },
        );
        let strategy = NativeExodus::new(config(), state).expect("strategy");
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(now_ms);

        strategy.arm_next(&mut ctx);

        assert_eq!(ctx.arm_calls.len(), 1);
        assert_eq!(
            ctx.arm_calls[0].due_ns - ctx.arm_calls[0].armed_ns,
            5_000_000_000
        );
    }

    #[test]
    fn unobserved_account_blocks_input_and_retries_an_unfilled_open_record() {
        let now_ms = 1_700_000_000_000;
        let mut state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        state.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 10.0,
                settlement_ts_ms: now_ms + 20 * 60_000,
                fired_ts_ms: now_ms - 60_000,
                target_qty: Some(1.0),
            },
        );
        let strategy = NativeExodus::new(config(), state).expect("strategy");
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(now_ms);
        ctx.set_now(0);

        assert!(
            !strategy
                .base_input(now_ms, Vec::new(), &ctx)
                .account_healthy
        );

        strategy.arm_next(&mut ctx);

        assert_eq!(ctx.arm_calls.len(), 1);
        assert_eq!(
            ctx.arm_calls[0].due_ns - ctx.arm_calls[0].armed_ns,
            u64::try_from(FAST_RETRY_MS).expect("retry") * 1_000_000
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
                NativeExodus::new(config(), SleeveState::default()).expect("strategy");
            let mut ctx = MockCtx::new();
            ctx.set_position("AUSDT", Side::Sell, 1.0, 100.0);
            let symbol = ctx.id_of("AUSDT");

            strategy.on_flatten_directional("flatten-exodus", &mut ctx);
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
                side: Side::Buy,
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
            assert_eq!(
                timer.due_ns - timer.armed_ns,
                u64::try_from(FAST_RETRY_MS).expect("retry") * 1_000_000
            );

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
    fn terminal_entry_refusal_waits_thirty_seconds_and_cover_exit_is_not_suppressed() {
        let now_ms = 1_700_000_000_000;
        let mut state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        state.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 100.0,
                settlement_ts_ms: now_ms + 20 * 60_000,
                fired_ts_ms: now_ms - 10 * 60_000,
                target_qty: Some(1.0),
            },
        );
        let mut strategy = NativeExodus::new(config(), state).expect("strategy");
        let mut ctx = MockCtx::new();
        ctx.set_wall_ms(now_ms);
        ctx.add_symbol("AUSDT");
        ctx.set_strategy_id("carry", StrategyId(0));

        strategy.defer_opening("AUSDT".into(), "venue_reject", &mut ctx);
        let retry_at = strategy.core.state.entry_retry_after_ms["AUSDT"];
        assert_eq!(retry_at, now_ms + TERMINAL_ENTRY_RETRY_MS);

        let mut near_deadline_state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            ..SleeveState::default()
        };
        near_deadline_state.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 100.0,
                settlement_ts_ms: now_ms - 290_000,
                fired_ts_ms: now_ms - 600_000,
                target_qty: Some(1.0),
            },
        );
        let mut near_deadline =
            NativeExodus::new(config(), near_deadline_state).expect("deadline strategy");
        let mut deadline_ctx = MockCtx::new();
        deadline_ctx.set_wall_ms(now_ms);
        deadline_ctx.add_symbol("AUSDT");
        deadline_ctx.set_strategy_id("carry", StrategyId(0));
        near_deadline.defer_opening("AUSDT".into(), "venue_reject", &mut deadline_ctx);
        assert_eq!(
            near_deadline.core.state.entry_retry_after_ms["AUSDT"],
            now_ms + 10_000,
            "the bounded retry cannot outlive the entry deadline"
        );

        let mut harness = Harness {
            ctx,
            strategy: Box::new(strategy),
        };
        harness.ctx.set_rule(
            "AUSDT",
            engine_types::InstrumentRule {
                tick_size: 0.01,
                qty_step: 0.1,
                min_qty: 0.1,
                min_notional: 5.0,
            },
        );
        harness.ctx.set_wall_ms(now_ms + 10_000);
        harness.quote("AUSDT", 99.0, 100.0);
        assert!(harness.drain().is_empty(), "no opening retry before due");

        harness.ctx.set_wall_ms(retry_at);
        let now_ns = harness.ctx.now_ns();
        harness.strategy.on_event(
            &engine_types::EngineEvent::Timer { id: TIMER, now_ns },
            &mut harness.ctx,
        );
        let retry = harness.drain();
        assert_eq!(retry.len(), 1, "one opening reattempt at the due time");
        assert!(!retry[0].reduce_only);

        let mut due_state = SleeveState {
            schema_version: DIRECTIONAL_CHECKPOINT_SCHEMA_VERSION,
            refused_entries: BTreeSet::from(["AUSDT".into()]),
            entry_retry_after_ms: BTreeMap::from([(
                "AUSDT".into(),
                now_ms + TERMINAL_ENTRY_RETRY_MS,
            )]),
            ..SleeveState::default()
        };
        due_state.open.insert(
            "AUSDT".into(),
            OpenRecord {
                symbol: "AUSDT".into(),
                notional_usdt: 100.0,
                settlement_ts_ms: now_ms - 61 * 60_000,
                fired_ts_ms: now_ms - 70 * 60_000,
                target_qty: Some(1.0),
            },
        );
        let mut cover = Harness::new(Box::new(
            NativeExodus::new(config(), due_state).expect("cover strategy"),
        ));
        cover.ctx.set_wall_ms(now_ms);
        cover.ctx.set_strategy_id("carry", StrategyId(0));
        cover
            .ctx
            .set_position("AUSDT", engine_types::Side::Sell, 1.0, 100.0);
        cover.boot();
        let exits = cover.drain();
        assert_eq!(exits.len(), 1);
        assert!(
            exits[0].reduce_only,
            "terminal entry throttle cannot block cover"
        );
    }
}
