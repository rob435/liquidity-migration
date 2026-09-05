//! Seeded faults on the three boundaries the engine has with the world:
//! the venue's command replies, the private stream, and the market feed.
//!
//! Each wrapper draws exactly one number per call, from its own stream, and
//! decides before it awaits anything. That is what keeps two runs of one
//! seed identical: the order in which components run can never change what
//! any of them draws.
//!
//! Every wrapper is cancel-safe in the way the loop's `select!` requires: an
//! update or event taken from the inner feed is either returned on that poll
//! or parked in the wrapper and returned on the next, never lost.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::numeric::ExactInstrumentSpec;
use engine_types::orders::{OrderLookup, OrderLookupClient};
use engine_types::{
    AccountIdentity, AccountInventory, AccountView, AmendSpec, Feed, FeedError, InstrumentRule,
    MarketEvent, MarketFeed, OrderAck, OrderFeed, OrderRequest, OrderUpdate, Symbol, SymbolId,
    VenueCaps, VenueError, VenueExecution, VenueGateway, VenueMutationTiming, VenueOrder,
};

use super::rng::Rng;
use crate::backtest::scheduler::{Scheduler, WaiterKind};

/// Bybit's parameter-error code, the shape of a refusal that never reached
/// the matching engine.
const REJECT_PARAMS: i64 = 10001;

/// Per-call probabilities. Each group is drawn once per call, in the order
/// the fields are listed, so the sum of a group must stay below one.
#[derive(Clone, Copy, Debug, PartialEq, serde::Serialize)]
pub struct FaultRates {
    pub venue_reject: f64,
    pub venue_lost_before: f64,
    pub venue_reply_lost: f64,
    pub venue_slow: f64,
    pub account_view_fail: f64,
    pub private_drop: f64,
    pub private_duplicate: f64,
    pub private_delay: f64,
    pub private_hiccup: f64,
    pub market_hiccup: f64,
    pub market_reset: f64,
}

impl FaultRates {
    pub const NONE: FaultRates = FaultRates {
        venue_reject: 0.0,
        venue_lost_before: 0.0,
        venue_reply_lost: 0.0,
        venue_slow: 0.0,
        account_view_fail: 0.0,
        private_drop: 0.0,
        private_duplicate: 0.0,
        private_delay: 0.0,
        private_hiccup: 0.0,
        market_hiccup: 0.0,
        market_reset: 0.0,
    };

    /// A bad day at a real venue: one command in fifty goes wrong somehow.
    pub const LIGHT: FaultRates = FaultRates {
        venue_reject: 0.005,
        venue_lost_before: 0.005,
        venue_reply_lost: 0.01,
        venue_slow: 0.03,
        account_view_fail: 0.02,
        private_drop: 0.01,
        private_duplicate: 0.02,
        private_delay: 0.05,
        private_hiccup: 0.005,
        market_hiccup: 0.002,
        market_reset: 0.001,
    };

    /// An outage in progress.
    pub const HEAVY: FaultRates = FaultRates {
        venue_reject: 0.02,
        venue_lost_before: 0.03,
        venue_reply_lost: 0.05,
        venue_slow: 0.10,
        account_view_fail: 0.10,
        private_drop: 0.05,
        private_duplicate: 0.05,
        private_delay: 0.15,
        private_hiccup: 0.02,
        market_hiccup: 0.01,
        market_reset: 0.005,
    };

    pub fn named(name: &str) -> Option<FaultRates> {
        match name {
            "none" => Some(Self::NONE),
            "light" => Some(Self::LIGHT),
            "heavy" => Some(Self::HEAVY),
            _ => None,
        }
    }
}

/// What was injected, by name, for the report.
#[derive(Debug, Default)]
pub struct FaultLog {
    counts: BTreeMap<&'static str, u64>,
}

pub type SharedFaultLog = Arc<Mutex<FaultLog>>;
pub type SharedRng = Arc<Mutex<Rng>>;

impl FaultLog {
    pub fn shared() -> SharedFaultLog {
        Arc::new(Mutex::new(FaultLog::default()))
    }

    pub fn note(log: &SharedFaultLog, what: &'static str) {
        *lock(log).counts.entry(what).or_insert(0) += 1;
    }

    pub fn snapshot(log: &SharedFaultLog) -> BTreeMap<String, u64> {
        lock(log)
            .counts
            .iter()
            .map(|(k, v)| ((*k).to_string(), *v))
            .collect()
    }
}

pub fn shared_rng(rng: Rng) -> SharedRng {
    Arc::new(Mutex::new(rng))
}

fn lock<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// One draw against a list of `(probability, outcome)` edges, in order.
fn draw<T: Copy>(rng: &SharedRng, edges: &[(f64, T)], none: T) -> T {
    let x = lock(rng).unit();
    let mut edge = 0.0;
    for (probability, outcome) in edges {
        edge += probability;
        if x < edge {
            return *outcome;
        }
    }
    none
}

// ------------------------------------------------------------------ venue

/// A late reply: the venue's answer takes `by` longer on the virtual clock.
/// A free function so the wait captures no reference to the gateway.
async fn hold(scheduler: Scheduler, by: Duration) {
    scheduler.sleep(by, WaiterKind::Venue).await;
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum VenueFault {
    None,
    /// The venue refused it; nothing changed there.
    Reject,
    /// The request never arrived; nothing changed there.
    LostBefore,
    /// The venue acted and the reply never arrived: the ambiguous case.
    ReplyLost,
    /// The reply arrives, late.
    Slow,
}

/// A venue gateway whose replies are unreliable in the ways real ones are.
pub struct FaultyGateway<G> {
    inner: G,
    rng: SharedRng,
    rates: FaultRates,
    scheduler: Scheduler,
    log: SharedFaultLog,
    slow_by: Duration,
}

impl<G: VenueGateway> FaultyGateway<G> {
    pub fn new(
        inner: G,
        rates: FaultRates,
        rng: SharedRng,
        scheduler: Scheduler,
        log: SharedFaultLog,
        slow_by: Duration,
    ) -> Self {
        FaultyGateway {
            inner,
            rng,
            rates,
            scheduler,
            log,
            slow_by,
        }
    }

    fn roll(&self) -> VenueFault {
        let r = &self.rates;
        draw(
            &self.rng,
            &[
                (r.venue_reject, VenueFault::Reject),
                (r.venue_lost_before, VenueFault::LostBefore),
                (r.venue_reply_lost, VenueFault::ReplyLost),
                (r.venue_slow, VenueFault::Slow),
            ],
            VenueFault::None,
        )
    }

    fn refused(what: &str) -> VenueError {
        VenueError::Rejected {
            code: REJECT_PARAMS,
            message: format!("simulated: the venue refused the {what}"),
        }
    }

    fn lost_before(what: &str) -> VenueError {
        VenueError::Transport(format!("simulated: the {what} was lost before the venue"))
    }

    fn reply_lost(what: &str) -> VenueError {
        VenueError::Transport(format!(
            "simulated: the venue answered the {what} and the reply was lost"
        ))
    }
}

#[engine_types::async_trait]
impl<G: VenueGateway> VenueGateway for FaultyGateway<G> {
    fn caps(&self) -> VenueCaps {
        self.inner.caps()
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        self.inner.account_identity().await
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        match self.roll() {
            VenueFault::None => self.inner.send_order(req).await,
            VenueFault::Reject => {
                FaultLog::note(&self.log, "venue.reject");
                Err(Self::refused("order"))
            }
            VenueFault::LostBefore => {
                FaultLog::note(&self.log, "venue.lost_before");
                Err(Self::lost_before("order"))
            }
            VenueFault::ReplyLost => {
                let _ = self.inner.send_order(req).await;
                FaultLog::note(&self.log, "venue.reply_lost");
                Err(Self::reply_lost("order"))
            }
            VenueFault::Slow => {
                FaultLog::note(&self.log, "venue.slow");
                hold(self.scheduler.clone(), self.slow_by).await;
                self.inner.send_order(req).await
            }
        }
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        match self.roll() {
            VenueFault::None => self.inner.cancel_order(symbol, client_order_id).await,
            VenueFault::Reject => {
                FaultLog::note(&self.log, "venue.reject");
                Err(Self::refused("cancel"))
            }
            VenueFault::LostBefore => {
                FaultLog::note(&self.log, "venue.lost_before");
                Err(Self::lost_before("cancel"))
            }
            VenueFault::ReplyLost => {
                let _ = self.inner.cancel_order(symbol, client_order_id).await;
                FaultLog::note(&self.log, "venue.reply_lost");
                Err(Self::reply_lost("cancel"))
            }
            VenueFault::Slow => {
                FaultLog::note(&self.log, "venue.slow");
                hold(self.scheduler.clone(), self.slow_by).await;
                self.inner.cancel_order(symbol, client_order_id).await
            }
        }
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        match self.roll() {
            VenueFault::None => self.inner.amend_order(symbol, client_order_id, spec).await,
            VenueFault::Reject => {
                FaultLog::note(&self.log, "venue.reject");
                Err(Self::refused("amend"))
            }
            VenueFault::LostBefore => {
                FaultLog::note(&self.log, "venue.lost_before");
                Err(Self::lost_before("amend"))
            }
            VenueFault::ReplyLost => {
                let _ = self.inner.amend_order(symbol, client_order_id, spec).await;
                FaultLog::note(&self.log, "venue.reply_lost");
                Err(Self::reply_lost("amend"))
            }
            VenueFault::Slow => {
                FaultLog::note(&self.log, "venue.slow");
                hold(self.scheduler.clone(), self.slow_by).await;
                self.inner.amend_order(symbol, client_order_id, spec).await
            }
        }
    }

    fn take_mutation_timing(&mut self) -> Option<VenueMutationTiming> {
        self.inner.take_mutation_timing()
    }

    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        self.inner.take_rate_wait_ns()
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        match self.roll() {
            VenueFault::None => self.inner.set_stop(symbol, trigger_px).await,
            VenueFault::Reject => {
                FaultLog::note(&self.log, "venue.reject");
                Err(Self::refused("stop"))
            }
            VenueFault::LostBefore => {
                FaultLog::note(&self.log, "venue.lost_before");
                Err(Self::lost_before("stop"))
            }
            VenueFault::ReplyLost => {
                let _ = self.inner.set_stop(symbol, trigger_px).await;
                FaultLog::note(&self.log, "venue.reply_lost");
                Err(Self::reply_lost("stop"))
            }
            VenueFault::Slow => {
                FaultLog::note(&self.log, "venue.slow");
                hold(self.scheduler.clone(), self.slow_by).await;
                self.inner.set_stop(symbol, trigger_px).await
            }
        }
    }

    async fn set_stop_exact(
        &mut self,
        symbol: SymbolId,
        terms: &engine_types::order_terms::ExactStopTerms,
    ) -> Result<(), VenueError> {
        match self.roll() {
            VenueFault::None => self.inner.set_stop_exact(symbol, terms).await,
            VenueFault::Reject => {
                FaultLog::note(&self.log, "venue.reject");
                Err(Self::refused("stop"))
            }
            VenueFault::LostBefore => {
                FaultLog::note(&self.log, "venue.lost_before");
                Err(Self::lost_before("stop"))
            }
            VenueFault::ReplyLost => {
                let _ = self.inner.set_stop_exact(symbol, terms).await;
                FaultLog::note(&self.log, "venue.reply_lost");
                Err(Self::reply_lost("stop"))
            }
            VenueFault::Slow => {
                FaultLog::note(&self.log, "venue.slow");
                hold(self.scheduler.clone(), self.slow_by).await;
                self.inner.set_stop_exact(symbol, terms).await
            }
        }
    }

    fn add_symbol(&mut self, symbol: &str) -> Option<SymbolId> {
        self.inner.add_symbol(symbol)
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        self.inner.set_leverage(symbol, leverage).await
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        let fail = lock(&self.rng).chance(self.rates.account_view_fail);
        if fail {
            FaultLog::note(&self.log, "venue.account_view_fail");
            return Err(VenueError::Transport(
                "simulated: the account read timed out".to_string(),
            ));
        }
        self.inner.account_view().await
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.inner.instrument_rules().await
    }

    fn order_lookup_client(&self) -> Option<Box<dyn OrderLookupClient>> {
        self.inner.order_lookup_client()
    }

    fn account_recovery_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::AccountRecoveryClient>> {
        self.inner.account_recovery_client()
    }

    fn instrument_catalog_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::InstrumentCatalogClient>> {
        self.inner.instrument_catalog_client()
    }

    fn restore_instrument_catalog(
        &self,
        checkpoint: &engine_types::orders::InstrumentCatalogCheckpoint,
    ) -> Result<engine_types::orders::InstrumentCatalog, VenueError> {
        self.inner.restore_instrument_catalog(checkpoint)
    }

    fn install_instrument_catalog(
        &mut self,
        catalog: &engine_types::orders::InstrumentCatalog,
    ) -> Result<(), VenueError> {
        self.inner.install_instrument_catalog(catalog)
    }

    async fn order_status(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<OrderLookup, VenueError> {
        self.inner.order_status(symbol, client_order_id).await
    }

    async fn instrument_specs(&mut self) -> Result<Vec<(Symbol, ExactInstrumentSpec)>, VenueError> {
        self.inner.instrument_specs().await
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        self.inner.working_orders().await
    }

    async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        self.inner.account_inventory().await
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        self.inner.executions(start_ms, end_ms).await
    }
}

// --------------------------------------------------------- private stream

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum PrivateFault {
    None,
    /// Swallowed. The socket then reconnects, which is how a real gap looks.
    Drop,
    /// Delivered twice: at-least-once delivery.
    Duplicate,
    /// Delivered late.
    Delay,
    /// The socket errors; the update is delivered after the reconnect.
    Hiccup,
}

/// A private stream that drops, repeats, delays and reconnects.
pub struct FaultyOrderFeed<F> {
    inner: F,
    rng: SharedRng,
    rates: FaultRates,
    scheduler: Scheduler,
    log: SharedFaultLog,
    delay_by: Duration,
    /// Say the stream (re)connected before anything else.
    announce_reset: bool,
    /// An update held back until a virtual instant.
    held: Option<(u64, OrderUpdate)>,
    /// An update to deliver a second time.
    repeat: Option<OrderUpdate>,
}

impl<F: OrderFeed> FaultyOrderFeed<F> {
    pub fn new(
        inner: F,
        rates: FaultRates,
        rng: SharedRng,
        scheduler: Scheduler,
        log: SharedFaultLog,
        delay_by: Duration,
        reconnecting: bool,
    ) -> Self {
        FaultyOrderFeed {
            inner,
            rng,
            rates,
            scheduler,
            log,
            delay_by,
            announce_reset: reconnecting,
            held: None,
            repeat: None,
        }
    }

    fn roll(&self) -> PrivateFault {
        let r = &self.rates;
        draw(
            &self.rng,
            &[
                (r.private_drop, PrivateFault::Drop),
                (r.private_duplicate, PrivateFault::Duplicate),
                (r.private_delay, PrivateFault::Delay),
                (r.private_hiccup, PrivateFault::Hiccup),
            ],
            PrivateFault::None,
        )
    }

    fn reset(&self) -> OrderUpdate {
        OrderUpdate::StreamReset {
            recv_ns: self.scheduler.now_ns(),
        }
    }
}

impl<F: OrderFeed> OrderFeed for FaultyOrderFeed<F> {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        loop {
            if self.announce_reset {
                self.announce_reset = false;
                return Ok(self.reset());
            }
            if let Some((at, _)) = &self.held {
                let at = *at;
                self.scheduler.sleep_until(at, WaiterKind::Private).await;
                if let Some((_, update)) = self.held.take() {
                    return Ok(update);
                }
            }
            if let Some(update) = self.repeat.take() {
                return Ok(update);
            }
            let update = self.inner.next_update().await?;
            if matches!(update, OrderUpdate::StreamReset { .. }) {
                return Ok(update);
            }
            match self.roll() {
                PrivateFault::None => return Ok(update),
                PrivateFault::Drop => {
                    FaultLog::note(&self.log, "private.drop");
                    self.announce_reset = true;
                }
                PrivateFault::Duplicate => {
                    FaultLog::note(&self.log, "private.duplicate");
                    self.repeat = Some(update.clone());
                    return Ok(update);
                }
                PrivateFault::Delay => {
                    FaultLog::note(&self.log, "private.delay");
                    let extra = 1 + lock(&self.rng).below(3);
                    let at = self
                        .scheduler
                        .now_ns()
                        .saturating_add(self.delay_by.as_nanos() as u64 * extra);
                    self.held = Some((at, update));
                }
                PrivateFault::Hiccup => {
                    FaultLog::note(&self.log, "private.hiccup");
                    self.held = Some((self.scheduler.now_ns(), update));
                    self.announce_reset = true;
                    return Err(FeedError::Transport(
                        "simulated: the private socket dropped".to_string(),
                    ));
                }
            }
        }
    }

    fn learn(&mut self, symbol: &str, id: SymbolId) {
        self.inner.learn(symbol, id);
    }

    fn learn_instrument(&mut self, id: SymbolId, spec: &ExactInstrumentSpec) {
        self.inner.learn_instrument(id, spec);
    }
}

// ------------------------------------------------------------ market feed

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum MarketFault {
    None,
    Hiccup,
    Reset,
}

/// A market feed that errors and resets the way a reconnecting socket does.
pub struct FaultyMarketFeed<M> {
    inner: M,
    rng: SharedRng,
    rates: FaultRates,
    scheduler: Scheduler,
    log: SharedFaultLog,
    held: Option<MarketEvent>,
}

impl<M: MarketFeed> FaultyMarketFeed<M> {
    pub fn new(
        inner: M,
        rates: FaultRates,
        rng: SharedRng,
        scheduler: Scheduler,
        log: SharedFaultLog,
    ) -> Self {
        FaultyMarketFeed {
            inner,
            rng,
            rates,
            scheduler,
            log,
            held: None,
        }
    }

    fn roll(&self) -> MarketFault {
        let r = &self.rates;
        draw(
            &self.rng,
            &[
                (r.market_hiccup, MarketFault::Hiccup),
                (r.market_reset, MarketFault::Reset),
            ],
            MarketFault::None,
        )
    }
}

impl<M: MarketFeed> MarketFeed for FaultyMarketFeed<M> {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if let Some(event) = self.held.take() {
            return Ok(event);
        }
        let event = self.inner.next_event().await?;
        match self.roll() {
            MarketFault::None => Ok(event),
            MarketFault::Hiccup => {
                FaultLog::note(&self.log, "market.hiccup");
                self.held = Some(event);
                Err(FeedError::Transport(
                    "simulated: the market socket dropped".to_string(),
                ))
            }
            MarketFault::Reset => {
                FaultLog::note(&self.log, "market.reset");
                self.held = Some(event);
                Ok(MarketEvent::FeedReset {
                    recv_ns: self.scheduler.now_ns(),
                })
            }
        }
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        self.inner.admit(symbol, feed)
    }

    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        self.inner.retire(symbol, feed)
    }
}
