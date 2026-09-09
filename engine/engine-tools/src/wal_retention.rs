//! The lowest log segment a running engine may still open.
//!
//! The host reclaims old sealed segments from the front of a WAL family once
//! they are verified in cloud backup. Two readers decide how far forward that
//! may go: boot replays the newest segment it trusts, or the trusted segment
//! before it when the newest one turns out to be an abandoned rotation, and
//! retained callback recovery opens whatever segment a cursor in that
//! restatement names. The lower of those is the floor. Nothing at or above it
//! is deletable; everything below it is an archive.
//!
//! One frame per segment is read, never a segment: a segment is 256 MiB, the
//! restatement at its head is a few, and the host has 8 GB.

use std::error::Error;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};

use engine_core::callback_recovery::paging::CallbackPages;
use engine_wal::WalRecord;

#[derive(Debug, serde::Serialize)]
pub struct Segment {
    pub index: u64,
    pub path: PathBuf,
    pub bytes: u64,
}

#[derive(Debug, serde::Serialize)]
pub struct Retention {
    pub segments: Vec<Segment>,
    pub current_segment: u64,
    pub newest_trusted_segment: u64,
    pub boot_fallback_segment: u64,
    pub callback_floor_segment: Option<u64>,
    pub retention_floor_segment: u64,
}

/// Every segment index a cursor in this restatement names. The queue slots go
/// through the engine's own replay of the frame; the source frontiers and the
/// legacy inputs are read from the same frame that replay reads.
fn callback_floor(base: &WalRecord, segment: u64) -> Result<Option<u64>, Box<dyn Error>> {
    let WalRecord::SegmentBase {
        strategies,
        strategy_callbacks,
        strategy_callback_sources,
        ..
    } = base
    else {
        return Ok(None);
    };
    let (state, pages) =
        CallbackPages::replay(std::slice::from_ref(base), strategies.len(), segment)
            .map_err(|error| format!("retained callback restatement: {error}"))?;
    let mut named: Vec<u64> = Vec::new();
    for slot in pages.slots.values() {
        named.push(slot.queued.segment);
        named.extend(slot.prepared.map(|cursor| cursor.segment));
    }
    for input in state.inputs.values() {
        named.extend(input.order_origin.map(|origin| origin.segment));
    }
    for source in strategy_callback_sources {
        named.push(source.cursor.segment);
        named.push(source.latest.segment);
        named.extend(source.accepted.map(|origin| origin.segment));
    }
    for input in strategy_callbacks {
        named.extend(input.order_origin.map(|origin| origin.segment));
    }
    Ok(named.into_iter().filter(|segment| *segment != 0).min())
}

pub fn read(family: &Path) -> Result<Retention, Box<dyn Error>> {
    let mut segments = Vec::new();
    let mut trusted: Vec<(u64, Option<WalRecord>)> = Vec::new();
    for (index, path) in engine_wal::segments(family)? {
        let bytes = std::fs::metadata(&path)?.len();
        let first = engine_wal::first_record(&path)?;
        if index <= 1 || matches!(first, Some(WalRecord::SegmentBase { .. })) {
            trusted.push((index, first));
        }
        segments.push(Segment { index, path, bytes });
    }
    let current_segment = segments
        .last()
        .map(|segment| segment.index)
        .ok_or_else(|| format!("no log segment under {}", family.display()))?;
    let (newest_trusted_segment, newest_base) = trusted
        .pop()
        .ok_or_else(|| format!("no trusted log segment under {}", family.display()))?;
    let boot_fallback_segment = trusted
        .last()
        .map_or(newest_trusted_segment, |(index, _)| *index);
    let callback_floor_segment = match &newest_base {
        Some(base) => callback_floor(base, newest_trusted_segment)?,
        None => None,
    };
    Ok(Retention {
        segments,
        current_segment,
        newest_trusted_segment,
        boot_fallback_segment,
        callback_floor_segment,
        retention_floor_segment: boot_fallback_segment
            .min(callback_floor_segment.unwrap_or(u64::MAX)),
    })
}

impl Retention {
    pub fn table(&self) -> String {
        let mut out = String::new();
        let _ = writeln!(out, "segment          bytes  path");
        for segment in &self.segments {
            let _ = writeln!(
                out,
                "{:>7}  {:>13}  {}",
                segment.index,
                segment.bytes,
                segment.path.display()
            );
        }
        let _ = writeln!(out, "\ncurrent_segment          {}", self.current_segment);
        let _ = writeln!(
            out,
            "newest_trusted_segment   {}",
            self.newest_trusted_segment
        );
        let _ = writeln!(
            out,
            "boot_fallback_segment    {}",
            self.boot_fallback_segment
        );
        let _ = writeln!(
            out,
            "callback_floor_segment   {}",
            match self.callback_floor_segment {
                Some(segment) => segment.to_string(),
                None => "none".to_string(),
            }
        );
        let _ = writeln!(
            out,
            "retention_floor_segment  {}",
            self.retention_floor_segment
        );
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::strategy_process::{CallbackQueueSlot, CallbackWalCursor};
    use engine_types::{StrategyId, Wal};

    fn base(queues: Vec<CallbackQueueSlot>) -> WalRecord {
        let mut record: WalRecord = serde_json::from_value(serde_json::json!({
            "kind": "segment_base", "wall_ts_ms": 1, "strategies": ["owner"],
            "symbols": ["BTCUSDT"], "may_open": false, "control_anchors": [],
            "attribution": [], "logged_exposure": [], "intended_stops": [],
            "portfolio": engine_types::portfolio::PortfolioState::default(),
            "open_trade_lots": [], "open_orders": [],
        }))
        .unwrap();
        let WalRecord::SegmentBase {
            strategy_callback_queues,
            ..
        } = &mut record
        else {
            unreachable!()
        };
        *strategy_callback_queues = queues;
        record
    }

    /// Four segments, a restatement at the head of 2, 3 and 4.
    fn family(dir: &Path, newest: Vec<CallbackQueueSlot>) -> PathBuf {
        let path = dir.join("engine.wal");
        let (mut wal, _) = engine_wal::WalWriter::open(&path).unwrap();
        wal.rotate(&base(Vec::new())).unwrap();
        wal.rotate(&base(Vec::new())).unwrap();
        wal.rotate(&base(newest)).unwrap();
        wal.barrier().unwrap();
        path
    }

    fn slot_at(segment: u64) -> CallbackQueueSlot {
        CallbackQueueSlot {
            callback_id: 1,
            strategy: StrategyId(0),
            queued: CallbackWalCursor {
                segment,
                sequence: 1,
                offset: 0,
            },
            prepared: None,
            event_sha256: [0; 32],
        }
    }

    #[test]
    fn the_floor_is_the_trusted_segment_boot_falls_back_to() {
        let dir = tempfile::tempdir().unwrap();
        let family = family(dir.path(), Vec::new());
        let report = read(&family).unwrap();
        assert_eq!(
            report
                .segments
                .iter()
                .map(|segment| segment.index)
                .collect::<Vec<_>>(),
            [1, 2, 3, 4]
        );
        assert_eq!(report.current_segment, 4);
        assert_eq!(report.newest_trusted_segment, 4);
        assert_eq!(report.boot_fallback_segment, 3);
        assert_eq!(report.callback_floor_segment, None);
        assert_eq!(report.retention_floor_segment, 3);

        assert!(
            report.table().contains("retention_floor_segment  3"),
            "{}",
            report.table()
        );
        let json: serde_json::Value =
            serde_json::from_str(&serde_json::to_string(&report).unwrap()).unwrap();
        assert_eq!(
            json.as_object().unwrap().keys().collect::<Vec<_>>(),
            [
                "boot_fallback_segment",
                "callback_floor_segment",
                "current_segment",
                "newest_trusted_segment",
                "retention_floor_segment",
                "segments"
            ],
            "--json carries exactly these keys"
        );
    }

    #[test]
    fn a_torn_restatement_drops_the_floor_to_the_segment_before_it() {
        let dir = tempfile::tempdir().unwrap();
        let family = family(dir.path(), Vec::new());
        let newest = engine_wal::segments(&family).unwrap().pop().unwrap().1;
        let whole = std::fs::read(&newest).unwrap();
        std::fs::write(&newest, &whole[..whole.len() / 2]).unwrap();
        let report = read(&family).unwrap();
        assert_eq!(report.current_segment, 4, "the file is still there");
        assert_eq!(report.newest_trusted_segment, 3);
        assert_eq!(report.boot_fallback_segment, 2);
        assert_eq!(report.retention_floor_segment, 2);
    }

    #[test]
    fn a_retained_callback_cursor_holds_its_own_segment() {
        let dir = tempfile::tempdir().unwrap();
        let family = family(dir.path(), vec![slot_at(2)]);
        let report = read(&family).unwrap();
        assert_eq!(report.newest_trusted_segment, 4);
        assert_eq!(report.boot_fallback_segment, 3);
        assert_eq!(report.callback_floor_segment, Some(2));
        assert_eq!(report.retention_floor_segment, 2);
    }
}
