//! The latency ledger: how long each part of our own side of the wire took.
//!
//! Timing segments, all measured on the one monotonic clock:
//!
//! - **decide** — market message arrived (`recv_ns`) until the strategy's
//!   intent was decided.
//! - **durable** — intent decided until the order record is on disk, past the
//!   barrier. This is the fsync.
//! - **submit result** — intent decided until `send_orders` returned (in
//!   shadow mode, until the point where the send was skipped). This includes
//!   the adapter's request, response, and reply parsing; it is not a socket
//!   write timestamp.
//! - **API round trip** — socket write completion until the adapter parsed
//!   the venue acknowledgement. This excludes local queueing and signing.
//! - **dispatch queue** — command queued until the venue task began it.
//! - **venue task** — venue task start until the adapter returned.
//! - **core resume** — adapter return until the engine handled the result.
//!
//! **market to submit result** is market message in until the batch submit
//! call returned and result handling began.

use hdrhistogram::Histogram;

use engine_types::WalRecord;

pub const WINDOW_NS: u64 = 60_000_000_000;

/// Histograms are sized once, at boot, to cover a nanosecond up to a minute
/// with three digits of precision. A self-resizing histogram would allocate
/// in the middle of the hot path and measure its own allocation; anything
/// slower than a minute is recorded as a minute.
const CEILING_NS: u64 = 60_000_000_000;

#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub struct Quantiles {
    pub count: u64,
    pub p50_ns: u64,
    pub p90_ns: u64,
    pub p99_ns: u64,
    pub max_ns: u64,
}

#[derive(Copy, Clone, Debug, PartialEq)]
pub enum Segment {
    Decide,
    Durable,
    /// What the order path actually waited for the disk, after the barrier
    /// was moved alongside the send. Usually zero: the venue's round trip
    /// outlasts the disk's. A number here is the disk winning that race.
    BarrierWait,
    /// How long the venue adapter held a command back to stay inside the
    /// venue's request quota. Pacing this engine chose, not latency the venue
    /// imposed — the two ask for opposite fixes.
    QuotaHold,
    Wire,
    Ack,
    DispatchQueue,
    VenueTask,
    CoreResume,
    EndToEnd,
}

impl Segment {
    pub fn plain_name(self) -> &'static str {
        match self {
            Segment::Decide => "think",
            Segment::Durable => "write it down",
            Segment::BarrierWait => "still waiting on the disk",
            Segment::QuotaHold => "held back for quota",
            Segment::Wire => "submit result",
            Segment::Ack => "API round trip",
            Segment::DispatchQueue => "dispatch queue",
            Segment::VenueTask => "venue task",
            Segment::CoreResume => "core resume",
            Segment::EndToEnd => "market to submit result",
        }
    }
}

pub struct LatencyLedger {
    decide: Histogram<u64>,
    durable: Histogram<u64>,
    barrier_wait: Histogram<u64>,
    quota_hold: Histogram<u64>,
    wire: Histogram<u64>,
    ack: Histogram<u64>,
    dispatch_queue: Histogram<u64>,
    venue_task: Histogram<u64>,
    core_resume: Histogram<u64>,
    end_to_end: Histogram<u64>,
    events: u64,
    window_start_ns: u64,
}

impl LatencyLedger {
    pub fn new(now_ns: u64) -> Self {
        let make = || Histogram::<u64>::new_with_bounds(1, CEILING_NS, 3).expect("histogram");
        LatencyLedger {
            decide: make(),
            durable: make(),
            barrier_wait: make(),
            quota_hold: make(),
            wire: make(),
            ack: make(),
            dispatch_queue: make(),
            venue_task: make(),
            core_resume: make(),
            end_to_end: make(),
            events: 0,
            window_start_ns: now_ns,
        }
    }

    pub fn record(&mut self, segment: Segment, ns: u64) {
        self.histogram(segment).saturating_record(ns);
    }

    /// One more market message seen. Counted whether or not it led anywhere.
    pub fn saw_event(&mut self) {
        self.events += 1;
    }

    pub fn quantiles(&self, segment: Segment) -> Quantiles {
        let h = match segment {
            Segment::Decide => &self.decide,
            Segment::Durable => &self.durable,
            Segment::BarrierWait => &self.barrier_wait,
            Segment::QuotaHold => &self.quota_hold,
            Segment::Wire => &self.wire,
            Segment::Ack => &self.ack,
            Segment::DispatchQueue => &self.dispatch_queue,
            Segment::VenueTask => &self.venue_task,
            Segment::CoreResume => &self.core_resume,
            Segment::EndToEnd => &self.end_to_end,
        };
        Quantiles {
            count: h.len(),
            p50_ns: h.value_at_quantile(0.50),
            p90_ns: h.value_at_quantile(0.90),
            p99_ns: h.value_at_quantile(0.99),
            max_ns: h.max(),
        }
    }

    pub fn events(&self) -> u64 {
        self.events
    }

    pub fn due(&self, now_ns: u64) -> bool {
        now_ns.saturating_sub(self.window_start_ns) >= WINDOW_NS
    }

    pub fn decisions(&self) -> u64 {
        self.decide.len()
    }

    /// The log record for the window just ended.
    pub fn record_for_wal(&self, now_ns: u64) -> WalRecord {
        let decide = self.quantiles(Segment::Decide);
        let durable = self.quantiles(Segment::Durable);
        let barrier_wait = self.quantiles(Segment::BarrierWait);
        let wire = self.quantiles(Segment::Wire);
        let ack = self.quantiles(Segment::Ack);
        let dispatch_queue = self.quantiles(Segment::DispatchQueue);
        let venue_task = self.quantiles(Segment::VenueTask);
        let core_resume = self.quantiles(Segment::CoreResume);
        let end_to_end = self.quantiles(Segment::EndToEnd);
        WalRecord::LatencyLedger {
            window_s: (now_ns.saturating_sub(self.window_start_ns) / 1_000_000_000) as u32,
            events: self.events,
            decide_p50_ns: decide.p50_ns,
            decide_p99_ns: decide.p99_ns,
            durable_p50_ns: durable.p50_ns,
            durable_p99_ns: durable.p99_ns,
            barrier_wait_p50_ns: barrier_wait.p50_ns,
            barrier_wait_p99_ns: barrier_wait.p99_ns,
            wire_p50_ns: wire.p50_ns,
            wire_p99_ns: wire.p99_ns,
            ack_p50_ns: ack.p50_ns,
            ack_p99_ns: ack.p99_ns,
            dispatch_queue_p50_ns: dispatch_queue.p50_ns,
            dispatch_queue_p99_ns: dispatch_queue.p99_ns,
            venue_task_p50_ns: venue_task.p50_ns,
            venue_task_p99_ns: venue_task.p99_ns,
            core_resume_p50_ns: core_resume.p50_ns,
            core_resume_p99_ns: core_resume.p99_ns,
            end_to_end_p50_ns: end_to_end.p50_ns,
            end_to_end_p99_ns: end_to_end.p99_ns,
        }
    }

    pub fn reset(&mut self, now_ns: u64) {
        self.decide.reset();
        self.durable.reset();
        self.wire.reset();
        self.ack.reset();
        self.dispatch_queue.reset();
        self.venue_task.reset();
        self.core_resume.reset();
        self.end_to_end.reset();
        self.events = 0;
        self.window_start_ns = now_ns;
    }

    /// One line a person can read without a key.
    pub fn plain_line(&self, now_ns: u64) -> String {
        let secs = (now_ns.saturating_sub(self.window_start_ns) / 1_000_000_000).max(1);
        let mut parts = vec![format!(
            "last {}s: {} market messages, {} orders decided",
            secs,
            self.events,
            self.decisions()
        )];
        for segment in [
            Segment::Decide,
            Segment::Durable,
            Segment::BarrierWait,
            Segment::QuotaHold,
            Segment::Wire,
            Segment::Ack,
            Segment::DispatchQueue,
            Segment::VenueTask,
            Segment::CoreResume,
            Segment::EndToEnd,
        ] {
            let q = self.quantiles(segment);
            if q.count == 0 {
                continue;
            }
            parts.push(format!(
                "{} usually {}, slow one in a hundred {}",
                segment.plain_name(),
                pretty(q.p50_ns),
                pretty(q.p99_ns)
            ));
        }
        parts.join("; ")
    }

    fn histogram(&mut self, segment: Segment) -> &mut Histogram<u64> {
        match segment {
            Segment::Decide => &mut self.decide,
            Segment::Durable => &mut self.durable,
            Segment::BarrierWait => &mut self.barrier_wait,
            Segment::QuotaHold => &mut self.quota_hold,
            Segment::Wire => &mut self.wire,
            Segment::Ack => &mut self.ack,
            Segment::DispatchQueue => &mut self.dispatch_queue,
            Segment::VenueTask => &mut self.venue_task,
            Segment::CoreResume => &mut self.core_resume,
            Segment::EndToEnd => &mut self.end_to_end,
        }
    }
}

/// Nanoseconds in units a person reads at a glance.
pub fn pretty(ns: u64) -> String {
    if ns < 1_000 {
        format!("{ns}ns")
    } else if ns < 1_000_000 {
        format!("{:.1}us", ns as f64 / 1_000.0)
    } else if ns < 1_000_000_000 {
        format!("{:.2}ms", ns as f64 / 1_000_000.0)
    } else {
        format!("{:.2}s", ns as f64 / 1_000_000_000.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quantiles_track_what_was_recorded() {
        let mut ledger = LatencyLedger::new(0);
        for ns in 1..=1000u64 {
            ledger.record(Segment::Decide, ns * 1_000);
        }
        let q = ledger.quantiles(Segment::Decide);
        assert_eq!(q.count, 1000);
        assert!(q.p50_ns > 490_000 && q.p50_ns < 510_000, "p50 {}", q.p50_ns);
        assert!(q.p99_ns > 980_000, "p99 {}", q.p99_ns);
        assert!(q.max_ns >= 1_000_000);
    }

    #[test]
    fn the_window_rolls_after_a_minute_and_clears() {
        let mut ledger = LatencyLedger::new(0);
        ledger.record(Segment::Wire, 5_000);
        ledger.saw_event();
        assert!(!ledger.due(WINDOW_NS - 1));
        assert!(ledger.due(WINDOW_NS));
        match ledger.record_for_wal(WINDOW_NS) {
            WalRecord::LatencyLedger {
                window_s,
                events,
                wire_p50_ns,
                ..
            } => {
                assert_eq!(window_s, 60);
                assert_eq!(events, 1);
                assert!(wire_p50_ns > 0);
            }
            other => panic!("wrong record: {other:?}"),
        }
        ledger.reset(WINDOW_NS);
        assert_eq!(ledger.quantiles(Segment::Wire).count, 0);
        assert_eq!(ledger.events(), 0);
    }

    #[test]
    fn plain_line_names_the_segments_in_words() {
        let mut ledger = LatencyLedger::new(0);
        ledger.saw_event();
        ledger.record(Segment::Decide, 12_000);
        ledger.record(Segment::Durable, 2_200_000);
        ledger.record(Segment::Wire, 2_400_000);
        let line = ledger.plain_line(1_000_000_000);
        assert!(line.contains("think"), "{line}");
        assert!(line.contains("write it down"), "{line}");
        assert!(line.contains("submit result"), "{line}");
        assert!(!line.contains("out the door"), "{line}");
        assert!(line.contains("2.20ms"), "{line}");
        assert!(!line.contains("p99"), "no jargon: {line}");
    }

    #[test]
    fn pretty_picks_readable_units() {
        assert_eq!(pretty(999), "999ns");
        assert_eq!(pretty(12_340), "12.3us");
        assert_eq!(pretty(2_200_000), "2.20ms");
        assert_eq!(pretty(1_500_000_000), "1.50s");
    }
}
