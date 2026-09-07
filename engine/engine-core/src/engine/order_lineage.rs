use super::*;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};

const TERMINAL_CACHE_ROWS: usize = 256;
const TERMINAL_CACHE_BYTES: usize = 4 * 1024 * 1024;
const RETRY_NS: u64 = 1_000_000_000;

pub(super) fn trim_boot_order_cache(orders: &mut LedgerOfOrders) -> Result<(), String> {
    orders.trim_terminal_cache(TERMINAL_CACHE_ROWS, TERMINAL_CACHE_BYTES)?;
    Ok(())
}

pub(super) struct PendingLineage {
    id: String,
    symbol: Option<SymbolId>,
    update: Option<OrderUpdate>,
    running: bool,
    retry_at_ns: u64,
    reported: Option<String>,
    cancel: Arc<AtomicBool>,
}

pub(super) struct OrderLineage {
    pub pending: Option<PendingLineage>,
    missing: Option<String>,
    pub completed: tokio::sync::mpsc::Receiver<Result<Option<inflight::OrderRec>, String>>,
    send: tokio::sync::mpsc::Sender<Result<Option<inflight::OrderRec>, String>>,
}
impl Default for OrderLineage {
    fn default() -> Self {
        let (send, completed) = tokio::sync::mpsc::channel(1);
        Self {
            pending: None,
            missing: None,
            completed,
            send,
        }
    }
}
impl OrderLineage {
    pub fn waiting(&self) -> bool {
        self.pending.is_some()
    }
    pub fn running(&self) -> bool {
        self.pending.as_ref().is_some_and(|row| row.running)
    }
}
impl Drop for OrderLineage {
    fn drop(&mut self) {
        if let Some(pending) = &self.pending {
            pending.cancel.store(true, Ordering::Relaxed);
        }
    }
}

struct CancelLineageRead(Arc<AtomicBool>);
impl Drop for CancelLineageRead {
    fn drop(&mut self) {
        self.0.store(true, Ordering::Relaxed);
    }
}

pub(super) async fn load_order_lineage(
    mut reader: Box<dyn engine_types::wal::OrderLineageReader>,
    id: String,
) -> Result<Option<inflight::OrderRec>, String> {
    let cancel = CancelLineageRead(Arc::new(AtomicBool::new(false)));
    reader.set_cancel(Arc::clone(&cancel.0));
    tokio::task::spawn_blocking(move || read_order_lineage(reader, &id))
        .await
        .map_err(|error| format!("order lineage archive worker failed: {error}"))?
}

fn read_order_lineage(
    mut reader: Box<dyn engine_types::wal::OrderLineageReader>,
    id: &str,
) -> Result<Option<inflight::OrderRec>, String> {
    let mut orders = LedgerOfOrders::default();
    while let Some(record) = reader.next().map_err(|error| error.to_string())? {
        orders.try_apply(&record)?;
    }
    Ok(orders.orders.remove(id))
}

pub(super) fn activate_order_lineage(
    wal: &mut impl Wal,
    orders: &mut LedgerOfOrders,
    row: inflight::OrderRec,
) -> Result<(), EngineError> {
    let restored = WalRecord::OrderLineageRestored {
        order: row.snapshot(clock::wall_ms()),
    };
    orders
        .validate_record_quantities(&restored)
        .map_err(EngineError::State)?;
    wal.append(&restored)?;
    orders.try_apply(&restored).map_err(EngineError::State)
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn require_order_lineage(
        &mut self,
        id: &str,
        symbol: Option<SymbolId>,
        update: Option<OrderUpdate>,
    ) -> Result<bool, EngineError> {
        if self.books.orders.contains(id)
            || !id.starts_with("eng-")
            || !self.wal.supports_order_lineage_archive()
        {
            return Ok(true);
        }
        if self.order_lineage.missing.as_deref() == Some(id) {
            self.order_lineage.missing = None;
            return Ok(true);
        }
        if let Some(pending) = &self.order_lineage.pending {
            if pending.id != id || update.is_some() {
                return Err(EngineError::State(
                    "private execution overtook an unresolved order lineage lookup".into(),
                ));
            }
            return Ok(false);
        }
        self.mark_symbols_busy(symbol);
        self.order_lineage.pending = Some(PendingLineage {
            id: id.into(),
            symbol,
            update,
            running: false,
            retry_at_ns: 0,
            reported: None,
            cancel: Arc::new(AtomicBool::new(false)),
        });
        self.start_order_lineage()?;
        Ok(false)
    }

    fn start_order_lineage(&mut self) -> Result<(), EngineError> {
        let Some(pending) = &mut self.order_lineage.pending else {
            return Ok(());
        };
        if pending.running || clock::now_ns() < pending.retry_at_ns {
            return Ok(());
        }
        let id = pending.id.clone();
        let reader = self.wal.order_lineage_reader(&id);
        let result = match reader {
            Ok(Some(reader)) => reader,
            Ok(None) => {
                self.order_lineage
                    .send
                    .try_send(Err("order lineage archive reader is unavailable".into()))
                    .map_err(|error| EngineError::State(error.to_string()))?;
                pending.running = true;
                return Ok(());
            }
            Err(error) => {
                self.order_lineage
                    .send
                    .try_send(Err(error.to_string()))
                    .map_err(|error| EngineError::State(error.to_string()))?;
                pending.running = true;
                return Ok(());
            }
        };
        let send = self.order_lineage.send.clone();
        let cancel = Arc::clone(&pending.cancel);
        pending.running = true;
        std::thread::Builder::new()
            .name("order-lineage".into())
            .spawn(move || {
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    let mut reader = result;
                    reader.set_cancel(Arc::clone(&cancel));
                    let mut orders = LedgerOfOrders::default();
                    while !cancel.load(Ordering::Relaxed) {
                        let Some(record) = reader.next().map_err(|error| error.to_string())? else {
                            return Ok(orders.orders.remove(&id));
                        };
                        orders.try_apply(&record)?;
                    }
                    Err("order lineage lookup cancelled".into())
                }))
                .unwrap_or_else(|_| Err("order lineage lookup worker panicked".into()));
                let _ = send.blocking_send(result);
            })
            .map_err(|error| EngineError::State(error.to_string()))?;
        Ok(())
    }

    pub(super) async fn on_order_lineage(
        &mut self,
        result: Result<Option<inflight::OrderRec>, String>,
    ) -> Result<(), EngineError> {
        if let Err(error) = result {
            let pending = self.order_lineage.pending.as_mut().ok_or_else(|| {
                EngineError::State("lineage completion has no event owner".into())
            })?;
            pending.running = false;
            pending.retry_at_ns = clock::now_ns().saturating_add(RETRY_NS);
            if pending.reported.as_ref() != Some(&error) {
                self.wal.append(&WalRecord::Note {
                    source: "order-lineage".into(),
                    text: format!("{}: {error}; private execution remains pending", pending.id),
                })?;
                pending.reported = Some(error);
            }
            self.recovery.history_requested = true;
            return Ok(());
        }
        let pending =
            self.order_lineage.pending.take().ok_or_else(|| {
                EngineError::State("lineage completion has no event owner".into())
            })?;
        if let Some(order) = result.expect("error returned above") {
            activate_order_lineage(&mut self.wal, &mut self.books.orders, order)?;
        } else if pending.update.is_none() {
            self.order_lineage.missing = Some(pending.id);
        }
        if let Some(update) = pending.update {
            self.take_update_ready(update).await?;
        }
        self.release_symbols(pending.symbol);
        Ok(())
    }

    pub(super) async fn service_order_lineage(&mut self) -> Result<(), EngineError> {
        if let Ok(result) = self.order_lineage.completed.try_recv() {
            self.on_order_lineage(result).await?;
        }
        self.start_order_lineage()
    }

    pub(super) async fn settle_order_lineage(&mut self) -> Result<(), EngineError> {
        self.start_order_lineage()?;
        if !self.order_lineage.running() {
            return Err(EngineError::State("order lineage remains unresolved during graceful stop; execution history must recover it".into()));
        }
        let result =
            tokio::time::timeout(MUTATION_DRAIN_TIMEOUT, self.order_lineage.completed.recv())
                .await
                .map_err(|_| EngineError::TimedOut("order lineage during graceful stop".into()))?
                .ok_or_else(|| EngineError::State("order lineage reader stopped".into()))?;
        self.on_order_lineage(result).await
    }

    pub(super) fn trim_order_lineage_cache(&mut self) -> Result<(), EngineError> {
        if self.wal.supports_order_lineage_archive() {
            let removed = self
                .books
                .orders
                .trim_terminal_cache(TERMINAL_CACHE_ROWS, TERMINAL_CACHE_BYTES)
                .map_err(EngineError::State)?;
            for id in removed {
                self.books.registry.forget(&id);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::numeric::{AssetAmount, AssetId, Exact, ExactNumber, ExecutionAmounts};
    use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};

    struct GateWal {
        inner: engine_wal::WalWriter,
        gate: Arc<AtomicBool>,
        fail: Arc<AtomicBool>,
    }
    struct GateReader {
        inner: Box<dyn engine_types::wal::OrderLineageReader>,
        gate: Arc<AtomicBool>,
    }
    impl engine_types::wal::OrderLineageReader for GateReader {
        fn set_cancel(&mut self, cancel: Arc<AtomicBool>) {
            self.inner.set_cancel(cancel);
        }
        fn next(&mut self) -> Result<Option<WalRecord>, WalError> {
            while !self.gate.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(1));
            }
            self.inner.next()
        }
    }
    impl Wal for GateWal {
        fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
            self.inner.append(record)
        }
        fn barrier(&mut self) -> Result<(), WalError> {
            self.inner.barrier()
        }
        fn barrier_begin(&mut self) -> Result<engine_types::wal::PendingBarrier, WalError> {
            self.inner.barrier_begin()
        }
        fn flush(&mut self) -> Result<(), WalError> {
            self.inner.flush()
        }
        fn rotate(&mut self, base: &WalRecord) -> Result<bool, WalError> {
            self.inner.rotate(base)
        }
        fn supports_order_lineage_archive(&self) -> bool {
            true
        }
        fn order_epoch_reader(
            &mut self,
        ) -> Result<Option<Box<dyn engine_types::wal::OrderEpochReader>>, WalError> {
            self.inner.order_epoch_reader()
        }
        fn order_lineage_reader(
            &mut self,
            id: &str,
        ) -> Result<Option<Box<dyn engine_types::wal::OrderLineageReader>>, WalError> {
            if self.fail.swap(false, Ordering::Relaxed) {
                return Err(WalError::Io(std::io::Error::other("archive read fault")));
            }
            Ok(self.inner.order_lineage_reader(id)?.map(|inner| {
                Box::new(GateReader {
                    inner,
                    gate: Arc::clone(&self.gate),
                }) as Box<dyn engine_types::wal::OrderLineageReader>
            }))
        }
    }

    #[tokio::test(start_paused = true)]
    async fn archived_rejection_loads_off_core_retains_failure_and_applies_late_fill_once() {
        let _io = crate::test_io::IoProgress::new();
        let prior = crate::tests::shared_sleeves::fragmented_engine().await;
        let mut base = prior.rotation_base(clock::wall_ms());
        let id = "eng-archive-late-1";
        let mut request = OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.4,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: true,
            close_position: false,
            exact_terms: None,
            sleeve_effect: Some(engine_types::orders::SleeveOrderEffect::Reduce),
        };
        ExactOrderTerms {
            quantity: Exact::parse_decimal("0.4").unwrap(),
            limit_price: None,
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: OrderInputPolicy::CanonicalPortfolio,
        }
        .apply_projection(&mut request)
        .unwrap();
        let sent = WalRecord::OrderSent {
            request,
            dispatch: None,
            wire_ns: clock::now_ns(),
            arrival_mid: 100.0,
        };
        let rejected = WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Reject {
                client_order_id: id.into(),
                code: 1,
                reason: "refused before delayed fill".into(),
            },
        };
        let directory = crate::testpath::temp_path("archived-terminal-lineage");
        std::fs::create_dir_all(directory.path()).unwrap();
        let path = directory.path().join("engine.wal");
        let (mut disk, _) = engine_wal::WalWriter::open(&path).unwrap();
        disk.append(&base).unwrap();
        disk.append(&sent).unwrap();
        disk.append(&rejected).unwrap();
        if let WalRecord::SegmentBase { open_orders, .. } = &mut base {
            open_orders.clear();
        }
        disk.rotate(&base).unwrap();
        drop(disk);
        let (disk, replayed) = engine_wal::open_current(&path).unwrap();
        let records = replayed.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        let gate = Arc::new(AtomicBool::new(false));
        let fail = Arc::new(AtomicBool::new(true));
        let wal = GateWal {
            inner: disk,
            gate: Arc::clone(&gate),
            fail,
        };
        let mut engine = crate::tests::shared_sleeves::restart_portfolio_with_wal(
            wal,
            &records,
            crate::tests::shared_sleeves::physical_long(1.0),
        )
        .await;
        let before = engine.books.attribution.snapshot();
        let fill = OrderUpdate::Fill {
            client_order_id: id.into(),
            exec_id: "late-archived-fill".into(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 0.1,
            px: 99.0,
            fee: Some(0.001),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: clock::wall_ms(),
            recv_ns: clock::now_ns(),
            allocation: None,
            amounts: Some(Box::new(ExecutionAmounts {
                quantity: ExactNumber::venue_decimal("0.1").unwrap(),
                price: ExactNumber::venue_decimal("99").unwrap(),
                fee: Some(AssetAmount {
                    asset: AssetId::Named("USDT".into()),
                    amount: ExactNumber::venue_decimal("0.001").unwrap(),
                }),
                settlement_asset: AssetId::Named("USDT".into()),
            })),
        };
        engine.take_update(fill.clone()).await.unwrap();
        assert!(engine.order_lineage.waiting());
        assert!(engine.busy_symbols.contains_key(&SymbolId(0)));
        assert_eq!(engine.books.attribution.snapshot(), before);
        let result = engine.order_lineage.completed.recv().await.unwrap();
        engine.on_order_lineage(result).await.unwrap();
        assert!(
            engine.order_lineage.waiting(),
            "lookup failure dropped the private execution owner"
        );
        assert_eq!(engine.books.attribution.snapshot(), before);
        engine.order_lineage.pending.as_mut().unwrap().retry_at_ns = 0;
        engine.service_order_lineage().await.unwrap();
        tokio::time::timeout(Duration::from_millis(100), engine.on_tick())
            .await
            .unwrap()
            .unwrap();
        assert!(
            engine.order_lineage.waiting(),
            "gated reader blocked the core turn"
        );
        gate.store(true, Ordering::Relaxed);
        let result = tokio::time::timeout(
            Duration::from_secs(2),
            engine.order_lineage.completed.recv(),
        )
        .await
        .unwrap()
        .unwrap();
        engine.on_order_lineage(result).await.unwrap();
        assert!(!engine.order_lineage.waiting());
        assert!(!engine.busy_symbols.contains_key(&SymbolId(0)));
        assert_eq!(
            engine
                .books
                .attribution
                .signed_exact(StrategyId(0), SymbolId(0)),
            Exact::parse_decimal("0.3").unwrap()
        );
        assert_eq!(
            engine
                .books
                .attribution
                .signed_exact(StrategyId(1), SymbolId(0)),
            Exact::parse_decimal("0.6").unwrap()
        );
        let once = engine.books.attribution.snapshot();
        engine.wal.flush().unwrap();
        let current = engine_wal::replay_current(&path)
            .unwrap()
            .0
            .into_iter()
            .map(|(_, row)| row)
            .collect::<Vec<_>>();
        assert_eq!(
            crate::attribution::Attribution::try_from_records(&current)
                .unwrap()
                .snapshot(),
            once,
            "cold activation did not make the current segment replay its owned fill"
        );
        assert_eq!(
            LedgerOfOrders::try_from_records(&current).unwrap().orders[id]
                .remaining_exact()
                .unwrap(),
            Exact::parse_decimal("0.3").unwrap()
        );
        engine.books.orders.trim_terminal_cache(0, 0).unwrap();
        engine.take_update(fill).await.unwrap();
        assert!(
            !engine.order_lineage.waiting(),
            "already committed execution required a cold lookup"
        );
        assert_eq!(engine.books.attribution.snapshot(), once);
        let rotated = engine.rotation_base(clock::wall_ms());
        engine.wal.rotate(&rotated).unwrap();
        drop(engine);
        let (disk, replayed) = engine_wal::open_current(&path).unwrap();
        drop(disk);
        let records = replayed.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        assert_eq!(
            crate::attribution::Attribution::try_from_records(&records)
                .unwrap()
                .snapshot(),
            once
        );
    }

    #[test]
    fn terminal_payload_cache_is_bounded_without_evicting_a_live_reduction() {
        let mut orders = LedgerOfOrders::default();
        for index in 0..400 {
            let id = format!("eng-cache-{index}");
            let request = OrderRequest {
                client_order_id: id.clone(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Sell,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: true,
                close_position: false,
                exact_terms: None,
                sleeve_effect: Some(engine_types::orders::SleeveOrderEffect::Reduce),
            };
            orders
                .try_apply(&WalRecord::OrderSent {
                    request,
                    dispatch: None,
                    wire_ns: index,
                    arrival_mid: 100.0,
                })
                .unwrap();
            if index != 0 {
                orders
                    .try_apply_update(&OrderUpdate::Reject {
                        client_order_id: id,
                        code: 1,
                        reason: "x".repeat(1024),
                    })
                    .unwrap();
            }
        }
        let removed = orders.trim_terminal_cache(256, 16 * 1024).unwrap();
        assert!(removed.len() > 256);
        assert!(orders.orders["eng-cache-0"].in_flight());
        let bytes: usize = orders
            .orders
            .values()
            .filter(|row| !row.in_flight())
            .map(|row| serde_json::to_vec(&row.snapshot(0)).unwrap().len())
            .sum();
        assert!(bytes <= 16 * 1024);
        assert!(
            orders
                .orders
                .values()
                .filter(|row| !row.in_flight())
                .count()
                <= 256
        );
        let scanned = orders.terminal_cache_scanned_rows();
        for _ in 0..10_000 {
            assert!(orders
                .trim_terminal_cache(256, 16 * 1024)
                .unwrap()
                .is_empty());
        }
        assert_eq!(
            orders.terminal_cache_scanned_rows(),
            scanned,
            "unchanged housekeeping serialized terminal payloads again"
        );
    }
}

#[cfg(test)]
mod boot_cancellation_tests {
    use super::*;

    struct BlockingReader {
        cancel: Arc<AtomicBool>,
        started: Option<tokio::sync::oneshot::Sender<()>>,
        finished: Option<tokio::sync::oneshot::Sender<bool>>,
    }
    impl engine_types::wal::OrderLineageReader for BlockingReader {
        fn set_cancel(&mut self, cancel: Arc<AtomicBool>) {
            self.cancel = cancel;
        }
        fn next(&mut self) -> Result<Option<WalRecord>, WalError> {
            if let Some(started) = self.started.take() {
                let _ = started.send(());
            }
            let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
            while !self.cancel.load(Ordering::Relaxed) && std::time::Instant::now() < deadline {
                std::thread::sleep(std::time::Duration::from_millis(1));
            }
            Ok(None)
        }
    }
    impl Drop for BlockingReader {
        fn drop(&mut self) {
            if let Some(finished) = self.finished.take() {
                let _ = finished.send(self.cancel.load(Ordering::Relaxed));
            }
        }
    }

    #[tokio::test(start_paused = true)]
    async fn dropping_boot_lineage_recovery_cancels_and_releases_its_active_reader() {
        let _io = crate::test_io::IoProgress::new();
        let (start_send, started) = tokio::sync::oneshot::channel();
        let (finish_send, finished) = tokio::sync::oneshot::channel();
        let reader = BlockingReader {
            cancel: Arc::new(AtomicBool::new(false)),
            started: Some(start_send),
            finished: Some(finish_send),
        };
        let task = tokio::spawn(load_order_lineage(Box::new(reader), "eng-missing".into()));
        tokio::time::timeout(std::time::Duration::from_secs(1), started)
            .await
            .unwrap()
            .unwrap();
        task.abort();
        assert!(task.await.unwrap_err().is_cancelled());
        assert!(
            tokio::time::timeout(std::time::Duration::from_millis(500), finished)
                .await
                .expect("boot cancellation left the archive reader running")
                .unwrap()
        );
    }
}
