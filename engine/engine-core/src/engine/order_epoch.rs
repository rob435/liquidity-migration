use super::*;

struct CancelRead(std::sync::Arc<std::sync::atomic::AtomicBool>);
impl Drop for CancelRead {
    fn drop(&mut self) {
        self.0.store(true, std::sync::atomic::Ordering::Relaxed);
    }
}

pub(super) const MAX_COUNTER: u64 = (1 << 18) - 1;

fn id_epoch(id: &str) -> Option<i64> {
    let (epoch, counter) = id.strip_prefix("eng-")?.split_once('-')?;
    counter.parse::<u64>().ok()?;
    epoch.parse().ok()
}
fn record_epoch(record: &WalRecord) -> Option<i64> {
    match record {
        WalRecord::OrderIdEpoch { epoch_ms } => Some(*epoch_ms),
        WalRecord::Boot { wall_ts_ms, .. } => Some(*wall_ts_ms),
        WalRecord::OrderSent { request, .. } => id_epoch(&request.client_order_id),
        WalRecord::SegmentBase {
            order_id_epoch_ms,
            wall_ts_ms,
            open_orders,
            ..
        } => order_id_epoch_ms
            .iter()
            .copied()
            .chain(std::iter::once(*wall_ts_ms))
            .chain(
                open_orders
                    .iter()
                    .filter_map(|order| id_epoch(&order.request.client_order_id)),
            )
            .max(),
        _ => None,
    }
}
fn next_epoch(now_ms: i64, prior: Option<i64>) -> Result<i64, EngineError> {
    let now_ms = now_ms - now_ms.rem_euclid(1000);
    let epoch = match prior {
        Some(prior) => now_ms.max(
            (prior - prior.rem_euclid(1000))
                .checked_add(1000)
                .ok_or_else(|| EngineError::State("order ID epoch exhausted".into()))?,
        ),
        None => now_ms,
    };
    if epoch < 0 {
        return Err(EngineError::State("order ID epoch is negative".into()));
    }
    Ok(epoch)
}

pub(super) async fn select_boot_epoch<W: Wal>(
    wal: &mut W,
    replayed: &[WalRecord],
    now_ms: i64,
) -> Result<i64, EngineError> {
    let persisted = replayed
        .iter()
        .filter_map(|record| match record {
            WalRecord::OrderIdEpoch { epoch_ms } => Some(*epoch_ms),
            WalRecord::SegmentBase {
                order_id_epoch_ms, ..
            } => *order_id_epoch_ms,
            _ => None,
        })
        .max();
    let prior = if persisted.is_some() {
        persisted
    } else if let Some(mut reader) = wal.order_epoch_reader()? {
        let cancel = CancelRead(Default::default());
        reader.set_cancel(cancel.0.clone());
        tokio::task::spawn_blocking(move || reader.max_order_epoch_ms())
            .await
            .map_err(|error| {
                EngineError::Boot(format!("order epoch archive read failed: {error}"))
            })??
    } else {
        replayed.iter().filter_map(record_epoch).max()
    };
    next_epoch(now_ms, prior)
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    pub(super) fn mint_id(&mut self) -> Result<String, EngineError> {
        loop {
            if self.next_order_n >= MAX_COUNTER {
                let epoch_ms = next_epoch(clock::wall_ms(), Some(self.order_id_epoch_ms))?;
                self.wal.append(&WalRecord::OrderIdEpoch { epoch_ms })?;
                self.order_id_epoch_ms = epoch_ms;
                self.books.registry.set_boot_epoch(epoch_ms);
                self.next_order_n = 0;
            }
            self.next_order_n += 1;
            let id = format!("{}{}", self.books.registry.prefix(), self.next_order_n);
            if !self.books.orders.contains(&id) && self.books.registry.owner_of(&id).is_none() {
                return Ok(id);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn same_second_restart_with_evicted_orders_reserves_a_distinct_epoch() {
        let (mut engine, _) = crate::tests::callback_test_fixture(Vec::new()).await;
        let first = engine.mint_id().unwrap();
        let epoch = engine.order_id_epoch_ms;
        let base = engine.rotation_base(epoch);
        let next = select_boot_epoch(&mut engine.wal, &[base], epoch - 3_600_000)
            .await
            .unwrap();
        assert_eq!(next, epoch + 1000);
        engine.order_id_epoch_ms = next;
        engine.books.registry.set_boot_epoch(next);
        engine.next_order_n = 0;
        let second = engine.mint_id().unwrap();
        assert_ne!(first, second);
        assert_eq!(id_epoch(&second), Some(next));
    }
    #[tokio::test]
    async fn counter_rollover_journals_the_epoch_before_issuing_a_reversible_id() {
        let (mut engine, records) = crate::tests::callback_test_fixture(Vec::new()).await;
        let prior = engine.order_id_epoch_ms;
        engine.next_order_n = MAX_COUNTER - 1;
        let last = engine.mint_id().unwrap();
        assert!(last.ends_with("-262143"));
        let next = engine.mint_id().unwrap();
        assert!(next.ends_with("-1"));
        assert_eq!(id_epoch(&next), Some(prior + 1000));
        assert!(
            matches!(records.lock().unwrap().last(), Some(WalRecord::OrderIdEpoch { epoch_ms }) if *epoch_ms == prior + 1000)
        );
        engine.next_order_n = MAX_COUNTER;
        let before = engine.order_id_epoch_ms;
        engine.wal.fail_append("order_id_epoch");
        assert!(engine.mint_id().is_err());
        assert_eq!(engine.order_id_epoch_ms, before);
    }
}
