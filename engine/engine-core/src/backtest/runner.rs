//! `engine backtest`: assemble the parts, run the loop, reconcile, report.
//!
//! The assembly mirrors the live runner's: the same `symbol_order` seeds the
//! venue, the feed, and the engine, so ids agree by construction; the log is
//! claimed before it is opened; the strategies and the risk kernel are the
//! configured ones. What differs is only what has to: a fresh log is
//! required (a replay that boots from another run's state is not a replay),
//! the venue is simulated, and the loop's timers run on the tape's clock.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::{InstrumentRule, Symbol, WalRecord};
use engine_wal::WalWriter;
use serde::Serialize;

use super::feed::{pump, Cursor, TapeFeed};
use super::scheduler::{Scheduler, VirtualTimer};
use super::signals::SignalReplayFeed;
use super::tape::{read_instruments, TapeReader, TapeStats};
use super::venue::{Accounting, SimOrderFeed, SimVenueGateway, SimulatedVenue, VenueParams};
use crate::assembly;
use crate::config;
use crate::controls::NoControls;
use crate::engine::{Engine, EngineError};
use crate::trades::Trades;

#[derive(Clone, Debug)]
pub struct BacktestOptions {
    pub engine_config_path: PathBuf,
    /// `market_tape` rows, receive-time ordered: a `.jsonl` or `.jsonl.zst`.
    pub tape_path: PathBuf,
    /// The recorder's `instruments_snapshot` (`_meta/instruments-*.json[.zst]`).
    pub instruments_path: PathBuf,
    pub signals_path: Option<PathBuf>,
    /// Must not exist, or be empty: every run starts from nothing.
    pub wal_path: PathBuf,
    pub trades_path: Option<PathBuf>,
    pub equity_path: Option<PathBuf>,
    pub report_path: Option<PathBuf>,
    pub initial_capital_usdt: f64,
    pub taker_fee_rate: f64,
    pub maker_fee_rate: f64,
    pub order_rtt_ms: u64,
    pub private_latency_ms: u64,
    pub maintenance_margin_rate: f64,
}

impl Default for BacktestOptions {
    fn default() -> Self {
        BacktestOptions {
            engine_config_path: PathBuf::from("engine.toml"),
            tape_path: PathBuf::new(),
            instruments_path: PathBuf::new(),
            signals_path: None,
            wal_path: PathBuf::new(),
            trades_path: None,
            equity_path: None,
            report_path: None,
            initial_capital_usdt: 10_000.0,
            // Bybit VIP0 linear perpetuals.
            taker_fee_rate: 0.00055,
            maker_fee_rate: 0.0002,
            // `engine bench` doc: the venue round trip from the host is about
            // 175 ms and no rebuild changes that.
            order_rtt_ms: 175,
            private_latency_ms: 60,
            // Bybit's lowest linear maintenance margin tier.
            maintenance_margin_rate: 0.005,
        }
    }
}

/// What the engine's own closed-trade file says, re-read after the run.
#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct EngineLedger {
    pub closed_trips: u64,
    /// Trips whose money is known (both legs in this log).
    pub priced_trips: u64,
    pub gross_usdt: f64,
    pub fees_usdt: f64,
    pub net_usdt: f64,
    pub wins: u64,
}

/// Whether the venue's books and the engine's ledger tell the same story.
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Reconciliation {
    /// Σ `round_trip.net_usdt` over the engine's closed trips.
    pub engine_closed_net_usdt: f64,
    /// Venue realized P&L less the fees on closed trips.
    pub venue_closed_net_usdt: f64,
    pub difference_usdt: f64,
    /// `Some(true)` only when the account is flat at the end and the two
    /// agree; `None` when positions are open, where a partial close is
    /// realized on the venue and still open in the engine's ledger.
    pub agrees: Option<bool>,
}

#[derive(Clone, Debug, Serialize)]
pub struct BacktestReport {
    pub tape: TapeStats,
    pub unknown_symbol_rows: u64,
    pub signals_replayed: usize,
    pub market_events: u64,
    pub orders_sent: u64,
    pub stopped_by: String,
    pub venue: Accounting,
    pub engine: EngineLedger,
    pub reconciliation: Reconciliation,
    pub start_wall_ms: i64,
    pub end_wall_ms: i64,
    pub wal_path: PathBuf,
    pub trades_path: Option<PathBuf>,
    pub equity_path: Option<PathBuf>,
}

impl BacktestReport {
    pub fn table(&self) -> String {
        let span_h = (self.end_wall_ms - self.start_wall_ms) as f64 / 3_600_000.0;
        let v = &self.venue;
        let e = &self.engine;
        let r = &self.reconciliation;
        let agrees = match r.agrees {
            Some(true) => "yes".to_string(),
            Some(false) => format!("NO, off by {:.6}", r.difference_usdt),
            None => "not checkable: positions open at tape end".to_string(),
        };
        format!(
            "the tape\n\
             \x20 rows {} (books {}, trades {}, tickers {}); {:.2} h of market time; unknown-symbol rows {}\n\
             \x20 skipped kinds: {}\n\
             the loop\n\
             \x20 market events {}, orders sent {}, stopped by {}, signals replayed {}\n\
             the venue's books\n\
             \x20 cash {:.2} -> {:.2} USDT; equity {:.2} USDT; unrealized {:.2}; open positions {}; resting orders {}\n\
             \x20 realized {:.2}; fees {:.2} (of which on open entries {:.2}); funding {:.2} over {} settlements\n\
             \x20 fills {} (maker {}, stop {}, liquidation {}, priced at mark for want of a book {}); rejected orders {}; liquidated {}\n\
             the engine's ledger\n\
             \x20 closed trips {} ({} priced, {} won); gross {:.2}; fees {:.2}; net {:.2} USDT\n\
             reconciliation\n\
             \x20 engine closed net {:.6} vs venue closed net {:.6}: agrees {}\n\
             files\n\
             \x20 log {}\n\x20 trades {}\n\x20 equity {}\n",
            self.tape.rows,
            self.tape.books,
            self.tape.trades,
            self.tape.tickers,
            span_h,
            self.unknown_symbol_rows,
            if self.tape.skipped_by_kind.is_empty() {
                "none".to_string()
            } else {
                self.tape
                    .skipped_by_kind
                    .iter()
                    .map(|(k, n)| format!("{k}={n}"))
                    .collect::<Vec<_>>()
                    .join(", ")
            },
            self.market_events,
            self.orders_sent,
            self.stopped_by,
            self.signals_replayed,
            v.initial_cash_usdt,
            v.cash_usdt,
            v.equity_usdt,
            v.unrealized_usdt,
            v.open_positions,
            v.resting_orders,
            v.realized_pnl_usdt,
            v.fees_paid_usdt,
            v.open_entry_fees_usdt,
            v.funding_paid_usdt,
            v.funding_settlements,
            v.fills,
            v.maker_fills,
            v.stop_fills,
            v.liquidation_fills,
            v.fills_without_book,
            v.rejected_orders,
            v.liquidated,
            e.closed_trips,
            e.priced_trips,
            e.wins,
            e.gross_usdt,
            e.fees_usdt,
            e.net_usdt,
            r.engine_closed_net_usdt,
            r.venue_closed_net_usdt,
            agrees,
            self.wal_path.display(),
            self.trades_path
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "not written".to_string()),
            self.equity_path
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "not written".to_string()),
        )
    }
}

fn state(message: impl Into<String>) -> EngineError {
    EngineError::State(message.into())
}

/// A log this run may write: absent or empty. Anything else is another
/// run's state, and booting from it would make this run a continuation.
fn require_fresh_log(path: &Path) -> Result<(), EngineError> {
    match std::fs::metadata(path) {
        Ok(meta) if meta.len() > 0 => Err(EngineError::Boot(format!(
            "the log at {} already holds {} bytes; a backtest starts from an empty log — \
             name a new file or remove the old one",
            path.display(),
            meta.len()
        ))),
        Ok(_) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(EngineError::Boot(format!(
            "cannot inspect {}: {e}",
            path.display()
        ))),
    }
}

fn read_engine_ledger(path: &Path) -> Result<EngineLedger, EngineError> {
    let mut ledger = EngineLedger::default();
    let text = match std::fs::read_to_string(path) {
        Ok(text) => text,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(ledger),
        Err(e) => return Err(state(format!("cannot read {}: {e}", path.display()))),
    };
    for (n, line) in text.lines().enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let row: serde_json::Value = serde_json::from_str(line).map_err(|e| {
            state(format!(
                "{} line {}: not a closed trade: {e}",
                path.display(),
                n + 1
            ))
        })?;
        ledger.closed_trips += 1;
        if let Some(trip) = row.get("round_trip").filter(|v| !v.is_null()) {
            let num = |name: &str| {
                trip.get(name)
                    .and_then(serde_json::Value::as_f64)
                    .unwrap_or(0.0)
            };
            ledger.priced_trips += 1;
            ledger.gross_usdt += num("gross_usdt");
            ledger.fees_usdt += num("fees_usdt");
            let net = num("net_usdt");
            ledger.net_usdt += net;
            if net > 0.0 {
                ledger.wins += 1;
            }
        }
    }
    Ok(ledger)
}

fn write_equity(path: &Path, venue: &SimulatedVenue) -> Result<(), EngineError> {
    let mut out = String::new();
    for point in &venue.equity_log {
        out.push_str(&serde_json::to_string(point).map_err(|e| state(e.to_string()))?);
        out.push('\n');
    }
    std::fs::write(path, out).map_err(|e| state(format!("cannot write {}: {e}", path.display())))
}

pub async fn run(opts: BacktestOptions) -> Result<BacktestReport, EngineError> {
    let loaded = config::load(&opts.engine_config_path)
        .map_err(|e| EngineError::Boot(format!("config: {e}")))?;
    let mut settings = loaded.config.engine.clone();
    settings.wal_path = opts.wal_path.clone();
    settings.trades_path = opts.trades_path.clone();
    settings.heartbeat_path = None;
    settings.signal_spool_path = None;
    settings.control_spool_path = None;

    let strategies = assembly::strategies(&loaded.config.strategies)
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    let sleeves: Vec<String> = loaded
        .config
        .strategies
        .iter()
        .map(|s| s.sleeve_name().to_string())
        .collect();
    let wanted: Vec<_> = strategies.iter().flat_map(|s| s.subscriptions()).collect();
    let risk = assembly::risk(&loaded.config.risk).map_err(|e| EngineError::Boot(e.to_string()))?;

    // The log: claimed, fresh, then opened — the live runner's order, with
    // the freshness check a live engine must never have.
    require_fresh_log(&opts.wal_path)?;
    let _log_claim =
        engine_wal::lock(&opts.wal_path).map_err(|e| EngineError::Boot(e.to_string()))?;
    let (wal, replayed) = WalWriter::open(&opts.wal_path)?;
    let replayed: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    let symbols: Vec<Symbol> = assembly::symbol_order(&replayed, &wanted);

    let rules: Vec<(Symbol, InstrumentRule)> = read_instruments(&opts.instruments_path)
        .map_err(|e| EngineError::Boot(format!("instruments: {e}")))?;
    let missing: Vec<&str> = symbols
        .iter()
        .filter(|s| !rules.iter().any(|(name, _)| name == *s))
        .map(String::as_str)
        .collect();
    if !missing.is_empty() {
        return Err(EngineError::Boot(format!(
            "the instruments snapshot at {} has no rules for {}",
            opts.instruments_path.display(),
            missing.join(", ")
        )));
    }

    // The tape is opened first so the clock can start where it starts.
    let reader =
        TapeReader::open(&opts.tape_path).map_err(|e| EngineError::Boot(format!("tape: {e}")))?;
    let scheduler = Scheduler::default();
    let venue = Arc::new(Mutex::new(SimulatedVenue::new(
        VenueParams {
            initial_cash_usdt: opts.initial_capital_usdt,
            taker_fee_rate: opts.taker_fee_rate,
            maker_fee_rate: opts.maker_fee_rate,
            order_rtt_ns: Duration::from_millis(opts.order_rtt_ms).as_nanos() as u64,
            private_latency_ns: Duration::from_millis(opts.private_latency_ms).as_nanos() as u64,
            default_leverage: loaded
                .config
                .risk
                .get("leverage")
                .and_then(toml::Value::as_float)
                .unwrap_or(1.0),
            maintenance_margin_rate: opts.maintenance_margin_rate,
        },
        symbols.clone(),
        &rules,
        scheduler.clone(),
    )));
    let subscriptions = assembly::boot_subscriptions(&symbols, &wanted);
    let cursor = Arc::new(Mutex::new(Cursor::new(
        reader,
        venue.clone(),
        &symbols,
        &subscriptions,
    )));
    let mut market_feed = TapeFeed::new(cursor.clone(), scheduler.clone());
    let first_row = cursor
        .lock()
        .unwrap_or_else(|p| p.into_inner())
        .next_row_at()
        .map_err(|e| EngineError::Boot(format!("tape: {e}")))?;
    let Some(start_ns) = first_row else {
        return Err(EngineError::Boot(format!(
            "the tape at {} has no rows this replay can use",
            opts.tape_path.display()
        )));
    };

    // Virtual time begins at the first row and is held for the whole run.
    // Wall and monotonic share the tape's receive stamp as their origin.
    let _clock = engine_types::clock::install_virtual(start_ns, start_ns)
        .map_err(|e| EngineError::Boot(format!("virtual clock: {e}")))?;
    scheduler.advance_to(start_ns);

    let gateway = SimVenueGateway::new(
        venue.clone(),
        scheduler.clone(),
        Duration::from_millis(opts.order_rtt_ms).as_nanos() as u64,
    );
    let mut order_feed = SimOrderFeed::new(venue.clone(), scheduler.clone());
    let mut signal_feed = match &opts.signals_path {
        Some(dir) => SignalReplayFeed::from_directory(dir, scheduler.clone())
            .map_err(|e| EngineError::Boot(format!("signals: {e}")))?,
        None => SignalReplayFeed::empty(scheduler.clone()),
    };
    let signals_replayed = signal_feed.len();
    let mut controls = NoControls;

    let mut engine = Engine::boot_as(
        &settings,
        &loaded.sha256,
        wal,
        risk,
        gateway,
        strategies,
        &sleeves,
        &replayed,
    )
    .await?;
    if let Some(path) = &opts.trades_path {
        engine.write_trades(Trades::new(path.clone()));
    }

    // The idle pump: moves the clock only while the loop waits on the venue
    // outside its `select!`. Stopped as soon as the loop returns.
    let pump_task = tokio::spawn(pump(cursor.clone(), scheduler.clone()));
    let outcome = engine
        .run_with_inputs_on(
            &mut market_feed,
            &mut order_feed,
            &mut signal_feed,
            &mut controls,
            std::future::pending::<()>(),
            VirtualTimer::new(scheduler.clone()),
        )
        .await;
    pump_task.abort();
    let outcome = outcome?;

    let accounting = venue.lock().unwrap_or_else(|p| p.into_inner()).accounting();
    if let Some(path) = &opts.equity_path {
        write_equity(path, &venue.lock().unwrap_or_else(|p| p.into_inner()))?;
    }
    let ledger = match &opts.trades_path {
        Some(path) => read_engine_ledger(path)?,
        None => EngineLedger::default(),
    };
    let venue_closed_net = accounting.realized_pnl_usdt
        - (accounting.fees_paid_usdt - accounting.open_entry_fees_usdt);
    let engine_closed_net = ledger.net_usdt;
    let difference = engine_closed_net - venue_closed_net;
    let tolerance = 1e-6 * engine_closed_net.abs().max(1.0);
    let agrees = if opts.trades_path.is_none() {
        None
    } else if accounting.open_positions == 0 {
        Some(difference.abs() <= tolerance)
    } else {
        None
    };
    let (stats, unknown_symbol_rows) = {
        let cursor = cursor.lock().unwrap_or_else(|p| p.into_inner());
        (cursor.stats().clone(), cursor.unknown_symbol_rows)
    };
    drop(market_feed);
    let report = BacktestReport {
        start_wall_ms: (start_ns / 1_000_000) as i64,
        end_wall_ms: (stats.last_recv_ns.unwrap_or(start_ns) / 1_000_000) as i64,
        tape: stats,
        unknown_symbol_rows,
        signals_replayed,
        market_events: outcome.market_events,
        orders_sent: outcome.orders_sent,
        stopped_by: format!("{:?}", outcome.stopped_by),
        venue: accounting,
        engine: ledger,
        reconciliation: Reconciliation {
            engine_closed_net_usdt: engine_closed_net,
            venue_closed_net_usdt: venue_closed_net,
            difference_usdt: difference,
            agrees,
        },
        wal_path: opts.wal_path.clone(),
        trades_path: opts.trades_path.clone(),
        equity_path: opts.equity_path.clone(),
    };
    if let Some(path) = &opts.report_path {
        let json = serde_json::to_string_pretty(&report).map_err(|e| state(e.to_string()))?;
        std::fs::write(path, json)
            .map_err(|e| state(format!("cannot write {}: {e}", path.display())))?;
    }
    if report.reconciliation.agrees == Some(false) {
        return Err(state(format!(
            "the venue's books and the engine's ledger disagree by {difference:.6} USDT with a flat \
             account; the report at {} has both sides",
            opts.report_path
                .as_ref()
                .map(|p| p.display().to_string())
                .unwrap_or_else(|| "(not written)".to_string())
        )));
    }
    Ok(report)
}
