//! What a `--contention` run measured, read back from the log it wrote.
//!
//! The venue task holds one gateway call at a time and serves the classes in
//! priority order, cancels first. So the question a slow venue asks is: how
//! long does a risk-reducing command wait when the openings ahead of it are
//! slow? Every stamp needed to answer it is already in the log — this reads
//! the [`WalRecord::VenueTiming`] rows back, groups them by the operation the
//! engine wrote, and counts the openings the venue never saw because their
//! authority ran out while they queued.

use engine_types::{OrderUpdate, WalRecord};

use super::wal_timing::quantiles;
use crate::ledger::Quantiles;

/// The words `engine_types::authority_refusal` puts in front of an opening
/// whose dispatch TTL ran out in the venue queue. It reaches the log as the
/// reject reason of an order that was never transmitted, and nothing typed
/// carries the expiry — so a change to that sentence must change this one.
/// `a_dispatch_ttl_under_the_queue_refuses_openings_unsent` is what notices.
const EXPIRED: &str = "authority: expired";

/// The operation strings [`WalRecord::VenueTiming`] carries for the two
/// commands this workload issues.
const PLACE: &str = "place";
const CANCEL: &str = "cancel";

#[derive(Clone, Debug)]
pub struct ContentionResult {
    pub venue_delay_ms: u64,
    pub cancel_after: u64,
    pub ttl_ms: u64,
    pub symbols: usize,
    /// Whether the pretend venue also held a local request quota.
    pub quota: bool,
    /// Openings the venue answered. `opening_queue_wait.count` is every
    /// placement, answered or refused unsent.
    pub openings_sent: u64,
    pub cancels_sent: u64,
    /// Openings refused unsent because their authority expired in the queue.
    pub never_sent_expired: u64,
    /// Queued to picked up by the venue task, per operation.
    pub cancel_queue_wait: Quantiles,
    pub opening_queue_wait: Quantiles,
    /// Picked up to answered, less any quota hold: the call itself.
    pub cancel_venue_span: Quantiles,
}

/// `queued_ns` to `task_started_ns` and the call span, per operation, plus the
/// expiries. `symbols` is carried through because the queue depth a cancel
/// waits behind is one per symbol: the engine holds a symbol busy for the
/// length of its own command.
pub(super) fn read(
    records: &[WalRecord],
    venue_delay_ms: u64,
    cancel_after: u64,
    ttl_ms: u64,
    symbols: usize,
    quota: bool,
) -> ContentionResult {
    let mut opening_waits = Vec::new();
    let mut cancel_waits = Vec::new();
    let mut cancel_spans = Vec::new();
    let mut openings_sent = 0u64;
    let mut never_sent_expired = 0u64;
    for record in records {
        match record {
            WalRecord::VenueTiming {
                operation,
                queued_ns,
                task_started_ns,
                ack_ns,
                rate_wait_ns,
                task_completed_ns,
                ..
            } => {
                let waited = task_started_ns.saturating_sub(*queued_ns);
                match operation.as_str() {
                    // A placement refused at the send boundary is journaled
                    // too, with the wait it served and no transport stamps. It
                    // belongs in the wait: that wait is why it was refused.
                    PLACE => {
                        opening_waits.push(waited);
                        openings_sent += u64::from(ack_ns.is_some());
                    }
                    CANCEL => {
                        cancel_waits.push(waited);
                        cancel_spans.push(
                            task_completed_ns
                                .saturating_sub(*task_started_ns)
                                .saturating_sub(rate_wait_ns.unwrap_or(0)),
                        );
                    }
                    _ => {}
                }
            }
            WalRecord::OrderUpdate {
                update: OrderUpdate::Reject { reason, .. },
                ..
            } if reason.contains(EXPIRED) => never_sent_expired += 1,
            _ => {}
        }
    }
    ContentionResult {
        venue_delay_ms,
        cancel_after,
        ttl_ms,
        symbols,
        quota,
        openings_sent,
        cancels_sent: cancel_waits.len() as u64,
        never_sent_expired,
        cancel_queue_wait: quantiles(&cancel_waits),
        opening_queue_wait: quantiles(&opening_waits),
        cancel_venue_span: quantiles(&cancel_spans),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::SymbolId;

    /// One command the venue answered. `held_ns` is a quota hold inside the
    /// task's own span; `None` for a command that was never transmitted, which
    /// carries a queue wait and no transport stamps.
    fn timing(
        operation: &str,
        queued_ns: u64,
        started_ns: u64,
        completed_ns: u64,
        held_ns: Option<u64>,
    ) -> WalRecord {
        let answered = completed_ns > started_ns;
        WalRecord::VenueTiming {
            command_id: 1,
            operation: operation.into(),
            client_order_id: "eng-1".into(),
            queued_ns,
            task_started_ns: started_ns,
            socket_write_ns: answered.then_some(started_ns),
            ack_ns: answered.then_some(completed_ns),
            rate_wait_ns: held_ns,
            task_completed_ns: completed_ns,
            core_handled_ns: completed_ns,
            core_handled_wall_ns: 0,
        }
    }

    fn refused(reason: &str) -> WalRecord {
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Reject {
                client_order_id: "eng-2".into(),
                code: 0,
                reason: reason.into(),
            },
        }
    }

    #[test]
    fn the_two_operations_are_read_apart_and_expiries_are_counted() {
        let records = vec![
            timing(PLACE, 0, 100, 300, None),
            // Refused at the send boundary after waiting 890 ns.
            timing(PLACE, 10, 900, 900, None),
            timing(CANCEL, 200, 300, 310, None),
            refused("never sent: authority: expired after 890 ms in the venue queue"),
            refused("never sent: authority: epoch 1 superseded by 2"),
            WalRecord::CancelSent {
                symbol: SymbolId(0),
                client_order_id: "eng-1".into(),
                wire_ns: 1,
            },
        ];
        let read = read(&records, 200, 3, 500, 4, false);
        assert_eq!(
            (read.venue_delay_ms, read.cancel_after, read.ttl_ms),
            (200, 3, 500)
        );
        assert_eq!(read.symbols, 4);
        // Two placement rows, one of which the venue never answered.
        assert_eq!(read.opening_queue_wait.count, 2);
        assert_eq!(read.openings_sent, 1);
        assert_eq!(read.never_sent_expired, 1, "supersession is not an expiry");
        assert_eq!(read.cancels_sent, 1);
        assert_eq!(read.cancel_queue_wait.p50_ns, 100);
        assert_eq!(read.cancel_venue_span.p50_ns, 10);
        assert_eq!(read.opening_queue_wait.max_ns, 890);
    }

    #[test]
    fn a_quota_hold_comes_out_of_the_cancel_call_span() {
        let read = read(
            &[timing(CANCEL, 0, 100, 200, Some(40))],
            0,
            3,
            10_000,
            1,
            true,
        );
        assert_eq!(read.cancel_venue_span.p50_ns, 60);
    }
}
