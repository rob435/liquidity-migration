//! The process boundary between a strategy and the authoritative engine.

use serde::{Deserialize, Serialize};

use crate::{
    Action, BookLevel, Depth, EngineEvent, InstrumentRule, MarketEvent, OrderFacts, OrderKind,
    OrderUpdate, PositionView, Quote, RestingOrder, Side, SignalObservation,
    StrategyAccountSummary, StrategyCheckpoint, StrategyCtx, StrategyEvent, StrategyId,
    StrategyPositionFacts, SymbolId, Ticker, TimerId, TradeFlow,
};

pub const STRATEGY_PROCESS_SCHEMA: u16 = 1;
pub const MAX_PROCESS_FRAME_BYTES: usize = 64 * 1024;
pub const MAX_PROCESS_STATE_BYTES: usize = crate::strategy::MAX_STRATEGY_STATE_BYTES;
pub const MAX_PROCESS_PROPOSAL_BYTES: usize = 64 * 1024 * 1024;
// Registered plugs use at most three timer IDs; ownership includes unrearmed deadlines.
pub const MAX_PROCESS_TIMERS: usize = 256;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyRuntimeState {
    pub schema_version: u16,
    pub kind: String,
    pub configuration_sha256: String,
    pub payload: Vec<u8>,
}

impl StrategyRuntimeState {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != STRATEGY_PROCESS_SCHEMA {
            return Err("unsupported strategy process state schema".into());
        }
        if self.kind.is_empty() || self.kind.len() > 128 {
            return Err("strategy process kind must contain 1..=128 bytes".into());
        }
        if self.configuration_sha256.len() != 64
            || !self
                .configuration_sha256
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit())
        {
            return Err("strategy process configuration hash is invalid".into());
        }
        if self.payload.len() > MAX_PROCESS_STATE_BYTES {
            return Err("strategy process state exceeds its byte limit".into());
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyTimerState {
    pub id: TimerId,
    pub deadline_ns: u64,
    pub deadline_wall_ms: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyProcessState {
    pub strategy: StrategyId,
    pub last_callback_id: u64,
    pub runtime: StrategyRuntimeState,
    pub timers: Vec<StrategyTimerState>,
    #[serde(default)]
    pub retained_signal_subscriptions: Option<Vec<crate::Subscription>>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct DepthSnapshot {
    pub bids: Vec<BookLevel>,
    pub asks: Vec<BookLevel>,
    pub update_id: u64,
    pub seq: u64,
    pub venue_ts_ms: i64,
    pub recv_ns: u64,
}

impl From<&Depth> for DepthSnapshot {
    fn from(depth: &Depth) -> Self {
        Self {
            bids: depth.bids[..depth.bid_len as usize].to_vec(),
            asks: depth.asks[..depth.ask_len as usize].to_vec(),
            update_id: depth.update_id,
            seq: depth.seq,
            venue_ts_ms: depth.venue_ts_ms,
            recv_ns: depth.recv_ns,
        }
    }
}

impl TryFrom<&DepthSnapshot> for Depth {
    type Error = String;

    fn try_from(wire: &DepthSnapshot) -> Result<Self, String> {
        if wire.bids.len() > crate::BOOK_DEPTH || wire.asks.len() > crate::BOOK_DEPTH {
            return Err("strategy depth snapshot exceeds L50".into());
        }
        let mut result = Self {
            bid_len: wire.bids.len() as u8,
            ask_len: wire.asks.len() as u8,
            update_id: wire.update_id,
            seq: wire.seq,
            venue_ts_ms: wire.venue_ts_ms,
            recv_ns: wire.recv_ns,
            ..Self::default()
        };
        result.bids[..wire.bids.len()].copy_from_slice(&wire.bids);
        result.asks[..wire.asks.len()].copy_from_slice(&wire.asks);
        Ok(result)
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CallbackEvent {
    Boot,
    Quote {
        symbol: SymbolId,
        quote: Quote,
    },
    Depth {
        symbol: SymbolId,
        depth: DepthSnapshot,
    },
    Trades {
        symbol: SymbolId,
        trades: TradeFlow,
    },
    Ticker {
        symbol: SymbolId,
        ticker: Ticker,
    },
    FeedReset {
        recv_ns: u64,
    },
    Timer {
        id: TimerId,
        now_ns: u64,
    },
    Order {
        update: OrderUpdate,
    },
    Signal {
        observation: SignalObservation,
    },
    StrategyEvent {
        event: StrategyEvent,
    },
    IntentRefused {
        symbol: SymbolId,
        reduce_only: bool,
        reason: String,
    },
    EntryPermission {
        request_id: String,
        entries_enabled: bool,
    },
    FlattenDirectional {
        request_id: String,
    },
}

impl From<&EngineEvent> for CallbackEvent {
    fn from(event: &EngineEvent) -> Self {
        match event {
            EngineEvent::Boot => Self::Boot,
            EngineEvent::Market(MarketEvent::Quote { symbol, quote }) => Self::Quote {
                symbol: *symbol,
                quote: *quote,
            },
            EngineEvent::Market(MarketEvent::Depth { symbol, depth }) => Self::Depth {
                symbol: *symbol,
                depth: depth.into(),
            },
            EngineEvent::Market(MarketEvent::Trades { symbol, trades }) => Self::Trades {
                symbol: *symbol,
                trades: *trades,
            },
            EngineEvent::Market(MarketEvent::Ticker { symbol, ticker }) => Self::Ticker {
                symbol: *symbol,
                ticker: *ticker,
            },
            EngineEvent::Market(MarketEvent::FeedReset { recv_ns }) => {
                Self::FeedReset { recv_ns: *recv_ns }
            }
            EngineEvent::Timer { id, now_ns } => Self::Timer {
                id: *id,
                now_ns: *now_ns,
            },
            EngineEvent::Order(update) => Self::Order {
                update: update.clone(),
            },
            EngineEvent::Signal(observation) => Self::Signal {
                observation: observation.clone(),
            },
            EngineEvent::StrategyEvent(event) => Self::StrategyEvent {
                event: event.clone(),
            },
            EngineEvent::IntentRefused {
                symbol,
                reduce_only,
                reason,
            } => Self::IntentRefused {
                symbol: *symbol,
                reduce_only: *reduce_only,
                reason: reason.clone(),
            },
            EngineEvent::EntryPermission {
                request_id,
                entries_enabled,
            } => Self::EntryPermission {
                request_id: request_id.clone(),
                entries_enabled: *entries_enabled,
            },
            EngineEvent::FlattenDirectional { request_id } => Self::FlattenDirectional {
                request_id: request_id.clone(),
            },
        }
    }
}

impl TryFrom<&CallbackEvent> for EngineEvent {
    type Error = String;

    fn try_from(event: &CallbackEvent) -> Result<Self, String> {
        Ok(match event {
            CallbackEvent::Boot => Self::Boot,
            CallbackEvent::Quote { symbol, quote } => Self::Market(MarketEvent::Quote {
                symbol: *symbol,
                quote: *quote,
            }),
            CallbackEvent::Depth { symbol, depth } => Self::Market(MarketEvent::Depth {
                symbol: *symbol,
                depth: depth.try_into()?,
            }),
            CallbackEvent::Trades { symbol, trades } => Self::Market(MarketEvent::Trades {
                symbol: *symbol,
                trades: *trades,
            }),
            CallbackEvent::Ticker { symbol, ticker } => Self::Market(MarketEvent::Ticker {
                symbol: *symbol,
                ticker: *ticker,
            }),
            CallbackEvent::FeedReset { recv_ns } => {
                Self::Market(MarketEvent::FeedReset { recv_ns: *recv_ns })
            }
            CallbackEvent::Timer { id, now_ns } => Self::Timer {
                id: *id,
                now_ns: *now_ns,
            },
            CallbackEvent::Order { update } => Self::Order(update.clone()),
            CallbackEvent::Signal { observation } => Self::Signal(observation.clone()),
            CallbackEvent::StrategyEvent { event } => Self::StrategyEvent(event.clone()),
            CallbackEvent::IntentRefused {
                symbol,
                reduce_only,
                reason,
            } => Self::IntentRefused {
                symbol: *symbol,
                reduce_only: *reduce_only,
                reason: reason.clone(),
            },
            CallbackEvent::EntryPermission {
                request_id,
                entries_enabled,
            } => Self::EntryPermission {
                request_id: request_id.clone(),
                entries_enabled: *entries_enabled,
            },
            CallbackEvent::FlattenDirectional { request_id } => Self::FlattenDirectional {
                request_id: request_id.clone(),
            },
        })
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SymbolSnapshot {
    pub id: SymbolId,
    pub name: String,
    pub quote: Quote,
    pub depth: DepthSnapshot,
    pub trades: TradeFlow,
    pub ticker: Ticker,
    pub instrument: Option<InstrumentRule>,
    pub position: Option<PositionView>,
    pub foreign_position: bool,
    pub my_position: f64,
    pub in_flight: f64,
    pub facts: Option<StrategyPositionFacts>,
    pub checkpoint: Option<StrategyCheckpoint>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OwnedOrderSnapshot {
    pub id: String,
    pub symbol: SymbolId,
    pub side: Side,
    pub kind: OrderKind,
    pub qty: f64,
    pub filled_qty: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub remaining_qty: Option<f64>,
    pub reduce_only: bool,
    pub acked: bool,
    pub resting: bool,
}

impl OwnedOrderSnapshot {
    pub fn facts(&self) -> OrderFacts {
        OrderFacts {
            symbol: self.symbol,
            side: self.side,
            qty: self.qty,
            filled_qty: self.filled_qty,
            remaining_qty: self.remaining_qty,
            reduce_only: self.reduce_only,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CallbackSnapshot {
    pub strategy: StrategyId,
    pub now_ns: u64,
    pub wall_ms: i64,
    pub entries_enabled: bool,
    pub account: StrategyAccountSummary,
    pub symbols: Vec<SymbolSnapshot>,
    pub orders: Vec<OwnedOrderSnapshot>,
    pub global_checkpoint: Option<StrategyCheckpoint>,
    pub strategy_names: Vec<String>,
    pub strategy_events: Vec<StrategyEvent>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct CallbackRequest {
    pub schema_version: u16,
    pub callback_id: u64,
    pub state: StrategyRuntimeState,
    pub event: CallbackEvent,
    pub snapshot: CallbackSnapshot,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "phase", rename_all = "snake_case")]
pub enum CallbackPreparation {
    Queued,
    Prepared { snapshot: CallbackSnapshot },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StrategyCallbackInput {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub order_origin: Option<CallbackOrderOrigin>,
    pub callback_id: u64,
    pub strategy: StrategyId,
    pub event: CallbackEvent,
    pub preparation: CallbackPreparation,
}

impl StrategyCallbackInput {
    pub fn snapshot(&self) -> Option<&CallbackSnapshot> {
        match &self.preparation {
            CallbackPreparation::Queued => None,
            CallbackPreparation::Prepared { snapshot } => Some(snapshot),
        }
    }

    pub fn request(&self, state: StrategyRuntimeState) -> Result<CallbackRequest, String> {
        Ok(CallbackRequest {
            schema_version: STRATEGY_PROCESS_SCHEMA,
            callback_id: self.callback_id,
            state,
            event: self.event.clone(),
            snapshot: self
                .snapshot()
                .ok_or("strategy input has no prepared invocation")?
                .clone(),
        })
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum CallbackReply {
    Action {
        action: Action,
    },
    Timer {
        timer: StrategyTimerState,
    },
    Finished {
        callback_id: u64,
        state: StrategyRuntimeState,
        #[serde(default)]
        retained_signal_subscriptions: Option<Vec<crate::Subscription>>,
    },
    Aborted {
        callback_id: u64,
        reason: String,
    },
}

pub struct SnapshotCtx<'a, F> {
    snapshot: &'a CallbackSnapshot,
    depths: Vec<Depth>,
    emit: F,
    missing_quote: Quote,
    missing_depth: Depth,
    missing_trades: TradeFlow,
    missing_ticker: Ticker,
}

impl<'a, F: FnMut(CallbackReply)> SnapshotCtx<'a, F> {
    pub fn new(snapshot: &'a CallbackSnapshot, emit: F) -> Result<Self, String> {
        if snapshot.orders.iter().any(|row| {
            row.remaining_qty.is_some_and(|qty| {
                !qty.is_finite() || qty < 0.0 || !row.qty.is_finite() || qty > row.qty
            })
        }) {
            return Err("strategy order snapshot has an invalid canonical remainder".into());
        }
        let depths = snapshot
            .symbols
            .iter()
            .map(|row| (&row.depth).try_into())
            .collect::<Result<_, _>>()?;
        Ok(Self {
            snapshot,
            depths,
            emit,
            missing_quote: Quote::default(),
            missing_depth: Depth::default(),
            missing_trades: TradeFlow::default(),
            missing_ticker: Ticker::default(),
        })
    }

    fn symbol(&self, symbol: SymbolId) -> Option<&SymbolSnapshot> {
        self.snapshot
            .symbols
            .get(symbol.idx())
            .filter(|row| row.id == symbol)
    }
}

impl<F: FnMut(CallbackReply)> StrategyCtx for SnapshotCtx<'_, F> {
    fn quote(&self, symbol: SymbolId) -> &Quote {
        self.symbol(symbol)
            .map(|row| &row.quote)
            .unwrap_or(&self.missing_quote)
    }
    fn depth(&self, symbol: SymbolId) -> &Depth {
        self.depths.get(symbol.idx()).unwrap_or(&self.missing_depth)
    }
    fn trade_flow(&self, symbol: SymbolId) -> &TradeFlow {
        self.symbol(symbol)
            .map(|row| &row.trades)
            .unwrap_or(&self.missing_trades)
    }
    fn ticker(&self, symbol: SymbolId) -> &Ticker {
        self.symbol(symbol)
            .map(|row| &row.ticker)
            .unwrap_or(&self.missing_ticker)
    }
    fn symbol_id(&self, name: &str) -> Option<SymbolId> {
        self.snapshot
            .symbols
            .iter()
            .find(|row| row.name == name)
            .map(|row| row.id)
    }
    fn symbol_name(&self, symbol: SymbolId) -> Option<&str> {
        self.symbol(symbol).map(|row| row.name.as_str())
    }
    fn now_ns(&self) -> u64 {
        self.snapshot.now_ns
    }
    fn wall_ms(&self) -> i64 {
        self.snapshot.wall_ms
    }
    fn entries_enabled(&self, configured: bool) -> bool {
        configured && self.snapshot.entries_enabled
    }
    fn account_summary(&self) -> StrategyAccountSummary {
        self.snapshot.account
    }
    fn position(&self, symbol: SymbolId) -> Option<PositionView> {
        self.symbol(symbol).and_then(|row| row.position.clone())
    }
    fn foreign_position(&self, symbol: SymbolId) -> bool {
        self.symbol(symbol).is_some_and(|row| row.foreign_position)
    }
    fn my_position(&self, symbol: SymbolId) -> f64 {
        self.symbol(symbol).map_or(0.0, |row| row.my_position)
    }
    fn in_flight(&self, symbol: SymbolId) -> f64 {
        self.symbol(symbol).map_or(0.0, |row| row.in_flight)
    }
    fn my_position_facts(&self, symbol: SymbolId) -> Option<StrategyPositionFacts> {
        self.symbol(symbol).and_then(|row| row.facts.clone())
    }
    fn my_positions(&self, out: &mut Vec<StrategyPositionFacts>) {
        out.extend(
            self.snapshot
                .symbols
                .iter()
                .filter_map(|row| row.facts.clone()),
        );
    }
    fn my_position_names<'a>(&'a self, out: &mut Vec<&'a str>) {
        let start = out.len();
        out.extend(
            self.snapshot
                .symbols
                .iter()
                .filter(|row| row.my_position != 0.0)
                .map(|row| row.name.as_str()),
        );
        out[start..].sort_unstable();
    }
    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule> {
        self.symbol(symbol).and_then(|row| row.instrument)
    }
    fn emit(&mut self, action: Action) {
        (self.emit)(CallbackReply::Action { action });
    }
    fn arm_timer(&mut self, id: TimerId, after_ns: u64) {
        (self.emit)(CallbackReply::Timer {
            timer: StrategyTimerState {
                id,
                deadline_ns: self.snapshot.now_ns.saturating_add(after_ns),
                deadline_wall_ms: self.snapshot.wall_ms.saturating_add(
                    i64::try_from(after_ns.div_ceil(1_000_000)).unwrap_or(i64::MAX),
                ),
            },
        });
    }
    fn resting<'a>(&'a self, out: &mut Vec<RestingOrder<'a>>) {
        out.extend(
            self.snapshot
                .orders
                .iter()
                .filter(|row| row.resting)
                .map(|row| RestingOrder {
                    client_order_id: &row.id,
                    symbol: row.symbol,
                    side: row.side,
                    kind: row.kind,
                    qty: row.qty,
                    filled_qty: row.filled_qty,
                    remaining_qty: row.remaining_qty,
                    reduce_only: row.reduce_only,
                    acked: row.acked,
                }),
        );
    }
    fn order_facts(&self, id: &str) -> Option<OrderFacts> {
        self.snapshot
            .orders
            .iter()
            .find(|row| row.id == id)
            .map(OwnedOrderSnapshot::facts)
    }
    fn strategy_checkpoint(&self, symbol: SymbolId) -> Option<&StrategyCheckpoint> {
        self.symbol(symbol).and_then(|row| row.checkpoint.as_ref())
    }
    fn strategy_global_checkpoint(&self) -> Option<&StrategyCheckpoint> {
        self.snapshot.global_checkpoint.as_ref()
    }
    fn strategy_id(&self, name: &str) -> Option<StrategyId> {
        self.snapshot
            .strategy_names
            .iter()
            .position(|known| known == name)
            .and_then(|index| u16::try_from(index).ok())
            .map(StrategyId)
    }
    fn strategy_events(&self, out: &mut Vec<StrategyEvent>) {
        out.extend(self.snapshot.strategy_events.iter().cloned());
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackOrderOrigin {
    pub segment: u64,
    pub sequence: u64,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackWalCursor {
    pub segment: u64,
    pub sequence: u64,
    pub offset: u64,
}

pub struct CallbackWalRecord {
    pub cursor: CallbackWalCursor,
    pub next: CallbackWalCursor,
    pub source: Option<(Vec<StrategyId>, CallbackEvent)>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackQueueSlot {
    pub callback_id: u64,
    pub strategy: StrategyId,
    pub queued: CallbackWalCursor,
    pub prepared: Option<CallbackWalCursor>,
    pub event_sha256: [u8; 32],
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CallbackSourceFrontier {
    pub strategy: StrategyId,
    pub cursor: CallbackWalCursor,
    pub accepted: Option<CallbackOrderOrigin>,
    pub latest: CallbackOrderOrigin,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalCallbackDelivery {
    pub strategy: StrategyId,
    pub source: String,
    pub sequence: u64,
    pub observation_id: String,
}

pub trait CallbackWalReader: Send {
    fn start(&self) -> CallbackWalCursor;
    fn read_callback(
        &mut self,
        cursor: CallbackWalCursor,
        callback_id: u64,
    ) -> Result<StrategyCallbackInput, crate::WalError> {
        let _ = callback_id;
        Err(crate::WalError::Corrupt {
            offset: cursor.offset,
            detail: "WAL does not support callback paging".into(),
        })
    }
    fn next(
        &mut self,
        cursor: CallbackWalCursor,
    ) -> Result<Option<CallbackWalRecord>, crate::WalError>;
}
