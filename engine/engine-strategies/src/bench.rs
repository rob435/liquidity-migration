//! Benchmark workload delivered through the same embedded host as registered plugs.

use engine_types::{
    EngineEvent, Feed, Intent, MarketEvent, OrderKind, Side, StopSpec, Strategy, StrategyCtx,
    StrategyId, Subscription,
};

#[derive(serde::Serialize, serde::Deserialize)]
pub struct BenchStrategy {
    symbols: Vec<String>,
    every_nth: u64,
}

impl BenchStrategy {
    pub fn new(symbols: &[String], every_nth: u64) -> Self {
        BenchStrategy {
            symbols: symbols.to_vec(),
            every_nth: every_nth.max(1),
        }
    }
}

impl Strategy for BenchStrategy {
    fn runtime_state(
        &self,
    ) -> Result<Option<engine_types::strategy_process::StrategyRuntimeState>, String> {
        crate::runtime::snapshot("bench", self, &(&self.symbols, self.every_nth)).map(Some)
    }

    fn name(&self) -> &str {
        "bench"
    }

    /// A market order with a stop and nothing else: the workload never
    /// cancels, amends, or reads a position back.
    fn execution_requirements(&self) -> Vec<engine_types::Capability> {
        use engine_types::Capability as C;
        vec![C::Submit, C::ProtectionPlace]
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
        if !quote.seq.is_multiple_of(self.every_nth) {
            return;
        }
        ctx.place(Intent {
            exact_prices: None,
            exact_quantity: None,
            strategy: StrategyId(0),
            symbol: *symbol,
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec {
                trigger_px: quote.bid_px * 0.99,
            }),
            reduce_only: false,
            tag: "bench".into(),
            decided_ns: ctx.now_ns(),
            // The bench measures the order path itself, so nothing is worked:
            // a resting entry would put a reprice in the middle of the
            // numbers the latency table is read off.
            work: None,
            leverage: None,
        });
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
}
