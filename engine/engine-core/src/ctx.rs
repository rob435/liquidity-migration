//! The window a strategy sees, and the timers it can set.
//!
//! A strategy gets read-only market state, the account reading, the
//! instrument rules, the clock, its own resting orders, a way to hand back an
//! action, and one-shot timers. Nothing else: no venue, no log, no other
//! strategy's state. Timers are scoped per strategy, so two strategies may
//! both use timer 1 without colliding, and re-arming the same number before
//! it fires replaces the old one.

use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};

use engine_types::{
    AccountView, Action, EngineEvent, InstrumentRule, MarketState, PositionView, Quote,
    RestingOrder, Strategy, StrategyAccountSummary, StrategyCheckpoint, StrategyCtx, StrategyEvent,
    StrategyGlobalCheckpointState, StrategyId, StrategyPositionFacts, SymbolId, Ticker, TimerId,
};

use crate::attribution::Attribution;
use crate::covers::CoverBook;
use crate::inflight::{LedgerOfOrders, OrderRegistry};

#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
struct Pending {
    deadline_ns: u64,
    strategy: u16,
    timer: u32,
}

#[derive(Default)]
pub struct Timers {
    scheduled: BTreeSet<Pending>,
    armed: HashMap<(u16, u32), u64>,
}

impl Timers {
    pub fn arm(&mut self, strategy: StrategyId, timer: TimerId, deadline_ns: u64) {
        if let Some(previous) = self.armed.insert((strategy.0, timer.0), deadline_ns) {
            if previous == deadline_ns {
                return;
            }
            self.scheduled.remove(&Pending {
                deadline_ns: previous,
                strategy: strategy.0,
                timer: timer.0,
            });
        }
        self.scheduled.insert(Pending {
            deadline_ns,
            strategy: strategy.0,
            timer: timer.0,
        });
    }

    pub(crate) fn restore(
        &mut self,
        strategy: StrategyId,
        timers: &[engine_types::strategy_process::StrategyTimerState],
        now_ns: u64,
        wall_ms: i64,
    ) {
        self.scheduled
            .retain(|pending| pending.strategy != strategy.0);
        self.armed.retain(|(owner, _), _| *owner != strategy.0);
        for timer in timers {
            let remaining_ms = timer.deadline_wall_ms.saturating_sub(wall_ms).max(0) as u64;
            self.arm(
                strategy,
                timer.id,
                now_ns.saturating_add(remaining_ms.saturating_mul(1_000_000)),
            );
        }
    }

    /// Earliest deadline among the currently armed timers.
    pub fn next_deadline(&mut self) -> Option<u64> {
        self.scheduled.first().map(|pending| pending.deadline_ns)
    }

    pub fn pop_due(&mut self, now_ns: u64) -> Option<(StrategyId, TimerId)> {
        let top = self.scheduled.first().copied()?;
        if top.deadline_ns > now_ns {
            return None;
        }
        self.scheduled.pop_first();
        self.armed.remove(&(top.strategy, top.timer));
        Some((StrategyId(top.strategy), TimerId(top.timer)))
    }

    pub(crate) fn is_armed(&self, strategy: StrategyId, timer: TimerId) -> bool {
        self.armed.contains_key(&(strategy.0, timer.0))
    }

    pub fn armed_count(&self) -> usize {
        self.armed.len()
    }
}

/// Handed to one strategy for the length of one callback.
/// What every strategy reads and none may edit: one market, one account
/// reading, the venue's rules, and the books the engine keeps about orders,
/// their owners, and what is in flight ahead of the reading.
pub struct Books {
    pub market: MarketState,
    /// The engine's own account reading, the same one the risk kernel judges
    /// against. Shared rather than copied per strategy: one reading, one
    /// truth, and nobody can edit it on the way past.
    pub account: AccountView,
    /// The venue's instrument rules, indexed by symbol, exactly as the
    /// engine quantizes against.
    pub rules: Vec<Option<InstrumentRule>>,
    pub portfolio_symbols: BTreeSet<SymbolId>,
    /// What the log says is still out there. Filtered by the registry below
    /// before a strategy is shown any of it.
    pub orders: LedgerOfOrders,
    /// Who placed each order. Without it one strategy could read, and then
    /// cancel, another's working orders.
    pub registry: OrderRegistry,
    /// Whose each position is, summed from the fills of the orders each
    /// strategy placed. The account reading is per symbol and says nothing
    /// about whose a position is.
    pub attribution: Attribution,
    /// What each strategy has sent that the account reading has not yet
    /// absorbed. The engine books and releases these; a strategy reads its
    /// own sum through `in_flight`.
    pub covers: CoverBook,
}

#[derive(Clone, Debug)]
pub struct PendingAction {
    pub caller: Option<StrategyId>,
    pub action: Action,
    pub(crate) effect: Option<crate::effects::EffectKey>,
    pub(crate) callback_id: Option<u64>,
}

impl From<Action> for PendingAction {
    fn from(action: Action) -> Self {
        Self {
            caller: None,
            action,
            effect: None,
            callback_id: None,
        }
    }
}

/// The strategies and what the engine holds on their behalf.
pub struct StrategyHost {
    pub strategies: Vec<Box<dyn Strategy>>,
    pub names: Vec<String>,
    pub timers: Timers,
    /// Actions emitted and not yet drained, in emission order.
    pub pending: VecDeque<PendingAction>,
    pub(crate) effects: crate::effects::Effects,
    pub(crate) callbacks: crate::strategy_process::host::CallbackHost,
    /// Strategy-owned state, persisted before the action it guards and
    /// restated through rotation. The engine stores bytes, not meaning.
    pub checkpoints: BTreeMap<(StrategyId, SymbolId), StrategyCheckpoint>,
    /// Whole-sleeve reducer state. Separate key space: no sentinel symbol can
    /// collide with a venue name admitted later.
    pub global_checkpoints: BTreeMap<StrategyId, StrategyGlobalCheckpointState>,
    /// Cross-sleeve events waiting for the addressed strategy to consume them.
    pub events: BTreeMap<(StrategyId, String), StrategyEvent>,
    /// The newest durable runtime entry override per strategy.
    pub entries_enabled: BTreeMap<StrategyId, bool>,
}

impl StrategyHost {
    pub(crate) fn snapshot(
        &mut self,
        books: &Books,
        sid: StrategyId,
        now_ns: u64,
    ) -> Result<engine_types::strategy_process::CallbackSnapshot, String> {
        let mut actions = VecDeque::new();
        let ctx = Ctx {
            books,
            now_ns,
            strategy: sid,
            out: &mut actions,
            timers: &mut self.timers,
            checkpoints: &self.checkpoints,
            global_checkpoints: &self.global_checkpoints,
            strategy_events: &self.events,
            strategy_names: &self.names,
            runtime_entries_enabled: self.entries_enabled.get(&sid).copied(),
        };
        ctx.callback_snapshot()
    }

    /// Wake one strategy with an event. Its actions land in `pending`.
    pub fn feed(
        &mut self,
        books: &Books,
        sid: StrategyId,
        event: &EngineEvent,
        now_ns: u64,
    ) -> bool {
        if self.callbacks.isolated() {
            if let Err(error) = self.callbacks.enqueue(sid, event) {
                let crate::strategy_process::host::EnqueueError::Fault(error) = error else {
                    return false;
                };
                if self.callbacks.faults.get(&sid) != Some(&error) {
                    tracing::error!(
                        strategy = sid.0,
                        error,
                        "strategy callback not accepted; source must retain delivery"
                    );
                }
                self.callbacks.faults.insert(sid, error);
                return false;
            }
            return true;
        }
        let Some(strategy) = self.strategies.get_mut(sid.idx()) else {
            return false;
        };
        let mut actions = VecDeque::new();
        let mut ctx = Ctx {
            books,
            now_ns,
            strategy: sid,
            out: &mut actions,
            timers: &mut self.timers,
            checkpoints: &self.checkpoints,
            global_checkpoints: &self.global_checkpoints,
            strategy_events: &self.events,
            strategy_names: &self.names,
            runtime_entries_enabled: self.entries_enabled.get(&sid).copied(),
        };
        strategy.on_event(event, &mut ctx);
        if actions.is_empty() {
            return true;
        }
        let actions: Vec<_> = actions.into_iter().collect();
        let durable = actions.iter().any(|action| {
            matches!(
                action,
                Action::SetStrategyCheckpoint { .. }
                    | Action::SetStrategyGlobalCheckpoint { .. }
                    | Action::PublishStrategyEvent { .. }
                    | Action::ConsumeStrategyEvent { .. }
                    | Action::ConsumeSignalObservation { .. }
                    | Action::RejectSignalObservation { .. }
                    | Action::ConsumeRuntimeControl { .. }
            )
        });
        if !durable {
            let callback_id = self.effects.next_id;
            self.effects.next_id = callback_id
                .checked_add(1)
                .expect("strategy callback id exhausted");
            self.pending
                .extend(actions.into_iter().map(|action| PendingAction {
                    caller: Some(sid),
                    action,
                    effect: None,
                    callback_id: Some(callback_id),
                }));
            return true;
        }
        let transition_id = self.effects.capture(sid, actions.clone());
        self.pending.extend(
            actions
                .into_iter()
                .enumerate()
                .map(|(index, action)| PendingAction {
                    caller: Some(sid),
                    action,
                    effect: Some(crate::effects::EffectKey {
                        transition_id,
                        index,
                    }),
                    callback_id: Some(transition_id),
                }),
        );
        true
    }
}

pub struct Ctx<'a> {
    pub books: &'a Books,
    pub now_ns: u64,
    pub strategy: StrategyId,
    pub out: &'a mut VecDeque<Action>,
    pub timers: &'a mut Timers,
    pub checkpoints: &'a BTreeMap<(StrategyId, SymbolId), StrategyCheckpoint>,
    pub global_checkpoints: &'a BTreeMap<StrategyId, StrategyGlobalCheckpointState>,
    pub strategy_events: &'a BTreeMap<(StrategyId, String), StrategyEvent>,
    pub strategy_names: &'a [String],
    pub runtime_entries_enabled: Option<bool>,
}

pub(crate) fn bind_action(strategy: StrategyId, now_ns: u64, action: Action) -> Action {
    match action {
        Action::Place(mut intent) => {
            intent.strategy = strategy;
            if intent.decided_ns == 0 {
                intent.decided_ns = now_ns;
            }
            Action::Place(intent)
        }
        Action::RecordQuoteFill { mut features } => {
            features.strategy = strategy;
            Action::RecordQuoteFill { features }
        }
        Action::SetStrategyCheckpoint {
            symbol, checkpoint, ..
        } => Action::SetStrategyCheckpoint {
            strategy,
            symbol,
            checkpoint,
        },
        Action::SetStrategyGlobalCheckpoint { checkpoint, .. } => {
            Action::SetStrategyGlobalCheckpoint {
                strategy,
                checkpoint,
            }
        }
        Action::PublishStrategyEvent { mut event } => {
            event.source = strategy;
            Action::PublishStrategyEvent { event }
        }
        Action::ConsumeStrategyEvent {
            source, event_id, ..
        } => Action::ConsumeStrategyEvent {
            source,
            destination: strategy,
            event_id,
        },
        Action::ConsumeSignalObservation {
            source,
            sequence,
            observation_id,
            ..
        } => Action::ConsumeSignalObservation {
            strategy,
            source,
            sequence,
            observation_id,
        },
        Action::RejectSignalObservation {
            source,
            sequence,
            observation_id,
            reason,
            ..
        } => Action::RejectSignalObservation {
            strategy,
            source,
            sequence,
            observation_id,
            reason,
        },
        Action::ConsumeRuntimeControl { request_id, .. } => Action::ConsumeRuntimeControl {
            strategy,
            request_id,
        },
        other => other,
    }
}

impl StrategyCtx for Ctx<'_> {
    fn quote(&self, symbol: SymbolId) -> &Quote {
        self.books.market.quote(symbol)
    }

    fn depth(&self, symbol: SymbolId) -> &engine_types::Depth {
        self.books.market.depth(symbol)
    }

    fn trade_flow(&self, symbol: SymbolId) -> &engine_types::TradeFlow {
        self.books.market.trade_flow(symbol)
    }

    fn ticker(&self, symbol: SymbolId) -> &Ticker {
        self.books.market.ticker(symbol)
    }

    fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.books.market.table.get(name)
    }

    fn symbol_name(&self, symbol: SymbolId) -> Option<&str> {
        ((symbol.0 as usize) < self.books.market.table.len())
            .then(|| self.books.market.table.name(symbol))
    }

    fn now_ns(&self) -> u64 {
        self.now_ns
    }

    fn entries_enabled(&self, config_default: bool) -> bool {
        config_default && self.runtime_entries_enabled.unwrap_or(true)
    }

    fn account_summary(&self) -> StrategyAccountSummary {
        StrategyAccountSummary {
            equity_usdt: self.books.account.equity_usdt,
            available_margin_usdt: self.books.account.available_usdt,
            observed_ns: self.books.account.observed_ns,
        }
    }

    fn position(&self, symbol: SymbolId) -> Option<PositionView> {
        // A row saying zero is a flat symbol, and flat is not a position: an
        // exit sized off one would be an order for nothing.
        self.books
            .account
            .positions
            .iter()
            .find(|p| p.symbol == symbol && p.qty > 0.0)
            .cloned()
    }

    fn foreign_position(&self, symbol: SymbolId) -> bool {
        if self.books.portfolio_symbols.contains(&symbol) {
            return false;
        }
        self.books
            .attribution
            .held_by_another(self.strategy, symbol)
            || self
                .books
                .orders
                .opening_owned_by_another(self.strategy, symbol)
    }

    fn my_position(&self, symbol: SymbolId) -> f64 {
        self.books.attribution.signed(self.strategy, symbol)
    }

    fn my_position_exact(
        &self,
        symbol: SymbolId,
    ) -> Result<engine_types::numeric::Exact, engine_types::numeric::ExactError> {
        Ok(self.books.attribution.signed_exact(self.strategy, symbol))
    }

    fn in_flight_exact(
        &self,
        symbol: SymbolId,
    ) -> Result<engine_types::numeric::Exact, engine_types::numeric::ExactError> {
        use engine_types::numeric::{Exact, ExactError};
        if !self.books.portfolio_symbols.contains(&symbol) {
            return Exact::from_legacy_f64(self.in_flight(symbol));
        }
        self.books
            .orders
            .orders
            .values()
            .filter(|order| {
                order.request.sleeve_owner() == Some(self.strategy)
                    && order.request.symbol == symbol
                    && order.in_flight()
            })
            .try_fold(Exact::zero(), |sum, order| {
                let quantity = order
                    .remaining_exact()
                    .map_err(|_| ExactError::InvalidProjection)?;
                Ok(sum
                    + if order.request.side == engine_types::Side::Buy {
                        quantity
                    } else {
                        -quantity
                    })
            })
    }

    fn my_position_names<'a>(&'a self, out: &mut Vec<&'a str>) {
        let start = out.len();
        out.extend(
            self.books
                .attribution
                .symbols(self.strategy)
                .map(|symbol| self.books.market.table.name(symbol)),
        );
        out[start..].sort_unstable();
    }

    fn in_flight(&self, symbol: SymbolId) -> f64 {
        self.books.covers.in_flight(self.strategy, symbol)
    }

    fn my_position_facts(&self, symbol: SymbolId) -> Option<StrategyPositionFacts> {
        let attributed_signed_qty = self.books.attribution.signed(self.strategy, symbol);
        let mut in_flight_signed_qty = 0.0;
        let mut open_order_count = 0;
        for order in self.books.orders.orders.values().filter(|order| {
            order.request.sleeve_owner() == Some(self.strategy)
                && order.request.symbol == symbol
                && order.in_flight()
        }) {
            let qty = order.remaining_qty().expect("validated order quantity");
            in_flight_signed_qty += if order.request.side == engine_types::Side::Buy {
                qty
            } else {
                -qty
            };
            open_order_count += 1;
        }
        if attributed_signed_qty == 0.0 && open_order_count == 0 {
            return None;
        }
        Some(StrategyPositionFacts {
            symbol,
            attributed_signed_qty,
            venue: self.position(symbol),
            in_flight_signed_qty,
            open_order_count,
            allocated: Some(
                self.books
                    .attribution
                    .allocated(self.strategy, symbol)
                    .unwrap_or(engine_types::strategy::StrategyAllocatedPosition {
                        entry_px: None,
                        stop_px: None,
                    }),
            ),
        })
    }

    fn my_positions(&self, out: &mut Vec<StrategyPositionFacts>) {
        let mut symbols: std::collections::BTreeSet<u16> = self
            .books
            .attribution
            .symbols(self.strategy)
            .map(|symbol| symbol.0)
            .collect();
        symbols.extend(self.books.orders.orders.values().filter_map(|order| {
            (order.request.sleeve_owner() == Some(self.strategy) && order.in_flight())
                .then_some(order.request.symbol.0)
        }));
        out.extend(
            symbols
                .into_iter()
                .filter_map(|symbol| self.my_position_facts(SymbolId(symbol))),
        );
    }

    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule> {
        self.books.rules.get(symbol.0 as usize).copied().flatten()
    }

    fn wall_ms(&self) -> i64 {
        // Read when asked rather than stamped once per wake, so a strategy
        // that never asks the wall clock never pays for it.
        engine_types::clock::wall_ms()
    }

    fn emit(&mut self, action: Action) {
        self.out
            .push_back(bind_action(self.strategy, self.now_ns, action));
    }

    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        self.timers
            .arm(self.strategy, id, self.now_ns.saturating_add(after_ns));
    }

    fn resting<'a>(&'a self, out: &mut Vec<RestingOrder<'a>>) {
        for (id, order) in &self.books.orders.orders {
            // Someone else's order is none of this strategy's business, and
            // an order the log has already ended cannot be pulled or moved.
            if !order.in_flight() || self.books.registry.owner_of(id) != Some(self.strategy) {
                continue;
            }
            let request = &order.request;
            out.push(RestingOrder {
                client_order_id: id.as_str(),
                symbol: request.symbol,
                side: request.side,
                kind: request.kind,
                qty: request.qty,
                filled_qty: order.filled_qty,
                remaining_qty: Some(
                    order
                        .remaining_qty()
                        .expect("validated canonical order remainder"),
                ),
                reduce_only: request.is_sleeve_reduction(),
                acked: order.acked,
            });
        }
    }

    fn order_facts(&self, client_order_id: &str) -> Option<engine_types::OrderFacts> {
        // The ledger rather than the registry, for the same reason
        // `owner_of` prefers it: terminal news can arrive for an order an
        // earlier boot sent. Another strategy's order stays none of this
        // one's business.
        let order = self.books.orders.orders.get(client_order_id)?;
        if order.request.sleeve_owner() != Some(self.strategy) {
            return None;
        }
        Some(engine_types::OrderFacts {
            symbol: order.request.symbol,
            side: order.request.side,
            qty: order.request.qty,
            filled_qty: order.filled_qty,
            remaining_qty: Some(
                order
                    .remaining_qty()
                    .expect("validated canonical order remainder"),
            ),
            reduce_only: order.request.is_sleeve_reduction(),
        })
    }

    fn strategy_checkpoint(&self, symbol: SymbolId) -> Option<&StrategyCheckpoint> {
        self.checkpoints.get(&(self.strategy, symbol))
    }

    fn strategy_global_checkpoint(&self) -> Option<&StrategyCheckpoint> {
        self.global_checkpoints
            .get(&self.strategy)
            .map(|state| &state.checkpoint)
    }

    fn strategy_id(&self, name: &str) -> Option<StrategyId> {
        self.strategy_names
            .iter()
            .position(|known| known == name)
            .and_then(|at| u16::try_from(at).ok())
            .map(StrategyId)
    }

    fn strategy_events(&self, out: &mut Vec<StrategyEvent>) {
        out.extend(
            self.strategy_events
                .values()
                .filter(|event| event.source == self.strategy || event.destination == self.strategy)
                .cloned(),
        );
    }
}

#[cfg(test)]
mod tests {
    use std::sync::OnceLock;

    use super::*;
    use engine_types::{Intent, OrderKind, OrderRequest, OrderUpdate, Side, WalRecord};

    fn sent(id: &str, strategy: StrategyId) -> WalRecord {
        WalRecord::OrderSent {
            dispatch: None,
            request: OrderRequest {
                client_order_id: id.into(),
                strategy,
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Limit {
                    px: 100.0,
                    tif: engine_types::TimeInForce::Gtc,
                },
                stop: None,
                reduce_only: false,
                exact_terms: None,
                sleeve_effect: None,
                close_position: false,
            },
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    /// An account reading with nothing open, which is what most of these
    /// tests want the context to be sitting on.
    fn flat_account() -> AccountView {
        AccountView {
            exact_amounts: None,
            equity_usdt: 1_000.0,
            available_usdt: 1_000.0,
            positions: Vec::new(),
            observed_ns: 1,
        }
    }

    /// One context over a hand-built book, so the filters can be read off
    /// directly instead of through a whole engine run.
    /// Books over a hand-built order book: flat account, nobody attributed,
    /// nothing in flight, which is what every test not about those means.
    fn books_over(market: MarketState, orders: LedgerOfOrders, registry: OrderRegistry) -> Books {
        Books {
            market,
            account: flat_account(),
            rules: Vec::new(),
            portfolio_symbols: BTreeSet::new(),
            orders,
            registry,
            attribution: Attribution::default(),
            covers: CoverBook::default(),
        }
    }

    fn ctx_over<'a>(
        books: &'a Books,
        out: &'a mut VecDeque<Action>,
        timers: &'a mut Timers,
        strategy: StrategyId,
    ) -> Ctx<'a> {
        static NO_CHECKPOINTS: OnceLock<
            std::collections::BTreeMap<(StrategyId, SymbolId), StrategyCheckpoint>,
        > = OnceLock::new();
        static NO_GLOBAL_CHECKPOINTS: OnceLock<
            std::collections::BTreeMap<StrategyId, StrategyGlobalCheckpointState>,
        > = OnceLock::new();
        static NO_STRATEGY_EVENTS: OnceLock<
            std::collections::BTreeMap<(StrategyId, String), StrategyEvent>,
        > = OnceLock::new();
        static NO_STRATEGY_NAMES: OnceLock<Vec<String>> = OnceLock::new();
        Ctx {
            books,
            now_ns: 42,
            strategy,
            out,
            timers,
            checkpoints: NO_CHECKPOINTS.get_or_init(Default::default),
            global_checkpoints: NO_GLOBAL_CHECKPOINTS.get_or_init(Default::default),
            strategy_events: NO_STRATEGY_EVENTS.get_or_init(Default::default),
            strategy_names: NO_STRATEGY_NAMES.get_or_init(Vec::new),
            runtime_entries_enabled: None,
        }
    }

    #[test]
    fn sleeve_pending_is_unfilled_quantity_before_account_catchup_and_after_restart() {
        let owner = StrategyId(0);
        let symbol = SymbolId(0);
        let records = vec![
            sent("entry", owner),
            engine_types::WalRecord::OrderUpdate {
                callbacks: None,
                update: engine_types::OrderUpdate::Fill {
                    allocation: None,
                    amounts: None,
                    exec_id: "partial".into(),
                    client_order_id: "entry".into(),
                    symbol,
                    side: engine_types::Side::Buy,
                    qty: 0.4,
                    px: 100.0,
                    fee: Some(0.0),
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: 2,
                    recv_ns: 2,
                },
            },
        ];
        for restarted in [false, true] {
            let mut books = books_over(
                MarketState::default(),
                LedgerOfOrders::from_records(&records),
                OrderRegistry::default(),
            );
            books.attribution = Attribution::from_records(&records);
            if !restarted {
                books
                    .covers
                    .register(owner, symbol, engine_types::Side::Buy, 1.0, &books.account);
            }
            let mut out = VecDeque::new();
            let mut timers = Timers::default();
            let ctx = ctx_over(&books, &mut out, &mut timers, owner);
            let facts = ctx
                .my_position_facts(symbol)
                .expect("owned partial execution");
            assert_eq!(facts.attributed_signed_qty, 0.4);
            assert_eq!(
                facts.in_flight_signed_qty, 0.6,
                "restart={restarted}: only the unfilled remainder is pending"
            );
        }
    }

    #[test]
    fn timers_fire_in_time_order_and_are_scoped_per_strategy() {
        let mut timers = Timers::default();
        timers.arm(StrategyId(1), TimerId(1), 300);
        timers.arm(StrategyId(0), TimerId(1), 100);
        assert_eq!(timers.next_deadline(), Some(100));
        assert_eq!(timers.pop_due(150), Some((StrategyId(0), TimerId(1))));
        assert_eq!(timers.pop_due(150), None, "strategy 1's timer is not due");
        assert_eq!(timers.pop_due(300), Some((StrategyId(1), TimerId(1))));
        assert_eq!(timers.pop_due(300), None);
        assert_eq!(timers.armed_count(), 0);
    }

    #[test]
    fn rearming_the_same_number_replaces_the_old_deadline() {
        let mut timers = Timers::default();
        timers.arm(StrategyId(0), TimerId(7), 100);
        timers.arm(StrategyId(0), TimerId(7), 500);
        assert_eq!(timers.next_deadline(), Some(500));
        assert_eq!(timers.pop_due(200), None);
        assert_eq!(timers.pop_due(600), Some((StrategyId(0), TimerId(7))));
        assert_eq!(timers.pop_due(600), None, "only one firing");
    }

    #[test]
    fn rearming_one_timer_retains_only_one_scheduled_node() {
        let mut timers = Timers::default();
        for deadline_ns in 100_000..150_000 {
            timers.arm(StrategyId(3), TimerId(7), deadline_ns);
        }
        assert_eq!(timers.armed_count(), 1);
        assert_eq!(timers.scheduled.len(), timers.armed_count());
        assert_eq!(timers.next_deadline(), Some(149_999));
        assert_eq!(timers.pop_due(149_998), None);
        assert_eq!(timers.pop_due(149_999), Some((StrategyId(3), TimerId(7))));
        assert_eq!(timers.pop_due(u64::MAX), None);
        assert_eq!(timers.armed_count(), 0);
        assert!(timers.scheduled.is_empty());
    }

    #[test]
    fn timer_ties_keep_strategy_then_timer_order_after_rearms() {
        let mut timers = Timers::default();
        for (strategy, timer, deadline) in [
            (9, 3, 10),
            (1, u32::MAX, 10),
            (0, 7, 11),
            (1, 0, 10),
            (u16::MAX, 0, 10),
            (0, 2, 10),
            (0, 7, 10),
            (0, 2, 11),
            (0, 7, 10),
        ] {
            timers.arm(StrategyId(strategy), TimerId(timer), deadline);
        }
        assert_eq!(timers.armed_count(), 6);
        assert_eq!(timers.scheduled.len(), 6);
        assert_eq!(timers.pop_due(9), None);
        for (strategy, timer) in [(0, 7), (1, 0), (1, u32::MAX), (9, 3), (u16::MAX, 0)] {
            assert_eq!(timers.next_deadline(), Some(10));
            assert_eq!(
                timers.pop_due(10),
                Some((StrategyId(strategy), TimerId(timer)))
            );
        }
        assert_eq!(timers.next_deadline(), Some(11));
        assert_eq!(timers.pop_due(10), None);
        assert_eq!(timers.pop_due(11), Some((StrategyId(0), TimerId(2))));
        assert_eq!(timers.next_deadline(), None);
    }

    #[test]
    fn is_armed_distinguishes_a_popped_timer_from_its_replacement() {
        let mut timers = Timers::default();
        let strategy = StrategyId(4);
        let timer = TimerId(7);
        assert!(!timers.is_armed(strategy, timer));
        timers.arm(strategy, timer, 100);
        assert!(timers.is_armed(strategy, timer));
        assert!(!timers.is_armed(StrategyId(5), timer));
        assert!(!timers.is_armed(strategy, TimerId(8)));
        assert_eq!(timers.pop_due(99), None);
        assert!(timers.is_armed(strategy, timer));
        assert_eq!(timers.pop_due(100), Some((strategy, timer)));
        assert!(!timers.is_armed(strategy, timer));
        timers.arm(strategy, timer, 100);
        assert!(timers.is_armed(strategy, timer));
        assert_eq!(timers.pop_due(100), Some((strategy, timer)));
        assert!(!timers.is_armed(strategy, timer));
    }

    #[test]
    fn timer_rearms_and_pops_match_a_last_arm_reference_model() {
        fn pop_reference(
            current: &mut std::collections::BTreeMap<(u16, u32), u64>,
            now_ns: u64,
        ) -> Option<(StrategyId, TimerId)> {
            let (deadline, strategy, timer) = current
                .iter()
                .map(|(&(strategy, timer), &deadline)| (deadline, strategy, timer))
                .min()?;
            if deadline > now_ns {
                return None;
            }
            current.remove(&(strategy, timer));
            Some((StrategyId(strategy), TimerId(timer)))
        }

        let mut timers = Timers::default();
        let mut current = std::collections::BTreeMap::new();
        let mut seed = 0x9e37_79b9_7f4a_7c15_u64;
        for step in 0..20_000 {
            seed = seed.wrapping_mul(6_364_136_223_846_793_005).wrapping_add(1);
            let strategy = [0, 1, 2, u16::MAX][((seed >> 32) & 3) as usize];
            let timer = if step % 31 == 0 {
                u32::MAX
            } else {
                ((seed >> 40) % 17) as u32
            };
            if step % 5 < 3 {
                let deadline = match step % 7 {
                    0 => 0,
                    1 => u64::MAX,
                    2 => current.get(&(strategy, timer)).copied().unwrap_or(99),
                    _ => seed & 511,
                };
                current.insert((strategy, timer), deadline);
                timers.arm(StrategyId(strategy), TimerId(timer), deadline);
            } else {
                let now = if step % 13 == 0 {
                    u64::MAX
                } else {
                    (seed >> 16) & 511
                };
                assert_eq!(
                    timers.pop_due(now),
                    pop_reference(&mut current, now),
                    "step {step}"
                );
            }
            assert_eq!(
                timers.next_deadline(),
                current.values().copied().min(),
                "step {step}"
            );
            assert_eq!(timers.armed_count(), current.len(), "step {step}");
            assert_eq!(timers.scheduled.len(), current.len(), "step {step}");
        }
        while !current.is_empty() {
            assert_eq!(
                timers.pop_due(u64::MAX),
                pop_reference(&mut current, u64::MAX)
            );
        }
        assert_eq!(timers.pop_due(u64::MAX), None);
        assert_eq!(timers.armed_count(), 0);
        assert!(timers.scheduled.is_empty());
    }

    #[test]
    fn emit_files_the_intent_under_the_calling_strategy() {
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let books = books_over(market, orders, registry);
        let mut ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(3));
        ctx.place(Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(9),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            tag: "t".into(),
            decided_ns: 0,
            work: None,
            leverage: None,
        });
        let Some(Action::Place(got)) = out.pop_front() else {
            panic!("expected a placement");
        };
        assert_eq!(got.strategy, StrategyId(3));
        assert_eq!(got.decided_ns, 42);
    }

    #[test]
    fn a_cancel_reaches_the_engine_naming_the_order_it_pulls() {
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let books = books_over(market, orders, registry);
        let mut ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(3));
        ctx.cancel(SymbolId(1), "eng-7");
        assert_eq!(
            out.pop_front(),
            Some(Action::Cancel {
                symbol: SymbolId(1),
                client_order_id: "eng-7".into(),
            })
        );
    }

    fn exact_partial_books(total: &str, part: &str) -> Books {
        use engine_types::numeric::{AssetId, Exact, ExactNumber, ExecutionAmounts};
        use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
        let mut sent = sent("canonical", StrategyId(1));
        let WalRecord::OrderSent { request, .. } = &mut sent else {
            unreachable!()
        };
        ExactOrderTerms {
            quantity: Exact::parse_decimal(total).unwrap(),
            limit_price: Some(Exact::from_i64(100)),
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: OrderInputPolicy::StrategyShortestDecimal,
        }
        .apply_projection(request)
        .unwrap();
        let fill = WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                client_order_id: "canonical".into(),
                exec_id: "canonical-part".into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: part.parse().unwrap(),
                px: 100.0,
                fee: None,
                is_maker: true,
                forced_close: None,
                venue_ts_ms: 1,
                recv_ns: 2,
                allocation: None,
                amounts: Some(Box::new(ExecutionAmounts {
                    quantity: ExactNumber::venue_decimal(part).unwrap(),
                    price: ExactNumber::venue_decimal("100").unwrap(),
                    fee: None,
                    settlement_asset: AssetId::Unknown,
                })),
            },
        };
        let orders = LedgerOfOrders::try_from_records(&[sent, fill]).unwrap();
        let mut registry = OrderRegistry::default();
        registry.own("canonical", StrategyId(1));
        books_over(MarketState::default(), orders, registry)
    }

    #[tokio::test(start_paused = true)]
    async fn canonical_partial_remainder_survives_live_context_rotation_and_callback_json() {
        use engine_types::strategy_process::{CallbackSnapshot, SnapshotCtx};
        let (engine, _) = crate::tests::lifecycle_test_fixture(vec![]).await;
        for (total, part, expected) in [
            ("0.3", "0.1", 0.2),
            ("0.1", "0.09999999999999999999", 1e-20),
        ] {
            let mut books = exact_partial_books(total, part);
            let mut base = engine.rotation_base(7);
            let row = &books.orders.orders["canonical"];
            if let WalRecord::SegmentBase { open_orders, .. } = &mut base {
                open_orders.push(engine_types::OpenOrderState {
                    request: row.request.clone(),
                    wire_ns: row.wire_ns,
                    arrival_mid: row.arrival_mid,
                    acked: row.acked,
                    filled_qty: row.filled_qty,
                    fill_quantity: Some(row.fill_quantity.clone()),
                    reservation_low_px: row.reservation_low_px,
                    reservation_high_px: row.reservation_high_px,
                    exact_price_range: Some(row.exact_price_range.clone()),
                    terminal: None,
                });
            }
            let base: WalRecord =
                serde_json::from_slice(&serde_json::to_vec(&base).unwrap()).unwrap();
            books.orders = LedgerOfOrders::try_from_records(&[base]).unwrap();
            let mut out = VecDeque::new();
            let mut timers = Timers::default();
            let ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(1));
            let mut resting = Vec::new();
            ctx.resting(&mut resting);
            assert_eq!(resting.len(), 1);
            assert_eq!(
                resting[0].remaining_qty(),
                expected,
                "live context re-subtracted compatibility scalars after exact rotation"
            );
            if expected == 0.2 {
                let terms = engine_types::order_terms::quantize_order(
                    &crate::tests::shared_sleeves::spec(),
                    Side::Buy,
                    resting[0].remaining_qty(),
                    OrderKind::Market,
                    None,
                    Some(100.0),
                    engine_types::order_terms::QuantityPolicy::Normal,
                )
                .unwrap();
                assert_eq!(terms.quantity, engine_types::numeric::Exact::parse_decimal("0.2").unwrap(), "the strategy must not lose one legal lot by re-quantizing its canonical remaining quantity");
            }
            let snapshot: CallbackSnapshot = serde_json::from_slice(
                &serde_json::to_vec(&ctx.callback_snapshot().unwrap()).unwrap(),
            )
            .unwrap();
            let callback = SnapshotCtx::new(&snapshot, |_| {}).unwrap();
            let mut resting = Vec::new();
            callback.resting(&mut resting);
            assert_eq!(
                resting[0].remaining_qty(),
                expected,
                "serialized worker callback changed the canonical remainder"
            );
        }
    }

    #[test]
    fn callback_order_facts_preserve_the_sleeve_role_when_physical_role_differs() {
        let mut books = exact_partial_books("0.3", "0.1");
        let request = &mut books.orders.orders.get_mut("canonical").unwrap().request;
        request.sleeve_effect = Some(engine_types::orders::SleeveOrderEffect::Reduce);
        request.reduce_only = false;
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(1));
        assert!(ctx.order_facts("canonical").unwrap().reduce_only);
        let snapshot = ctx.callback_snapshot().unwrap();
        let callback = engine_types::strategy_process::SnapshotCtx::new(&snapshot, |_| {}).unwrap();
        assert!(
            callback.order_facts("canonical").unwrap().reduce_only,
            "the worker was shown the venue role instead of its own sleeve's reduction"
        );
    }

    #[test]
    fn resting_shows_a_strategy_its_own_working_orders_and_nobody_elses() {
        // A strategy that could read another's book could cancel it too.
        let market = MarketState::default();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::from_records(&[
            sent("mine-open", StrategyId(1)),
            sent("theirs", StrategyId(2)),
            sent("mine-filled", StrategyId(1)),
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Fill {
                    allocation: None,
                    amounts: None,
                    exec_id: String::new(),
                    client_order_id: "mine-filled".into(),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 100.0,
                    fee: Some(0.0),
                    is_maker: false,
                    forced_close: None,
                    venue_ts_ms: 0,
                    recv_ns: 0,
                },
            },
        ]);
        // The filled one stays owned: ownership outlives the order, so this
        // is the in-flight filter being tested and not a missing owner.
        let mut registry = OrderRegistry::new("eng-1-".into());
        registry.own("mine-open", StrategyId(1));
        registry.own("mine-filled", StrategyId(1));
        registry.own("theirs", StrategyId(2));

        let mut out = VecDeque::new();
        let books = books_over(market, orders, registry);
        let ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(1));
        let mut seen = Vec::new();
        ctx.resting(&mut seen);
        let ids: Vec<&str> = seen.iter().map(|o| o.client_order_id).collect();
        assert_eq!(
            ids,
            vec!["mine-open"],
            "own and still working, nothing else"
        );
        assert_eq!(seen[0].px(), Some(100.0));
        assert_eq!(seen[0].remaining_qty(), 1.0);
        assert!(!seen[0].acked, "no ack has arrived for it");

        let mut out = VecDeque::new();
        let ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(2));
        let mut seen = Vec::new();
        ctx.resting(&mut seen);
        let ids: Vec<&str> = seen.iter().map(|o| o.client_order_id).collect();
        assert_eq!(ids, vec!["theirs"]);
    }

    const RULE: InstrumentRule = InstrumentRule {
        tick_size: 0.5,
        qty_step: 0.001,
        min_qty: 0.001,
        min_notional: 5.0,
    };

    fn holding(symbol: SymbolId, side: Side, qty: f64) -> PositionView {
        PositionView {
            exact_amounts: None,
            exact_stop_px: None,
            symbol,
            side,
            qty,
            entry_px: 100.0,
            stop_attached: true,
            stop_px: 0.0,
            leverage: None,
        }
    }

    #[test]
    fn a_strategy_reads_the_position_the_rule_and_the_wall_clock_the_engine_holds() {
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let account = AccountView {
            exact_amounts: None,
            positions: vec![holding(SymbolId(1), Side::Sell, 3.0)],
            ..flat_account()
        };
        let rules = [None, Some(RULE)];
        let attribution = Attribution::default();
        let covers = CoverBook::default();
        let checkpoints = std::collections::BTreeMap::new();
        let global_checkpoints = std::collections::BTreeMap::new();
        let strategy_events = std::collections::BTreeMap::new();
        let strategy_names = Vec::new();
        let books = Books {
            market,
            account,
            rules: rules.to_vec(),
            portfolio_symbols: BTreeSet::new(),
            orders,
            registry,
            attribution,
            covers,
        };
        let ctx = Ctx {
            books: &books,
            now_ns: 42,
            strategy: StrategyId(0),
            out: &mut out,
            timers: &mut timers,
            checkpoints: &checkpoints,
            global_checkpoints: &global_checkpoints,
            strategy_events: &strategy_events,
            strategy_names: &strategy_names,
            runtime_entries_enabled: None,
        };

        assert_eq!(
            ctx.position(SymbolId(1)),
            Some(holding(SymbolId(1), Side::Sell, 3.0))
        );
        assert_eq!(
            ctx.position(SymbolId(0)),
            None,
            "nothing is held in that one"
        );
        assert_eq!(ctx.instrument(SymbolId(1)), Some(RULE));
        assert_eq!(
            ctx.instrument(SymbolId(0)),
            None,
            "the venue named no rule for it"
        );
        assert_eq!(
            ctx.instrument(SymbolId(7)),
            None,
            "past the end of the table"
        );

        // Wall time, not the monotonic stamp: a book's validity window can
        // only be judged against a clock of the same kind.
        let wall = ctx.wall_ms();
        let now = crate::clock::wall_ms();
        assert!(
            wall > 1_600_000_000_000,
            "expected a unix millisecond stamp, got {wall}"
        );
        assert!(
            (now - wall).abs() < 60_000,
            "the ctx clock is the engine's: {wall} vs {now}"
        );
        assert_ne!(
            wall as u64,
            ctx.now_ns(),
            "the two clocks are not the same clock"
        );
    }

    #[test]
    fn typed_portfolio_context_distinguishes_another_sleeve_from_foreign_inventory() {
        let mut books = books_over(
            MarketState::default(),
            LedgerOfOrders::default(),
            OrderRegistry::default(),
        );
        books.portfolio_symbols.insert(SymbolId(0));
        books
            .attribution
            .note(StrategyId(1), SymbolId(0), Side::Buy, 1.0);
        let mut actions = VecDeque::new();
        let mut timers = Timers::default();
        let ctx = ctx_over(&books, &mut actions, &mut timers, StrategyId(0));
        assert!(
            !ctx.foreign_position(SymbolId(0)),
            "a known sleeve must not suppress another sleeve's decisions"
        );
        assert_eq!(ctx.my_position(SymbolId(0)), 0.0);
        books.portfolio_symbols.clear();
        let ctx = ctx_over(&books, &mut actions, &mut timers, StrategyId(0));
        assert!(
            ctx.foreign_position(SymbolId(0)),
            "legacy exclusive instruments retain their ownership contract"
        );
    }

    #[test]
    fn a_flat_row_in_the_account_reading_is_not_a_position() {
        // A venue that reports a symbol it once held with size zero must not
        // read as something to exit: the exit would be an order for nothing.
        let market = MarketState::default();
        let mut out = VecDeque::new();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::default();
        let registry = OrderRegistry::default();
        let account = AccountView {
            exact_amounts: None,
            positions: vec![holding(SymbolId(0), Side::Buy, 0.0)],
            ..flat_account()
        };
        let attribution = Attribution::default();
        let covers = CoverBook::default();
        let checkpoints = std::collections::BTreeMap::new();
        let global_checkpoints = std::collections::BTreeMap::new();
        let strategy_events = std::collections::BTreeMap::new();
        let strategy_names = Vec::new();
        let books = Books {
            market,
            account,
            rules: Vec::new(),
            portfolio_symbols: BTreeSet::new(),
            orders,
            registry,
            attribution,
            covers,
        };
        let ctx = Ctx {
            books: &books,
            now_ns: 42,
            strategy: StrategyId(0),
            out: &mut out,
            timers: &mut timers,
            checkpoints: &checkpoints,
            global_checkpoints: &global_checkpoints,
            strategy_events: &strategy_events,
            strategy_names: &strategy_names,
            runtime_entries_enabled: None,
        };
        assert_eq!(ctx.position(SymbolId(0)), None);
    }

    #[test]
    fn resting_appends_and_leaves_the_buffer_reusable() {
        // The contract is "appended to out", so a strategy can keep one
        // buffer between wakes and pay nothing for the read.
        let market = MarketState::default();
        let mut timers = Timers::default();
        let orders = LedgerOfOrders::from_records(&[sent("a", StrategyId(0))]);
        let mut registry = OrderRegistry::default();
        registry.own("a", StrategyId(0));
        let mut out = VecDeque::new();
        let books = books_over(market, orders, registry);
        let ctx = ctx_over(&books, &mut out, &mut timers, StrategyId(0));
        let mut seen = Vec::with_capacity(8);
        ctx.resting(&mut seen);
        ctx.resting(&mut seen);
        assert_eq!(seen.len(), 2, "the second read appended, it did not clear");
        seen.clear();
        ctx.resting(&mut seen);
        assert_eq!(seen.len(), 1);
    }
}
