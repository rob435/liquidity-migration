//! A small file that says how the engine is, for something outside the
//! process to read.
//!
//! Nothing else here answers that question. The log says what happened, but
//! reading it means parsing every record, and a log that has stopped growing
//! looks exactly like a quiet market. This is one line, rewritten every few
//! seconds: when it was written, live or shadow, whose account it is on,
//! whether it is still allowed to open positions, and how much it has seen
//! and sent.
//!
//! It decides nothing. Whether a stale file, or a `may_open` that has gone
//! false, is worth waking somebody for belongs to the fleet's watchdog in
//! `scripts/runtime/check_fleet_liveness.py`, which already knows how to
//! reach a person. A second alarm inside the engine would be a second thing
//! to keep right and a second thing to be wrong.
//!
//! The write is a temp file and a rename, never an edit in place. A reader
//! with no lock of its own — `cat`, `jq`, a watchdog on its own schedule —
//! then sees either the whole of this heartbeat or the whole of the last one.
//! Writing over the file in place would leave a window where it is empty or
//! half a line, and a reader cannot tell that from a broken engine.
//!
//! `wall_ts_ms` is a wall clock in milliseconds, spelled the way the log's own
//! Boot and Reconciled records spell it. The engine's other `*_ns` stamps come
//! from a monotonic clock whose origin is this process, so they mean nothing to
//! anybody else and could not be aged from outside.
//!
//! What this engine does not know, it writes as null rather than inventing:
//! a shadow run holds no lease, and a run that cannot reach the venue never
//! learns the account number.
//!
//! Nothing here is on the order path, and nothing here can stop the engine
//! trading. A heartbeat that cannot be written is a heartbeat nobody reads.

use std::io;
use std::path::{Path, PathBuf};
use std::time::Duration;

use engine_types::risk::RollingLossView;
use engine_types::wal::DurabilityMode;
use engine_types::{AccountIdentity, Side};

use crate::clock;
use crate::engine::canary::CanaryStatus;
use crate::engine::{ENGINE_COMMIT, ENGINE_VERSION};
use crate::execution::Costs;
use crate::ledger::Quantiles;
use crate::venue_runtime::{DispatchClass, VenueQueueSnapshot};

/// How often the file is rewritten. Far slower than the group-flush tick it
/// rides on, so it costs nothing next to the trading it reports; far quicker
/// than the fleet's watchdog, which ages things in minutes.
pub const DEFAULT_EVERY: Duration = Duration::from_secs(5);

/// What the engine is doing right now. Everything here changes between beats;
/// what does not is held by the [`Heartbeat`] itself.
#[derive(Copy, Clone, Debug)]
pub struct Facts<'a> {
    /// True: orders are worked out and written down, never sent.
    /// False means boot found orders or exposure the log could not account
    /// for, and this engine will not open anything new until somebody looks.
    /// It is the field to read first: an engine in that state answers every
    /// other question exactly like a healthy one.
    pub may_open: bool,
    /// Whether the private account channel is usable right now, and how long
    /// it has been unusable (`None` while usable). Separate from `may_open`:
    /// this one clears itself, so a watcher must read the age rather than the
    /// bit to tell a venue's paced re-read from a stream that never returned.
    pub private_stream_ready: bool,
    pub private_stream_unready_ms: Option<u64>,
    /// Market messages seen since boot, and orders sent since boot. Two
    /// numbers that both stop moving is a wedged loop; a quiet market moves
    /// the first and not the second.
    pub market_events: u64,
    pub orders_sent: u64,
    pub strategies: &'a [String],
    /// Effective entry gate per configured strategy, after committed config
    /// and the newest durable runtime override are both applied.
    pub strategy_entries_enabled: &'a [(String, bool)],
    /// Accepted directional flatten requests which the destination reducer
    /// has not durably acknowledged yet.
    pub pending_flatten_requests: &'a [(String, String)],
    /// The latency ledger's current window. A part nothing has been recorded
    /// into is written as null.
    pub decide: Quantiles,
    pub durable: Quantiles,
    pub wire: Quantiles,
    pub ack: Quantiles,
    pub dispatch_queue: Quantiles,
    pub venue_task: Quantiles,
    pub core_resume: Quantiles,
    pub end_to_end: Quantiles,
    /// Disk-barrier wait before dispatch and hold time for the venue quota.
    /// Neither interval measures venue latency.
    pub barrier_wait: Quantiles,
    pub quota_hold: Quantiles,
    /// Since boot: amends whose working price the venue stated, against
    /// amends pulled because it never did. Pulls climbing is the private
    /// stream not republishing, and each pull costs the queue position the
    /// confirmation exists to keep.
    pub amends_confirmed: u64,
    pub amends_pulled_unconfirmed: u64,
    /// Private-stream resets since boot, including the initial subscription;
    /// each one is a recovered gap.
    pub stream_resets: u64,
    /// Seconds this engine has been running. The counters around it are all
    /// since-boot, and "fills 0" means something different two minutes after
    /// a deploy than it does at the end of a day.
    pub uptime_s: u64,
    /// Venue clock minus this box's clock, in milliseconds, off the freshest
    /// quote. Null before the first quote. A box that drifts makes every
    /// venue-stamp comparison quietly wrong.
    pub venue_clock_offset_ms: Option<i64>,
    /// The account as the venue last described it, and how old that reading
    /// is. Native reducers receive the same account state directly in the
    /// engine; the heartbeat is its read-only observer projection. All three
    /// are written as null when no reading has been taken, rather than as a
    /// confident zero.
    ///
    /// The **age** crosses, not the stamp. The engine's clock is monotonic: it
    /// counts from an arbitrary instant near boot, so `observed_ns` is a few
    /// seconds after the engine started and cannot be compared with a wall
    /// clock. An age is meaningful in either clock, so the renderer turns it
    /// into a wall stamp beside its own for outside observers.
    pub equity_usdt: f64,
    pub available_usdt: f64,
    /// `None` when the engine has not read the venue yet.
    pub account_age_ns: Option<u64>,
    /// What the venue says is held, by name, from the same reading as the
    /// equity above. Native reducers use this account truth for ownership,
    /// sizing, stop recovery, and whether an earlier intent actually became a
    /// position.
    ///
    /// The venue's own per-symbol reading is published, plus the configured
    /// strategy name only when the fill ledger proves that exactly one sleeve
    /// owns the symbol. An inherited, manual, or shared position is `null`:
    /// assigning it by guess would let one reducer close another sleeve's
    /// holding. Attribution is rebuilt from the WAL before the first beat.
    pub holdings: &'a [(String, Side, f64, f64, Option<String>)],
    pub account_metrics: Option<&'a engine_types::AccountView>,
    /// Why each asked-for name is not being opened right now, as
    /// (strategy, symbol, reason) rows gathered from the strategies.
    ///
    /// Empty is a real answer: every requested entry is held, being worked, or
    /// not blocked at all.
    pub entry_blockers: &'a [(String, String, String)],
    /// Current strategy-level faults as (strategy, error) rows. A reducer or
    /// contract fault belongs here, never disguised as a symbol blocker.
    pub strategy_errors: &'a [(String, String)],
    /// Unfinished opening orders as (strategy, symbol) rows. These include
    /// uncertain sends rebuilt from the WAL, not only venue-visible rests.
    pub working_entries: &'a [(String, String)],
    /// What the fills have cost so far this run.
    ///
    /// Five numbers, and they answer the question the latency pair beside them
    /// cannot: the engine can be fast and still be trading badly. Everything
    /// unmeasured is null rather than zero — a zero here would read as "we
    /// checked, and it cost nothing", which is the opposite of the truth.
    ///
    /// The full picture, per sleeve and symbol and at every horizon, is
    /// `engine fills --wal PATH` off the log. This is the glance.
    pub costs: &'a Costs,
    /// What this engine's own closed round trips have made inside the rolling
    /// window, and whether that has stopped it opening. `None` from a kernel
    /// that keeps no such window.
    pub rolling_loss: Option<RollingLossView>,
    /// One reading of what the venue task is holding: what is ready to send
    /// per lane, how long the oldest has waited, what is on the wire, and
    /// what the client refused because a lane was full. `None` from a run
    /// with no venue queue to read.
    pub venue_queue: Option<VenueQueueSnapshot>,
    /// How long the oldest cancel or stop this engine has handed over has
    /// gone unanswered. `None` when no protective work is outstanding, which
    /// is not the same reading as an instant one.
    pub protective_backlog_oldest_ms: Option<u64>,
    /// Where the log's durability barriers run. `None` from a log that does
    /// not say — the in-memory test doubles.
    pub wal_durability_mode: Option<DurabilityMode>,
    /// Bytes in the current log segment. `None` for a log that does not live
    /// in a file, which is also a log that never rotates.
    pub wal_segment_bytes: Option<u64>,
    /// What the realm's canary operating policy is doing right now. `None` on
    /// a realm that runs under no policy.
    pub canary: Option<CanaryStatus>,
}

/// The heartbeat writer: where the file goes, how often, and the facts about
/// this run that do not change.
pub struct Heartbeat {
    path: PathBuf,
    temp: PathBuf,
    every_ns: u64,
    due_ns: u64,
    account: Option<AccountIdentity>,
    lease_path: Option<PathBuf>,
    /// The last thing that went wrong, so a path that is wrong — and it is
    /// wrong every few seconds, forever — is said once.
    last_complaint: Option<String>,
    notify: Option<std::os::unix::net::UnixDatagram>,
    /// Turns of the engine's group-flush tick since this writer was
    /// configured. Held here rather than gathered with the rest because it is
    /// counted every tick and published every fifth second: a loop that is
    /// alive but stuck reads as unchanged between two beats, and nothing else
    /// in this file shows that.
    loop_iterations: u64,
    /// The last log rotation this run made. `None` until one happens, which
    /// is most runs.
    last_rotation: Option<Rotation>,
}

/// What one log rotation cost and what it left behind.
#[derive(Copy, Clone, Debug)]
struct Rotation {
    /// Wall time inside the `rotate` call itself, on the engine loop — for a
    /// durable log, two fdatasyncs and a directory fsync. Whole milliseconds,
    /// so a rotation under one reads as 0.
    took_ms: u64,
    /// The fresh segment's size the moment rotation returned: the file magic,
    /// one frame header, and the restatement record, which is all of it.
    base_bytes: u64,
}

impl Heartbeat {
    /// `account` and `lease_path` are what the run learned before the engine
    /// booted: whose account these credentials open, and the lock file this
    /// process holds. Either can be missing — a shadow run holds no lease,
    /// and a run that cannot reach the venue never learns the account — and a
    /// missing one is written as null rather than guessed at.
    pub fn new(
        path: PathBuf,
        account: Option<AccountIdentity>,
        lease_path: Option<PathBuf>,
    ) -> Heartbeat {
        Heartbeat::with_every(path, account, lease_path, DEFAULT_EVERY)
    }

    /// The same, at a cadence of the caller's choosing. Tests only — the live
    /// cadence is [`DEFAULT_EVERY`].
    pub fn with_every(
        path: PathBuf,
        account: Option<AccountIdentity>,
        lease_path: Option<PathBuf>,
        every: Duration,
    ) -> Heartbeat {
        Heartbeat {
            temp: temp_beside(&path),
            path,
            every_ns: u64::try_from(every.as_nanos()).unwrap_or(u64::MAX),
            // Due at the first tick. An engine that says nothing about itself
            // for its first few seconds reads as dead to whoever looks.
            due_ns: 0,
            account,
            lease_path,
            last_complaint: None,
            notify: notify::from_environment(),
            loop_iterations: 0,
            last_rotation: None,
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// One turn of the engine loop. Called every tick, whether or not a beat
    /// is due: what makes the published count worth reading is that it moves
    /// between beats on a healthy engine.
    pub fn count_iteration(&mut self) {
        self.loop_iterations = self.loop_iterations.saturating_add(1);
    }

    /// What the rotation that just finished took, and the size of the segment
    /// it started. `segment_bytes_after` is read straight off the log the
    /// moment `rotate` returned.
    pub fn record_rotation(&mut self, took_ns: u64, segment_bytes_after: u64) {
        self.last_rotation = Some(Rotation {
            took_ms: took_ns / 1_000_000,
            base_bytes: segment_bytes_after,
        });
    }

    /// Where the next heartbeat is built before it is renamed into place.
    pub fn temp_path(&self) -> &Path {
        &self.temp
    }

    /// Whether this beat's turn has come. Asked before the numbers are
    /// gathered, so a tick that is not due costs nothing.
    pub fn due(&self, now_ns: u64) -> bool {
        now_ns >= self.due_ns
    }

    /// Write one heartbeat, whatever the clock says.
    pub fn write(&mut self, now_ns: u64, facts: &Facts) {
        // Moved on before the write, not after: a path that cannot be written
        // must not turn the group-flush tick into a retry every 250ms.
        self.due_ns = now_ns.saturating_add(self.every_ns);
        let text = self.render(facts, clock::wall_ms());
        match self.put(&text) {
            Ok(()) => {
                self.last_complaint = None;
                if let Some(socket) = &self.notify {
                    if let Err(error) = socket.send(b"READY=1\nWATCHDOG=1") {
                        self.complain(format!("cannot notify systemd ({error})"));
                    }
                }
            }
            Err(e) => self.complain(format!("cannot write the heartbeat ({e})")),
        }
    }

    /// One line of JSON: keys sorted, a newline after it. The same spelling
    /// the lease note uses, so anything in the fleet that reads one reads the
    /// other.
    pub fn render(&self, facts: &Facts, wall_ts_ms: i64) -> String {
        use serde::Serialize;
        let taken = facts.account_age_ns.is_some();
        let output = HeartbeatOutput {
            account_available_usdt: taken.then(|| amount(facts.available_usdt)).flatten(),
            account_equity_usdt: taken.then(|| amount(facts.equity_usdt)).flatten(),
            account_observed_wall_ts_ms: facts
                .account_age_ns
                .map(|age_ns| wall_ts_ms - (age_ns / 1_000_000) as i64),
            account_user_id: self.account.as_ref().map(|a| a.user_id.as_str()),
            ack_p50_ns: figure(facts.ack.count, facts.ack.p50_ns),
            ack_p999_ns: figure(facts.ack.count, facts.ack.p999_ns),
            ack_p99_ns: figure(facts.ack.count, facts.ack.p99_ns),
            amends_confirmed: facts.amends_confirmed,
            amends_pulled_unconfirmed: facts.amends_pulled_unconfirmed,
            barrier_wait_p999_ns: figure(facts.barrier_wait.count, facts.barrier_wait.p999_ns),
            barrier_wait_p99_ns: figure(facts.barrier_wait.count, facts.barrier_wait.p99_ns),
            canary: facts.canary.map(|status| CanaryOut {
                blocked: status.blocked,
                expires_in_s: status.expires_in_s,
                gross_notional_usdt: amount(status.gross_notional_usdt),
                loss_usdt: amount(status.loss_usdt),
                open_orders: status.open_orders,
                positions: status.positions,
                unvalued_trips: status.unvalued_trips,
            }),
            core_resume_p50_ns: figure(facts.core_resume.count, facts.core_resume.p50_ns),
            core_resume_p999_ns: figure(facts.core_resume.count, facts.core_resume.p999_ns),
            core_resume_p99_ns: figure(facts.core_resume.count, facts.core_resume.p99_ns),
            decide_p50_ns: figure(facts.decide.count, facts.decide.p50_ns),
            decide_p999_ns: figure(facts.decide.count, facts.decide.p999_ns),
            decide_p99_ns: figure(facts.decide.count, facts.decide.p99_ns),
            dispatch_queue_p50_ns: figure(facts.dispatch_queue.count, facts.dispatch_queue.p50_ns),
            dispatch_queue_p999_ns: figure(
                facts.dispatch_queue.count,
                facts.dispatch_queue.p999_ns,
            ),
            dispatch_queue_p99_ns: figure(facts.dispatch_queue.count, facts.dispatch_queue.p99_ns),
            durable_p50_ns: figure(facts.durable.count, facts.durable.p50_ns),
            durable_p999_ns: figure(facts.durable.count, facts.durable.p999_ns),
            durable_p99_ns: figure(facts.durable.count, facts.durable.p99_ns),
            end_to_end_p50_ns: figure(facts.end_to_end.count, facts.end_to_end.p50_ns),
            end_to_end_p999_ns: figure(facts.end_to_end.count, facts.end_to_end.p999_ns),
            end_to_end_p99_ns: figure(facts.end_to_end.count, facts.end_to_end.p99_ns),
            engine_commit: ENGINE_COMMIT,
            engine_version: ENGINE_VERSION,
            entry_blockers: facts
                .entry_blockers
                .iter()
                .map(|(strategy, symbol, reason)| Blocker {
                    strategy,
                    symbol,
                    reason,
                })
                .collect(),
            fill_all_in_arrival_bps: facts.costs.all_in_arrival_bps().and_then(bps),
            fill_arrival_shortfall_bps: facts.costs.arrival_shortfall.mean().and_then(bps),
            fill_fee_coverage: facts.costs.fee_coverage().and_then(share),
            fill_markout_1m_our_way_bps: facts.costs.markout[2].mean().and_then(bps),
            fills: facts.costs.fills,
            fills_maker_share: facts.costs.maker_share().and_then(share),
            lease_path: self.lease_path.as_ref().map(|p| p.display().to_string()),
            loop_iterations: self.loop_iterations,
            market_events: facts.market_events,
            may_open: facts.may_open,
            mode: "live",
            orders_sent: facts.orders_sent,
            pending_flatten_requests: facts
                .pending_flatten_requests
                .iter()
                .map(|(strategy, request_id)| PendingFlatten {
                    strategy,
                    request_id,
                })
                .collect(),
            pid: std::process::id(),
            private_stream_ready: facts.private_stream_ready,
            private_stream_unready_ms: facts.private_stream_unready_ms,
            protective_backlog_oldest_ms: facts.protective_backlog_oldest_ms,
            account_metrics: facts.account_metrics,
            positions: facts
                .holdings
                .iter()
                .map(|(symbol, side, qty, entry_px, strategy)| Position {
                    symbol,
                    side: match side {
                        Side::Buy => "long",
                        Side::Sell => "short",
                    },
                    qty: amount(*qty),
                    entry_px: amount(*entry_px),
                    strategy: strategy.as_deref(),
                })
                .collect(),
            quota_hold_p999_ns: figure(facts.quota_hold.count, facts.quota_hold.p999_ns),
            quota_hold_p99_ns: figure(facts.quota_hold.count, facts.quota_hold.p99_ns),
            realm: self.account.as_ref().map(|a| a.realm.as_str()),
            rolling_loss_limit_usdt: facts
                .rolling_loss
                .and_then(|window| amount(window.limit_usdt)),
            rolling_loss_net_usdt: facts
                .rolling_loss
                .filter(|window| window.trades > 0 || window.net_usdt != 0.0)
                .and_then(|window| amount(window.net_usdt)),
            rolling_loss_trades: facts.rolling_loss.map(|window| window.trades),
            rolling_loss_tripped: facts.rolling_loss.is_some_and(|window| window.tripped),
            rolling_loss_window_ms: facts.rolling_loss.map(|window| window.window_ms),
            strategies: facts.strategies,
            strategy_entries_enabled: facts
                .strategy_entries_enabled
                .iter()
                .map(|(strategy, entries_enabled)| StrategyPermission {
                    strategy,
                    entries_enabled: *entries_enabled,
                })
                .collect(),
            strategy_errors: facts
                .strategy_errors
                .iter()
                .map(|(strategy, error)| StrategyError { strategy, error })
                .collect(),
            stream_resets: facts.stream_resets,
            uptime_s: facts.uptime_s,
            venue: self.account.as_ref().map(|a| a.venue.as_str()),
            venue_clock_offset_ms: facts.venue_clock_offset_ms,
            venue_queue: facts.venue_queue.map(|queue| VenueQueueOut {
                in_flight_class: queue.in_flight_class.map(class_name),
                // Nothing on the wire has no age, and nothing queued has no
                // oldest. The gauges spell both as 0, which would read here
                // as "measured, and it was instant".
                in_flight_ms: queue.in_flight_class.map(|_| queue.in_flight_ms),
                oldest_queued_ms: (queue.ready_ordinary + queue.ready_urgent > 0)
                    .then_some(queue.oldest_ready_ms),
                ordinary_capacity: queue.ordinary_capacity,
                ready_ordinary: queue.ready_ordinary,
                ready_urgent: queue.ready_urgent,
                refused_ordinary: queue.refused_ordinary,
                refused_urgent: queue.refused_urgent,
                urgent_capacity: queue.urgent_capacity,
            }),
            venue_task_p50_ns: figure(facts.venue_task.count, facts.venue_task.p50_ns),
            venue_task_p999_ns: figure(facts.venue_task.count, facts.venue_task.p999_ns),
            venue_task_p99_ns: figure(facts.venue_task.count, facts.venue_task.p99_ns),
            wal: WalOut {
                durability_mode: facts.wal_durability_mode.map(DurabilityMode::as_str),
                last_rotation_base_bytes: self.last_rotation.map(|last| last.base_bytes),
                last_rotation_ms: self.last_rotation.map(|last| last.took_ms),
                // A log with no file is a log with no segment, not a segment
                // of no bytes.
                segment_bytes: facts.wal_segment_bytes.filter(|bytes| *bytes > 0),
            },
            wall_ts_ms,
            wire_p50_ns: figure(facts.wire.count, facts.wire.p50_ns),
            wire_p999_ns: figure(facts.wire.count, facts.wire.p999_ns),
            wire_p99_ns: figure(facts.wire.count, facts.wire.p99_ns),
            working_entries: facts
                .working_entries
                .iter()
                .map(|(strategy, symbol)| WorkingEntry { strategy, symbol })
                .collect(),
        };
        let mut bytes = Vec::new();
        output
            .serialize(&mut serde_json::Serializer::with_formatter(
                &mut bytes,
                FlatFormatter,
            ))
            .expect("heartbeat fields are JSON values");
        bytes.push(b'\n');
        String::from_utf8(bytes).expect("JSON is UTF-8")
    }

    /// Temp file first, then rename into place.
    ///
    /// Rename replaces the old file in one step, so a reader opens one whole
    /// heartbeat or the other and never a torn one.
    ///
    /// **There is deliberately no fsync here, and adding one would be a
    /// mistake.** The barrier is the slowest call the engine makes — about
    /// 2.2ms on this box, the same one the order path pays for durability it
    /// genuinely needs. This file needs none: it is worth reading only while
    /// the process that wrote it is still running, and a heartbeat that
    /// survived a power cut would be a heartbeat describing an engine that no
    /// longer exists. The rename is what a reader depends on, and the rename
    /// is atomic without it.
    fn put(&self, text: &str) -> io::Result<()> {
        std::fs::write(&self.temp, text)?;
        std::fs::rename(&self.temp, &self.path)
    }

    fn complain(&mut self, what: String) {
        if self.last_complaint.as_deref() == Some(what.as_str()) {
            return;
        }
        tracing::warn!(path = %self.path.display(), "{what}");
        self.last_complaint = Some(what);
    }
}

/// The temp file the next heartbeat is built in: beside the real one, because
/// a rename is only one step within a single filesystem, and named after this
/// process so two engines pointed at one path cannot each write half of the
/// other's file.
fn temp_beside(path: &Path) -> PathBuf {
    let dir = path.parent().unwrap_or_else(|| Path::new("."));
    let name = path
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_else(|| "heartbeat.json".to_string());
    dir.join(format!(".{name}.{}.tmp", std::process::id()))
}

#[derive(serde::Serialize)]
struct HeartbeatOutput<'a> {
    account_available_usdt: Option<Number>,
    account_equity_usdt: Option<Number>,
    #[serde(skip_serializing_if = "Option::is_none")]
    account_metrics: Option<&'a engine_types::AccountView>,
    account_observed_wall_ts_ms: Option<i64>,
    account_user_id: Option<&'a str>,
    ack_p50_ns: Option<u64>,
    ack_p999_ns: Option<u64>,
    ack_p99_ns: Option<u64>,
    amends_confirmed: u64,
    amends_pulled_unconfirmed: u64,
    barrier_wait_p999_ns: Option<u64>,
    barrier_wait_p99_ns: Option<u64>,
    canary: Option<CanaryOut>,
    core_resume_p50_ns: Option<u64>,
    core_resume_p999_ns: Option<u64>,
    core_resume_p99_ns: Option<u64>,
    decide_p50_ns: Option<u64>,
    decide_p999_ns: Option<u64>,
    decide_p99_ns: Option<u64>,
    dispatch_queue_p50_ns: Option<u64>,
    dispatch_queue_p999_ns: Option<u64>,
    dispatch_queue_p99_ns: Option<u64>,
    durable_p50_ns: Option<u64>,
    durable_p999_ns: Option<u64>,
    durable_p99_ns: Option<u64>,
    end_to_end_p50_ns: Option<u64>,
    end_to_end_p999_ns: Option<u64>,
    end_to_end_p99_ns: Option<u64>,
    engine_commit: &'a str,
    engine_version: &'a str,
    entry_blockers: Vec<Blocker<'a>>,
    fill_all_in_arrival_bps: Option<Number>,
    fill_arrival_shortfall_bps: Option<Number>,
    fill_fee_coverage: Option<Number>,
    fill_markout_1m_our_way_bps: Option<Number>,
    fills: u64,
    fills_maker_share: Option<Number>,
    lease_path: Option<String>,
    loop_iterations: u64,
    market_events: u64,
    may_open: bool,
    mode: &'a str,
    orders_sent: u64,
    pending_flatten_requests: Vec<PendingFlatten<'a>>,
    pid: u32,
    positions: Vec<Position<'a>>,
    private_stream_ready: bool,
    private_stream_unready_ms: Option<u64>,
    protective_backlog_oldest_ms: Option<u64>,
    quota_hold_p999_ns: Option<u64>,
    quota_hold_p99_ns: Option<u64>,
    realm: Option<&'a str>,
    rolling_loss_limit_usdt: Option<Number>,
    rolling_loss_net_usdt: Option<Number>,
    rolling_loss_trades: Option<usize>,
    rolling_loss_tripped: bool,
    rolling_loss_window_ms: Option<i64>,
    strategies: &'a [String],
    strategy_entries_enabled: Vec<StrategyPermission<'a>>,
    strategy_errors: Vec<StrategyError<'a>>,
    stream_resets: u64,
    uptime_s: u64,
    venue: Option<&'a str>,
    venue_clock_offset_ms: Option<i64>,
    venue_queue: Option<VenueQueueOut>,
    venue_task_p50_ns: Option<u64>,
    venue_task_p999_ns: Option<u64>,
    venue_task_p99_ns: Option<u64>,
    wal: WalOut,
    wall_ts_ms: i64,
    wire_p50_ns: Option<u64>,
    wire_p999_ns: Option<u64>,
    wire_p99_ns: Option<u64>,
    working_entries: Vec<WorkingEntry<'a>>,
}

/// What the realm's canary operating policy is doing, as the watchdog reads
/// it. `blocked` carries the refusal word every opening would get.
#[derive(serde::Serialize)]
struct CanaryOut {
    blocked: Option<&'static str>,
    expires_in_s: i64,
    gross_notional_usdt: Option<Number>,
    loss_usdt: Option<Number>,
    open_orders: usize,
    positions: usize,
    unvalued_trips: usize,
}

/// One reading of the venue task's two lanes. Counts and capacities are
/// readings, so a zero is a zero; the two ages are null when there is nothing
/// to age.
#[derive(serde::Serialize)]
struct VenueQueueOut {
    in_flight_class: Option<&'static str>,
    in_flight_ms: Option<u64>,
    oldest_queued_ms: Option<u64>,
    ordinary_capacity: usize,
    ready_ordinary: usize,
    ready_urgent: usize,
    refused_ordinary: u64,
    refused_urgent: u64,
    urgent_capacity: usize,
}

/// The log as the engine loop sees it: where its barriers run, what the last
/// rotation cost, and how large the current segment is.
#[derive(serde::Serialize)]
struct WalOut {
    durability_mode: Option<&'static str>,
    last_rotation_base_bytes: Option<u64>,
    last_rotation_ms: Option<u64>,
    segment_bytes: Option<u64>,
}

fn class_name(class: DispatchClass) -> &'static str {
    match class {
        DispatchClass::RiskReducing => "risk-reducing",
        DispatchClass::Amend => "amend",
        DispatchClass::Opening => "opening",
        DispatchClass::Administration => "administration",
    }
}

/// Said once for the life of the process: the log has no durability thread,
/// so every barrier the order path takes is paid on the engine loop itself.
/// Same durability, and the loop waits for it.
pub fn warn_once_on_caller_thread_barriers(mode: Option<DurabilityMode>) {
    static SAID: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    if mode == Some(DurabilityMode::CallerThread)
        && !SAID.swap(true, std::sync::atomic::Ordering::Relaxed)
    {
        tracing::warn!(
            "the log has no durability thread: every barrier runs on the engine loop, \
             which waits for the disk before the send it protects"
        );
    }
}

#[derive(serde::Serialize)]
struct Position<'a> {
    symbol: &'a str,
    side: &'a str,
    qty: Option<Number>,
    entry_px: Option<Number>,
    strategy: Option<&'a str>,
}

#[derive(serde::Serialize)]
struct Blocker<'a> {
    strategy: &'a str,
    symbol: &'a str,
    reason: &'a str,
}

#[derive(serde::Serialize)]
struct StrategyError<'a> {
    strategy: &'a str,
    error: &'a str,
}

#[derive(serde::Serialize)]
struct StrategyPermission<'a> {
    strategy: &'a str,
    entries_enabled: bool,
}

#[derive(serde::Serialize)]
struct PendingFlatten<'a> {
    strategy: &'a str,
    request_id: &'a str,
}

#[derive(serde::Serialize)]
struct WorkingEntry<'a> {
    strategy: &'a str,
    symbol: &'a str,
}

// Preserve the fleet's number spelling, including integer amounts and trailing decimal places.
#[derive(serde::Serialize)]
#[serde(transparent)]
struct Number(Box<serde_json::value::RawValue>);

fn number(value: f64, text: impl FnOnce() -> String) -> Option<Number> {
    value.is_finite().then(|| {
        Number(
            serde_json::value::RawValue::from_string(text())
                .expect("a finite formatted number is JSON"),
        )
    })
}

fn amount(value: f64) -> Option<Number> {
    number(value, || format!("{value}"))
}
fn bps(value: f64) -> Option<Number> {
    number(value, || format!("{value:.2}"))
}
fn share(value: f64) -> Option<Number> {
    number(value, || format!("{value:.4}"))
}
fn figure(count: u64, ns: u64) -> Option<u64> {
    (count != 0).then_some(ns)
}

struct FlatFormatter;

impl serde_json::ser::Formatter for FlatFormatter {
    fn begin_array_value<W: ?Sized + io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }
    fn begin_object_key<W: ?Sized + io::Write>(
        &mut self,
        writer: &mut W,
        first: bool,
    ) -> io::Result<()> {
        if first {
            Ok(())
        } else {
            writer.write_all(b", ")
        }
    }
    fn begin_object_value<W: ?Sized + io::Write>(&mut self, writer: &mut W) -> io::Result<()> {
        writer.write_all(b": ")
    }
}

#[cfg(test)]
mod tests;

#[cfg(test)]
mod fill_cost_tests;

mod notify;
