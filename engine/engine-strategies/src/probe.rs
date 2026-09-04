//! `probe`: one venue-minimum post-only order well away from the touch, on a
//! wall-clock schedule, pulled as soon as it has rested.
//!
//! The order path — `decide`, `durable`, `wire`, `ack`, `end_to_end` — is only
//! measured when an order goes out, and the sleeves can go a day without one.
//! This plug keeps the measurement coming. Every `every_s`, on a wall-clock
//! boundary, it rests one buy `offset_bps` under the bid at the venue minimum
//! and cancels it `rest_ms` later. Post-only and that far from the touch, the
//! order is not meant to fill; if it does, the inventory is closed at market
//! at once and the heartbeat shows the sleeve blocked until it is flat. It
//! never raises a strategy error: the watchdog pages on those, and a probe
//! must never page anybody.
//!
//! Wall-clock boundaries rather than "every N seconds since boot", so the
//! minute sampler that reads the heartbeat's 60-second latency window at :20
//! sees every probe.
//!
//! It stands aside for the sleeves. A Bybit entry carries `stopLoss` with
//! `tpslMode: Full`, so the stop it names belongs to the whole position on
//! that symbol — and this probe's stop is deliberately far away. Placing on a
//! symbol a sleeve is holding would put that sleeve's position behind the
//! probe's stop instead of its own, so a symbol with a foreign position is
//! skipped until it is flat. The probe's own symbol is in LONG's universe by
//! design: BTCUSDT is the venue's most liquid book and therefore the honest
//! benchmark, and a paused measurement is worth more than a moved stop.
//!
//! It is not a strategy and cannot make money. It costs the venue two
//! requests per probe and no fee.

use engine_types::quantize::{quantize_px, round_clean};
use engine_types::{
    EngineEvent, Feed, InstrumentRule, Intent, OrderKind, OrderUpdate, RestingOrder, Side,
    StopSpec, Strategy, StrategyCtx, StrategyId, Subscription, SymbolId, TimeInForce, TimerId,
};

use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "probe";
pub const TAG: &str = "probe";
pub const DRAIN_TAG: &str = "probe-drain";

pub const FIRE: TimerId = TimerId(1);
pub const PULL: TimerId = TimerId(2);
const DRAIN_RETRY: TimerId = TimerId(3);

const DRAIN_RETRY_AFTER_NS: u64 = 1_000_000_000;
/// A boundary this close is skipped for the next one: the timer would fire
/// into the same engine wake that armed it.
const MIN_LEAD_MS: i64 = 1_000;
/// The venue prices its minimum at its own mark; the order sits under the bid.
const MIN_NOTIONAL_CUSHION: f64 = 1.02;
const FLAT_EPS: f64 = 1e-12;
/// The blocker shown while a filled probe is being closed.
const CLOSING: &str = "closing a filled probe";

const KNOWN_PARAMS: &[&str] = &[
    "symbol",
    "every_s",
    "rest_ms",
    "offset_bps",
    "notional_usdt",
    "stop_loss_fraction",
    "max_quote_age_ms",
    "enabled",
];

pub struct Probe {
    id: StrategyId,
    symbol_name: String,
    symbol: Option<SymbolId>,
    enabled: bool,
    every_ms: i64,
    rest_ns: u64,
    offset: f64,
    notional_usdt: f64,
    stop_fraction: f64,
    max_quote_age_ns: u64,
    /// Since boot, for the log.
    fired: u64,
    refused: u64,
    filled: u64,
    /// Why the last boundary placed nothing, for the heartbeat's blockers.
    skipped: Option<String>,
    draining: bool,
}

impl Probe {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(KNOWN_PARAMS)?;
        let symbol_name = p.string("symbol")?;
        if symbol_name.is_empty() {
            return Err(p.invalid("symbol", "expected a venue symbol, got an empty string"));
        }
        let every_s = p.positive("every_s")?;
        if every_s < 60.0 {
            return Err(p.invalid(
                "every_s",
                "expected at least 60; a probe more often than once a minute is load, not a measurement",
            ));
        }
        let rest_ms = p.opt_positive("rest_ms")?.unwrap_or(2_000.0);
        if rest_ms >= every_s * 1_000.0 {
            return Err(p.invalid("rest_ms", "expected shorter than every_s"));
        }
        let offset_bps = p.opt_positive("offset_bps")?.unwrap_or(300.0);
        if offset_bps >= 10_000.0 {
            return Err(p.invalid(
                "offset_bps",
                "expected below 10000; a price at or under zero",
            ));
        }
        let notional_usdt = p.opt_positive("notional_usdt")?.unwrap_or(5.5);
        let stop_fraction = p.opt_positive("stop_loss_fraction")?.unwrap_or(0.08);
        if stop_fraction >= 1.0 {
            return Err(p.invalid(
                "stop_loss_fraction",
                "expected a fraction below 1; a stop at or past 100% is not a stop",
            ));
        }
        let max_quote_age_ms = p.opt_positive("max_quote_age_ms")?.unwrap_or(30_000.0);
        let enabled = p.bool_or("enabled", true)?;
        Ok(Self {
            id,
            symbol_name,
            symbol: None,
            enabled,
            every_ms: (every_s * 1_000.0) as i64,
            rest_ns: (rest_ms * 1_000_000.0) as u64,
            offset: offset_bps / 10_000.0,
            notional_usdt,
            stop_fraction,
            max_quote_age_ns: (max_quote_age_ms * 1_000_000.0) as u64,
            fired: 0,
            refused: 0,
            filled: 0,
            skipped: None,
            draining: false,
        })
    }

    fn symbol(&mut self, ctx: &dyn StrategyCtx) -> Option<SymbolId> {
        if self.symbol.is_none() {
            self.symbol = ctx.symbol_id(&self.symbol_name);
        }
        self.symbol
    }

    /// Milliseconds from `wall_ms` to the next boundary this probe fires on.
    pub fn lead_ms(&self, wall_ms: i64) -> i64 {
        let mut lead = self.every_ms - wall_ms.rem_euclid(self.every_ms);
        if lead < MIN_LEAD_MS {
            lead += self.every_ms;
        }
        lead
    }

    fn schedule(&self, ctx: &mut dyn StrategyCtx) {
        let lead = self.lead_ms(ctx.wall_ms());
        ctx.arm_timer(FIRE, (lead as u64).saturating_mul(1_000_000));
    }

    fn skip(&mut self, reason: impl Into<String>) {
        self.skipped = Some(reason.into());
    }

    fn fire(&mut self, now_ns: u64, ctx: &mut dyn StrategyCtx) {
        // Re-armed first: nothing below may cost the next measurement.
        self.schedule(ctx);
        if !ctx.entries_enabled(self.enabled) {
            self.skip("entries disabled");
            return;
        }
        let Some(symbol) = self.symbol(ctx) else {
            self.skip("symbol not interned");
            return;
        };
        if self.draining {
            self.skip(CLOSING);
            return;
        }
        if ctx.foreign_position(symbol) {
            // One venue stop per position, and this one's is far away: see the
            // module note. The sleeve that holds the symbol keeps it.
            self.skip("another sleeve holds this symbol");
            return;
        }
        if self.resting_probe(symbol, ctx).is_some() {
            // The pull is still in flight; asking again is another request.
            self.skip("previous probe still resting");
            return;
        }
        let quote = *ctx.quote(symbol);
        if quote.recv_ns == 0 || quote.bid_px <= 0.0 {
            self.skip("no quote");
            return;
        }
        if now_ns.saturating_sub(quote.recv_ns) > self.max_quote_age_ns {
            self.skip("quote stale");
            return;
        }
        let Some(rule) = ctx.instrument(symbol) else {
            self.skip("no instrument rule");
            return;
        };
        let px = quantize_px(quote.bid_px * (1.0 - self.offset), Side::Buy, &rule);
        if px <= 0.0 || px >= quote.bid_px {
            self.skip("price would not sit under the bid");
            return;
        }
        let Some(qty) = size(px, self.notional_usdt, &rule) else {
            self.skip("no size meets the venue minimum");
            return;
        };
        let trigger_px = round_clean(px * (1.0 - self.stop_fraction), rule.tick_size);
        if trigger_px <= 0.0 || trigger_px >= px {
            self.skip("stop would not sit under the price");
            return;
        }
        ctx.place(Intent {
            strategy: self.id,
            symbol,
            side: Side::Buy,
            qty,
            kind: OrderKind::Limit {
                px,
                tif: TimeInForce::PostOnly,
            },
            stop: Some(StopSpec { trigger_px }),
            reduce_only: false,
            tag: TAG.to_string(),
            decided_ns: now_ns,
            work: None,
            leverage: None,
        });
        ctx.arm_timer(PULL, self.rest_ns);
        self.fired += 1;
        self.skipped = None;
        tracing::debug!(
            symbol = %self.symbol_name,
            px,
            qty,
            fired = self.fired,
            "probe rested"
        );
    }

    fn resting_probe(&self, symbol: SymbolId, ctx: &dyn StrategyCtx) -> Option<String> {
        let mut resting: Vec<RestingOrder<'_>> = Vec::new();
        ctx.resting(&mut resting);
        resting
            .iter()
            .find(|order| order.symbol == symbol && !order.reduce_only)
            .map(|order| order.client_order_id.to_string())
    }

    fn pull(&mut self, ctx: &mut dyn StrategyCtx) {
        let Some(symbol) = self.symbol(ctx) else {
            return;
        };
        let mut resting: Vec<RestingOrder<'_>> = Vec::new();
        ctx.resting(&mut resting);
        let ids: Vec<String> = resting
            .iter()
            .filter(|order| order.symbol == symbol && !order.reduce_only)
            .map(|order| order.client_order_id.to_string())
            .collect();
        for id in ids {
            ctx.cancel(symbol, &id);
        }
    }

    /// Close whatever this sleeve holds, at market. Idempotent while one
    /// drain is working.
    fn drain(&mut self, ctx: &mut dyn StrategyCtx) {
        let Some(symbol) = self.symbol(ctx) else {
            return;
        };
        let held = ctx.my_position(symbol);
        if held.abs() <= FLAT_EPS {
            self.draining = false;
            if self
                .skipped
                .as_deref()
                .is_some_and(|reason| reason.starts_with(CLOSING))
            {
                self.skipped = None;
            }
            return;
        }
        if self.draining {
            return;
        }
        self.draining = true;
        self.skip(format!("{CLOSING} ({held} held)"));
        ctx.place(Intent {
            strategy: self.id,
            symbol,
            side: if held > 0.0 { Side::Sell } else { Side::Buy },
            qty: held.abs(),
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            tag: DRAIN_TAG.to_string(),
            decided_ns: ctx.now_ns(),
            work: None,
            leverage: None,
        });
    }

    fn retry_drain_later(&mut self, ctx: &mut dyn StrategyCtx) {
        self.draining = false;
        ctx.arm_timer(DRAIN_RETRY, DRAIN_RETRY_AFTER_NS);
    }
}

/// The smallest size the venue takes at `px`, at or above the wanted notional.
fn size(px: f64, notional_usdt: f64, rule: &InstrumentRule) -> Option<f64> {
    if px.is_nan()
        || px <= 0.0
        || rule.qty_step.is_nan()
        || rule.qty_step <= 0.0
        || !rule.min_qty.is_finite()
    {
        return None;
    }
    let want_usdt = notional_usdt.max(rule.min_notional * MIN_NOTIONAL_CUSHION);
    let mut steps = (want_usdt / px / rule.qty_step).ceil().max(1.0);
    for _ in 0..8 {
        let qty = round_clean(steps * rule.qty_step, rule.qty_step).max(rule.min_qty);
        if qty * px + 1e-9 >= rule.min_notional {
            return Some(qty);
        }
        steps += 1.0;
    }
    None
}

impl Strategy for Probe {
    fn name(&self) -> &str {
        NAME
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.symbol_name.clone(),
            feed: Feed::Quote,
        }]
    }

    fn configured_entries_enabled(&self) -> bool {
        self.enabled
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Boot => {
                self.symbol(ctx);
                self.schedule(ctx);
                // Inventory left by a fill before a restart is still ours.
                self.drain(ctx);
            }
            EngineEvent::Timer { id, now_ns } if *id == FIRE => self.fire(*now_ns, ctx),
            EngineEvent::Timer { id, .. } if *id == PULL => self.pull(ctx),
            EngineEvent::Timer { id, .. } if *id == DRAIN_RETRY => self.drain(ctx),
            EngineEvent::Order(OrderUpdate::Fill {
                symbol,
                client_order_id,
                qty,
                px,
                ..
            }) => {
                if Some(*symbol) != self.symbol(ctx) {
                    return;
                }
                let entry = !ctx
                    .order_facts(client_order_id)
                    .is_some_and(|order| order.reduce_only);
                if entry && !self.draining {
                    self.filled += 1;
                    tracing::warn!(
                        symbol = %self.symbol_name,
                        qty,
                        px,
                        filled = self.filled,
                        "a probe filled; closing it at market"
                    );
                }
                // Recomputed from the sleeve's own inventory either way: an
                // entry fill opens a drain, a drain fill that leaves it flat
                // clears the blocker.
                self.draining = false;
                self.drain(ctx);
            }
            EngineEvent::Order(OrderUpdate::Reject {
                client_order_id,
                code,
                reason,
            }) => {
                let Some(order) = ctx.order_facts(client_order_id) else {
                    return;
                };
                if Some(order.symbol) != self.symbol(ctx) {
                    return;
                }
                if order.reduce_only {
                    self.retry_drain_later(ctx);
                } else {
                    self.refused += 1;
                    self.skip(format!("rejected {code}: {reason}"));
                    tracing::warn!(symbol = %self.symbol_name, code, reason, "probe rejected");
                }
            }
            EngineEvent::Order(OrderUpdate::Cancelled {
                client_order_id, ..
            }) => {
                if ctx
                    .order_facts(client_order_id)
                    .is_some_and(|order| order.reduce_only && Some(order.symbol) == self.symbol)
                {
                    self.retry_drain_later(ctx);
                }
            }
            EngineEvent::IntentRefused {
                symbol,
                reduce_only,
                reason,
            } => {
                if Some(*symbol) != self.symbol(ctx) {
                    return;
                }
                if *reduce_only {
                    self.retry_drain_later(ctx);
                } else {
                    self.refused += 1;
                    self.skip(format!("refused: {reason}"));
                    tracing::warn!(symbol = %self.symbol_name, reason, "probe refused");
                }
            }
            _ => {}
        }
    }

    fn entry_blockers(&self) -> Vec<(String, String)> {
        self.skipped
            .iter()
            .map(|reason| (self.symbol_name.clone(), reason.clone()))
            .collect()
    }
}
