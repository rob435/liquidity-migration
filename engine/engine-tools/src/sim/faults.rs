//! Seeded faults on the four boundaries the engine has with the world:
//! the venue's command replies, the private stream, the market feed, and the
//! signal spool.
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
    MarketEvent, MarketFeed, OrderAck, OrderFeed, OrderRequest, OrderUpdate, SignalError,
    SignalFeed, SignalFeedEvent, SignalGapRequest, SignalObservation, StrategyId, Symbol, SymbolId,
    VenueCaps, VenueError, VenueGateway, VenueMutationTiming, VenueOrder,
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
    pub signal_delay: f64,
    pub signal_duplicate: f64,
    pub signal_withhold: f64,
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
        signal_delay: 0.0,
        signal_duplicate: 0.0,
        signal_withhold: 0.0,
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
        signal_delay: 0.05,
        signal_duplicate: 0.02,
        signal_withhold: 0.01,
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
        signal_delay: 0.15,
        signal_duplicate: 0.05,
        signal_withhold: 0.03,
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

    fn quota_wait(&self, command: engine_types::QueuedCommand) -> std::time::Duration {
        self.inner.quota_wait(command)
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
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
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

// ----------------------------------------------------------- signal spool

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SignalFault {
    None,
    /// Delivered later than the worker published it.
    Delay,
    /// Delivered twice: at-least-once delivery from an ordered spool.
    Duplicate,
    /// Held back so the engine sees a hole in the source's sequence.
    Withhold,
}

/// A row the wrapper took out of the spool and owes the engine. It lives
/// outside the wrapper because the spool's durable bytes outlive the process
/// reading them: a death must not lose a row the worker already published.
#[derive(Clone, Debug)]
pub struct Parked {
    due_ns: u64,
    row: SignalObservation,
    /// A withheld row waits for a later row of its own source to be
    /// delivered, so the engine sees the hole before the row that fills it,
    /// or for the engine to ask for it by name.
    holds_for_successor: bool,
}

pub type SharedParkedSignals = Arc<Mutex<Vec<Parked>>>;

pub fn shared_parked_signals() -> SharedParkedSignals {
    Arc::new(Mutex::new(Vec::new()))
}

/// A signal spool whose rows arrive late, twice, or out of order.
///
/// The wrapper borrows the durable feed rather than owning it, so one spool
/// serves every boot of one seed. A row it takes for itself is acknowledged
/// out of the inner feed on the spot and parked: the engine's acknowledgement
/// of that delivery is the wrapper's, not the spool's.
pub struct FaultySignalFeed<'a, F> {
    inner: &'a mut F,
    rng: SharedRng,
    rates: FaultRates,
    scheduler: Scheduler,
    log: SharedFaultLog,
    parked: SharedParkedSignals,
    delay_by: Duration,
    /// A copy to deliver a second time.
    repeat: Option<SignalObservation>,
    /// The last delivery came from the wrapper's own hand.
    owned: bool,
    /// The rows the engine is asking for by name. A live spool keeps its
    /// bytes on disk, so a request always finds the row: nothing the engine
    /// has asked for may stay withheld.
    requested: Vec<SignalGapRequest>,
}

impl<'a, F: SignalFeed> FaultySignalFeed<'a, F> {
    pub fn new(
        inner: &'a mut F,
        rates: FaultRates,
        rng: SharedRng,
        scheduler: Scheduler,
        log: SharedFaultLog,
        parked: SharedParkedSignals,
        delay_by: Duration,
    ) -> Self {
        FaultySignalFeed {
            inner,
            rng,
            rates,
            scheduler,
            log,
            parked,
            delay_by,
            repeat: None,
            owned: false,
            requested: Vec::new(),
        }
    }

    fn roll(&self) -> SignalFault {
        let r = &self.rates;
        draw(
            &self.rng,
            &[
                (r.signal_delay, SignalFault::Delay),
                (r.signal_duplicate, SignalFault::Duplicate),
                (r.signal_withhold, SignalFault::Withhold),
            ],
            SignalFault::None,
        )
    }

    fn park(&self, row: SignalObservation, due_ns: u64, holds_for_successor: bool) {
        let mut parked = lock(&self.parked);
        parked.push(Parked {
            due_ns,
            row,
            holds_for_successor,
        });
        parked.sort_by(|a, b| {
            a.due_ns
                .cmp(&b.due_ns)
                .then_with(|| a.row.source.cmp(&b.row.source))
                .then_with(|| a.row.sequence.cmp(&b.row.sequence))
        });
    }

    /// The engine named this row in a gap request. The successor that would
    /// release a withheld row can never arrive while the request stands — the
    /// spool serves nothing past the hole — so the request is what ends the
    /// withhold, exactly as re-reading the file does live.
    fn requested(&self, row: &SignalObservation) -> bool {
        self.requested
            .iter()
            .any(|gap| gap.next_sequence == row.sequence && gap.source == row.source)
    }

    fn owed(&self, parked: &Parked) -> bool {
        !parked.holds_for_successor || self.requested(&parked.row)
    }

    fn take_due(&self, now_ns: u64) -> Option<SignalObservation> {
        let mut parked = lock(&self.parked);
        let index = parked
            .iter()
            .position(|row| self.owed(row) && row.due_ns <= now_ns)?;
        Some(parked.remove(index).row)
    }

    fn earliest_due(&self) -> Option<u64> {
        lock(&self.parked)
            .iter()
            .find(|row| self.owed(row))
            .map(|row| row.due_ns)
    }

    /// A later row of `source` has been delivered: whatever was withheld from
    /// that source is now owed to the engine.
    fn release_successors(&self, source: &str, now_ns: u64) {
        for parked in lock(&self.parked).iter_mut() {
            if parked.holds_for_successor && parked.row.source == source {
                parked.holds_for_successor = false;
                parked.due_ns = now_ns;
            }
        }
    }
}

impl<F: SignalFeed> SignalFeed for FaultySignalFeed<'_, F> {
    fn set_sleeve_keys(
        &mut self,
        keys: Vec<engine_types::identity::SleeveKey>,
    ) -> Result<(), SignalError> {
        self.inner.set_sleeve_keys(keys)
    }

    fn request_readiness(&mut self) -> Result<(), SignalError> {
        self.inner.request_readiness()
    }

    fn request_lifecycle(
        &mut self,
        producers: Vec<engine_types::SignalProducerLifecycle>,
        legacy_sources: Vec<engine_types::SignalSourceFrontier>,
    ) -> Result<(), SignalError> {
        self.inner.request_lifecycle(producers, legacy_sources)
    }

    async fn next_event(&mut self) -> Result<SignalFeedEvent, SignalError> {
        loop {
            if let Some(row) = self.repeat.take() {
                self.owned = true;
                return Ok(SignalFeedEvent::Observation(row));
            }
            if let Some(row) = self.take_due(self.scheduler.now_ns()) {
                self.owned = true;
                return Ok(SignalFeedEvent::Observation(row));
            }
            // Sequential, never a `select!`: a wait raced against the inner
            // feed resolves in whichever order the two tasks happen to be
            // polled, and two runs of one seed then write different logs.
            if let Some(due) = self.earliest_due() {
                self.scheduler.sleep_until(due, WaiterKind::Signal).await;
                continue;
            }
            let event = self.inner.next_event().await?;
            let SignalFeedEvent::Observation(row) = event else {
                return Ok(event);
            };
            self.release_successors(&row.source, self.scheduler.now_ns());
            // Decided before anything is awaited, and what the wrapper keeps
            // is parked where a death cannot lose it.
            match self.roll() {
                SignalFault::None => {
                    self.owned = false;
                    return Ok(SignalFeedEvent::Observation(row));
                }
                SignalFault::Delay => {
                    FaultLog::note(&self.log, "signal.delay");
                    let steps = 1 + lock(&self.rng).below(6);
                    let due = self
                        .scheduler
                        .now_ns()
                        .saturating_add(self.delay_by.as_nanos() as u64 * steps);
                    self.inner.acknowledge_last()?;
                    self.park(row, due, false);
                }
                SignalFault::Duplicate => {
                    FaultLog::note(&self.log, "signal.duplicate");
                    self.repeat = Some(row.clone());
                    self.owned = false;
                    return Ok(SignalFeedEvent::Observation(row));
                }
                SignalFault::Withhold if self.requested(&row) => {
                    self.owned = false;
                    return Ok(SignalFeedEvent::Observation(row));
                }
                SignalFault::Withhold => {
                    FaultLog::note(&self.log, "signal.withhold");
                    self.inner.acknowledge_last()?;
                    self.park(row, self.scheduler.now_ns(), true);
                }
            }
        }
    }

    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        self.requested = gaps.to_vec();
        self.inner.set_gap_requests(gaps, blocked_destinations)
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        if std::mem::take(&mut self.owned) {
            return Ok(());
        }
        self.inner.acknowledge_last()
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        if std::mem::take(&mut self.owned) {
            // The engine could not take it yet. Park it a delay out rather
            // than now, so a destination that stays blocked is re-offered on
            // a later poll instead of spinning against this one.
            let due = self
                .scheduler
                .now_ns()
                .saturating_add(self.delay_by.as_nanos() as u64);
            self.park(observation, due, false);
            return Ok(());
        }
        self.inner.defer_last(observation)
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        match self.next_event().await? {
            SignalFeedEvent::Observation(row) => Ok(row),
            _ => Err(SignalError::Source(
                "the simulated spool advertised readiness where a row was expected".into(),
            )),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::backtest::signals::SignalReplayFeed;
    use engine_types::SIGNAL_OBSERVATION_SCHEMA_VERSION;

    fn withhold_always() -> FaultRates {
        FaultRates {
            signal_withhold: 1.0,
            ..FaultRates::NONE
        }
    }

    fn row(source: &str, sequence: u64) -> SignalObservation {
        let mut row = SignalObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "sim".into(),
            destination: StrategyId(0),
            source: source.into(),
            sequence,
            observation_id: format!("{source}-{sequence}"),
            kind: "test".into(),
            observed_wall_ts_ms: 1,
            available_wall_ts_ms: 1,
            subscriptions: Vec::new(),
            payload: b"{}".to_vec(),
            content_sha256: String::new(),
        };
        row.content_sha256 = crate::signals::content_sha256(&row);
        row
    }

    async fn observation<F: SignalFeed>(feed: &mut F) -> SignalObservation {
        loop {
            match feed.next_event().await.expect("the spool answers") {
                SignalFeedEvent::Observation(row) => return row,
                _ => continue,
            }
        }
    }

    /// A row withheld across a death is a hole the engine learns about from
    /// the producer's frontier at its next boot, not from a later row: the
    /// request it then makes is the only thing that can release the row,
    /// because the spool serves nothing past the hole it is asked to fill.
    #[tokio::test(start_paused = true)]
    async fn a_requested_row_leaves_the_withhold() {
        let scheduler = Scheduler::starting_at(2_000_000);
        scheduler.open();
        let mut spool = SignalReplayFeed::from_observations(
            vec![row("s", 1), row("s", 2), row("s", 3)],
            scheduler.clone(),
        );
        let parked = shared_parked_signals();
        let mut feed = FaultySignalFeed::new(
            &mut spool,
            withhold_always(),
            shared_rng(Rng::new(7)),
            scheduler.clone(),
            FaultLog::shared(),
            parked.clone(),
            Duration::from_secs(60),
        );
        assert_eq!(observation(&mut feed).await.sequence, 1);
        feed.acknowledge_last().unwrap();
        assert_eq!(lock(&parked).len(), 1, "sequence 2 is withheld");
        feed.set_gap_requests(
            &[SignalGapRequest {
                source: "s".into(),
                next_sequence: 2,
            }],
            &[],
        )
        .unwrap();
        let served = tokio::time::timeout(Duration::from_secs(60), observation(&mut feed))
            .await
            .expect("the wrapper still owes the engine the row it asked for");
        assert_eq!(served.sequence, 2);
        feed.acknowledge_last().unwrap();
    }
}
