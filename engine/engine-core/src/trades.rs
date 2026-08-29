//! Closed round trips, one JSON line each, for something outside the process
//! to read.
//!
//! The engine reaches nobody itself — its units carry no chat credentials at
//! all, on purpose ([`docs/notifications.md`]). What it can do is write down
//! what a position came to, and it is the only thing that can: the target
//! books say what a sleeve asked for, and what it got is in the fills.
//! `scripts/runtime/notify_book_changes.py` reads this file and is what
//! reaches a phone.
//!
//! Appended, never rewritten, so a reader that remembers a byte offset reads
//! only what is new. **There is no fsync**, for the same reason
//! [`crate::heartbeat`] has none: a line lost to a power cut costs one
//! message, and the log the line was computed from still holds the trade.
//!
//! Nothing here returns an error. An engine that stopped trading because it
//! could not describe a trade it had already made would be a worse answer
//! than one nobody can see.
//!
//! [`docs/notifications.md`]: ../../../docs/notifications.md

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;

use crate::execution::roundtrip::ClosedTrade;

pub struct Trades {
    path: PathBuf,
    /// The last thing that went wrong, so a broken path says so once rather
    /// than on every trade.
    last_complaint: Option<String>,
}

impl Trades {
    pub fn new(path: PathBuf) -> Self {
        Trades {
            path,
            last_complaint: None,
        }
    }

    /// Append a line per trade. An empty list touches no disk at all — the
    /// engine calls this on every tick and trades are rare.
    pub fn write(&mut self, trades: &[ClosedTrade]) {
        if trades.is_empty() {
            return;
        }
        let mut body = String::new();
        for trade in trades {
            match serde_json::to_string(trade) {
                Ok(line) => {
                    body.push_str(&line);
                    body.push('\n');
                }
                Err(e) => self.complain(format!("cannot describe a closed trade: {e}")),
            }
        }
        if body.is_empty() {
            return;
        }
        match self.put(&body) {
            Ok(()) => {
                self.last_complaint = None;
                for trade in trades {
                    tracing::info!(
                        sleeve = %trade.sleeve,
                        symbol = %trade.symbol,
                        side = trade.side,
                        net_usdt = trade.round_trip.as_ref().map(|rt| rt.net_usdt),
                        "a position closed"
                    );
                }
            }
            Err(e) => self.complain(format!("cannot write {}: {e}", self.path.display())),
        }
    }

    fn put(&self, body: &str) -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)?;
        file.write_all(body.as_bytes())?;
        file.flush()
    }

    fn complain(&mut self, what: String) {
        if self.last_complaint.as_deref() == Some(what.as_str()) {
            return;
        }
        tracing::warn!(detail = %what, "the closed-trade file is not being written");
        self.last_complaint = Some(what);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::execution::roundtrip::RoundTrip;
    use crate::testpath::temp_path;

    fn trade(symbol: &str, net: Option<f64>) -> ClosedTrade {
        ClosedTrade {
            sleeve: "carry".into(),
            symbol: symbol.into(),
            side: "long",
            qty: 5_056.0,
            exit_px: 0.0886,
            closed_ms: 1_787_000_000_000,
            fills: 3,
            maker_share: Some(1.0),
            arrival_shortfall_bps: Some(0.4),
            round_trip: net.map(|net_usdt| RoundTrip {
                entry_px: 0.068,
                entry_notional_usdt: 478.10,
                gross_usdt: net_usdt + 0.53,
                fees_usdt: 0.53,
                net_usdt,
                net_bps: 10_000.0 * net_usdt / 478.10,
                opened_ms: 1_786_999_000_000,
                held_ms: 1_000_000,
            }),
        }
    }

    /// These key names are what `scripts/runtime/notify_book_changes.py`
    /// reads. Renaming one here renames it there.
    #[test]
    fn a_trade_is_one_line_of_json_with_the_keys_the_notifier_reads() {
        let path = temp_path("trades");
        Trades::new(path.to_path_buf()).write(&[trade("ONGUSDT", Some(16.28))]);

        let written = std::fs::read_to_string(path.path()).expect("a file");
        assert_eq!(written.lines().count(), 1);
        let line: serde_json::Value = serde_json::from_str(written.trim()).expect("json");
        for key in [
            "sleeve",
            "symbol",
            "side",
            "qty",
            "exit_px",
            "closed_ms",
            "fills",
            "maker_share",
            "arrival_shortfall_bps",
            "round_trip",
        ] {
            assert!(line.get(key).is_some(), "{key} is missing from {line}");
        }
        for key in [
            "entry_px",
            "entry_notional_usdt",
            "gross_usdt",
            "fees_usdt",
            "net_usdt",
            "net_bps",
            "opened_ms",
            "held_ms",
        ] {
            assert!(line["round_trip"].get(key).is_some(), "{key} is missing");
        }
        assert_eq!(line["round_trip"]["net_usdt"], 16.28);
    }

    /// A close the log cannot price still reaches the phone. The notifier
    /// tells the two apart by this field being null, so it has to BE null
    /// rather than absent.
    #[test]
    fn an_unpriced_close_writes_a_null_round_trip() {
        let path = temp_path("trades-unpriced");
        Trades::new(path.to_path_buf()).write(&[trade("COTIUSDT", None)]);

        let written = std::fs::read_to_string(path.path()).expect("a file");
        let line: serde_json::Value = serde_json::from_str(written.trim()).expect("json");
        assert!(line["round_trip"].is_null(), "{line}");
        assert_eq!(line["symbol"], "COTIUSDT");
    }

    /// A reader that remembers a byte offset depends on this.
    #[test]
    fn writes_append_rather_than_replace() {
        let path = temp_path("trades-append");
        let mut trades = Trades::new(path.to_path_buf());
        trades.write(&[trade("ONGUSDT", Some(1.0))]);
        trades.write(&[trade("MOVEUSDT", Some(2.0)), trade("ACEUSDT", Some(3.0))]);

        let written = std::fs::read_to_string(path.path()).expect("a file");
        assert_eq!(written.lines().count(), 3, "{written}");
        assert!(written.lines().next().unwrap().contains("ONGUSDT"));
    }

    #[test]
    fn nothing_to_say_writes_no_file_at_all() {
        let path = temp_path("trades-empty");
        Trades::new(path.to_path_buf()).write(&[]);
        assert!(!path.path().exists());
    }
}
