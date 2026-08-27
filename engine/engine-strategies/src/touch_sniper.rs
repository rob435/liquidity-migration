//! `touch_sniper`: buy (or sell) the moment the price touches a level, hold,
//! and leave on a profit level or a timer.
//!
//! This is the latency-relevant part of the fleet's LONG sleeve reduced to
//! its essence. Everything slow — which symbol, which level, how big — is
//! decided by the research system and arrives as config. What is left here
//! is the part that must happen in microseconds: see the touch, send once.

use engine_types::{
    Feed, Intent, MarketEvent, OrderKind, OrderUpdate, Quote, Side, StopSpec, Strategy,
    StrategyCtx, StrategyId, Subscription, SymbolId, TimerId,
};

use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "touch_sniper";

/// The auto-exit timer. Timer ids are the strategy's own; the engine echoes
/// this one back when it expires.
const TTL_TIMER: TimerId = TimerId(1);

const ENTRY_TAG: &str = "touch-entry";
const EXIT_TAG: &str = "touch-exit";

#[derive(Copy, Clone, Debug, PartialEq, Eq)]
enum State {
    /// Watching for the level.
    Armed,
    /// Entry is with the venue. Never send a second one.
    EntrySent,
    /// Position open, watching for the profit level or the timer.
    Holding,
    /// Exit is with the venue.
    ExitSent,
    /// Nothing further to do at this layer.
    Done,
}

pub struct TouchSniper {
    id: StrategyId,
    symbol_name: String,
    symbol: Option<SymbolId>,
    side: Side,
    trigger_px: f64,
    qty: f64,
    stop_px: f64,
    take_px: Option<f64>,
    ttl_ns: Option<u64>,
    state: State,
    /// Venue id of our entry, learned from the first update about it.
    entry_order: Option<String>,
    exit_order: Option<String>,
    entry_filled_qty: f64,
    exit_filled_qty: f64,
    exit_working_until: f64,
    exit_rejects: u8,
    resend_exit_on_next_quote: bool,
}

impl TouchSniper {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&["symbol", "side", "trigger_px", "qty", "stop_px", "take_px", "ttl_s"])?;

        let symbol_name = p.string("symbol")?;
        if symbol_name.is_empty() {
            return Err(p.invalid("symbol", "expected a venue symbol, got an empty string"));
        }
        let side = match p.string("side")?.as_str() {
            "buy" => Side::Buy,
            "sell" => Side::Sell,
            other => {
                return Err(p.invalid("side", format!("expected \"buy\" or \"sell\", got \"{other}\"")))
            }
        };
        // An entry always carries a stop, so `stop_px` is required, not optional.
        let stop_px = p.positive("stop_px")?;
        let ttl_ns = match p.opt_u64("ttl_s")? {
            None => None,
            Some(0) => return Err(p.invalid("ttl_s", "expected a number of seconds above 0, got 0")),
            Some(s) => Some(s.saturating_mul(1_000_000_000)),
        };

        Ok(Self {
            id,
            symbol_name,
            symbol: None,
            side,
            trigger_px: p.positive("trigger_px")?,
            qty: p.positive("qty")?,
            stop_px,
            take_px: p.opt_positive("take_px")?,
            ttl_ns,
            state: State::Armed,
            entry_order: None,
            exit_order: None,
            entry_filled_qty: 0.0,
            exit_filled_qty: 0.0,
            exit_working_until: 0.0,
            exit_rejects: 0,
            resend_exit_on_next_quote: false,
        })
    }

    /// The engine interns every subscribed symbol at boot, so this resolves on
    /// the first event and is cached from then on.
    fn resolve(&mut self, ctx: &dyn StrategyCtx) -> Option<SymbolId> {
        if self.symbol.is_none() {
            self.symbol = ctx.symbol_id(&self.symbol_name);
        }
        self.symbol
    }

    /// The price we would pay has reached the level. An empty book side
    /// renders as price 0, which is not a price — never a touch.
    fn entry_touched(&self, quote: &Quote) -> bool {
        match self.side {
            Side::Buy => quote.ask_px > 0.0 && quote.ask_px <= self.trigger_px,
            Side::Sell => quote.bid_px >= self.trigger_px,
        }
    }

    /// The price we would get on the way out has reached the profit level.
    /// Same rule: an empty side is never a touch.
    fn take_touched(&self, quote: &Quote) -> bool {
        let Some(take_px) = self.take_px else {
            return false;
        };
        match self.side {
            Side::Buy => quote.bid_px >= take_px,
            Side::Sell => quote.ask_px > 0.0 && quote.ask_px <= take_px,
        }
    }

    fn send_entry(&mut self, symbol: SymbolId, ctx: &mut dyn StrategyCtx) {
        let decided_ns = ctx.now_ns();
        ctx.place(Intent {
            strategy: self.id,
            symbol,
            side: self.side,
            qty: self.qty,
            kind: OrderKind::Market,
            stop: Some(StopSpec { trigger_px: self.stop_px }),
            reduce_only: false,
            tag: ENTRY_TAG.to_string(),
            decided_ns,
            work: None,
            // This plug sizes in quantity, not margin, so it leaves the
            // symbol's leverage alone.
            leverage: None,
        });
        self.state = State::EntrySent;
    }

    fn send_exit(&mut self, symbol: SymbolId, ctx: &mut dyn StrategyCtx) {
        let decided_ns = ctx.now_ns();
        // Leave what we actually got filled. The configured size is only a
        // fallback for the case where the fill news has not reached us.
        let qty = (self.entry_filled_qty - self.exit_filled_qty).max(0.0);
        if qty <= self.qty.max(1.0) * 1e-12 { self.state = State::Done; return; }
        ctx.place(Intent {
            strategy: self.id,
            symbol,
            side: self.side.flipped(),
            qty,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: EXIT_TAG.to_string(),
            decided_ns,
            work: None,
            // This plug sizes in quantity, not margin, so it leaves the
            // symbol's leverage alone.
            leverage: None,
        });
        self.exit_order = None;
        self.exit_working_until = self.exit_filled_qty + qty;
        self.resend_exit_on_next_quote = false;
        self.state = State::ExitSent;
    }

    fn on_quote(&mut self, symbol: SymbolId, quote: &Quote, ctx: &mut dyn StrategyCtx) {
        match self.state {
            State::Armed => {
                if self.entry_touched(quote) {
                    self.send_entry(symbol, ctx);
                }
            }
            State::Holding => {
                if self.take_touched(quote) {
                    self.send_exit(symbol, ctx);
                }
            }
            State::ExitSent => {
                if self.resend_exit_on_next_quote {
                    self.send_exit(symbol, ctx);
                }
            }
            // Entry in flight: the level is already spoken for.
            State::EntrySent | State::Done => {}
        }
    }

    fn order_news(&mut self, update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        match self.state {
            State::EntrySent => match update {
                OrderUpdate::Ack(ack) if is_ours(&self.entry_order, &ack.client_order_id) => {
                    self.entry_order = Some(ack.client_order_id.clone());
                }
                OrderUpdate::Fill { client_order_id, qty, .. }
                    if is_ours(&self.entry_order, client_order_id) =>
                {
                    self.entry_order = Some(client_order_id.clone());
                    self.entry_filled_qty += qty;
                    self.state = State::Holding;
                    if let Some(ttl_ns) = self.ttl_ns {
                        ctx.arm_timer(TTL_TIMER, ttl_ns);
                    }
                }
                OrderUpdate::Reject { client_order_id, .. }
                    if is_ours(&self.entry_order, client_order_id) =>
                {
                    // Sending the same entry again at the same level is how you
                    // end up chasing a price down. Whether to re-arm is the
                    // research side's call, not this layer's.
                    self.state = State::Done;
                }
                _ => {}
            },
            State::Holding => {
                // Later pieces of the same entry still count toward exit size.
                if let OrderUpdate::Fill { client_order_id, qty, .. } = update {
                    if is_ours(&self.entry_order, client_order_id) {
                        self.entry_filled_qty += qty;
                    }
                }
            }
            State::ExitSent => match update {
                OrderUpdate::Ack(ack) if is_ours(&self.exit_order, &ack.client_order_id) => {
                    self.exit_order = Some(ack.client_order_id.clone());
                }
                OrderUpdate::Fill { client_order_id, qty, .. }
                    if is_ours(&self.entry_order, client_order_id) =>
                {
                    self.entry_filled_qty += qty;
                }
                OrderUpdate::Fill { client_order_id, qty, .. }
                    if is_ours(&self.exit_order, client_order_id) =>
                {
                    self.exit_filled_qty += qty;
                    let tolerance = self.qty.max(1.0) * 1e-12;
                    if self.entry_filled_qty - self.exit_filled_qty <= tolerance {
                        self.state = State::Done;
                    } else if self.exit_filled_qty + tolerance >= self.exit_working_until {
                        self.resend_exit_on_next_quote = true;
                    }
                }
                OrderUpdate::Reject { client_order_id, .. }
                    if is_ours(&self.exit_order, client_order_id) =>
                {
                    // An exit must not silently die, so try once more on the
                    // next quote. If the venue refuses twice the position needs
                    // a human, not another order from here.
                    self.exit_rejects += 1;
                    if self.exit_rejects >= 2 {
                        self.state = State::Done;
                    } else {
                        self.resend_exit_on_next_quote = true;
                    }
                }
                _ => {}
            },
            State::Done => {
                if let OrderUpdate::Fill { client_order_id, qty, .. } = update {
                    if is_ours(&self.entry_order, client_order_id) {
                        self.entry_filled_qty += qty;
                        if self.entry_filled_qty > self.exit_filled_qty + self.qty.max(1.0) * 1e-12 {
                            self.state = State::ExitSent;
                            self.resend_exit_on_next_quote = true;
                        }
                    }
                }
            }
            State::Armed => {}
        }
    }
}

impl Strategy for TouchSniper {
    fn name(&self) -> &str {
        NAME
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription { symbol: self.symbol_name.clone(), feed: Feed::Quote }]
    }

    // Only what this plug acts on is overridden: quotes, its own timer, and
    // news about its own orders. A target book is research changing what
    // carry holds, which says nothing about a level someone asked this one
    // to watch; a refused intent needs nothing unwound here, because this
    // plug keeps no sent-ahead cover and its own state advances on order
    // news. Both fall through to the trait's do-nothing defaults.

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        if self.state == State::Done {
            return;
        }
        let Some(symbol) = self.resolve(&*ctx) else {
            return;
        };
        match event {
            MarketEvent::Quote { symbol: from, quote } if *from == symbol => {
                self.on_quote(symbol, quote, ctx)
            }
            // Another symbol, a ticker, or a feed reset. A reset in particular
            // changes nothing: the levels are absolute prices, not something
            // derived from the stream that just dropped, and news about our
            // orders comes from the order feed, not this one.
            _ => {}
        }
    }

    fn on_timer(&mut self, id: TimerId, _now_ns: u64, ctx: &mut dyn StrategyCtx) {
        if self.state == State::Done {
            return;
        }
        let Some(symbol) = self.resolve(&*ctx) else {
            return;
        };
        if id == TTL_TIMER && self.state == State::Holding {
            self.send_exit(symbol, ctx);
        }
    }

    fn on_order(&mut self, update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        if self.resolve(&*ctx).is_none() {
            return;
        }
        self.order_news(update, ctx);
    }

    fn on_intent_refused(&mut self, symbol: SymbolId, reduce_only: bool, _reason: &str, ctx: &mut dyn StrategyCtx) {
        if self.resolve(&*ctx) != Some(symbol) { return; }
        if !reduce_only && self.state == State::EntrySent {
            self.state = State::Done;
        } else if reduce_only && self.state == State::ExitSent {
            self.exit_rejects += 1;
            if self.exit_rejects >= 2 { self.state = State::Done; }
            else { self.resend_exit_on_next_quote = true; }
        }
    }
}

/// One order of ours is with the venue at a time. Until an update teaches us
/// its id, whatever the engine routes here belongs to that order.
fn is_ours(known: &Option<String>, client_order_id: &str) -> bool {
    match known {
        Some(ours) => ours == client_order_id,
        None => true,
    }
}

#[cfg(test)]
mod tests;
