use std::collections::{BTreeSet, VecDeque};

use engine_types::{EngineEvent, MarketEvent, MarketState, StrategyId, SymbolId};

#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum MarketSlot {
    Quote(SymbolId),
    Depth(SymbolId),
    Trades(SymbolId),
    Ticker(SymbolId),
    Reset,
}

impl MarketSlot {
    fn from_event(event: &MarketEvent) -> Self {
        match event {
            MarketEvent::Quote { symbol, .. } => Self::Quote(*symbol),
            MarketEvent::Depth { symbol, .. } => Self::Depth(*symbol),
            MarketEvent::Trades { symbol, .. } => Self::Trades(*symbol),
            MarketEvent::Ticker { symbol, .. } => Self::Ticker(*symbol),
            MarketEvent::FeedReset { .. } => Self::Reset,
        }
    }

    pub fn latest(self, market: &MarketState, reset_ns: u64) -> MarketEvent {
        match self {
            Self::Quote(symbol) => MarketEvent::Quote {
                symbol,
                quote: *market.quote(symbol),
            },
            Self::Depth(symbol) => MarketEvent::Depth {
                symbol,
                depth: *market.depth(symbol),
            },
            Self::Trades(symbol) => MarketEvent::Trades {
                symbol,
                trades: *market.trade_flow(symbol),
            },
            Self::Ticker(symbol) => MarketEvent::Ticker {
                symbol,
                ticker: *market.ticker(symbol),
            },
            Self::Reset => MarketEvent::FeedReset { recv_ns: reset_ns },
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DurableRetry {
    StrategyEvent {
        source: StrategyId,
        destination: StrategyId,
        event_id: String,
    },
    Control {
        strategy: StrategyId,
        request_id: String,
    },
}

impl DurableRetry {
    pub fn owner(&self) -> StrategyId {
        match self {
            Self::StrategyEvent { destination, .. } => *destination,
            Self::Control { strategy, .. } => *strategy,
        }
    }
    fn from_event(strategy: StrategyId, event: &EngineEvent) -> Option<Self> {
        match event {
            EngineEvent::StrategyEvent(event) => Some(Self::StrategyEvent {
                source: event.source,
                destination: event.destination,
                event_id: event.event_id.clone(),
            }),
            EngineEvent::EntryPermission { request_id, .. }
            | EngineEvent::FlattenDirectional { request_id } => Some(Self::Control {
                strategy,
                request_id: request_id.clone(),
            }),
            _ => None,
        }
    }
}

#[derive(Default)]
pub struct RetryInputs {
    pub market: BTreeSet<(StrategyId, MarketSlot)>,
    pub reset_ns: u64,
    pub durable: VecDeque<DurableRetry>,
}

impl RetryInputs {
    pub fn blocks(&self, strategy: StrategyId, event: &EngineEvent) -> bool {
        self.durable
            .iter()
            .find(|pending| pending.owner() == strategy)
            .is_some_and(|first| DurableRetry::from_event(strategy, event).as_ref() != Some(first))
    }

    pub fn remember(&mut self, strategy: StrategyId, event: &EngineEvent) {
        if let EngineEvent::Market(event) = event {
            if let MarketEvent::FeedReset { recv_ns } = event {
                self.reset_ns = self.reset_ns.max(*recv_ns);
            }
            self.market
                .insert((strategy, MarketSlot::from_event(event)));
        }
        if let Some(key) = DurableRetry::from_event(strategy, event) {
            if !self.durable.contains(&key) {
                self.durable.push_back(key);
            }
        }
    }

    pub fn forget(&mut self, strategy: StrategyId, event: &EngineEvent) {
        if let EngineEvent::Market(event) = event {
            self.market
                .remove(&(strategy, MarketSlot::from_event(event)));
        }
        if let Some(key) = DurableRetry::from_event(strategy, event) {
            self.durable.retain(|pending| pending != &key);
        }
    }
}
