//! MEXC's private side: a paced resync rather than a live order stream.
//!
//! **Why this is not a socket, and how that differs from Lighter's reason.**
//! MEXC's private channel `push.personal.order` does carry `externalOid`, so a
//! fill on it could be attributed to the strategy that caused it — the venue is
//! not the obstacle. What is missing is proof: the channel is entered by a
//! login frame on the shared socket, and MEXC publishes no testnet, so the
//! handshake cannot be exercised anywhere except against a funded account.
//! Guessing a login frame and finding out on real money is not a trade this
//! adapter makes.
//!
//! So this feed does the one thing it can do honestly: it raises
//! [`OrderUpdate::StreamReset`] on a timer. That is the engine's existing "you
//! may have missed something" signal — it refreshes the account view and reads
//! the venue's own execution history, which carries `externalOid`
//! ([`super::gateway::MexcGateway::executions`]). Fills therefore arrive within
//! one resync period instead of within a millisecond.
//!
//! What that costs, plainly: a strategy that reacts to its own fill reacts a
//! few seconds late here, and the working supervisor's repricing is that much
//! coarser. What it does not cost is correctness — the log still learns every
//! fill, from the venue's own history, with the engine's own ids on it.
//!
//! When the login frame has been exercised against the venue, this becomes an
//! ordinary socket feed and the rest of the adapter does not move.

use std::time::Duration;

use tokio::time::Instant;

use engine_types::ids::SymbolId;
use engine_types::market::{FeedError, OrderFeed};
use engine_types::orders::OrderUpdate;
use engine_types::VenueError;

use super::realm::MexcRealm;
use crate::mono_ns;

/// How often the engine is told to re-read. The engine's own account refresh
/// runs at a few seconds, so this is the same order of magnitude.
///
/// It also sits well inside MEXC's own budget: the history call this triggers
/// is 20 requests per 2 seconds, per symbol.
pub const DEFAULT_RESYNC: Duration = Duration::from_secs(5);

pub struct MexcOrderFeed {
    every: Duration,
    /// When the next resync is owed. A deadline and not a sleep: the engine
    /// polls this inside a `select!`, so every turn another branch wins drops
    /// this future — and a relative sleep created inside it would restart from
    /// zero each time. With market events arriving faster than the period it
    /// would never once elapse, and no fill would ever reach the engine.
    ///
    /// `None` means owed now, which is what a boot wants: recover whatever the
    /// log missed while the engine was down without waiting a period first.
    due: Option<Instant>,
}

impl MexcOrderFeed {
    /// The live feed. Credentials are read so that an unarmed or misconfigured
    /// host fails here, at boot, rather than at the first order — even though
    /// this feed itself opens nothing.
    pub fn new(realm: MexcRealm) -> Result<Self, VenueError> {
        let _ = realm.credentials()?;
        Ok(Self::with_period(DEFAULT_RESYNC))
    }

    pub fn with_period(every: Duration) -> Self {
        MexcOrderFeed { every, due: None }
    }
}

impl OrderFeed for MexcOrderFeed {
    fn learn(&mut self, _symbol: &str, _id: SymbolId) {}

    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        if let Some(due) = self.due {
            tokio::time::sleep_until(due).await;
        }
        self.due = Some(Instant::now() + self.every);
        Ok(OrderUpdate::StreamReset { recv_ns: mono_ns() })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn the_first_resync_goes_out_without_waiting() {
        // A boot has to recover whatever the log missed while the engine was
        // down; waiting a period first would leave it blind for that long.
        let mut feed = MexcOrderFeed::with_period(Duration::from_secs(3600));
        let started = std::time::Instant::now();
        assert!(matches!(
            feed.next_update().await.unwrap(),
            OrderUpdate::StreamReset { .. }
        ));
        assert!(
            started.elapsed() < Duration::from_millis(500),
            "the first resync waited"
        );
    }

    #[tokio::test]
    async fn a_resync_survives_being_dropped_over_and_over() {
        // The engine polls this in a `select!` and drops the losing branch's
        // future every turn. A relative sleep would restart on each drop and,
        // with market events arriving faster than the period, would never
        // elapse — so no MEXC fill would ever be read.
        const PERIOD: Duration = Duration::from_millis(400);
        let mut feed = MexcOrderFeed::with_period(PERIOD);
        feed.next_update().await.unwrap();
        let started = std::time::Instant::now();
        for _ in 0..30 {
            let cancelled =
                tokio::time::timeout(Duration::from_millis(10), feed.next_update()).await;
            assert!(cancelled.is_err(), "the resync fired early");
        }
        assert!(
            started.elapsed() < PERIOD,
            "the cancelling itself took a whole period"
        );
        let resumed = tokio::time::timeout(PERIOD * 3, feed.next_update())
            .await
            .expect("the resync never came")
            .unwrap();
        assert!(matches!(resumed, OrderUpdate::StreamReset { .. }));
        let waited = started.elapsed();
        assert!(
            waited < PERIOD + PERIOD / 2,
            "the period restarted on every cancellation: {waited:?} for a {PERIOD:?} period"
        );
    }

    #[tokio::test]
    async fn later_resyncs_are_paced() {
        let mut feed = MexcOrderFeed::with_period(Duration::from_millis(80));
        feed.next_update().await.unwrap();
        let started = std::time::Instant::now();
        assert!(matches!(
            feed.next_update().await.unwrap(),
            OrderUpdate::StreamReset { .. }
        ));
        assert!(
            started.elapsed() >= Duration::from_millis(60),
            "the resync did not wait"
        );
    }
}
