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

use engine_types::AccountIdentity;

use crate::clock;
use crate::engine::ENGINE_VERSION;
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
    pub shadow: bool,
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
    /// The account as the venue last described it, and when that reading was
    /// taken. This is not telemetry like the rest of this struct: the target
    /// producers size their entries from the equity here, having read it from
    /// the Python owner's health file until that owner was deleted. A reading
    /// the engine has not taken yet has `observed_ns == 0`, and all three are
    /// written as null rather than as a confident zero — a producer must be
    /// able to tell "no reading" from "no money".
    pub equity_usdt: f64,
    pub available_usdt: f64,
    pub account_observed_ns: u64,
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
        let taken = facts.account_observed_ns != 0;
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
                "account_observed_ns",
                or_null(taken.then(|| facts.account_observed_ns.to_string())),
            ),
            (
                "account_user_id",
                or_null(self.account.as_ref().map(|a| quoted(&a.user_id))),
            ),
            ("decide_p50_ns", figure(facts.decide.count, facts.decide.p50_ns)),
            ("decide_p99_ns", figure(facts.decide.count, facts.decide.p99_ns)),
            ("engine_version", quoted(ENGINE_VERSION)),
            (
                "lease_path",
                or_null(self.lease_path.as_ref().map(|p| quoted(&p.display().to_string()))),
            ),
            ("market_events", facts.market_events.to_string()),
            ("may_open", facts.may_open.to_string()),
            ("mode", quoted(if facts.shadow { "shadow" } else { "live" })),
            ("orders_sent", facts.orders_sent.to_string()),
            ("pid", std::process::id().to_string()),
            ("realm", or_null(self.account.as_ref().map(|a| quoted(&a.realm)))),
            ("strategies", list(facts.strategies)),
            ("wall_ts_ms", wall_ts_ms.to_string()),
            ("wire_p50_ns", figure(facts.wire.count, facts.wire.p50_ns)),
            ("wire_p99_ns", figure(facts.wire.count, facts.wire.p99_ns)),
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

/// A latency figure, or null when nothing has been measured in this window.
/// Zero would read as "instant" rather than "no orders yet".
fn figure(count: u64, ns: u64) -> String {
    if count == 0 {
        "null".to_string()
    } else {
        ns.to_string()
    }
}

fn list(names: &[String]) -> String {
    let names: Vec<String> = names.iter().map(|name| quoted(name.as_str())).collect();
    format!("[{}]", names.join(", "))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testpath::temp_path;

    /// Every key the file carries, in the order it must read in.
    const KEYS: [&str; 18] = [
        "account_available_usdt",
        "account_equity_usdt",
        "account_observed_ns",
        "account_user_id",
        "decide_p50_ns",
        "decide_p99_ns",
        "engine_version",
        "lease_path",
        "market_events",
        "may_open",
        "mode",
        "orders_sent",
        "pid",
        "realm",
        "strategies",
        "wall_ts_ms",
        "wire_p50_ns",
        "wire_p99_ns",
    ];

    fn measured(count: u64, p50_ns: u64, p99_ns: u64) -> Quantiles {
        Quantiles { count, p50_ns, p90_ns: p50_ns, p99_ns, max_ns: p99_ns }
    }

    fn facts(strategies: &[String]) -> Facts<'_> {
        Facts {
            shadow: true,
            may_open: true,
            market_events: 1234,
            orders_sent: 7,
            strategies,
            decide: measured(7, 83, 400),
            wire: measured(7, 2_600_000, 4_100_000),
            equity_usdt: 10_250.5,
            available_usdt: 4_100.25,
            account_observed_ns: 1_786_000_000_000_000_000,
        }
    }

    fn on_the_demo_account(path: PathBuf) -> Heartbeat {
        Heartbeat::new(
            path,
            Some(AccountIdentity { user_id: "6039967".into(), realm: "demo".into() }),
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
        let raw = on_the_demo_account("unused.json".into())
            .render(&facts(&names), 1_755_000_000_000);

        assert!(raw.ends_with('\n'), "a newline after it, like the lease note: {raw:?}");
        assert_eq!(raw.lines().count(), 1, "one line: {raw:?}");
        let fields = parsed(&raw);

        let mut keys: Vec<&str> = fields.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, KEYS, "the file carries exactly these");

        assert_eq!(fields["account_user_id"], "6039967");
        assert_eq!(fields["realm"], "demo");
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
        let raw = on_the_demo_account("unused.json".into())
            .render(&facts(&names), 1_755_000_000_000);
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
    fn it_says_shadow_in_shadow_and_live_when_live() {
        let names = vec!["touch_sniper".to_string()];
        let beat = on_the_demo_account("unused.json".into());

        let mut in_shadow = facts(&names);
        in_shadow.shadow = true;
        assert_eq!(parsed(&beat.render(&in_shadow, 1))["mode"], "shadow");

        let mut sending = facts(&names);
        sending.shadow = false;
        assert_eq!(parsed(&beat.render(&sending, 1))["mode"], "live");
    }

    #[test]
    fn an_engine_that_will_not_open_says_so() {
        // The reason this field is here at all: everything else about a
        // latched engine reads healthy.
        let names = vec!["touch_sniper".to_string()];
        let mut latched = facts(&names);
        latched.may_open = false;
        let fields = parsed(&on_the_demo_account("unused.json".into()).render(&latched, 1));
        assert_eq!(fields["may_open"].as_bool(), Some(false));
    }

    #[test]
    fn a_latency_nobody_has_measured_is_null_and_not_zero() {
        let names = vec!["touch_sniper".to_string()];
        let mut quiet = facts(&names);
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
        let fields = parsed(&Heartbeat::new("unused.json".into(), None, None).render(&facts(&names), 1));
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
        let mut beat = on_the_demo_account(path.path().to_path_buf());

        let mut first = facts(&names);
        first.market_events = 11;
        beat.write(1, &first);

        let link = temp_path("heartbeat-link");
        std::fs::hard_link(path.path(), link.path()).expect("a second name for the file");

        let mut second = facts(&names);
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
        let mut beat = on_the_demo_account(path.path().to_path_buf());
        assert_eq!(
            beat.temp_path().parent(),
            path.path().parent(),
            "the temp file has to be on the same filesystem"
        );
        beat.write(1, &facts(&names));
        assert!(path.path().exists(), "the heartbeat landed");
        assert!(!beat.temp_path().exists(), "the temp file was left behind");
    }

    #[test]
    fn a_heartbeat_that_cannot_be_written_leaves_no_file_and_no_half_file() {
        let missing = temp_path("heartbeat-nowhere");
        let path = missing.path().join("no-such-directory").join("beat.json");
        let names = vec!["touch_sniper".to_string()];
        let mut beat = Heartbeat::new(path.clone(), None, None);
        beat.write(1, &facts(&names));
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

        // Due straight away: the first tick of a run writes one.
        assert!(beat.due(0));
        beat.write(1_000, &facts(&names));
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
        let mut beat = Heartbeat::with_every(path, None, None, Duration::from_secs(5));
        beat.write(1_000, &facts(&names));
        assert!(!beat.due(1_000 + 4_999_999_999), "a failed write reset the cadence");
    }
}
