use std::collections::{BTreeMap, BTreeSet};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Instant;

use engine_types::wal::PendingBarrier;
use engine_types::{Wal, WalError, WalRecord};

use crate::ledger::Quantiles;

#[derive(Clone, Debug)]
pub struct BarrierTiming {
    pub records: String,
    pub asynchronous: bool,
    pub request: Quantiles,
    pub confirmation: Quantiles,
    pub failures: u64,
}

#[derive(Default)]
struct Samples {
    request: Vec<u64>,
    confirmation: Vec<u64>,
    failures: u64,
}

#[derive(Default)]
struct Measurements {
    barriers: BTreeMap<(String, bool), Samples>,
    completed_submits: u64,
    latency_records: u64,
}

#[derive(Clone, Default)]
pub(super) struct WalMeasurements(Arc<Mutex<Measurements>>);

impl WalMeasurements {
    pub fn snapshot(&self) -> (u64, u64, Vec<BarrierTiming>) {
        let measurements = self.0.lock().unwrap();
        (
            measurements.completed_submits,
            measurements.latency_records,
            measurements
                .barriers
                .iter()
                .map(|((records, asynchronous), samples)| BarrierTiming {
                    records: records.clone(),
                    asynchronous: *asynchronous,
                    request: quantiles(&samples.request),
                    confirmation: quantiles(&samples.confirmation),
                    failures: samples.failures,
                })
                .collect(),
        )
    }

    fn record(&self, key: (String, bool), request_ns: u64, confirmation_ns: u64, failed: bool) {
        let mut measurements = self.0.lock().unwrap();
        let samples = measurements.barriers.entry(key).or_default();
        samples.request.push(request_ns);
        samples.confirmation.push(confirmation_ns);
        samples.failures += u64::from(failed);
    }
}

fn elapsed_ns(start: Instant) -> u64 {
    start.elapsed().as_nanos().min(u128::from(u64::MAX)) as u64
}

fn quantiles(samples: &[u64]) -> Quantiles {
    let mut sorted = samples.to_vec();
    sorted.sort_unstable();
    let at = |quantile: f64| {
        if sorted.is_empty() {
            0
        } else {
            sorted[((sorted.len() as f64 * quantile).ceil() as usize).saturating_sub(1)]
        }
    };
    Quantiles {
        count: sorted.len() as u64,
        p50_ns: at(0.5),
        p90_ns: at(0.9),
        p99_ns: at(0.99),
        p999_ns: at(0.999),
        max_ns: sorted.last().copied().unwrap_or(0),
    }
}

struct Measurement {
    barrier: PendingBarrier,
    done: mpsc::Sender<Result<(), WalError>>,
    key: (String, bool),
    started: Instant,
    request_ns: u64,
}

/// The relay observes the real WAL confirmation before forwarding its result.
/// Its thread wake and channel hop are measurement overhead, included in results.
pub(super) struct TimedWal<W> {
    inner: W,
    measurements: WalMeasurements,
    pending_records: BTreeSet<&'static str>,
    relay: mpsc::Sender<Measurement>,
}

impl<W: Wal> TimedWal<W> {
    pub fn new(inner: W) -> Result<(Self, WalMeasurements), WalError> {
        let measurements = WalMeasurements::default();
        let observed = measurements.clone();
        let (relay, requests) = mpsc::channel::<Measurement>();
        std::thread::Builder::new()
            .name("bench-wal-observer".into())
            .spawn(move || {
                while let Ok(request) = requests.recv() {
                    let result = request.barrier.wait();
                    observed.record(
                        request.key,
                        request.request_ns,
                        elapsed_ns(request.started),
                        result.is_err(),
                    );
                    let _ = request.done.send(result);
                }
            })?;
        Ok((
            Self {
                inner,
                measurements: measurements.clone(),
                pending_records: BTreeSet::new(),
                relay,
            },
            measurements,
        ))
    }

    fn barrier_key(&mut self, asynchronous: bool) -> (String, bool) {
        // Volatile callbacks append all three records in one commit barrier.
        if self.pending_records.contains("callback commit") {
            self.pending_records.remove("callback input");
            self.pending_records.remove("callback preparation");
        }
        let records = if self.pending_records.is_empty() {
            "other".into()
        } else {
            std::mem::take(&mut self.pending_records)
                .into_iter()
                .collect::<Vec<_>>()
                .join(" + ")
        };
        (records, asynchronous)
    }
}

impl<W: Wal> Wal for TimedWal<W> {
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        let sequence = self.inner.append(record)?;
        let kind = match record {
            WalRecord::Retained(engine_types::wal::RetainedWalRecord::StrategyCallbackQueued {
                ..
            }) => Some("callback input"),
            WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyCallbackPrepared { .. },
            ) => Some("callback preparation"),
            WalRecord::Retained(
                engine_types::wal::RetainedWalRecord::StrategyProcessTransitionQueued { .. },
            ) => Some("callback commit"),
            WalRecord::OrderSent {
                dispatch: Some(_), ..
            }
            | WalRecord::OrderDispatchQueued { .. } => Some("dispatch queued"),
            WalRecord::OrderDispatchAttempted { .. } => Some("attempted send"),
            WalRecord::StrategyTransitionQueued { .. }
            | WalRecord::StrategyCheckpoint { .. }
            | WalRecord::StrategyGlobalCheckpoint { .. } => Some("strategy state"),
            WalRecord::VenueTiming { operation, .. } if operation == "place" => {
                self.measurements.0.lock().unwrap().completed_submits += 1;
                None
            }
            WalRecord::LatencyLedger { .. } => {
                self.measurements.0.lock().unwrap().latency_records += 1;
                None
            }
            _ => None,
        };
        if let Some(kind) = kind {
            self.pending_records.insert(kind);
        }
        Ok(sequence)
    }

    fn barrier(&mut self) -> Result<(), WalError> {
        let key = self.barrier_key(false);
        let started = Instant::now();
        let result = self.inner.barrier();
        let ns = elapsed_ns(started);
        self.measurements.record(key, ns, ns, result.is_err());
        result
    }

    fn barrier_begin(&mut self) -> Result<PendingBarrier, WalError> {
        let key = self.barrier_key(true);
        let started = Instant::now();
        let result = self.inner.barrier_begin();
        let request_ns = elapsed_ns(started);
        let barrier = match result {
            Ok(barrier) => barrier,
            Err(error) => {
                self.measurements.record(key, request_ns, request_ns, true);
                return Err(error);
            }
        };
        if !barrier.outstanding() {
            self.measurements.record(key, request_ns, request_ns, false);
            return Ok(barrier);
        }
        let (done, result) = mpsc::channel();
        self.relay
            .send(Measurement {
                barrier,
                done,
                key,
                started,
                request_ns,
            })
            .map_err(|_| std::io::Error::other("benchmark WAL observer stopped"))?;
        Ok(PendingBarrier::running(result))
    }

    fn flush(&mut self) -> Result<(), WalError> {
        self.inner.flush()
    }

    fn callback_reader(
        &mut self,
    ) -> Result<Option<Box<dyn engine_types::strategy_process::CallbackWalReader>>, WalError> {
        self.inner.callback_reader()
    }

    fn order_lineage_reader(
        &mut self,
        client_order_id: &str,
    ) -> Result<Option<Box<dyn engine_types::wal::OrderLineageReader>>, WalError> {
        self.inner.order_lineage_reader(client_order_id)
    }

    fn supports_order_lineage_archive(&self) -> bool {
        self.inner.supports_order_lineage_archive()
    }

    fn order_epoch_reader(
        &mut self,
    ) -> Result<Option<Box<dyn engine_types::wal::OrderEpochReader>>, WalError> {
        self.inner.order_epoch_reader()
    }

    fn segment_size(&self) -> u64 {
        self.inner.segment_size()
    }

    fn rotate(&mut self, base: &WalRecord) -> Result<bool, WalError> {
        self.inner.rotate(base)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct HeldWal(Option<PendingBarrier>);
    impl Wal for HeldWal {
        fn append(&mut self, _: &WalRecord) -> Result<u64, WalError> {
            Ok(1)
        }
        fn barrier(&mut self) -> Result<(), WalError> {
            self.0.take().unwrap().wait()
        }
        fn barrier_begin(&mut self) -> Result<PendingBarrier, WalError> {
            Ok(self.0.take().unwrap())
        }
        fn flush(&mut self) -> Result<(), WalError> {
            Ok(())
        }
    }

    #[test]
    fn observer_preserves_pending_confirmation_and_failure() {
        let (confirm, pending) = mpsc::channel();
        let (mut wal, measurements) =
            TimedWal::new(HeldWal(Some(PendingBarrier::running(pending)))).unwrap();
        wal.append(&WalRecord::OrderDispatchAttempted {
            client_order_id: "test".into(),
        })
        .unwrap();
        let barrier = wal.barrier_begin().unwrap();
        assert!(barrier.outstanding());
        assert!(measurements.snapshot().2.is_empty());
        confirm
            .send(Err(std::io::Error::other("disk failed").into()))
            .unwrap();
        assert!(barrier
            .wait()
            .unwrap_err()
            .to_string()
            .contains("disk failed"));
        let rows = measurements.snapshot().2;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].records, "attempted send");
        assert!(rows[0].asynchronous);
        assert_eq!(rows[0].failures, 1);
        assert_eq!(rows[0].confirmation.count, 1);
        assert!(rows[0].confirmation.max_ns >= rows[0].request.max_ns);
    }

    #[test]
    fn completed_submits_survive_live_histogram_window_rollover() {
        let (mut wal, measurements) = TimedWal::new(HeldWal(None)).unwrap();
        let mut ledger = crate::ledger::LatencyLedger::new(0);
        for now in [1, crate::ledger::WINDOW_NS + 1] {
            wal.append(&WalRecord::VenueTiming {
                command_id: now,
                operation: "place".into(),
                client_order_id: now.to_string(),
                queued_ns: now,
                task_started_ns: now,
                socket_write_ns: None,
                ack_ns: None,
                rate_wait_ns: None,
                task_completed_ns: now,
                core_handled_ns: now,
                core_handled_wall_ns: 0,
            })
            .unwrap();
            ledger.record(crate::ledger::Segment::Wire, 10);
            if now == 1 {
                wal.append(&ledger.record_for_wal(crate::ledger::WINDOW_NS))
                    .unwrap();
                ledger.reset(crate::ledger::WINDOW_NS);
            }
        }
        assert_eq!(ledger.quantiles(crate::ledger::Segment::Wire).count, 1);
        assert_eq!(measurements.snapshot().0, 2);
        assert_eq!(measurements.snapshot().1, 1);
    }
}
