//! `engine bench`: embedded production callbacks, real WAL barriers and a
//! signed local HTTP submit. The venue omits TLS and matching-engine delay.

use std::cell::Cell;
use std::fmt::Write as _;
use std::io::ErrorKind;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::rc::Rc;
use std::time::Duration;

use engine_types::{
    AccountIdentity, AccountView, AmendSpec, FeedError, InstrumentRule, Intent, MarketEvent,
    MarketFeed, OrderAck, OrderFeed, OrderKind, OrderRequest, OrderUpdate, Quote, RiskKernel,
    RiskVerdict, Side, Symbol, SymbolId, VenueCaps, VenueError, VenueGateway, VenueOrder, Wal,
    WalRecord,
};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::clock;
use crate::config::EngineSection;
use crate::engine::{Engine, EngineError};

use crate::ledger::{pretty, LatencyLedger, Quantiles, Segment};

mod contention;
mod wal_timing;
pub use contention::ContentionResult;
pub use wal_timing::BarrierTiming;

#[derive(Clone, Debug)]
pub struct BenchOptions {
    pub events: u64,
    /// Quotes per second. Zero means as fast as the loop will take them.
    pub rate: u64,
    /// One order every this many quotes.
    pub every_nth: u64,
    pub symbols: Vec<String>,
    pub wal_path: PathBuf,
    /// Fill accepted orders through the private feed.
    pub fills: bool,
    /// Delay each local venue reply by this duration. In contention mode only
    /// placements are held; cancels are answered at once.
    pub venue_delay: Duration,
    /// Rest a post-only entry per symbol and pull it, instead of crossing
    /// with a market order, so cancels queue behind slow openings.
    pub contention: bool,
    /// Quotes between resting an entry and pulling it. Contention mode only.
    pub cancel_after: u64,
    /// `engine.opening_dispatch_ttl_ms` for the run: an opening older than
    /// this when the venue task reaches it is refused unsent.
    pub ttl_ms: u64,
    /// Contention mode only: the quote feed closes once this many pulls have
    /// come back answered, `events` staying the ceiling. `None` runs every
    /// quote.
    pub pulls: Option<u64>,
    /// Give the pretend venue a local request quota of MEXC's shape, so
    /// openings run out of budget while risk-off still has a slot.
    pub quota: bool,
}

impl Default for BenchOptions {
    fn default() -> Self {
        BenchOptions {
            events: 20_000,
            rate: 0,
            every_nth: 20,
            symbols: vec!["BTCUSDT".to_string()],
            wal_path: PathBuf::from("engine-bench.wal"),
            fills: false,
            venue_delay: Duration::ZERO,
            contention: false,
            cancel_after: 3,
            ttl_ms: 10_000,
            pulls: None,
            quota: false,
        }
    }
}

impl BenchOptions {
    /// Openings the venue is slow to answer, with a cancel behind them.
    ///
    /// The engine holds a symbol busy for the length of its own command, so
    /// the queue depth a cancel waits behind is one opening per other symbol:
    /// four symbols put three openings in the venue task's queue while it
    /// answers the fourth. Every quote is a placement opportunity, so a symbol
    /// whose order was just pulled re-arms within four quotes — 20 ms at
    /// 200 Hz, against a 200 ms venue call.
    pub fn contention() -> Self {
        BenchOptions {
            events: 4_000,
            rate: 200,
            every_nth: 1,
            symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
                .iter()
                .map(|name| (*name).to_string())
                .collect(),
            venue_delay: Duration::from_millis(200),
            contention: true,
            ..BenchOptions::default()
        }
    }
}

pub struct BenchResult {
    pub callback_execution: &'static str,
    pub events: u64,
    pub orders: u64,
    pub order_opportunities: u64,
    pub orders_not_submitted: u64,
    pub elapsed_ns: u64,
    pub latency_window_events: u64,
    pub completed_latency_windows: u64,
    pub barriers: Vec<BarrierTiming>,
    pub segments: Vec<(Segment, Quantiles)>,
    /// Present only for a `--contention` run.
    pub contention: Option<ContentionResult>,
}

fn segment_name(segment: Segment) -> &'static str {
    match segment {
        Segment::Decide => "market to decision",
        Segment::Durable => "decision to dispatch ready",
        Segment::BarrierWait => "dispatch barrier observed",
        _ => segment.plain_name(),
    }
}

fn quantiles_json(q: Quantiles) -> serde_json::Value {
    serde_json::json!({
        "count": q.count, "p50_ns": q.p50_ns, "p90_ns": q.p90_ns,
        "p99_ns": q.p99_ns, "p999_ns": (q.count > 0).then_some(q.p999_ns), "max_ns": q.max_ns,
    })
}

impl BenchResult {
    pub fn table(&self) -> String {
        let mut out = format!(
            "  callbacks: embedded on loop thread; risk: engine-risk (100x, 1M USDT gross, 9K USDT margin caps)\n  source order opportunities: {}; without a completed submit attempt: {} (coalescing, refusal or shutdown)\n",
            self.order_opportunities, self.orders_not_submitted,
        );
        let _ = writeln!(
            out,
            "  elapsed: {}; latency histograms: {}, {} quotes; {} completed live windows",
            pretty(self.elapsed_ns),
            if self.completed_latency_windows == 0 {
                "whole run"
            } else {
                "final live window only (nominal 60s)"
            },
            self.latency_window_events,
            self.completed_latency_windows
        );
        out.push_str("  what happened                  count   typical(p50)  slow 1 in 10  slow 1 in 100         p99.9      worst\n");
        for (segment, q) in &self.segments {
            let _ = writeln!(
                out,
                "  {:<28} {:>7}  {:>12}  {:>12}  {:>13}  {:>12}  {:>9}",
                segment_name(*segment),
                q.count,
                pretty(q.p50_ns),
                pretty(q.p90_ns),
                pretty(q.p99_ns),
                if q.count > 0 {
                    pretty(q.p999_ns)
                } else {
                    "unavailable".into()
                },
                pretty(q.max_ns)
            );
        }
        out.push_str("\n  WAL barriers: whole run including boot; request to observed confirmation.\n  One observer thread relays asynchronous confirmations; its wake/channel overhead is included.\n  records covered                 mode   count     req p50     req p99    wait p50    wait p99  wait p99.9    wait max  failures\n");
        for row in &self.barriers {
            let _ = writeln!(
                out,
                "  {:<30} {:<5} {:>6}  {:>10}  {:>10}  {:>10}  {:>10}  {:>10}  {:>10}  {:>8}",
                row.records,
                if row.asynchronous { "async" } else { "sync" },
                row.confirmation.count,
                pretty(row.request.p50_ns),
                pretty(row.request.p99_ns),
                pretty(row.confirmation.p50_ns),
                pretty(row.confirmation.p99_ns),
                pretty(row.confirmation.p999_ns),
                pretty(row.confirmation.max_ns),
                row.failures
            );
        }
        if let Some(contention) = &self.contention {
            out.push_str(&contention_table(contention));
        }
        out
    }

    pub fn as_json(&self) -> String {
        let mut output = serde_json::json!({
            "callback_execution": self.callback_execution,
            "risk_kernel": "engine-risk", "events": self.events, "orders": self.orders,
            "order_opportunities": self.order_opportunities, "orders_not_submitted": self.orders_not_submitted,
            "elapsed_ns": self.elapsed_ns, "latency_window_events": self.latency_window_events,
            "completed_latency_windows": self.completed_latency_windows,
            "latency_scope": if self.completed_latency_windows == 0 { "whole_run" } else { "final_live_window" },
            "barrier_scope": "whole_run_including_boot",
            "barrier_instrumentation": "one observer thread; async confirmation relay overhead included",
            "barriers": self.barriers.iter().map(|row| serde_json::json!({
                "records": row.records, "asynchronous": row.asynchronous,
                "request": quantiles_json(row.request), "confirmation": quantiles_json(row.confirmation),
                "failures": row.failures,
            })).collect::<Vec<_>>(),
        });
        if let Some(contention) = &self.contention {
            output["contention"] = serde_json::json!({
                "venue_delay_ms": contention.venue_delay_ms,
                "cancel_after_quotes": contention.cancel_after,
                "opening_dispatch_ttl_ms": contention.ttl_ms,
                "symbols": contention.symbols,
                "local_quota": contention.quota.then_some(QUOTA_SHAPE),
                "risk_off_sweep_quotes": contention.quota.then_some(RISK_OFF_SWEEP_QUOTES),
                "openings_sent": contention.openings_sent,
                "cancels_sent": contention.cancels_sent,
                "never_sent_expired": contention.never_sent_expired,
                "cancel_queue_wait": quantiles_json(contention.cancel_queue_wait),
                "cancel_venue_span": quantiles_json(contention.cancel_venue_span),
                "opening_queue_wait": quantiles_json(contention.opening_queue_wait),
                "venue": "local synthetic venue; not live latency",
            });
        }
        let cells = self
            .segments
            .iter()
            .map(|(segment, q)| (segment_name(*segment).to_string(), quantiles_json(*q)));
        output.as_object_mut().unwrap().extend(cells);
        output.to_string()
    }
}

/// The contention section: the parameters beside the numbers, and no budget.
fn contention_table(contention: &ContentionResult) -> String {
    let mut out = String::from(
        "\n  contention: a post-only entry per symbol, pulled after a few quotes. The pretend\n  \
         venue holds placements and answers cancels at once, so a cancel queues behind openings.\n",
    );
    let _ = writeln!(
        out,
        "  parameters: venue delay {} ms on placements; pull after {} quote(s); \
         opening dispatch TTL {} ms; {} symbol(s)",
        contention.venue_delay_ms, contention.cancel_after, contention.ttl_ms, contention.symbols,
    );
    if contention.quota {
        let _ = writeln!(
            out,
            "  local request quota: {QUOTA_SHAPE}; risk-off sweep every \
             {RISK_OFF_SWEEP_QUOTES} quotes"
        );
    }
    let _ = writeln!(
        out,
        "  openings the venue answered: {}; refused unsent on expired authority: {}; cancels: {}",
        contention.openings_sent, contention.never_sent_expired, contention.cancels_sent,
    );
    out.push_str(
        "  what waited                    count   typical(p50)  slow 1 in 100      worst\n",
    );
    for (what, q) in [
        ("opening wait for the task", contention.opening_queue_wait),
        ("cancel wait for the task", contention.cancel_queue_wait),
        ("cancel call itself", contention.cancel_venue_span),
    ] {
        let _ = writeln!(
            out,
            "  {:<28} {:>7}  {:>12}  {:>13}  {:>9}",
            what,
            q.count,
            pretty(q.p50_ns),
            pretty(q.p99_ns),
            pretty(q.max_ns)
        );
    }
    out.push_str("  Local synthetic venue; not live latency. No budget is asserted here.\n");
    out
}

/// Build everything, run the real loop, read the histograms.
pub async fn run(options: &BenchOptions) -> Result<BenchResult, EngineError> {
    let venue_addr = start_mock_venue_delayed(VenueDelays {
        placement: options.venue_delay,
        // Risk-off is what the contention run measures waiting; a delay on the
        // answer would be added to that wait rather than measured beside it.
        cancel: if options.contention {
            Duration::ZERO
        } else {
            options.venue_delay
        },
    })?;
    let settings = EngineSection {
        execution_limits: None,
        wal_path: options.wal_path.clone(),
        // Named but unused: the bench builds its own pretend venue below.
        venue: engine_venue::BYBIT_DEMO.to_string(),
        group_flush_ms: 250,
        // Never rotates mid-bench: a rotation on the tick would put a
        // directory fsync into one unlucky sample.
        wal_rotate_mb: 0,
        account_view_max_age_ms: 60_000,
        opening_dispatch_ttl_ms: options.ttl_ms,
        // Wide, so a long low-rate bench never has its later orders refused
        // against the stamps of its own generated quotes.
        max_quote_age_ms: 600_000,
        // Shared, the default: the bench's orders carry no leverage, so the
        // authority mode never comes up — this just keeps the bench honest
        // about what a default config runs.
        leverage_authority: crate::config::LeverageAuthority::default(),
        // Shadow off on purpose: the point is to measure a real send. The
        // venue on the other end is the pretend one started just above.
        signal_spool_path: None,
        control_spool_path: None,
        // No heartbeat: the bench measures the order path, and a file write
        // riding the tick would be one more thing in the numbers.
        heartbeat_path: None,
        trades_path: None,
    };
    // The real log, so the measured barrier is the shipping fsync path.
    let (wal, _replayed) = engine_wal::WalWriter::open(&options.wal_path)?;
    let (wal, measurements) = wal_timing::TimedWal::new(wal)?;
    let strategy = match (options.contention, options.quota) {
        (true, true) => {
            BenchStrategy::contending(&options.symbols, options.every_nth, options.cancel_after)
                .sweeping(RISK_OFF_SWEEP_QUOTES)
        }
        (true, false) => {
            BenchStrategy::contending(&options.symbols, options.every_nth, options.cancel_after)
        }
        (false, _) => BenchStrategy::new(&options.symbols, options.every_nth),
    };
    let touch = LastTouch::default();
    let (accepted, filled) = tokio::sync::mpsc::unbounded_channel();
    let (taken, cancelled) = tokio::sync::mpsc::unbounded_channel();
    let mut venue = HttpVenue::new(venue_addr, options.symbols.clone());
    if options.quota {
        venue = venue.under_quota();
    }
    if options.fills {
        venue = venue.filling(accepted);
    }
    if options.contention {
        venue = venue.publishing_pulls(taken);
    }
    let private_stream = options.fills || options.contention;
    let mut engine = Engine::boot_as_exact(
        &settings,
        &format!("bench-{}-{}", options.events, options.every_nth),
        wal,
        benchmark_risk()?,
        venue,
        vec![Box::new(strategy)],
        &["bench".into()],
        &[],
    )
    .await?;

    let symbols: Vec<SymbolId> = options
        .symbols
        .iter()
        .filter_map(|name| engine.market().table.get(name))
        .collect();
    let pulls = PullsAnswered::default();
    let mut feed = ScriptedFeed::sharing(symbols, options.events, options.rate, touch.clone());
    if let Some(target) = options.pulls.filter(|_| options.contention) {
        feed = feed.closing_after_pulls(pulls.clone(), target);
    }

    // Branched rather than boxed: `OrderFeed::next_update` is an async trait
    // method, so the trait is not object-safe and there is no `dyn` to reach
    // for. Two arms is the whole cost.
    let started = std::time::Instant::now();
    let outcome = if private_stream {
        engine
            .run(
                &mut feed,
                &mut FillingOrderFeed::new(filled, cancelled, touch).counting_pulls(pulls),
                std::future::pending::<()>(),
            )
            .await?
    } else {
        engine
            .run(
                &mut feed,
                &mut SilentOrderFeed,
                std::future::pending::<()>(),
            )
            .await?
    };

    let (orders, latency_records, barriers) = measurements.snapshot();
    let mut result = summarise(
        engine.ledger(),
        outcome.market_events,
        orders,
        options.every_nth,
    );
    result.elapsed_ns = started.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64;
    // Engine::finish writes the final window without resetting its histogram.
    result.completed_latency_windows = latency_records.saturating_sub(1);
    result.barriers = barriers;
    if options.contention {
        // Read back out of the log rather than out of a side channel: the
        // stamps are already durable and this is the same pass `engine
        // latency` makes over a production log.
        engine.wal.flush()?;
        let (records, _torn) = engine_wal::replay_scan(&options.wal_path)
            .map_err(|error| EngineError::State(error.to_string()))?;
        let records: Vec<WalRecord> = records.into_iter().map(|(_, record)| record).collect();
        result.contention = Some(contention::read(
            &records,
            options.venue_delay.as_millis().min(u128::from(u64::MAX)) as u64,
            options.cancel_after,
            options.ttl_ms,
            options.symbols.len(),
            options.quota,
        ));
    }
    engine.wal.append(&WalRecord::Note {
        source: "bench".into(),
        text: result.as_json(),
    })?;
    engine.wal.flush()?;
    Ok(result)
}

fn summarise(ledger: &LatencyLedger, events: u64, orders: u64, every_nth: u64) -> BenchResult {
    let segments = [
        Segment::Decide,
        Segment::Durable,
        Segment::BarrierWait,
        Segment::Wire,
        Segment::Ack,
        Segment::DispatchQueue,
        Segment::VenueTask,
        Segment::CoreResume,
        Segment::EndToEnd,
    ]
    .into_iter()
    .map(|segment| (segment, ledger.quantiles(segment)))
    .collect();
    BenchResult {
        callback_execution: "embedded",
        events,
        orders,
        order_opportunities: events / every_nth.max(1),
        orders_not_submitted: (events / every_nth.max(1)).saturating_sub(orders),
        elapsed_ns: 0,
        latency_window_events: ledger.events(),
        completed_latency_windows: 0,
        barriers: Vec::new(),
        segments,
        contention: None,
    }
}

// ---------------------------------------------------------------- the parts

/// A quote stream with no venue behind it.
/// The touch the scripted feed last produced, so the pretend venue's fills
/// land at a price that means something. Both feeds live on the engine's own
/// thread, which is what makes a plain `Rc<Cell<_>>` the right sharing here.
pub type LastTouch = Rc<Cell<(f64, f64)>>;
/// Pulls the private stream has reported cancelled, shared the same way.
pub type PullsAnswered = Rc<Cell<u64>>;

pub struct ScriptedFeed {
    touch: LastTouch,
    symbols: Vec<SymbolId>,
    remaining: u64,
    sent: u64,
    gap: Option<Duration>,
    start: Option<tokio::time::Instant>,
    px: f64,
    close_after_pulls: Option<(PullsAnswered, u64)>,
}

impl ScriptedFeed {
    pub fn new(symbols: Vec<SymbolId>, events: u64, rate: u64) -> Self {
        ScriptedFeed::sharing(symbols, events, rate, LastTouch::default())
    }

    pub fn sharing(symbols: Vec<SymbolId>, events: u64, rate: u64, touch: LastTouch) -> Self {
        ScriptedFeed {
            touch,
            symbols,
            remaining: events,
            sent: 0,
            gap: if rate > 0 {
                Some(Duration::from_nanos(1_000_000_000 / rate.max(1)))
            } else {
                None
            },
            start: None,
            px: 30_000.0,
            close_after_pulls: None,
        }
    }

    /// Close once `target` pulls have come back, `events` staying the ceiling.
    pub fn closing_after_pulls(mut self, answered: PullsAnswered, target: u64) -> Self {
        self.close_after_pulls = Some((answered, target));
        self
    }
}

impl MarketFeed for ScriptedFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        let pulled_enough = self
            .close_after_pulls
            .as_ref()
            .is_some_and(|(answered, target)| answered.get() >= *target);
        if self.remaining == 0 || self.symbols.is_empty() || pulled_enough {
            return Err(FeedError::Closed);
        }
        if let Some(gap) = self.gap {
            let start = *self.start.get_or_insert_with(tokio::time::Instant::now);
            tokio::time::sleep_until(start + gap * self.sent as u32).await;
        }
        if self.gap.is_none() {
            tokio::task::yield_now().await;
        }
        self.remaining -= 1;
        self.sent += 1;
        // A small saw-tooth so the price is not constant.
        self.px += if self.sent.is_multiple_of(2) {
            0.5
        } else {
            -0.5
        };
        let symbol = self.symbols[(self.sent as usize) % self.symbols.len()];
        self.touch.set((self.px, self.px + 0.5));
        Ok(MarketEvent::Quote {
            symbol,
            quote: Quote {
                bid_px: self.px,
                bid_qty: 1.5,
                ask_px: self.px + 0.5,
                ask_qty: 1.5,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
                seq: self.sent,
            },
        })
    }
}

/// The private stream the `--fills` and `--contention` benches get:
/// everything the venue accepted comes back filled at the touch it would have
/// crossed, and everything it pulled comes back cancelled.
///
/// One fill per order, at the far touch and never partial. That is not what a
/// real venue does, and it does not need to be -- what this exists to drive is
/// the engine's own path from a fill to a priced one, which a bench whose
/// venue never fills leaves entirely unrun. The cancellation half is not a
/// nicety either: a pull is only terminal when the private stream says so, and
/// a workload that keeps resting new orders needs the old ones to end.
pub struct FillingOrderFeed {
    orders: tokio::sync::mpsc::UnboundedReceiver<OrderRequest>,
    pulled: tokio::sync::mpsc::UnboundedReceiver<String>,
    touch: LastTouch,
    answered_pulls: Option<PullsAnswered>,
}

impl FillingOrderFeed {
    pub fn new(
        orders: tokio::sync::mpsc::UnboundedReceiver<OrderRequest>,
        pulled: tokio::sync::mpsc::UnboundedReceiver<String>,
        touch: LastTouch,
    ) -> Self {
        FillingOrderFeed {
            orders,
            pulled,
            touch,
            answered_pulls: None,
        }
    }

    pub fn counting_pulls(mut self, into: PullsAnswered) -> Self {
        self.answered_pulls = Some(into);
        self
    }
}

impl OrderFeed for FillingOrderFeed {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        // `recv` is cancel-safe, which the loop's `select!` requires: it drops
        // the futures of every branch that did not win, this `select!`
        // included.
        let taken = tokio::select! {
            biased;
            pulled = self.pulled.recv() => {
                return match pulled {
                    Some(client_order_id) => {
                        if let Some(answered) = &self.answered_pulls {
                            answered.set(answered.get() + 1);
                        }
                        Ok(OrderUpdate::Cancelled {
                            client_order_id,
                            recv_ns: clock::now_ns(),
                        })
                    }
                    // Both senders live in the venue for the run's whole life.
                    None => std::future::pending().await,
                }
            }
            request = self.orders.recv() => request,
        };
        let Some(request) = taken else {
            return std::future::pending().await;
        };
        let (bid, ask) = self.touch.get();
        let px = match (request.kind, request.side) {
            (OrderKind::Limit { px, .. }, _) => px,
            (OrderKind::Market, Side::Buy) => ask,
            (OrderKind::Market, Side::Sell) => bid,
        };
        Ok(OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: String::new(),
            client_order_id: request.client_order_id,
            symbol: request.symbol,
            side: request.side,
            qty: request.qty,
            px,
            // A round two basis points, the maker rate this venue's real
            // counterpart charges on most names.
            fee: Some((px * request.qty * 0.0002).abs()),
            is_maker: matches!(request.kind, OrderKind::Limit { .. }),
            forced_close: None,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: clock::now_ns(),
        })
    }
}

/// No private stream in the bench: every reply comes back from the send.
pub struct SilentOrderFeed;

impl OrderFeed for SilentOrderFeed {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        std::future::pending().await
    }
}

fn benchmark_risk() -> Result<engine_risk::Kernel, EngineError> {
    engine_risk::Kernel::new(engine_risk::KernelConfig {
        max_account_view_age_ns: 600_000_000_000,
        envelope: engine_risk::EnvelopeConfig {
            tracks_equity: false,
            reference_usdt: 10_000.0,
            equity_fraction: 1.0,
            expand_dead_band_fraction: 0.0,
            gross_notional_multiple: 100.0,
            disaster_stop_fraction: 0.35,
            max_component_gross_notional_usdt: 1_000_000.0,
            max_symbol_notional_usdt: 1_000_000.0,
            max_initial_margin_usdt: 9_000.0,
        },
        leverage: 100.0,
        qty_tolerance: 1e-12,
        max_rolling_loss_fraction: 1.0,
    })
    .map_err(|error| EngineError::Boot(error.to_string()))
}

/// A protocol-test risk fixture; the benchmark uses `engine-risk`.
pub struct AllowEverything;

impl RiskKernel for AllowEverything {
    fn assess(&mut self, intent: &Intent, _account: &AccountView, _now_ns: u64) -> RiskVerdict {
        RiskVerdict::Allow { qty: intent.qty }
    }

    fn on_update(&mut self, _update: &OrderUpdate) {}
}

/// Buys a little every Nth quote, with a stop attached.
pub use engine_strategies::bench::BenchStrategy;

// ------------------------------------------------------- the pretend venue

/// What `--quota` gives the pretend venue, in the report's words.
const QUOTA_SHAPE: &str =
    "16 signed requests per 2 s, 4 of them reserved for risk-off (MEXC's shape)";

/// What `--quota` holds the pretend venue's reply for, unless `--venue-delay-ms`
/// says otherwise.
///
/// The venue task runs one call at a time, so a 200 ms round trip caps it at
/// five commands a second — below the quota's own eight, which then never
/// binds and holds nothing back. At 5 ms the quota is the tighter of the two,
/// which is the whole point of the dial.
pub const QUOTA_VENUE_DELAY: std::time::Duration = std::time::Duration::from_millis(5);

/// Quotes between risk-off sweeps under `--quota`: every resting order pulled
/// at once, on the quote clock rather than behind an answered opening. At the
/// `--contention` default of 200 Hz this is one sweep every 250 ms, several
/// inside one spent quota window, so a sweep lands while openings are queued.
const RISK_OFF_SWEEP_QUOTES: u64 = 50;

const QUOTA_WINDOW: std::time::Duration = std::time::Duration::from_secs(2);
const QUOTA_REQUESTS: usize = 16;
const QUOTA_RESERVED: usize = 4;

/// A local request quota with MEXC's shape: one rolling window over every
/// signed request, with the last four slots spendable only by risk-off. An
/// opening is therefore held back while a cancel still goes.
#[derive(Default)]
struct BenchQuota {
    /// Never locked across an await: the venue task reads the wait from a
    /// synchronous context.
    admissions: std::sync::Mutex<std::collections::VecDeque<tokio::time::Instant>>,
    held_ns: std::sync::atomic::AtomicU64,
}

impl BenchQuota {
    /// The wait a request of this kind would serve. Spends nothing.
    fn wait_at(&self, now: tokio::time::Instant, protective: bool) -> Duration {
        let mut admissions = self.admissions.lock().expect("bench quota poisoned");
        while admissions
            .front()
            .is_some_and(|at| now.duration_since(*at) >= QUOTA_WINDOW)
        {
            admissions.pop_front();
        }
        let limit = if protective {
            QUOTA_REQUESTS
        } else {
            QUOTA_REQUESTS - QUOTA_RESERVED
        };
        match admissions.front() {
            Some(oldest) if admissions.len() >= limit => {
                (*oldest + QUOTA_WINDOW).saturating_duration_since(now)
            }
            _ => Duration::ZERO,
        }
    }

    /// Wait out the quota and take a slot, the way an adapter's pacer does
    /// inside its own call.
    async fn admit(&self, protective: bool) {
        let began = tokio::time::Instant::now();
        loop {
            let now = tokio::time::Instant::now();
            let wait = self.wait_at(now, protective);
            if wait.is_zero() {
                self.admissions
                    .lock()
                    .expect("bench quota poisoned")
                    .push_back(now);
                // Added, not stored: one gateway call may admit several
                // requests, and what it held back is all of them together.
                self.held_ns.fetch_add(
                    began.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64,
                    std::sync::atomic::Ordering::Relaxed,
                );
                return;
            }
            tokio::time::sleep(wait).await;
        }
    }
}

/// The client side: signs, writes to a warm socket, reads the reply.
pub struct HttpVenue {
    /// Where accepted orders go to be filled, when the bench asked for fills.
    accepted: Option<tokio::sync::mpsc::UnboundedSender<OrderRequest>>,
    /// Where pulled orders go to be published as cancelled.
    pulled: Option<tokio::sync::mpsc::UnboundedSender<String>>,
    addr: SocketAddr,
    stream: Option<tokio::net::TcpStream>,
    buf: Vec<u8>,
    symbols: Vec<Symbol>,
    key: Vec<u8>,
    last_sent_ns: u64,
    quota: Option<BenchQuota>,
}

struct HttpAccountRecovery {
    addr: SocketAddr,
    key: Vec<u8>,
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for HttpAccountRecovery {
    async fn account_view(&self, _symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        let observed_ns = clock::now_ns();
        let mut stream = tokio::net::TcpStream::connect(self.addr)
            .await
            .map_err(|error| VenueError::Transport(error.to_string()))?;
        stream
            .set_nodelay(true)
            .map_err(|error| VenueError::Transport(error.to_string()))?;
        let request = signed_http_request(&self.key, "/v5/account/wallet-balance", "{}");
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|error| VenueError::Transport(error.to_string()))?;
        let mut buf = Vec::with_capacity(8 * 1024);
        let body = read_http_body(&mut stream, &mut buf).await?;
        let reply = serde_json::from_slice(&body)
            .map_err(|error| VenueError::BadReply(error.to_string()))?;
        Ok(bench_account_view(&reply, observed_ns))
    }
    async fn executions(
        &self,
        _symbols: &[Symbol],
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        Ok(engine_types::ExecutionHistory::default())
    }
}

fn signed_http_request(key: &[u8], path: &str, body: &str) -> String {
    let timestamp = clock::wall_ms();
    let signature = sign(key, &format!("{timestamp}bench5000{body}"));
    format!(
        "POST {path} HTTP/1.1\r\nHost: bench\r\nContent-Type: application/json\r\n\
         X-BAPI-API-KEY: bench\r\nX-BAPI-TIMESTAMP: {timestamp}\r\nX-BAPI-SIGN: {signature}\r\n\
         Content-Length: {}\r\n\r\n{body}",
        body.len()
    )
}

fn bench_account_view(reply: &serde_json::Value, observed_ns: u64) -> AccountView {
    AccountView {
        exact_amounts: None,
        equity_usdt: reply
            .pointer("/result/equity")
            .and_then(|value| value.as_f64())
            .unwrap_or(10_000.0),
        available_usdt: reply
            .pointer("/result/available")
            .and_then(|value| value.as_f64())
            .unwrap_or(10_000.0),
        positions: Vec::new(),
        observed_ns,
    }
}

impl HttpVenue {
    pub fn new(addr: SocketAddr, symbols: Vec<Symbol>) -> Self {
        HttpVenue {
            accepted: None,
            pulled: None,
            addr,
            stream: None,
            buf: Vec::with_capacity(8 * 1024),
            symbols,
            key: b"bench-secret-key".to_vec(),
            last_sent_ns: 0,
            quota: None,
        }
    }

    /// Pace this venue like an adapter that holds its own requests back.
    pub fn under_quota(mut self) -> Self {
        self.quota = Some(BenchQuota::default());
        self
    }

    /// Send every order this venue accepts to a feed that will fill it.
    pub fn filling(mut self, to: tokio::sync::mpsc::UnboundedSender<OrderRequest>) -> Self {
        self.accepted = Some(to);
        self
    }

    /// Publish every pull this venue takes, the way a venue's private stream
    /// does. Without it a cancelled order never reaches a terminal state.
    pub fn publishing_pulls(mut self, to: tokio::sync::mpsc::UnboundedSender<String>) -> Self {
        self.pulled = Some(to);
        self
    }

    async fn connect(&mut self) -> Result<(), VenueError> {
        if self.stream.is_some() {
            return Ok(());
        }
        let stream = tokio::net::TcpStream::connect(self.addr)
            .await
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        stream
            .set_nodelay(true)
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        self.stream = Some(stream);
        Ok(())
    }

    async fn call(&mut self, path: &str, body: &str) -> Result<serde_json::Value, VenueError> {
        for attempt in 0..2 {
            self.connect().await?;
            match self.try_call(path, body).await {
                Ok(value) => return Ok(value),
                Err(e) if attempt == 0 => {
                    // A keep-alive connection can be closed under us; one
                    // reconnect, then the error stands.
                    self.stream = None;
                    tracing::debug!(error = %e, "reconnecting to the bench venue");
                }
                Err(e) => return Err(e),
            }
        }
        Err(VenueError::Transport("unreachable".into()))
    }

    async fn try_call(&mut self, path: &str, body: &str) -> Result<serde_json::Value, VenueError> {
        let request = signed_http_request(&self.key, path, body);
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| VenueError::Transport("no connection".into()))?;
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        self.last_sent_ns = clock::now_ns();

        self.buf.clear();
        let body = read_http_body(self.stream.as_mut().unwrap(), &mut self.buf).await?;
        serde_json::from_slice(&body).map_err(|e| VenueError::BadReply(e.to_string()))
    }
}

#[engine_types::async_trait]
impl VenueGateway for HttpVenue {
    /// The same answers Bybit gives, so the bench walks the same paths the
    /// shipping gateway walks.
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            native_position_stop: true,
            amend_in_place: true,
            set_leverage: false,
            close_position_below_minimum: false,
        }
    }

    /// The bench never takes an account lease — it is a pretend venue on this
    /// box — but the loop is generic over the whole contract, so the answer
    /// has to exist.
    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            venue: "mock".to_string(),
            user_id: "7000001".to_string(),
            realm: "demo".to_string(),
        })
    }

    /// The stand-in quota answers the venue task the way a paced adapter
    /// does: what it reports here is what the call below then waits out.
    fn quota_wait(&self, command: engine_types::QueuedCommand) -> Duration {
        use engine_types::QueuedCommand as Queued;
        let protective = match command {
            Queued::Reducing { .. } | Queued::Cancel { .. } | Queued::PositionStop => true,
            Queued::Opening { .. } => false,
            // Neither call below admits, so neither is held back here.
            Queued::Amend { .. } | Queued::Administration => return Duration::ZERO,
        };
        self.quota
            .as_ref()
            .map(|quota| quota.wait_at(tokio::time::Instant::now(), protective))
            .unwrap_or_default()
    }

    /// The quota hold this command served, so the measured call span stays the
    /// call itself. Taking it clears it.
    fn take_rate_wait_ns(&mut self) -> Option<u64> {
        self.quota
            .as_ref()
            .map(|quota| quota.held_ns.swap(0, std::sync::atomic::Ordering::Relaxed))
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let px = match req.kind {
            OrderKind::Limit { px, .. } => px,
            OrderKind::Market => 0.0,
        };
        let body = format!(
            "{{\"category\":\"linear\",\"symbol\":\"{}\",\"side\":\"{:?}\",\"qty\":\"{}\",\
             \"price\":\"{px}\",\"orderLinkId\":\"{}\",\"reduceOnly\":{}}}",
            req.symbol.0, req.side, req.qty, req.client_order_id, req.reduce_only
        );
        if let Some(quota) = &self.quota {
            quota.admit(req.reduce_only).await;
        }
        let reply = self.call("/v5/order/create", &body).await?;
        let ack_ns = clock::now_ns();
        let code = reply.get("retCode").and_then(|v| v.as_i64()).unwrap_or(-1);
        if code != 0 {
            return Err(VenueError::Rejected {
                code,
                message: reply
                    .get("retMsg")
                    .and_then(|v| v.as_str())
                    .unwrap_or("no message")
                    .to_string(),
            });
        }
        let venue_order_id = reply
            .pointer("/result/orderId")
            .and_then(|v| v.as_str())
            .ok_or_else(|| VenueError::BadReply("no orderId".into()))?
            .to_string();
        // After the ack and never before it: a fill for an order the engine
        // has not been told was accepted is not a sequence any venue produces.
        if let Some(accepted) = &self.accepted {
            let _ = accepted.send(req.clone());
        }
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id,
            sent_ns: self.last_sent_ns,
            ack_ns,
        })
    }

    async fn cancel_order(&mut self, _symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        if let Some(quota) = &self.quota {
            quota.admit(true).await;
        }
        self.call("/v5/order/cancel", &format!("{{\"orderLinkId\":\"{id}\"}}"))
            .await?;
        // After the answer and never before it, the same order the accepted
        // placement observes above.
        if let Some(pulled) = &self.pulled {
            let _ = pulled.send(id.to_string());
        }
        Ok(())
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        let px = spec
            .px
            .map(|px| format!(",\"price\":\"{px}\""))
            .unwrap_or_default();
        let qty = spec
            .qty
            .map(|qty| format!(",\"qty\":\"{qty}\""))
            .unwrap_or_default();
        self.call(
            "/v5/order/amend",
            &format!("{{\"orderLinkId\":\"{id}\"{px}{qty}}}"),
        )
        .await
        .map(|_| ())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        if let Some(quota) = &self.quota {
            quota.admit(true).await;
        }
        self.call(
            "/v5/position/trading-stop",
            &format!(
                "{{\"symbol\":\"{}\",\"stopLoss\":\"{trigger_px}\"}}",
                symbol.0
            ),
        )
        .await
        .map(|_| ())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        let reply = self.call("/v5/account/wallet-balance", "{}").await?;
        Ok(bench_account_view(&reply, clock::now_ns()))
    }

    fn account_recovery_client(
        &self,
    ) -> Option<Box<dyn engine_types::orders::AccountRecoveryClient>> {
        Some(Box::new(HttpAccountRecovery {
            addr: self.addr,
            key: self.key.clone(),
        }))
    }

    /// The benchmark measures the order path, and this read happens once at
    /// boot, so it costs nothing to answer honestly: a pretend venue with a
    /// pretend book is working nothing.
    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        Ok(Vec::new())
    }

    /// The bench account exists only in this process. Its fill feed is
    /// drained by the same run and there is no older execution history.
    async fn executions(
        &mut self,
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        Ok(engine_types::ExecutionHistory::default())
    }

    async fn instrument_specs(
        &mut self,
    ) -> Result<Vec<(String, engine_types::numeric::ExactInstrumentSpec)>, VenueError> {
        use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
        self.symbols
            .iter()
            .map(|symbol| {
                let decimal = |text: &str| {
                    text.parse::<Exact>()
                        .map_err(|error| VenueError::BadReply(error.to_string()))
                };
                Ok((
                    symbol.clone(),
                    ExactInstrumentSpec {
                        native_symbol: symbol.clone(),
                        base_asset: AssetId::Named(
                            symbol.strip_suffix("USDT").unwrap_or(symbol).into(),
                        ),
                        quote_asset: AssetId::Named("USDT".into()),
                        settlement_asset: AssetId::Named("USDT".into()),
                        tick_size: Some(decimal("0.5")?),
                        min_price: None,
                        max_price: None,
                        price_precision: PricePrecision::Tick,
                        qty_step: Some(decimal("0.001")?),
                        min_qty: Some(decimal("0.001")?),
                        market_qty_step: Some(decimal("0.001")?),
                        market_min_qty: Some(decimal("0.001")?),
                        max_qty: None,
                        max_market_qty: None,
                        min_notional: Some(decimal("5")?),
                        contract_multiplier: Some(Exact::one()),
                        fee_assets: Some(vec![AssetId::Named("USDT".into())]),
                        fee_step: None,
                    },
                ))
            })
            .collect()
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.call("/v5/market/instruments-info", "{}").await?;
        Ok(self
            .symbols
            .iter()
            .map(|symbol| {
                (
                    symbol.clone(),
                    InstrumentRule {
                        tick_size: 0.5,
                        qty_step: 0.001,
                        min_qty: 0.001,
                        min_notional: 5.0,
                    },
                )
            })
            .collect())
    }
}

/// Start the pretend venue on its own thread, so it is somewhere else the way
/// a real venue is somewhere else, and the engine's thread does only its own
/// work.
pub fn start_mock_venue() -> Result<SocketAddr, EngineError> {
    start_mock_venue_with(Duration::ZERO)
}

/// Every reply held for the same length of time.
pub fn start_mock_venue_with(delay: Duration) -> Result<SocketAddr, EngineError> {
    start_mock_venue_delayed(VenueDelays {
        placement: delay,
        cancel: delay,
    })
}

/// How long the pretend venue holds each kind of reply. A venue that answers
/// a pull as slowly as a placement cannot show what a cancel waits for.
#[derive(Clone, Copy, Debug, Default)]
pub struct VenueDelays {
    /// `/v5/order/cancel`.
    pub cancel: Duration,
    /// Everything else, placements included.
    pub placement: Duration,
}

impl VenueDelays {
    /// Bybit's own path for a pull, which the bench's client writes verbatim.
    const CANCEL_PATH: &'static [u8] = b"/v5/order/cancel";

    fn of(&self, head: &[u8]) -> Duration {
        let request_line = head.split(|byte| *byte == b'\r').next().unwrap_or_default();
        if request_line
            .windows(Self::CANCEL_PATH.len())
            .any(|window| window == Self::CANCEL_PATH)
        {
            self.cancel
        } else {
            self.placement
        }
    }
}

pub fn start_mock_venue_delayed(delays: VenueDelays) -> Result<SocketAddr, EngineError> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")
        .map_err(|e| EngineError::Boot(format!("cannot open the bench venue socket: {e}")))?;
    let addr = listener
        .local_addr()
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    listener
        .set_nonblocking(true)
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    std::thread::Builder::new()
        .name("bench-venue".into())
        .spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("bench venue runtime");
            runtime.block_on(async move {
                let listener = tokio::net::TcpListener::from_std(listener).expect("listener");
                loop {
                    match listener.accept().await {
                        Ok((socket, _)) => {
                            tokio::spawn(serve(socket, delays));
                        }
                        Err(e) => {
                            tracing::warn!(error = %e, "bench venue accept failed");
                            return;
                        }
                    }
                }
            });
        })
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    Ok(addr)
}

async fn serve(mut socket: tokio::net::TcpStream, delays: VenueDelays) {
    let _ = socket.set_nodelay(true);
    let mut buf = Vec::with_capacity(8 * 1024);
    let mut orders = 0u64;
    loop {
        buf.clear();
        let (head, request) = match read_http_message(&mut socket, &mut buf).await {
            Ok(message) => message,
            Err(_) => return,
        };
        let delay = delays.of(&head);
        orders += 1;
        let link = serde_json::from_slice::<serde_json::Value>(&request)
            .ok()
            .and_then(|v| {
                v.get("orderLinkId")
                    .and_then(|s| s.as_str())
                    .map(|s| s.to_string())
            })
            .unwrap_or_default();
        let body = format!(
            "{{\"retCode\":0,\"retMsg\":\"OK\",\"result\":{{\"orderId\":\"v{orders}\",\
             \"orderLinkId\":\"{link}\",\"equity\":10000.0,\"available\":9000.0}}}}"
        );
        let reply = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        // Held before the reply is written, which is where a venue's distance
        // actually sits: after it has the request, before the answer starts back.
        if !delay.is_zero() {
            tokio::time::sleep(delay).await;
        }
        if socket.write_all(reply.as_bytes()).await.is_err() {
            return;
        }
    }
}

/// Read one HTTP message (request or response) and return its body.
async fn read_http_body(
    socket: &mut tokio::net::TcpStream,
    buf: &mut Vec<u8>,
) -> Result<Vec<u8>, VenueError> {
    read_http_message(socket, buf)
        .await
        .map(|(_head, body)| body)
}

/// One HTTP message split into its head and its body. The pretend venue reads
/// the head because the request line names the path, and the path is what
/// decides how long it holds the reply.
async fn read_http_message(
    socket: &mut tokio::net::TcpStream,
    buf: &mut Vec<u8>,
) -> Result<(Vec<u8>, Vec<u8>), VenueError> {
    let mut chunk = [0u8; 4096];
    loop {
        if let Some(head_end) = find_head_end(buf) {
            let length = content_length(&buf[..head_end]);
            let total = head_end + length;
            if buf.len() >= total {
                let head = buf[..head_end].to_vec();
                let body = buf[head_end..total].to_vec();
                buf.drain(..total);
                return Ok((head, body));
            }
        }
        let read = socket
            .read(&mut chunk)
            .await
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        if read == 0 {
            return Err(VenueError::Transport(
                std::io::Error::from(ErrorKind::UnexpectedEof).to_string(),
            ));
        }
        buf.extend_from_slice(&chunk[..read]);
    }
}

fn find_head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|p| p + 4)
}

fn content_length(head: &[u8]) -> usize {
    let text = String::from_utf8_lossy(head);
    for line in text.split("\r\n") {
        let mut parts = line.splitn(2, ':');
        if let (Some(name), Some(value)) = (parts.next(), parts.next()) {
            if name.eq_ignore_ascii_case("content-length") {
                return value.trim().parse().unwrap_or(0);
            }
        }
    }
    0
}

/// HMAC-SHA256, the same signing work the real gateway does, so the bench
/// carries that cost too.
fn sign(key: &[u8], message: &str) -> String {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        let digest = Sha256::digest(key);
        block[..32].copy_from_slice(&digest);
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner_pad = [0x36u8; 64];
    let mut outer_pad = [0x5cu8; 64];
    for i in 0..64 {
        inner_pad[i] ^= block[i];
        outer_pad[i] ^= block[i];
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(message.as_bytes());
    let inner_digest = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner_digest);
    hex::encode(outer.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn benchmark_reports_p999_distinct_from_p99_and_max() {
        let mut ledger = LatencyLedger::new(0);
        for (count, ns) in [(9_900, 100), (90, 1_000), (9, 10_000), (1, 100_000)] {
            for _ in 0..count {
                ledger.record(Segment::Decide, ns);
            }
        }
        let result = summarise(&ledger, 10_000, 10_000, 1);
        let json: serde_json::Value = serde_json::from_str(&result.as_json()).unwrap();
        let decide = &json[segment_name(Segment::Decide)];
        assert_eq!(decide["count"].as_u64(), Some(10_000));
        assert_eq!(decide["p99_ns"].as_u64(), Some(100));
        assert_eq!(decide["p999_ns"].as_u64(), Some(1_000));
        assert!(decide["max_ns"].as_u64().unwrap() >= 100_000);
        assert!(result.table().contains("p99.9"));
    }

    #[test]
    fn benchmark_p999_distinguishes_empty_from_measured_zero() {
        let mut ledger = LatencyLedger::new(0);
        ledger.record(Segment::Decide, 0);
        let result = summarise(&ledger, 1, 1, 1);
        let json: serde_json::Value = serde_json::from_str(&result.as_json()).unwrap();
        assert_eq!(json[segment_name(Segment::Decide)]["p999_ns"], 0);
        for (segment, q) in &result.segments {
            if *segment == Segment::Decide {
                continue;
            }
            assert_eq!(q.count, 0);
            assert_eq!(
                json[segment_name(*segment)].get("p999_ns"),
                Some(&serde_json::Value::Null)
            );
        }
        assert_eq!(
            result.table().matches("unavailable").count(),
            result.segments.len() - 1
        );
    }

    #[test]
    fn signing_is_hmac_sha256() {
        // RFC 4231 test case 1.
        let key = [0x0bu8; 20];
        assert_eq!(
            sign(&key, "Hi There"),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
    }

    #[test]
    fn http_head_and_length_are_parsed() {
        let head = b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\n";
        assert_eq!(find_head_end(head), Some(head.len()));
        assert_eq!(content_length(head), 17);
    }

    #[test]
    fn only_the_cancel_path_gets_the_cancel_delay() {
        let delays = VenueDelays {
            cancel: Duration::from_millis(1),
            placement: Duration::from_millis(200),
        };
        let head = |path: &str| format!("POST {path} HTTP/1.1\r\nHost: bench\r\n\r\n");
        assert_eq!(
            delays.of(head("/v5/order/cancel").as_bytes()),
            delays.cancel
        );
        for path in [
            "/v5/order/create",
            "/v5/order/amend",
            "/v5/position/trading-stop",
            "/v5/account/wallet-balance",
            "/v5/market/instruments-info",
        ] {
            assert_eq!(
                delays.of(head(path).as_bytes()),
                delays.placement,
                "{path} was answered on the cancel clock"
            );
        }
        // The body is not the request line: a cancel's own id in a later
        // header or in the body must not decide the wait.
        let disguised = "POST /v5/order/create HTTP/1.1\r\nX-Note: /v5/order/cancel\r\n\r\n";
        assert_eq!(delays.of(disguised.as_bytes()), delays.placement);
    }
}

#[cfg(test)]
mod recovery_client_tests {
    use super::*;
    #[tokio::test(start_paused = true)]
    async fn independent_bench_recovery_uses_another_http_socket() {
        use std::sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        };
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let connections = Arc::new(AtomicUsize::new(0));
        let seen = connections.clone();
        let server = tokio::spawn(async move {
            loop {
                let (socket, _) = listener.accept().await.unwrap();
                seen.fetch_add(1, Ordering::SeqCst);
                tokio::spawn(serve(socket, VenueDelays::default()));
            }
        });
        let mut venue = HttpVenue::new(address, vec!["BTCUSDT".into()]);
        venue.connect().await.unwrap();
        let mutation_socket = venue.stream.as_ref().unwrap().local_addr().unwrap();
        let client = venue
            .account_recovery_client()
            .expect("independent benchmark account recovery");
        let began = clock::now_ns();
        let view = client.account_view(&["BTCUSDT".into()]).await.unwrap();
        assert_eq!((view.equity_usdt, view.available_usdt), (10_000.0, 9_000.0));
        assert!(view.positions.is_empty());
        assert!((began..=clock::now_ns()).contains(&view.observed_ns));
        assert_eq!(
            connections.load(Ordering::SeqCst),
            2,
            "account read reused the mutation socket"
        );
        assert_eq!(
            venue.last_sent_ns, 0,
            "account read changed mutation timing"
        );
        assert_eq!(
            venue.stream.as_ref().unwrap().local_addr().unwrap(),
            mutation_socket
        );
        assert!(client
            .executions(&["BTCUSDT".into()], 0, 1)
            .await
            .unwrap()
            .is_empty());
        assert_eq!(
            venue.account_view().await.unwrap().available_usdt,
            view.available_usdt
        );
        assert_eq!(connections.load(Ordering::SeqCst), 2);
        server.abort();
    }
}
