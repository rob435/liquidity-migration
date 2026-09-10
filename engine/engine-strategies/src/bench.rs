//! Benchmark workload delivered through the same embedded host as registered plugs.

use engine_types::{
    EngineEvent, Feed, Intent, MarketEvent, OrderKind, Quote, Side, StopSpec, Strategy,
    StrategyCtx, StrategyId, Subscription, SymbolId, TimeInForce,
};

/// Where the contention workload rests, when the venue states no tick: one
/// basis point under the bid, so a post-only buy cannot cross.
const RESTING_FRACTION: f64 = 0.9999;

/// The contention workload's stop, as a fraction of its own resting price.
/// Below the entry, which the engine's stop cap requires; the cap then pulls
/// it in to whatever the configured leverage allows.
const STOP_FRACTION: f64 = 0.99;

/// What this sleeve has out on one symbol.
#[derive(Clone, Copy, serde::Serialize, serde::Deserialize)]
enum Rest {
    /// Nothing, or nothing the log still calls working.
    Idle,
    /// Resting since this quote sequence.
    Live(u64),
    /// A cancel is out and the order is still in the ledger.
    Pulling,
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct BenchStrategy {
    symbols: Vec<String>,
    every_nth: u64,
    /// Quotes between resting an entry and pulling it. `None` is the plain
    /// market-order workload the latency table was measured on.
    #[serde(default)]
    cancel_after: Option<u64>,
    #[serde(default)]
    rests: Vec<(SymbolId, Rest)>,
}

impl BenchStrategy {
    pub fn new(symbols: &[String], every_nth: u64) -> Self {
        BenchStrategy {
            symbols: symbols.to_vec(),
            every_nth: every_nth.max(1),
            cancel_after: None,
            rests: Vec::new(),
        }
    }

    /// Rest a post-only entry on every free symbol and pull it `cancel_after`
    /// quotes later, so a risk-reducing cancel is queued while the venue is
    /// still answering openings.
    pub fn contending(symbols: &[String], every_nth: u64, cancel_after: u64) -> Self {
        BenchStrategy {
            cancel_after: Some(cancel_after.max(1)),
            ..BenchStrategy::new(symbols, every_nth)
        }
    }

    fn rest(&mut self, symbol: SymbolId) -> &mut Rest {
        match self.rests.iter().position(|(held, _)| *held == symbol) {
            Some(slot) => &mut self.rests[slot].1,
            None => {
                self.rests.push((symbol, Rest::Idle));
                &mut self.rests.last_mut().expect("just pushed").1
            }
        }
    }

    fn market_entry(&self, symbol: SymbolId, quote: &Quote, now_ns: u64) -> Intent {
        Intent {
            kind: OrderKind::Market,
            stop: Some(StopSpec {
                trigger_px: quote.bid_px * 0.99,
            }),
            ..self.entry(symbol, now_ns)
        }
    }

    /// A post-only buy just under the bid.
    ///
    /// Just under, not far under: the risk kernel values an entry at the
    /// higher of its limit price and the market, so an order resting well
    /// below the touch reads as a stop distance no leverage allows and is
    /// refused before it can queue. The contention run does not fill, so how
    /// close it rests decides nothing else.
    fn resting_entry(
        &self,
        symbol: SymbolId,
        quote: &Quote,
        tick: Option<f64>,
        now_ns: u64,
    ) -> Intent {
        let px = match tick.filter(|tick| tick.is_finite() && *tick > 0.0) {
            Some(tick) => quote.bid_px - tick,
            None => quote.bid_px * RESTING_FRACTION,
        };
        Intent {
            kind: OrderKind::Limit {
                px,
                tif: TimeInForce::PostOnly,
            },
            stop: Some(StopSpec {
                trigger_px: px * STOP_FRACTION,
            }),
            ..self.entry(symbol, now_ns)
        }
    }

    fn entry(&self, symbol: SymbolId, now_ns: u64) -> Intent {
        Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol,
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
            tag: "bench".into(),
            decided_ns: now_ns,
            // The bench measures the order path itself, so nothing is worked:
            // a resting entry would put a reprice in the middle of the
            // numbers the latency table is read off.
            work: None,
            leverage: None,
        }
    }
}

impl Strategy for BenchStrategy {
    fn runtime_state(
        &self,
    ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String> {
        crate::runtime::snapshot(
            "bench",
            self,
            &(&self.symbols, self.every_nth, self.cancel_after),
        )
        .map(Some)
    }

    fn name(&self) -> &str {
        "bench"
    }

    /// A market order with a stop and nothing else: the workload never
    /// cancels, amends, or reads a position back. The contention workload
    /// rests a post-only entry and pulls it, so it needs two more.
    fn execution_requirements(&self) -> Vec<engine_types::Capability> {
        use engine_types::Capability as C;
        let mut required = vec![C::Submit, C::ProtectionPlace];
        if self.cancel_after.is_some() {
            required.extend([C::Cancel, C::PostOnly]);
        }
        required
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        self.symbols
            .iter()
            .map(|symbol| Subscription {
                symbol: symbol.clone(),
                feed: Feed::Quote,
            })
            .collect()
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event else {
            return;
        };
        let Some(cancel_after) = self.cancel_after else {
            if quote.seq.is_multiple_of(self.every_nth) {
                let intent = self.market_entry(*symbol, quote, ctx.now_ns());
                ctx.place(intent);
            }
            return;
        };
        // Owned, because the borrow behind a `RestingOrder` is the context the
        // cancel below needs mutably.
        let mine = {
            let mut resting = Vec::new();
            ctx.resting(&mut resting);
            resting
                .iter()
                .find(|order| order.symbol == *symbol)
                .map(|order| order.client_order_id.to_string())
        };
        match (mine, *self.rest(*symbol)) {
            (Some(id), Rest::Live(since)) if quote.seq.saturating_sub(since) >= cancel_after => {
                ctx.cancel(*symbol, &id);
                *self.rest(*symbol) = Rest::Pulling;
            }
            // A placement the ledger has not taken yet reads as nothing on the
            // symbol; wait a quote rather than doubling up on it.
            (None, Rest::Live(_)) => *self.rest(*symbol) = Rest::Idle,
            (None, _) if quote.seq.is_multiple_of(self.every_nth) => {
                let tick = ctx.instrument(*symbol).map(|rule| rule.tick_size);
                let intent = self.resting_entry(*symbol, quote, tick, ctx.now_ns());
                ctx.place(intent);
                *self.rest(*symbol) = Rest::Live(quote.seq);
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::Capability as C;

    #[test]
    fn the_bench_workload_declares_only_the_market_order_and_its_stop() {
        let bench = BenchStrategy::new(&["BTCUSDT".to_string()], 1);
        assert_eq!(
            bench.execution_requirements(),
            [C::Submit, C::ProtectionPlace]
        );
    }

    #[test]
    fn the_contention_workload_declares_the_pull_and_the_post_only_rest() {
        let bench = BenchStrategy::contending(&["BTCUSDT".to_string()], 1, 3);
        assert_eq!(
            bench.execution_requirements(),
            [C::Submit, C::ProtectionPlace, C::Cancel, C::PostOnly]
        );
    }

    #[test]
    fn the_contention_entry_rests_one_tick_under_the_bid_behind_a_lower_stop() {
        let bench = BenchStrategy::contending(&["BTCUSDT".to_string()], 1, 3);
        let quote = Quote {
            bid_px: 30_000.0,
            bid_qty: 1.0,
            ask_px: 30_000.5,
            ask_qty: 1.0,
            venue_ts_ms: 0,
            recv_ns: 0,
            seq: 1,
        };
        let intent = bench.resting_entry(SymbolId(0), &quote, Some(0.5), 7);
        let OrderKind::Limit { px, tif } = intent.kind else {
            panic!("the contention entry must rest, not cross");
        };
        assert_eq!(tif, TimeInForce::PostOnly);
        assert_eq!(px, 29_999.5);
        assert!(px < quote.ask_px, "a post-only buy may not be marketable");
        // Below the entry, which is what the engine's stop cap requires of a
        // long before it tightens the distance.
        assert!(intent.stop.expect("a stop").trigger_px < px);
        assert!(intent.work.is_none());
        // A venue that states no tick still gets a price under the bid.
        let tickless = bench.resting_entry(SymbolId(0), &quote, None, 7);
        let OrderKind::Limit { px, .. } = tickless.kind else {
            panic!("the contention entry must rest, not cross");
        };
        assert!(px < quote.bid_px);
    }
}
