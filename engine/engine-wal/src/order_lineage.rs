use std::fs::File;
use std::os::unix::fs::FileExt;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc,
};

use engine_types::strategy_process::CallbackWalCursor;
use engine_types::wal::{OpenOrderState, OrderLineageReader};
use engine_types::{WalError, WalRecord};
use serde::{
    de::{DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor},
    Deserialize,
};

pub(crate) const MAX_ROW_BYTES: u64 = 8 * 1024 * 1024;

struct Bounded<T> {
    budget: Arc<AtomicU64>,
    marker: std::marker::PhantomData<T>,
}
impl<T> Bounded<T> {
    fn new(budget: &Arc<AtomicU64>) -> Self {
        Self {
            budget: budget.clone(),
            marker: std::marker::PhantomData,
        }
    }
}
impl<'de, T: Deserialize<'de>> DeserializeSeed<'de> for Bounded<T> {
    type Value = T;
    fn deserialize<D: serde::Deserializer<'de>>(self, deserializer: D) -> Result<T, D::Error> {
        let previous = self.budget.swap(MAX_ROW_BYTES, Ordering::Relaxed);
        let result = T::deserialize(deserializer);
        let used = MAX_ROW_BYTES - self.budget.load(Ordering::Relaxed);
        self.budget
            .store(previous.saturating_sub(used), Ordering::Relaxed);
        result
    }
}

struct Identity(Arc<AtomicU64>);
impl<'de> DeserializeSeed<'de> for Identity {
    type Value = Option<String>;
    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        struct Fields(Arc<AtomicU64>);
        impl<'de> Visitor<'de> for Fields {
            type Value = Option<String>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("order identity")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut id = None;
                while let Some(key) = map.next_key::<String>()? {
                    match key.as_str() {
                        "client_order_id" => {
                            id = Some(map.next_value_seed(Bounded::<String>::new(&self.0))?)
                        }
                        "request" | "Ack" | "Fill" | "Cancelled" | "Reject" | "Amended"
                        | "FastFill" => {
                            if let Some(found) = map.next_value_seed(Identity(self.0.clone()))? {
                                id = Some(found);
                            }
                        }
                        _ => {
                            map.next_value::<IgnoredAny>()?;
                        }
                    }
                }
                Ok(id)
            }
        }
        deserializer.deserialize_map(Fields(self.0))
    }
}

struct Orders<'a> {
    wanted: &'a str,
    budget: Arc<AtomicU64>,
    select: Option<usize>,
}
struct Selection {
    kind: String,
    matches: bool,
    ordinal: Option<usize>,
    order: Option<OpenOrderState>,
}
impl<'de> Visitor<'de> for Orders<'_> {
    type Value = (Option<usize>, Option<OpenOrderState>);
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("order restatement")
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let mut found = None;
        let mut selected = None;
        let mut index = 0;
        loop {
            if self.select == Some(index) {
                selected = seq.next_element_seed(Bounded::<OpenOrderState>::new(&self.budget))?;
                if selected.is_none() {
                    break;
                }
                found = Some(index);
            } else {
                let Some(id) = seq.next_element_seed(Identity(self.budget.clone()))? else {
                    break;
                };
                if id.as_deref() == Some(self.wanted) {
                    if found.is_some() {
                        return Err(serde::de::Error::custom(
                            "duplicate order identity in restatement",
                        ));
                    }
                    found = Some(index);
                }
            }
            index += 1;
        }
        Ok((found, selected))
    }
}
impl<'de> DeserializeSeed<'de> for Orders<'_> {
    type Value = (Option<usize>, Option<OpenOrderState>);
    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_seq(self)
    }
}
struct Select<'a> {
    wanted: &'a str,
    budget: Arc<AtomicU64>,
    ordinal: Option<usize>,
}
impl<'de> Visitor<'de> for Select<'_> {
    type Value = Selection;
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("order lineage frame")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut selected = Selection {
            kind: String::new(),
            matches: false,
            ordinal: None,
            order: None,
        };
        while let Some(key) = map.next_key::<String>()? {
            match key.as_str() {
                "kind" => {
                    selected.kind = map.next_value_seed(Bounded::<String>::new(&self.budget))?
                }
                "client_order_id" => {
                    selected.matches |=
                        map.next_value_seed(Bounded::<String>::new(&self.budget))? == self.wanted
                }
                "request" | "update" | "order" => {
                    selected.matches |= map
                        .next_value_seed(Identity(self.budget.clone()))?
                        .as_deref()
                        == Some(self.wanted)
                }
                "open_orders" => {
                    let (ordinal, order) = map.next_value_seed(Orders {
                        wanted: self.wanted,
                        budget: self.budget.clone(),
                        select: self.ordinal,
                    })?;
                    selected.ordinal = ordinal;
                    selected.order = order;
                }
                _ => {
                    map.next_value::<IgnoredAny>()?;
                }
            }
        }
        Ok(selected)
    }
}
impl<'de> DeserializeSeed<'de> for Select<'_> {
    type Value = Selection;
    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_map(self)
    }
}

pub(crate) struct Reader {
    source: crate::callback_reader::Reader,
    pinned: File,
    last_segment: u64,
    last_offset: u64,
    cursor: CallbackWalCursor,
    wanted: String,
    cancel: Option<Arc<std::sync::atomic::AtomicBool>>,
}
impl Reader {
    fn check_cancel(&self) -> Result<(), WalError> {
        if self
            .cancel
            .as_ref()
            .is_some_and(|cancel| cancel.load(Ordering::Relaxed))
        {
            return Err(WalError::Io(std::io::Error::new(
                std::io::ErrorKind::Interrupted,
                "order archive read cancelled",
            )));
        }
        Ok(())
    }
    fn advance_segment(&mut self) {
        self.cursor.segment += 1;
        self.cursor.sequence = 1;
        self.cursor.offset = crate::HEADER_LEN;
    }

    fn next_frame(&mut self) -> Result<Option<(CallbackWalCursor, u64, u32)>, WalError> {
        loop {
            self.check_cancel()?;
            if self.cursor.segment > self.last_segment {
                return Ok(None);
            }
            if self.cursor.segment == self.last_segment {
                if self.source.segment != self.last_segment {
                    self.source.file = self.pinned.try_clone()?;
                    self.source.segment = self.last_segment;
                }
            } else if let Err(error) = self.source.select(self.cursor.segment) {
                let unfinished_header = matches!(&error, WalError::Corrupt { offset: 0, .. })
                    || matches!(&error, WalError::Io(error) if error.kind() == std::io::ErrorKind::UnexpectedEof);
                if self.cursor.segment > 1 && unfinished_header {
                    self.advance_segment();
                    continue;
                }
                return Err(WalError::Io(std::io::Error::other(format!(
                    "retained order lineage source segment {} unavailable: {error}",
                    self.cursor.segment
                ))));
            }
            let end = if self.cursor.segment == self.last_segment {
                self.last_offset
            } else {
                self.source.file.metadata()?.len()
            };
            let first = self.cursor.segment > 1 && self.cursor.sequence == 1;
            if self.cursor.offset == end {
                self.advance_segment();
                continue;
            }
            if first {
                if end.saturating_sub(crate::HEADER_LEN) < crate::FRAME_HEADER_LEN as u64 {
                    self.advance_segment();
                    continue;
                }
                let mut size = [0; 4];
                self.source
                    .file
                    .read_exact_at(&mut size, crate::HEADER_LEN)?;
                if u32::from_le_bytes(size) as u64
                    > end - crate::HEADER_LEN - crate::FRAME_HEADER_LEN as u64
                {
                    self.advance_segment();
                    continue;
                }
            }
            let cursor = self.cursor;
            let (length, crc) = self.source.header(cursor)?;
            let next = cursor
                .offset
                .checked_add(crate::FRAME_HEADER_LEN as u64)
                .and_then(|offset| offset.checked_add(length))
                .filter(|offset| *offset <= end)
                .ok_or_else(|| WalError::Corrupt {
                    offset: cursor.offset,
                    detail: "order lineage frame crosses read frontier".into(),
                })?;
            if first {
                let budget = Arc::new(AtomicU64::new(u64::MAX));
                let selected = self.source.decode(
                    cursor,
                    length,
                    crc,
                    budget.clone(),
                    Select {
                        wanted: "",
                        budget,
                        ordinal: None,
                    },
                )?;
                if !matches!(
                    selected.kind.as_str(),
                    "segment_base"
                        | "segment_base_v2"
                        | "segment_base_v3"
                        | "segment_base_v4"
                        | "segment_base_v5"
                        | "segment_base_v6"
                ) {
                    self.source.record(cursor, length)?;
                    self.advance_segment();
                    continue;
                }
            }
            self.cursor.offset = next;
            self.cursor.sequence += 1;
            return Ok(Some((cursor, length, crc)));
        }
    }

    pub fn new(
        pinned: File,
        segment: u64,
        family: PathBuf,
        wanted: String,
    ) -> Result<Self, WalError> {
        if wanted.len() as u64 > MAX_ROW_BYTES {
            return Err(WalError::Io(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "order identity exceeds byte limit",
            )));
        }
        let last_offset = pinned.metadata()?.len();
        Ok(Self {
            source: crate::callback_reader::Reader {
                file: pinned.try_clone()?,
                segment,
                family,
                cancel: None,
            },
            pinned,
            last_segment: segment,
            last_offset,
            cursor: CallbackWalCursor {
                segment: 1,
                sequence: 1,
                offset: crate::HEADER_LEN,
            },
            wanted,
            cancel: None,
        })
    }
}
impl OrderLineageReader for Reader {
    fn set_cancel(&mut self, cancel: Arc<std::sync::atomic::AtomicBool>) {
        self.cancel = Some(cancel.clone());
        self.source.cancel = Some(cancel);
    }
    fn next(&mut self) -> Result<Option<WalRecord>, WalError> {
        while let Some((cursor, length, crc)) = self.next_frame()? {
            let budget = Arc::new(AtomicU64::new(u64::MAX));
            let selected = self.source.decode(
                cursor,
                length,
                crc,
                budget.clone(),
                Select {
                    wanted: &self.wanted,
                    budget: budget.clone(),
                    ordinal: None,
                },
            )?;
            if selected.kind.starts_with("segment_base") {
                if let Some(ordinal) = selected.ordinal {
                    let budget = Arc::new(AtomicU64::new(u64::MAX));
                    let selected = self.source.decode(
                        cursor,
                        length,
                        crc,
                        budget.clone(),
                        Select {
                            wanted: &self.wanted,
                            budget,
                            ordinal: Some(ordinal),
                        },
                    )?;
                    let order = selected.order.ok_or_else(|| WalError::Corrupt {
                        offset: cursor.offset,
                        detail: "order lineage restatement lost its row".into(),
                    })?;
                    return serde_json::from_value(serde_json::json!({
                        "kind":"segment_base", "wall_ts_ms":0, "strategies":[], "symbols":[], "may_open":false,
                        "control_anchors":[], "attribution":[], "logged_exposure":[], "intended_stops":[], "open_orders":[order]
                    })).map(Some).map_err(crate::json_error);
                }
            } else if selected.matches
                && matches!(
                    selected.kind.as_str(),
                    "order_sent"
                        | "order_sent_v2"
                        | "order_update"
                        | "order_update_v2"
                        | "recovered_fill"
                        | "recovered_fill_v2"
                        | "amend_sent"
                        | "amend_sent_v2"
                        | "amend_resolved"
                        | "amend_resolved_v2"
                        | "order_lineage_restored"
                )
            {
                return self.source.record(cursor, length).map(Some);
            }
        }
        Ok(None)
    }
}

struct EpochSeed {
    budget: Arc<AtomicU64>,
}
impl<'de> Visitor<'de> for EpochSeed {
    type Value = Option<i64>;
    fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
        f.write_str("order epoch source")
    }
    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut kind = String::new();
        let mut from_ids = None;
        let mut wall_ms = None;
        let mut epoch = None;
        while let Some(key) = map.next_key::<String>()? {
            match key.as_str() {
                "kind" => kind = map.next_value_seed(Bounded::<String>::new(&self.budget))?,
                "wall_ts_ms" => wall_ms = Some(map.next_value::<i64>()?),
                "epoch_ms" | "order_id_epoch_ms" => {
                    epoch = epoch.max(map.next_value::<Option<i64>>()?)
                }
                "client_order_id" => {
                    let id: String = map.next_value_seed(Bounded::<String>::new(&self.budget))?;
                    if let Some((stamp, counter)) = id
                        .strip_prefix("eng-")
                        .and_then(|rest| rest.split_once('-'))
                    {
                        if counter.parse::<u64>().is_ok() {
                            from_ids = from_ids.max(stamp.parse::<i64>().ok());
                        }
                    }
                }
                "request" | "order" | "open_orders" => {
                    from_ids = from_ids.max(map.next_value_seed(EpochSeed {
                        budget: self.budget.clone(),
                    })?)
                }
                _ => {
                    map.next_value::<IgnoredAny>()?;
                }
            }
        }
        if kind == "boot" || kind.starts_with("segment_base") {
            from_ids = from_ids.max(wall_ms);
        }
        Ok(from_ids.max(epoch))
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let mut maximum = None;
        while let Some(epoch) = seq.next_element_seed(EpochSeed {
            budget: self.budget.clone(),
        })? {
            maximum = maximum.max(epoch);
        }
        Ok(maximum)
    }
}
impl<'de> DeserializeSeed<'de> for EpochSeed {
    type Value = Option<i64>;
    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_any(self)
    }
}
impl engine_types::wal::OrderEpochReader for Reader {
    fn set_cancel(&mut self, cancel: Arc<std::sync::atomic::AtomicBool>) {
        self.cancel = Some(cancel.clone());
        self.source.cancel = Some(cancel);
    }
    fn max_order_epoch_ms(&mut self) -> Result<Option<i64>, WalError> {
        let mut maximum = None;
        while let Some((cursor, length, crc)) = self.next_frame()? {
            let budget = Arc::new(AtomicU64::new(u64::MAX));
            maximum = maximum.max(self.source.decode(
                cursor,
                length,
                crc,
                budget.clone(),
                EpochSeed { budget },
            )?);
        }
        Ok(maximum)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderKind, OrderRequest, OrderUpdate, Side, StrategyId, SymbolId, Wal};

    fn sent(id: &str) -> WalRecord {
        WalRecord::OrderSent {
            dispatch: None,
            wire_ns: 1,
            arrival_mid: 100.0,
            request: OrderRequest {
                exact_terms: None,
                sleeve_effect: None,
                client_order_id: id.into(),
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                close_position: false,
            },
        }
    }
    fn base(orders: Vec<OpenOrderState>) -> WalRecord {
        serde_json::from_value(serde_json::json!({"kind":"segment_base", "wall_ts_ms":1,
            "strategies":["owner"], "symbols":["BTCUSDT"], "may_open":false, "control_anchors":[],
            "attribution":[], "logged_exposure":[], "intended_stops":[], "portfolio":engine_types::portfolio::PortfolioState::default(), "open_trade_lots":[], "open_orders":orders })).unwrap()
    }
    fn root(name: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "order-lineage-{name}-{}-{}",
            std::process::id(),
            engine_types::clock::mono_ns()
        ));
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    #[test]
    fn archived_terminal_lineage_survives_cache_omission_and_concurrent_rotation() {
        let root = root("rotation");
        let path = root.join("engine.wal");
        let (mut wal, _) = crate::WalWriter::open(&path).unwrap();
        let order = sent("kept");
        wal.append(&order).unwrap();
        wal.append(&sent("other")).unwrap();
        let rejected = WalRecord::OrderUpdate {
            callbacks: Some(vec![StrategyId(0)]),
            update: OrderUpdate::Reject {
                client_order_id: "kept".into(),
                code: 1,
                reason: "venue rejection".into(),
            },
        };
        wal.append(&rejected).unwrap();
        wal.rotate(&base(vec![])).unwrap();
        let mut reader = wal.order_lineage_reader("kept").unwrap().unwrap();
        let cancelled = WalRecord::OrderUpdate {
            callbacks: Some(vec![StrategyId(0)]),
            update: OrderUpdate::Cancelled {
                client_order_id: "kept".into(),
                recv_ns: 2,
            },
        };
        wal.append(&cancelled).unwrap();
        wal.rotate(&base(vec![])).unwrap();
        assert_eq!(reader.next().unwrap(), Some(order.clone()));
        assert_eq!(reader.next().unwrap(), Some(rejected.clone()));
        assert_eq!(
            reader.next().unwrap(),
            None,
            "read crossed its pinned byte frontier"
        );
        drop(wal);
        let (mut wal, _) = crate::open_current(&path).unwrap();
        let mut reader = wal.order_lineage_reader("kept").unwrap().unwrap();
        assert_eq!(reader.next().unwrap(), Some(order));
        assert_eq!(reader.next().unwrap(), Some(rejected));
        assert_eq!(reader.next().unwrap(), Some(cancelled));
        assert_eq!(reader.next().unwrap(), None);
        std::fs::remove_file(&path).unwrap();
        let mut unavailable = wal.order_lineage_reader("kept").unwrap().unwrap();
        assert!(unavailable
            .next()
            .unwrap_err()
            .to_string()
            .contains("source segment 1 unavailable"));
        drop(wal);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn archive_reads_one_restatement_row_and_skips_large_unrelated_payloads() {
        let root = root("stream");
        let path = root.join("engine.wal");
        let (mut wal, _) = crate::WalWriter::open(&path).unwrap();
        let WalRecord::OrderSent { request, .. } = sent("kept") else {
            unreachable!()
        };
        let order = OpenOrderState {
            request,
            wire_ns: 1,
            arrival_mid: 100.0,
            acked: true,
            filled_qty: 0.25,
            fill_quantity: Some(engine_types::wal::OrderFillQuantity::LegacyBinary64 {
                quantity: 0.25,
            }),
            reservation_low_px: 0.0,
            reservation_high_px: 0.0,
            exact_price_range: None,
            terminal: None,
        };
        let mut rows = Vec::new();
        for index in 0..1024 {
            let mut row = order.clone();
            row.request.client_order_id = format!("other-{index}");
            rows.push(row);
        }
        rows.insert(512, order.clone());
        wal.append(&WalRecord::Note {
            source: "unrelated".into(),
            text: "x".repeat(MAX_ROW_BYTES as usize + 1),
        })
        .unwrap();
        wal.rotate(&base(rows)).unwrap();
        let mut reader = wal.order_lineage_reader("kept").unwrap().unwrap();
        let WalRecord::SegmentBase { open_orders, .. } = reader.next().unwrap().unwrap() else {
            panic!("missing restatement");
        };
        assert_eq!(open_orders, vec![order]);
        assert_eq!(reader.next().unwrap(), None);
        drop(wal);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn legacy_epoch_scan_finds_cold_orders_before_a_backward_clock_rotation() {
        let root = root("epoch");
        let path = root.join("engine.wal");
        let (mut wal, _) = crate::WalWriter::open(&path).unwrap();
        wal.append(&sent("eng-1800000000000-262143")).unwrap();
        wal.append(&WalRecord::Boot {
            version: "legacy".into(),
            config_sha256: "fixture".into(),
            wall_ts_ms: 1700000000000,
            commit: String::new(),
        })
        .unwrap();
        wal.rotate(&base(vec![])).unwrap();
        let mut reader = wal.order_epoch_reader().unwrap().unwrap();
        wal.append(&WalRecord::OrderIdEpoch {
            epoch_ms: 1900000000000,
        })
        .unwrap();
        wal.rotate(&base(vec![])).unwrap();
        assert_eq!(reader.max_order_epoch_ms().unwrap(), Some(1800000000000));
        let mut newer = wal.order_epoch_reader().unwrap().unwrap();
        assert_eq!(newer.max_order_epoch_ms().unwrap(), Some(1900000000000));
        drop(wal);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn cancelling_a_missing_order_scan_releases_its_reader_without_finishing_the_family() {
        let root = root("cancel");
        let path = root.join("engine.wal");
        let (mut wal, _) = crate::WalWriter::open(&path).unwrap();
        for index in 0..1000 {
            wal.append(&sent(&format!("other-{index}"))).unwrap();
        }
        let mut reader = wal.order_lineage_reader("missing").unwrap().unwrap();
        let cancel = Arc::new(std::sync::atomic::AtomicBool::new(false));
        reader.set_cancel(cancel.clone());
        cancel.store(true, Ordering::Relaxed);
        assert!(reader.next().unwrap_err().to_string().contains("cancelled"));
        drop(reader);
        drop(wal);
        std::fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn abandoned_rotation_prefixes_do_not_strand_lineage_or_legacy_epoch_recovery() {
        let mut frame = crate::MAGIC.to_vec();
        frame.extend_from_slice(&[0; crate::FRAME_HEADER_LEN]);
        crate::write_record(&mut frame, &base(vec![])).unwrap();
        let payload = &frame[(crate::HEADER_LEN as usize + crate::FRAME_HEADER_LEN)..];
        let size = payload.len() as u32;
        let crc = crc32c::crc32c(payload);
        frame[8..12].copy_from_slice(&size.to_le_bytes());
        frame[12..16].copy_from_slice(&crc.to_le_bytes());
        for cut in [0, 1, 7, 8, 9, 15, 16, frame.len() / 2, frame.len() - 1] {
            let root = root(&format!("abandoned-{cut}"));
            let path = root.join("engine.wal");
            let (mut wal, _) = crate::WalWriter::open(&path).unwrap();
            let sent = sent("eng-1800000000000-1");
            wal.append(&sent).unwrap();
            wal.barrier().unwrap();
            std::fs::write(crate::segment_path(&path, 2), &frame[..cut]).unwrap();
            drop(wal);
            let (mut wal, records) = crate::open_current(&path).unwrap();
            assert_eq!(records.len(), 1);
            wal.rotate(&base(vec![])).unwrap();
            assert_eq!(wal.segment_index, 3);
            let mut reader = wal
                .order_lineage_reader("eng-1800000000000-1")
                .unwrap()
                .unwrap();
            assert_eq!(reader.next().unwrap(), Some(sent));
            assert_eq!(
                reader.next().unwrap(),
                None,
                "abandoned first-frame prefix {cut} was treated as committed history"
            );
            assert_eq!(
                wal.order_epoch_reader()
                    .unwrap()
                    .unwrap()
                    .max_order_epoch_ms()
                    .unwrap(),
                Some(1800000000000)
            );
            drop(wal);
            std::fs::remove_dir_all(root).unwrap();
        }
    }

    #[test]
    fn corruption_after_a_committed_restatement_is_never_skipped_as_an_abandoned_rotation() {
        let root = root("corrupt-archive");
        let path = root.join("engine.wal");
        let (mut wal, _) = crate::WalWriter::open(&path).unwrap();
        wal.append(&sent("eng-1800000000000-1")).unwrap();
        wal.rotate(&base(vec![])).unwrap();
        wal.append(&sent("later")).unwrap();
        wal.barrier().unwrap();
        let archived = crate::segment_path(&path, 2);
        wal.rotate(&base(vec![])).unwrap();
        let mut bytes = std::fs::read(&archived).unwrap();
        let end = bytes.len() - 1;
        bytes[end] ^= 1;
        std::fs::write(archived, bytes).unwrap();
        let mut reader = wal
            .order_lineage_reader("eng-1800000000000-1")
            .unwrap()
            .unwrap();
        assert!(reader.next().unwrap().is_some());
        assert!(reader.next().is_err());
        assert!(wal
            .order_epoch_reader()
            .unwrap()
            .unwrap()
            .max_order_epoch_ms()
            .is_err());
        drop(wal);
        std::fs::remove_dir_all(root).unwrap();
    }
}
