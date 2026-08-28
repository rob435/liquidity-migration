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

use engine_types::{AccountIdentity, Side};

use crate::clock;
use crate::engine::ENGINE_VERSION;
use crate::execution::Costs;
use crate::ledger::Quantiles;

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
    /// Market messages seen since boot, and orders sent since boot. Two
    /// numbers that both stop moving is a wedged loop; a quiet market moves
    /// the first and not the second.
    pub market_events: u64,
    pub orders_sent: u64,
    pub strategies: &'a [String],
    /// The latency ledger's current window: thinking time and time to the
    /// wire. A part nothing has been recorded into is written as null.
    pub decide: Quantiles,
    pub wire: Quantiles,
    /// The account as the venue last described it, and how old that reading
    /// is. This is not telemetry like the rest of this struct: the target
    /// producers size their entries from the equity here. All three are
    /// written as null when no reading has been taken yet, rather than as a
    /// confident zero — a producer must be able to tell "no reading" from
    /// "no money".
    ///
    /// The **age** crosses, not the stamp. The engine's clock is monotonic: it
    /// counts from an arbitrary instant near boot, so `observed_ns` is a few
    /// seconds after the engine started, and a producer comparing that against
    /// the wall clock reads a healthy engine as tens of thousands of years
    /// stale and blocks every entry. An age is the same number in both clocks,
    /// so the renderer turns it into a wall stamp beside its own, and nothing
    /// has to know which clock the other half keeps.
    pub equity_usdt: f64,
    pub available_usdt: f64,
    /// `None` when the engine has not read the venue yet.
    pub account_age_ns: Option<u64>,
    /// What the venue says is held, by name, from the same reading as the
    /// equity above.
    ///
    /// Also not telemetry. A producer writes an **absolute** book -- it says
    /// what it wants held -- so the one thing it cannot work out on its own is
    /// whether a name it asked for is actually there. Without this, a venue
    /// stop that fires is invisible to the producer: it goes on asking for the
    /// name, the engine refuses to buy it back, and the slot stays occupied
    /// until the producer's own deadline drops it, up to three days later.
    ///
    /// The venue's own per-symbol reading is published, plus the configured
    /// strategy name only when the fill ledger proves that exactly one sleeve
    /// owns the symbol. An inherited, manual, or shared position is `null`:
    /// assigning it by guess would let one producer close another sleeve's
    /// holding. Attribution is rebuilt from the WAL before the first beat.
    pub holdings: &'a [(String, Side, f64, f64, Option<String>)],
    /// Why each asked-for name is not being opened right now, as
    /// (strategy, symbol, reason) rows gathered from the strategies.
    ///
    /// Also read by the target producers, not telemetry: an ask the kernel
    /// refused or a size below the entry floor never becomes a position, and
    /// a producer that cannot tell that from "on its way" holds the slot for
    /// its whole deadline. Empty is a real answer — everything asked for is
    /// either held, being worked, or not blocked at all.
    pub entry_blockers: &'a [(String, String, String)],
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
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
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
            Ok(()) => self.last_complaint = None,
            Err(e) => self.complain(format!("cannot write the heartbeat ({e})")),
        }
    }

    /// One line of JSON: keys sorted, a newline after it. The same spelling
    /// the lease note uses, so anything in the fleet that reads one reads the
    /// other.
    pub fn render(&self, facts: &Facts, wall_ts_ms: i64) -> String {
        let taken = facts.account_age_ns.is_some();
        let mut fields: Vec<(&str, String)> = vec![
            (
                "account_available_usdt",
                or_null(taken.then(|| amount(facts.available_usdt))),
            ),
            (
                "account_equity_usdt",
                or_null(taken.then(|| amount(facts.equity_usdt))),
            ),
            (
                // When the venue reading was taken, on the same clock as
                // `wall_ts_ms` beside it. Deliberately not the engine's own
                // monotonic stamp: see `Facts::account_age_ns`.
                "account_observed_wall_ts_ms",
                or_null(facts.account_age_ns.map(|age_ns| {
                    (wall_ts_ms - (age_ns / 1_000_000) as i64).to_string()
                })),
            ),
            (
                "account_user_id",
                or_null(self.account.as_ref().map(|a| quoted(&a.user_id))),
            ),
            ("decide_p50_ns", figure(facts.decide.count, facts.decide.p50_ns)),
            ("decide_p99_ns", figure(facts.decide.count, facts.decide.p99_ns)),
            ("entry_blockers", blockers(facts.entry_blockers)),
            ("engine_version", quoted(ENGINE_VERSION)),
            ("fills", facts.costs.fills.to_string()),
            (
                // Positive is adverse, throughout. The exception is the
                // markout below, and its key says so.
                "fill_all_in_arrival_bps",
                or_null(facts.costs.all_in_arrival_bps().map(bps)),
            ),
            (
                "fill_arrival_shortfall_bps",
                or_null(facts.costs.arrival_shortfall.mean().map(bps)),
            ),
            (
                // The other way round: positive means the price moved our way
                // after the fill, so a persistently negative one is a book
                // being picked off. Named for its horizon, because a markout
                // without one is meaningless.
                "fill_markout_1m_our_way_bps",
                or_null(facts.costs.markout[2].mean().map(bps)),
            ),
            (
                "fills_maker_share",
                or_null(facts.costs.maker_share().map(|share| format!("{share:.4}"))),
            ),
            (
                "lease_path",
                or_null(self.lease_path.as_ref().map(|p| quoted(&p.display().to_string()))),
            ),
            ("market_events", facts.market_events.to_string()),
            ("may_open", facts.may_open.to_string()),
            ("mode", quoted("live")),
            ("orders_sent", facts.orders_sent.to_string()),
            ("pid", std::process::id().to_string()),
            ("realm", or_null(self.account.as_ref().map(|a| quoted(&a.realm)))),
            ("venue", or_null(self.account.as_ref().map(|a| quoted(&a.venue)))),
            ("positions", positions(facts.holdings)),
            ("strategies", list(facts.strategies)),
            ("wall_ts_ms", wall_ts_ms.to_string()),
            ("wire_p50_ns", figure(facts.wire.count, facts.wire.p50_ns)),
            ("wire_p99_ns", figure(facts.wire.count, facts.wire.p99_ns)),
            ("working_entries", working_entries(facts.working_entries)),
        ];
        fields.sort_by_key(|(key, _)| *key);
        let body: Vec<String> = fields
            .iter()
            .map(|(key, value)| format!("{}: {value}", quoted(key)))
            .collect();
        format!("{{{}}}\n", body.join(", "))
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

/// One JSON string. Every value in the file is a string, a number, a boolean,
/// null, or a list of strings, so this is most of the encoder.
fn quoted(text: &str) -> String {
    serde_json::to_string(text).expect("a string is always encodable as JSON")
}

/// One JSON number from a venue amount. A reading that is not finite is
/// written as null, because a producer sizes from this: NaN dressed as a
/// number would be multiplied by a weight and become a position, whereas null
/// is the same "no reading" every other absent field here says.
fn amount(value: f64) -> String {
    if value.is_finite() {
        format!("{value}")
    } else {
        "null".to_string()
    }
}

fn or_null(value: Option<String>) -> String {
    value.unwrap_or_else(|| "null".to_string())
}

/// A basis-point figure, to two places. An unreal one is null rather than a
/// number JSON cannot hold.
fn bps(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.2}")
    } else {
        "null".to_string()
    }
}

/// A latency figure, or null when nothing has been measured in this window.
/// Zero would read as "instant" rather than "no orders yet".
fn figure(count: u64, ns: u64) -> String {
    if count == 0 {
        "null".to_string()
    } else {
        ns.to_string()
    }
}

/// What is held, as an array of objects. Empty is a real answer and means
/// the account holds nothing -- not the same as the key being absent, which
/// would mean an engine too old to say.
fn positions(held: &[(String, Side, f64, f64, Option<String>)]) -> String {
    let rows: Vec<String> = held
        .iter()
        .map(|(symbol, side, qty, entry_px, strategy)| {
            format!(
                "{{{}: {}, {}: {}, {}: {}, {}: {}, {}: {}}}",
                quoted("symbol"),
                quoted(symbol),
                quoted("side"),
                quoted(match side {
                    Side::Buy => "long",
                    Side::Sell => "short",
                }),
                quoted("qty"),
                amount(*qty),
                quoted("entry_px"),
                amount(*entry_px),
                quoted("strategy"),
                or_null(strategy.as_ref().map(|name| quoted(name))),
            )
        })
        .collect();
    format!("[{}]", rows.join(", "))
}

fn list(names: &[String]) -> String {
    let names: Vec<String> = names.iter().map(|name| quoted(name.as_str())).collect();
    format!("[{}]", names.join(", "))
}

/// Why the asked-for names are not being opened, as an array of objects.
/// Empty is a real answer and means nothing is blocked -- not the same as the
/// key being absent, which would mean an engine too old to say.
fn blockers(rows: &[(String, String, String)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, symbol, reason)| {
            format!(
                "{{{}: {}, {}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("symbol"),
                quoted(symbol),
                quoted("reason"),
                quoted(reason)
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

fn working_entries(rows: &[(String, String)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, symbol)| {
            format!(
                "{{{}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("symbol"),
                quoted(symbol),
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testpath::temp_path;

    /// Every key the file carries, in the order it must read in.
    const KEYS: [&str; 27] = [
        "account_available_usdt",
        "account_equity_usdt",
        "account_observed_wall_ts_ms",
        "account_user_id",
        "decide_p50_ns",
        "decide_p99_ns",
        "engine_version",
        "entry_blockers",
        "fill_all_in_arrival_bps",
        "fill_arrival_shortfall_bps",
        "fill_markout_1m_our_way_bps",
        "fills",
        "fills_maker_share",
        "lease_path",
        "market_events",
        "may_open",
        "mode",
        "orders_sent",
        "pid",
        "positions",
        "realm",
        "strategies",
        "venue",
        "wall_ts_ms",
        "wire_p50_ns",
        "wire_p99_ns",
        "working_entries",
    ];

    fn measured(count: u64, p50_ns: u64, p99_ns: u64) -> Quantiles {
        Quantiles { count, p50_ns, p90_ns: p50_ns, p99_ns, max_ns: p99_ns }
    }

    /// An engine that has traded a little: one fill, and it cost something.
    pub(super) fn some_costs() -> Costs {
        let mut fills = crate::execution::Fills::default();
        fills.on_fill(
            &crate::execution::Fill {
                client_order_id: "eng-1".into(),
                strategy: engine_types::StrategyId(0),
                symbol: engine_types::SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                px: 101.0,
                fee: 0.0555,
                is_maker: true,
                arrival_mid: 100.0,
                venue_ts_ms: 1,
            },
            0,
        );
        fills.total()
    }

    fn facts<'a>(
        strategies: &'a [String],
        held: &'a [(String, Side, f64, f64, Option<String>)],
    ) -> Facts<'a> {
        // An engine that has not filled anything, which is what every test
        // that is not about fill costs means.
        static NOTHING_YET: std::sync::OnceLock<Costs> = std::sync::OnceLock::new();
        static NO_BLOCKERS: std::sync::OnceLock<Vec<(String, String, String)>> =
            std::sync::OnceLock::new();
        static NO_WORKING: std::sync::OnceLock<Vec<(String, String)>> =
            std::sync::OnceLock::new();
        Facts {
            costs: NOTHING_YET.get_or_init(Costs::default),
            may_open: true,
            market_events: 1234,
            orders_sent: 7,
            strategies,
            decide: measured(7, 83, 400),
            wire: measured(7, 2_600_000, 4_100_000),
            equity_usdt: 10_250.5,
            available_usdt: 4_100.25,
            // Two seconds old, on the engine's own monotonic clock.
            account_age_ns: Some(2_000_000_000),
            holdings: held,
            entry_blockers: NO_BLOCKERS.get_or_init(Vec::new),
            working_entries: NO_WORKING.get_or_init(Vec::new),
        }
    }

    /// One position, so the shape of the array is exercised rather than only
    /// the empty case.
    fn one_holding() -> Vec<(String, Side, f64, f64, Option<String>)> {
        vec![(
            "HOMEUSDT".to_string(),
            Side::Buy,
            14_110.0,
            0.009_7,
            Some("target_book_long".to_string()),
        )]
    }

    #[test]
    fn the_account_reading_is_stamped_on_the_wall_clock_beside_its_own() {
        // The fault this exists for, found on a live deploy: the engine's
        // clock is monotonic and starts near boot, so the raw `observed_ns`
        // was a few seconds. A producer comparing it against the wall clock
        // read a healthy engine as fifty-six thousand years stale and blocked
        // every entry, quietly, per cycle.
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book".to_string()];
        let held = one_holding();
        let wall_ts_ms = 1_786_737_867_645_i64;

        let fields = parsed(&beat.render(&facts(&names, &held), wall_ts_ms));

        let observed = fields["account_observed_wall_ts_ms"]
            .as_i64()
            .expect("a whole number of milliseconds");
        assert_eq!(observed, wall_ts_ms - 2_000, "two seconds before this beat");
        assert!(
            observed > 1_700_000_000_000,
            "a unix millisecond stamp, not a count since boot: {observed}"
        );
    }

    #[test]
    fn what_is_held_is_published_by_name_for_the_producers_to_read() {
        // A producer writes an absolute book, so the one thing it cannot work
        // out for itself is whether a name it asked for is actually there. Not
        // telemetry: without this a venue stop that fires is invisible to the
        // producer, which goes on asking for the name until its own deadline.
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book".to_string()];
        let held = one_holding();

        let fields = parsed(&beat.render(&facts(&names, &held), 1_755_000_000_000));

        let rows = fields["positions"].as_array().expect("an array");
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["symbol"], "HOMEUSDT", "by name, not by engine id");
        assert_eq!(rows[0]["side"], "long");
        assert_eq!(rows[0]["qty"].as_f64(), Some(14_110.0));
        assert!(rows[0]["entry_px"].as_f64().is_some_and(|px| px > 0.0));
        assert_eq!(rows[0]["strategy"], "target_book_long");
    }

    #[test]
    fn an_unattributed_account_position_does_not_guess_a_strategy() {
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book_long".to_string(), "target_book_carry".to_string()];
        let held = vec![("HOMEUSDT".to_string(), Side::Buy, 14_110.0, 0.009_7, None)];

        let fields = parsed(&beat.render(&facts(&names, &held), 1_755_000_000_000));

        assert!(fields["positions"][0]["strategy"].is_null());
    }

    #[test]
    fn holding_nothing_is_an_empty_array_and_not_a_missing_key() {
        // The difference a producer has to be able to tell: an account that
        // holds nothing, versus an engine too old to say what it holds. One
        // means drop the book; the other means do not act on this at all.
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book".to_string()];

        let fields = parsed(&beat.render(&facts(&names, &[]), 1_755_000_000_000));

        assert_eq!(
            fields["positions"].as_array().map(Vec::len),
            Some(0),
            "present and empty, never absent"
        );
    }

    #[test]
    fn a_blocked_entry_is_published_with_its_reason() {
        // An ask the kernel refused never becomes a position, and a producer
        // that cannot tell that from "on its way" holds the slot for its
        // whole deadline. The reason crosses so the producer's log says why,
        // not just that.
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book".to_string()];
        let held = one_holding();
        let blockers = vec![
            (
                "target_book_long".to_string(),
                "KAITOUSDT".to_string(),
                "below_entry_floor".to_string(),
            ),
            (
                "target_book_long".to_string(),
                "SOMIUSDT".to_string(),
                "AvailableMarginExhausted { additional_margin_usdt: 12.0, available_usdt: 0.5 }"
                    .to_string(),
            ),
        ];
        let mut facts = facts(&names, &held);
        facts.entry_blockers = &blockers;

        let fields = parsed(&beat.render(&facts, 1_755_000_000_000));

        let rows = fields["entry_blockers"].as_array().expect("an array");
        assert_eq!(rows.len(), 2, "one reason per blocked name");
        assert_eq!(rows[0]["strategy"], "target_book_long");
        assert_eq!(rows[0]["symbol"], "KAITOUSDT");
        assert_eq!(rows[0]["reason"], "below_entry_floor");
        assert_eq!(rows[1]["symbol"], "SOMIUSDT");
        assert!(
            rows[1]["reason"].as_str().unwrap_or("").contains("AvailableMargin"),
            "the kernel's own reason text crosses: {}",
            rows[1]["reason"]
        );
    }

    #[test]
    fn same_symbol_blockers_keep_their_strategy_identity() {
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book_long".to_string(), "target_book_carry".to_string()];
        let held = one_holding();
        let blockers = vec![
            (
                "target_book_long".to_string(),
                "KAITOUSDT".to_string(),
                "below_entry_floor".to_string(),
            ),
            (
                "target_book_carry".to_string(),
                "KAITOUSDT".to_string(),
                "available_margin_exhausted".to_string(),
            ),
        ];
        let mut facts = facts(&names, &held);
        facts.entry_blockers = &blockers;

        let fields = parsed(&beat.render(&facts, 1_755_000_000_000));
        let rows = fields["entry_blockers"].as_array().expect("an array");

        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["symbol"], "KAITOUSDT");
        assert_eq!(rows[1]["symbol"], "KAITOUSDT");
        assert_eq!(rows[0]["strategy"], "target_book_long");
        assert_eq!(rows[1]["strategy"], "target_book_carry");
    }

    #[test]
    fn nothing_blocked_is_an_empty_array_and_not_a_missing_key() {
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["target_book".to_string()];

        let fields = parsed(&beat.render(&facts(&names, &[]), 1_755_000_000_000));

        assert_eq!(
            fields["entry_blockers"].as_array().map(Vec::len),
            Some(0),
            "present and empty, never absent"
        );
    }

    #[test]
    fn unfinished_entries_keep_their_strategy_and_symbol() {
        let beat = on_the_demo_account(PathBuf::from("/does/not/matter"));
        let names = vec!["exodus".to_string()];
        let pending = vec![("exodus".to_string(), "DYNAMICUSDT".to_string())];
        let mut facts = facts(&names, &[]);
        facts.working_entries = &pending;

        let fields = parsed(&beat.render(&facts, 1_755_000_000_000));

        assert_eq!(fields["working_entries"][0]["strategy"], "exodus");
        assert_eq!(fields["working_entries"][0]["symbol"], "DYNAMICUSDT");
    }

    fn on_the_demo_account(path: PathBuf) -> Heartbeat {
        Heartbeat::new(
            path,
            Some(AccountIdentity {
                venue: "bybit".into(),
                user_id: "6039967".into(),
                realm: "demo".into(),
            }),
            Some(PathBuf::from(
                "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock",
            )),
        )
    }

    fn parsed(raw: &str) -> serde_json::Map<String, serde_json::Value> {
        serde_json::from_str::<serde_json::Value>(raw)
            .unwrap_or_else(|e| panic!("the heartbeat is not JSON ({e}): {raw}"))
            .as_object()
            .expect("the heartbeat is an object")
            .clone()
    }

    #[test]
    fn the_heartbeat_says_who_and_what_this_engine_is() {
        let names = vec!["touch_sniper".to_string(), "carry_follower".to_string()];
        let held = one_holding();
        let raw = on_the_demo_account("unused.json".into())
            .render(&facts(&names, &held), 1_755_000_000_000);

        assert!(raw.ends_with('\n'), "a newline after it, like the lease note: {raw:?}");
        assert_eq!(raw.lines().count(), 1, "one line: {raw:?}");
        let fields = parsed(&raw);

        let mut keys: Vec<&str> = fields.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, KEYS, "the file carries exactly these");

        assert_eq!(fields["account_user_id"], "6039967");
        assert_eq!(fields["realm"], "demo");
        assert_eq!(
            fields["venue"], "bybit",
            "with four venues a heartbeat has to say which one it is on"
        );
        assert_eq!(
            fields["lease_path"],
            "/run/lock/liquidity-migration/bybit-demo-user-6039967.lock"
        );
        assert_eq!(fields["may_open"].as_bool(), Some(true));
        assert_eq!(fields["market_events"], 1234);
        assert_eq!(fields["orders_sent"], 7);
        assert_eq!(fields["wall_ts_ms"], 1_755_000_000_000i64);
        assert_eq!(fields["pid"].as_u64(), Some(u64::from(std::process::id())));
        assert_eq!(fields["engine_version"], ENGINE_VERSION);
        assert_eq!(fields["strategies"][0], "touch_sniper");
        assert_eq!(fields["strategies"][1], "carry_follower");
        assert_eq!(fields["decide_p50_ns"], 83);
        assert_eq!(fields["wire_p99_ns"], 4_100_000);
    }

    #[test]
    fn the_keys_read_in_order_in_the_file_itself() {
        // Sorted the way Python's json.dumps(sort_keys=True) writes it, so a
        // person diffing two heartbeats sees only what changed.
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let raw = on_the_demo_account("unused.json".into())
            .render(&facts(&names, &held), 1_755_000_000_000);
        let mut last = 0;
        for key in KEYS {
            let at = raw
                .find(&format!("\"{key}\": "))
                .unwrap_or_else(|| panic!("no {key} in {raw}"));
            assert!(at >= last, "{key} is out of order: {raw}");
            last = at;
        }
    }

    #[test]
    fn it_always_says_live() {
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let beat = on_the_demo_account("unused.json".into());

        assert_eq!(parsed(&beat.render(&facts(&names, &held), 1))["mode"], "live");
    }

    #[test]
    fn an_engine_that_will_not_open_says_so() {
        // The reason this field is here at all: everything else about a
        // latched engine reads healthy.
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let mut latched = facts(&names, &held);
        latched.may_open = false;
        let fields = parsed(&on_the_demo_account("unused.json".into()).render(&latched, 1));
        assert_eq!(fields["may_open"].as_bool(), Some(false));
    }

    #[test]
    fn a_latency_nobody_has_measured_is_null_and_not_zero() {
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let mut quiet = facts(&names, &held);
        quiet.decide = measured(0, 0, 0);
        quiet.wire = measured(0, 0, 0);
        let fields = parsed(&on_the_demo_account("unused.json".into()).render(&quiet, 1));
        for key in ["decide_p50_ns", "decide_p99_ns", "wire_p50_ns", "wire_p99_ns"] {
            assert!(fields[key].is_null(), "{key} would read as instant: {fields:?}");
        }
    }

    #[test]
    fn what_this_run_does_not_know_is_null_rather_than_invented() {
        // A shadow run holds no lease, and one that cannot reach the venue
        // never learns the account number.
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let fields = parsed(&Heartbeat::new("unused.json".into(), None, None).render(&facts(&names, &held), 1));
        for key in ["account_user_id", "realm", "lease_path"] {
            assert!(fields[key].is_null(), "{key} was guessed at: {fields:?}");
        }
    }

    #[test]
    fn each_heartbeat_replaces_the_file_instead_of_writing_over_it() {
        // The atomicity proof, and it does not need a race to catch it. A
        // second name for the file follows the bytes that were there when the
        // link was made. If the writer edited the file in place, the link
        // would show the new heartbeat; because it renames a fresh file into
        // place, the link keeps the old one — which is exactly why a reader
        // holding the file open never sees half of anything.
        let path = temp_path("heartbeat-replaced");
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let mut beat = on_the_demo_account(path.path().to_path_buf());

        let mut first = facts(&names, &held);
        first.market_events = 11;
        beat.write(1, &first);

        let link = temp_path("heartbeat-link");
        std::fs::hard_link(path.path(), link.path()).expect("a second name for the file");

        let mut second = facts(&names, &held);
        second.market_events = 22;
        beat.write(1, &second);

        let now = std::fs::read_to_string(path.path()).expect("the heartbeat is there");
        let then = std::fs::read_to_string(link.path()).expect("the old one is still there");
        assert_eq!(parsed(&now)["market_events"], 22, "the newest heartbeat is at the path");
        assert_eq!(
            parsed(&then)["market_events"],
            11,
            "the file was edited in place, so a reader can see half a heartbeat"
        );
    }

    #[test]
    fn the_temp_file_sits_beside_the_heartbeat_and_does_not_stay() {
        // Beside it because a rename is one step only within one filesystem;
        // a temp file in /tmp would fail across a mount and there would be no
        // heartbeat at all.
        let path = temp_path("heartbeat-temp");
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let mut beat = on_the_demo_account(path.path().to_path_buf());
        assert_eq!(
            beat.temp_path().parent(),
            path.path().parent(),
            "the temp file has to be on the same filesystem"
        );
        beat.write(1, &facts(&names, &held));
        assert!(path.path().exists(), "the heartbeat landed");
        assert!(!beat.temp_path().exists(), "the temp file was left behind");
    }

    #[test]
    fn a_heartbeat_that_cannot_be_written_leaves_no_file_and_no_half_file() {
        let missing = temp_path("heartbeat-nowhere");
        let path = missing.path().join("no-such-directory").join("beat.json");
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let mut beat = Heartbeat::new(path.clone(), None, None);
        beat.write(1, &facts(&names, &held));
        assert!(!path.exists(), "nothing should have been written");
        assert!(!beat.temp_path().exists(), "nor a temp file");
    }

    #[test]
    fn the_cadence_holds_it_back_between_beats() {
        let path = temp_path("heartbeat-cadence");
        let mut beat = Heartbeat::with_every(
            path.path().to_path_buf(),
            None,
            None,
            Duration::from_secs(5),
        );
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();

        // Due straight away: the first tick of a run writes one.
        assert!(beat.due(0));
        beat.write(1_000, &facts(&names, &held));
        assert!(!beat.due(1_000 + 4_999_999_999), "under five seconds is too soon");
        assert!(beat.due(1_000 + 5_000_000_000), "five seconds on, it is due");
    }

    #[test]
    fn a_write_that_cannot_land_does_not_start_retrying_every_tick() {
        // The tick comes round four times a second. A wrong path is wrong
        // every one of them, and answering by trying again immediately would
        // put a failing file write in front of the account refresh.
        let missing = temp_path("heartbeat-no-retry");
        let path = missing.path().join("no-such-directory").join("beat.json");
        let names = vec!["touch_sniper".to_string()];
        let held = one_holding();
        let mut beat = Heartbeat::with_every(path, None, None, Duration::from_secs(5));
        beat.write(1_000, &facts(&names, &held));
        assert!(!beat.due(1_000 + 4_999_999_999), "a failed write reset the cadence");
    }
}

#[cfg(test)]
mod fill_cost_tests {
    use super::tests::some_costs;
    use super::*;

    fn beat_with(costs: &Costs) -> serde_json::Value {
        let facts = Facts {
            may_open: true,
            market_events: 1,
            orders_sent: 1,
            strategies: &[],
            decide: Quantiles::default(),
            wire: Quantiles::default(),
            equity_usdt: 100.0,
            available_usdt: 100.0,
            account_age_ns: Some(1),
            holdings: &[],
            entry_blockers: &[],
            working_entries: &[],
            costs,
        };
        let beat = Heartbeat::new("unused".into(), None, None);
        serde_json::from_str(&beat.render(&facts, 1_700_000_000_000)).expect("valid json")
    }

    #[test]
    fn the_beat_says_what_the_fills_cost() {
        // The engine can be fast and still be trading badly. The latency pair
        // beside these cannot tell anybody which.
        let fields = beat_with(&some_costs());
        assert_eq!(fields["fills"], 1);
        assert_eq!(fields["fills_maker_share"], 1.0, "the one fill rested");
        assert_eq!(fields["fill_arrival_shortfall_bps"], 100.0);
        assert_eq!(fields["fill_all_in_arrival_bps"], 105.5, "plus 5.5 bp of fee");
    }

    #[test]
    fn nothing_filled_yet_reads_as_null_and_never_as_zero() {
        // A zero here would say "we checked, and it cost nothing", which is
        // the opposite of the truth about an engine that has not traded.
        let fields = beat_with(&Costs::default());
        assert_eq!(fields["fills"], 0, "the count is a real zero");
        for key in [
            "fills_maker_share",
            "fill_arrival_shortfall_bps",
            "fill_all_in_arrival_bps",
            "fill_markout_1m_our_way_bps",
        ] {
            assert!(fields[key].is_null(), "{key} should be null: {}", fields[key]);
        }
    }

    #[test]
    fn a_markout_that_has_not_come_round_is_null_beside_a_priced_fill() {
        // The arrival numbers land the moment a fill does; the markout waits
        // for its horizon. One being absent must not make the other absent.
        let fields = beat_with(&some_costs());
        assert!(fields["fill_markout_1m_our_way_bps"].is_null());
        assert!(!fields["fill_arrival_shortfall_bps"].is_null());
    }
}
