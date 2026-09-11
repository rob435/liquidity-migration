//! What a restored WAL family says, read before anything is armed on top of
//! it.
//!
//! Read-only. No lock is taken, no torn tail is truncated, nothing is
//! written: `engine_wal::open_current` does all three and is never called
//! here. Peak memory is one segment, not one family — every segment is
//! scanned in turn and its records dropped, and only the newest trusted
//! segment, the one boot itself replays, is folded.
//!
//! The folds are the engine's own: [`LedgerOfOrders`] for the orders the log
//! left open, [`Attribution`] for the positions and the protection it expects
//! on them, and [`crate::cohort`] for the order decisions that reached no
//! recorded outcome. Nothing here re-derives any of the three.
//!
//! The log is one side of a comparison. A backup is up to one backup interval
//! behind the host that wrote it, so an order placed inside that window is at
//! the venue and not in this report. Nothing here reads a venue.

use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use engine_core::attribution::Attribution;
use engine_core::inflight::LedgerOfOrders;
use engine_core::replay::LogNames;
use engine_types::wal::OrderFillQuantity;
use engine_wal::{WalError, WalRecord};

/// The magic `engine-wal` writes at offset 0 and refuses any other bytes for.
const READER_FORMAT: &str = "EWAL0001";

/// The newest restatement kind this reader decodes; `segment_base` is its
/// alias. A segment headed by a later one is refused, not skipped.
const READER_SEGMENT_BASE: &str = "segment_base_v7";

/// Minutes. Two backup intervals, which is the age the backup watchdog alerts
/// at.
pub const DEFAULT_MAX_AGE_MIN: i64 = 30;

/// A deliverable spool file is `<sequence:020>-<sha256>.json`.
const SPOOL_SEQUENCE_DIGITS: usize = 20;

/// Counted apart from the deliverable rows, under the names the spool reader
/// itself skips.
const SPOOL_READINESS_FILES: [&str; 2] = [
    engine_types::SIGNAL_READINESS_REQUEST_FILE,
    engine_types::SIGNAL_READINESS_RESPONSE_FILE,
];

pub struct Options {
    pub family: PathBuf,
    pub spool: Option<PathBuf>,
    pub controls: Option<PathBuf>,
    pub max_age_min: i64,
    /// Realtime milliseconds the staleness check is measured against.
    pub now_ms: i64,
}

/// What the operator does next, and the exit status that says it without
/// reading the table. Declared in precedence order: the first that holds wins.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Verdict {
    /// The family could not be read at all.
    Unreadable,
    /// A frame checked out and this binary refused the record inside it.
    IncompatibleReader,
    /// The newest stamp in the log is older than `--max-age-min`.
    StaleBackup,
    /// The log leaves exposure, protection or a decision outstanding, so the
    /// venue must be read before arming.
    ReconcileRequired,
    /// Flat and current as far as the log knows.
    ReadyToReconcile,
}

impl Verdict {
    pub fn as_str(self) -> &'static str {
        match self {
            Verdict::Unreadable => "unreadable",
            Verdict::IncompatibleReader => "incompatible-reader",
            Verdict::StaleBackup => "stale-backup",
            Verdict::ReconcileRequired => "reconcile-required",
            Verdict::ReadyToReconcile => "ready-to-reconcile",
        }
    }

    pub fn exit_code(self) -> u8 {
        match self {
            Verdict::ReadyToReconcile => 0,
            Verdict::Unreadable => 1,
            Verdict::IncompatibleReader => 2,
            Verdict::ReconcileRequired => 3,
            Verdict::StaleBackup => 4,
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Segment {
    pub index: u64,
    pub path: PathBuf,
    pub bytes: Option<u64>,
    /// The wire kind of the first record that decoded. `None` where a
    /// rotation left nothing complete to read.
    pub first_record_kind: Option<String>,
    /// Whether boot may replay this segment alone. `None` where the segment
    /// did not decode.
    pub trusted: Option<bool>,
    /// Whether the segment ends part-way through a record. `None` where the
    /// segment did not decode.
    pub torn_tail: Option<bool>,
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct OpenOrder {
    pub client_order_id: String,
    pub symbol_id: u16,
    /// `None` where the log carries no name table for this id.
    pub symbol: Option<String>,
    pub side: engine_types::Side,
    pub qty: f64,
    pub filled: Option<f64>,
    pub remaining: Option<f64>,
    /// Whether the venue ever answered the send.
    pub acked: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Position {
    pub strategy_id: u16,
    pub strategy: Option<String>,
    pub symbol_id: u16,
    pub symbol: Option<String>,
    /// Exact, as the log states it. Positive is long.
    pub signed_qty: String,
    /// The protective trigger the log expects to find at the venue, absent
    /// where the log records none for this position.
    pub intended_stop_px: Option<String>,
}

/// One directory beside the log, as found. A directory that is not there is
/// `missing`; one no flag named is `not-checked`.
#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Directory {
    pub path: Option<PathBuf>,
    pub state: &'static str,
    pub files: Option<u64>,
    pub error: Option<String>,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize)]
pub struct Spool {
    #[serde(flatten)]
    pub directory: Directory,
    /// Files named `input-readiness-*.json`, which the engine never delivers.
    pub readiness_files: Option<u64>,
    /// Files under a `quarantine/` subdirectory. `None` where there is none.
    pub quarantined: Option<u64>,
    /// The highest sequence in the deliverable file names.
    pub newest_sequence: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Restore {
    pub family: PathBuf,
    /// What this binary accepts, so a report read on a replacement host can
    /// be compared against the binary that wrote the log.
    pub reader_format: &'static str,
    pub reader_segment_base: &'static str,
    pub segments: Vec<Segment>,
    /// Whether every segment decoded to its end.
    pub decoded: bool,
    /// The decoder's or the filesystem's own words, verbatim.
    pub error: Option<String>,
    pub newest_trusted_segment: Option<u64>,
    /// The sequence the next append to that segment would get.
    pub next_seq: Option<u64>,
    pub torn_tail: Option<bool>,
    /// The reconciliation latch as the log last set it. `None` where nothing
    /// in the replayed segment ever judged it.
    pub may_open: Option<bool>,
    pub open_orders: Option<Vec<OpenOrder>>,
    pub positions: Option<Vec<Position>>,
    pub intended_stops: Option<u64>,
    /// Order decisions the log neither resolved nor refused.
    pub unresolved_order_decisions: Option<u64>,
    pub newest_wall_ts_ms: Option<i64>,
    pub age_min: Option<i64>,
    pub max_age_min: i64,
    pub stale: Option<bool>,
    pub spool: Spool,
    pub controls: Directory,
    pub verdict: Verdict,
}

/// Read one restored family. Never fails: a family that cannot be read is a
/// report with a verdict and the reason, because the caller is an operator
/// deciding whether to arm, not a pipeline.
pub fn read(options: &Options) -> Restore {
    let family = options.family.clone();
    let mut report = Restore {
        family: family.clone(),
        reader_format: READER_FORMAT,
        reader_segment_base: READER_SEGMENT_BASE,
        segments: Vec::new(),
        decoded: false,
        error: None,
        newest_trusted_segment: None,
        next_seq: None,
        torn_tail: None,
        may_open: None,
        open_orders: None,
        positions: None,
        intended_stops: None,
        unresolved_order_decisions: None,
        newest_wall_ts_ms: None,
        age_min: None,
        max_age_min: options.max_age_min,
        stale: None,
        spool: spool(options.spool.as_deref()),
        controls: controls(options.controls.as_deref()),
        verdict: Verdict::Unreadable,
    };

    let listed = match engine_wal::segments(&family) {
        Ok(listed) if !listed.is_empty() => listed,
        Ok(_) => {
            note(
                &mut report,
                format!("no log segment under {}", family.display()),
            );
            return report;
        }
        Err(error) => {
            note(&mut report, error.to_string());
            return report;
        }
    };

    let mut unreadable = false;
    let mut refused = false;
    for (index, path) in listed {
        let bytes = std::fs::metadata(&path).map(|meta| meta.len()).ok();
        // One segment's records at a time: read for the listing, then
        // dropped. The fold below reopens only the one boot would replay.
        match scan(index, &path) {
            Ok((records, torn)) => report.segments.push(Segment {
                index,
                path,
                bytes,
                first_record_kind: records
                    .first()
                    .map(|record| crate::cohort::kind_of(record).to_string()),
                trusted: Some(index <= 1 || starts_with_base(records.first())),
                torn_tail: Some(torn),
            }),
            Err(error) => {
                match error {
                    WalError::Corrupt { .. } => refused = true,
                    WalError::Io(_) => unreadable = true,
                }
                note(&mut report, error.to_string());
                report.segments.push(Segment {
                    index,
                    path,
                    bytes,
                    first_record_kind: None,
                    trusted: None,
                    torn_tail: None,
                });
            }
        }
    }
    report.newest_trusted_segment = report
        .segments
        .iter()
        .rev()
        .find(|segment| segment.trusted == Some(true))
        .map(|segment| segment.index);

    match engine_wal::replay_current(&family) {
        Ok((replayed, torn)) => {
            report.torn_tail = Some(torn);
            report.next_seq = Some(replayed.last().map_or(1, |(sequence, _)| sequence + 1));
            fold(&mut report, replayed.into_iter().map(|(_, r)| r).collect());
        }
        Err(error) => {
            match error {
                WalError::Corrupt { .. } => refused = true,
                WalError::Io(_) => unreadable = true,
            }
            note(&mut report, error.to_string());
        }
    }
    report.decoded = !unreadable && !refused;

    if let Some(newest) = report.newest_wall_ts_ms {
        let age_min = (options.now_ms - newest) / 60_000;
        report.age_min = Some(age_min);
        report.stale = Some(age_min > options.max_age_min);
    }
    report.verdict = verdict(&report, unreadable, refused);
    report
}

/// The first reason, kept. A later one is a consequence of it and would push
/// the cause off the report.
fn note(report: &mut Restore, error: String) {
    if report.error.is_none() {
        report.error = Some(error);
    }
}

/// One segment, with `engine-wal`'s own tolerance: a numbered segment whose
/// header never finished is an abandoned rotation, not an unreadable log.
fn scan(index: u64, path: &Path) -> Result<(Vec<WalRecord>, bool), WalError> {
    match engine_wal::replay_scan(path) {
        Ok((records, torn)) => Ok((
            records.into_iter().map(|(_, record)| record).collect(),
            torn,
        )),
        Err(WalError::Corrupt { offset: 0, .. }) if index > 1 => Ok((Vec::new(), false)),
        Err(error) => Err(error),
    }
}

fn starts_with_base(first: Option<&WalRecord>) -> bool {
    matches!(first, Some(WalRecord::SegmentBase { .. }))
}

/// What the newest trusted segment leaves standing, through the engine's own
/// readers. A reader that refuses the records leaves its own fields `unknown`
/// and names the reason; the rest of the report still stands.
fn fold(report: &mut Restore, records: Vec<WalRecord>) {
    let names = LogNames::of_log(&records);
    report.may_open = records.iter().rev().find_map(|record| match record {
        WalRecord::Reconciled { may_open, .. } => Some(*may_open),
        WalRecord::SegmentBase { may_open, .. } => Some(*may_open),
        WalRecord::LatchCleared { .. } => Some(true),
        _ => None,
    });
    report.newest_wall_ts_ms = records.iter().filter_map(crate::cohort::wall_stamp).max();
    report.unresolved_order_decisions = Some(crate::cohort::of_log(&records).order_lane.unresolved);

    match LedgerOfOrders::try_from_records(&records) {
        Ok(ledger) => {
            report.open_orders = Some(
                ledger
                    .iter_in_flight()
                    .map(|order| {
                        let filled = match &order.fill_quantity {
                            OrderFillQuantity::Exact { quantity } => quantity.to_f64().ok(),
                            OrderFillQuantity::LegacyBinary64 { quantity } => Some(*quantity),
                        };
                        OpenOrder {
                            client_order_id: order.request.client_order_id.clone(),
                            symbol_id: order.request.symbol.0,
                            symbol: names.symbols.get(order.request.symbol.0 as usize).cloned(),
                            side: order.request.side,
                            qty: order.request.qty,
                            filled,
                            remaining: filled.map(|filled| (order.request.qty - filled).max(0.0)),
                            acked: order.acked,
                        }
                    })
                    .collect(),
            );
        }
        Err(error) => note(report, error),
    }

    match Attribution::try_from_records(&records) {
        Ok(attribution) => {
            let positions: Vec<Position> = attribution
                .snapshot()
                .positions
                .into_iter()
                .map(|row| Position {
                    strategy_id: row.strategy.0,
                    strategy: names.strategies.get(row.strategy.0 as usize).cloned(),
                    symbol_id: row.symbol.0,
                    symbol: names.symbols.get(row.symbol.0 as usize).cloned(),
                    signed_qty: row.signed_qty.to_string(),
                    intended_stop_px: row.stop_px.map(|px| px.to_string()),
                })
                .collect();
            report.intended_stops = Some(
                positions
                    .iter()
                    .filter(|row| row.intended_stop_px.is_some())
                    .count() as u64,
            );
            report.positions = Some(positions);
        }
        Err(error) => note(report, error),
    }
}

fn verdict(report: &Restore, unreadable: bool, refused: bool) -> Verdict {
    if unreadable {
        return Verdict::Unreadable;
    }
    if refused {
        return Verdict::IncompatibleReader;
    }
    if report.stale == Some(true) {
        return Verdict::StaleBackup;
    }
    // An unknown is not a zero: a fold that could not run leaves the venue to
    // be read, exactly as a standing order does.
    let standing = report
        .open_orders
        .as_ref()
        .is_none_or(|rows| !rows.is_empty())
        || report
            .positions
            .as_ref()
            .is_none_or(|rows| !rows.is_empty())
        || report.intended_stops != Some(0)
        || report.unresolved_order_decisions != Some(0);
    if standing {
        return Verdict::ReconcileRequired;
    }
    Verdict::ReadyToReconcile
}

fn entries(path: &Path) -> Result<Vec<PathBuf>, std::io::Error> {
    let mut found = Vec::new();
    for entry in std::fs::read_dir(path)? {
        found.push(entry?.path());
    }
    Ok(found)
}

fn absent(path: Option<&Path>) -> Directory {
    Directory {
        path: path.map(Path::to_path_buf),
        state: match path {
            None => "not-checked",
            Some(_) => "missing",
        },
        files: None,
        error: None,
    }
}

/// The sequence in a deliverable spool file name, or `None` for anything else
/// in the directory.
fn spool_sequence(path: &Path) -> Option<u64> {
    let name = path.file_name()?.to_str()?;
    let (sequence, _) = name.strip_suffix(".json")?.split_once('-')?;
    if sequence.len() != SPOOL_SEQUENCE_DIGITS {
        return None;
    }
    sequence.parse().ok()
}

fn spool(path: Option<&Path>) -> Spool {
    let empty = Spool {
        directory: absent(path),
        readiness_files: None,
        quarantined: None,
        newest_sequence: None,
    };
    let Some(path) = path else {
        return empty;
    };
    if !path.is_dir() {
        return empty;
    }
    let listed = match entries(path) {
        Ok(listed) => listed,
        Err(error) => {
            return Spool {
                directory: unreadable_directory(path, &error),
                ..empty
            }
        }
    };
    let sequences: Vec<u64> = listed
        .iter()
        .filter_map(|row| spool_sequence(row))
        .collect();
    let readiness = listed
        .iter()
        .filter(|row| {
            row.file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| SPOOL_READINESS_FILES.contains(&name))
        })
        .count() as u64;
    let quarantine = path.join("quarantine");
    Spool {
        directory: Directory {
            path: Some(path.to_path_buf()),
            state: "present",
            files: Some(sequences.len() as u64),
            error: None,
        },
        readiness_files: Some(readiness),
        quarantined: quarantine
            .is_dir()
            .then(|| entries(&quarantine).map_or(0, |rows| rows.len() as u64)),
        newest_sequence: sequences.into_iter().max(),
    }
}

fn unreadable_directory(path: &Path, error: &std::io::Error) -> Directory {
    Directory {
        path: Some(path.to_path_buf()),
        state: "unreadable",
        files: None,
        error: Some(error.to_string()),
    }
}

fn controls(path: Option<&Path>) -> Directory {
    let Some(path) = path else {
        return absent(None);
    };
    if !path.is_dir() {
        return absent(Some(path));
    }
    match entries(path) {
        Ok(listed) => Directory {
            path: Some(path.to_path_buf()),
            state: "present",
            files: Some(listed.len() as u64),
            error: None,
        },
        Err(error) => unreadable_directory(path, &error),
    }
}

fn known<T: std::fmt::Display>(value: Option<T>) -> String {
    value.map_or_else(|| "unknown".to_string(), |value| value.to_string())
}

fn yes_no(value: Option<bool>) -> String {
    value.map_or_else(
        || "unknown".to_string(),
        |value| if value { "yes" } else { "no" }.to_string(),
    )
}

impl Restore {
    pub fn table(&self) -> String {
        let mut out = String::new();
        let _ = writeln!(out, "restore-check family={}", self.family.display());
        let _ = writeln!(
            out,
            "reader    {} {}",
            self.reader_format, self.reader_segment_base
        );
        let _ = writeln!(
            out,
            "\nsegment          bytes  first record            trusted  torn  path"
        );
        for segment in &self.segments {
            let _ = writeln!(
                out,
                "{:>7}  {:>13}  {:<22}  {:>7}  {:>4}  {}",
                segment.index,
                known(segment.bytes),
                known(segment.first_record_kind.as_deref()),
                yes_no(segment.trusted),
                yes_no(segment.torn_tail),
                segment.path.display()
            );
        }
        let _ = writeln!(
            out,
            "\nnewest_trusted_segment      {}",
            known(self.newest_trusted_segment)
        );
        let _ = writeln!(out, "next_seq                    {}", known(self.next_seq));
        let _ = writeln!(
            out,
            "decoded                     {}",
            yes_no(Some(self.decoded))
        );
        let _ = writeln!(
            out,
            "torn_tail                   {}",
            yes_no(self.torn_tail)
        );
        let _ = writeln!(
            out,
            "error                       {}",
            known(self.error.as_deref())
        );
        let _ = writeln!(out, "may_open                    {}", yes_no(self.may_open));
        let _ = writeln!(
            out,
            "newest_wall_ts_ms           {}",
            known(self.newest_wall_ts_ms)
        );
        let _ = writeln!(
            out,
            "age_min                     {} (stale over {})",
            known(self.age_min),
            self.max_age_min
        );
        let _ = writeln!(out, "stale                       {}", yes_no(self.stale));
        let _ = writeln!(
            out,
            "unresolved_order_decisions  {}",
            known(self.unresolved_order_decisions)
        );
        let _ = writeln!(
            out,
            "intended_stops              {}",
            known(self.intended_stops)
        );

        let _ = writeln!(out, "\norders the log left out there");
        match self.open_orders.as_deref() {
            None => {
                let _ = writeln!(out, "  unknown");
            }
            Some([]) => {
                let _ = writeln!(out, "  none");
            }
            Some(rows) => {
                for row in rows {
                    let _ = writeln!(
                        out,
                        "  {} {} {:?} qty={} filled={} remaining={} acked={}",
                        row.client_order_id,
                        known(row.symbol.as_deref()),
                        row.side,
                        row.qty,
                        known(row.filled),
                        known(row.remaining),
                        row.acked
                    );
                }
            }
        }

        let _ = writeln!(out, "\npositions the log expects at the venue");
        match self.positions.as_deref() {
            None => {
                let _ = writeln!(out, "  unknown");
            }
            Some([]) => {
                let _ = writeln!(out, "  none");
            }
            Some(rows) => {
                for row in rows {
                    let _ = writeln!(
                        out,
                        "  {} {} {} stop={}",
                        known(row.strategy.as_deref()),
                        known(row.symbol.as_deref()),
                        row.signed_qty,
                        row.intended_stop_px.as_deref().unwrap_or("absent")
                    );
                }
            }
        }

        let _ = writeln!(
            out,
            "\nspool     {} {} deliverable={} readiness={} quarantined={} newest_sequence={}",
            known(self.spool.directory.path.as_deref().map(Path::display)),
            self.spool.directory.state,
            known(self.spool.directory.files),
            known(self.spool.readiness_files),
            known(self.spool.quarantined),
            known(self.spool.newest_sequence)
        );
        let _ = writeln!(
            out,
            "controls  {} {} files={}",
            known(self.controls.path.as_deref().map(Path::display)),
            self.controls.state,
            known(self.controls.files)
        );
        let _ = writeln!(
            out,
            "\nverdict   {} (exit {})",
            self.verdict.as_str(),
            self.verdict.exit_code()
        );
        out
    }
}

#[cfg(test)]
mod tests;
