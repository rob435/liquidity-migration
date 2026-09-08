//! Incremental, read-only WAL projection. Partial active frames remain for the next run.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, Read, Seek, SeekFrom};
use std::path::Path;

use engine_types::order_dispatch::QueuedOrderDispatch;
use engine_types::{InstrumentRule, OrderRequest, OrderUpdate};
use serde::Deserialize;

use super::{ActualFill, ObservedOrder, ObservedState, Result};

#[derive(Default, Deserialize)]
struct Catalog {
    #[serde(default)]
    rules: Vec<(String, InstrumentRule)>,
}

// Unselected fields, including callback histories in segment bases, are streamed and discarded.
#[derive(Default, Deserialize)]
struct Row {
    kind: String,
    #[serde(default)]
    strategies: Option<Vec<String>>,
    #[serde(default)]
    symbols: Option<Vec<String>>,
    wall_ts_ms: Option<i64>,
    commit: Option<String>,
    instrument_catalog: Option<Catalog>,
    checkpoint: Option<Catalog>,
    request: Option<serde_json::Value>,
    dispatch: Option<QueuedOrderDispatch>,
    wire_ns: Option<u64>,
    arrival_mid: Option<f64>,
    update: Option<OrderUpdate>,
    operation: Option<String>,
    client_order_id: Option<String>,
    socket_write_ns: Option<u64>,
    ack_ns: Option<u64>,
    core_handled_ns: Option<u64>,
    core_handled_wall_ns: Option<u64>,
    exec_id: Option<String>,
    qty: Option<f64>,
    px: Option<f64>,
    fee: Option<f64>,
    is_maker: Option<bool>,
    venue_ts_ms: Option<i64>,
}

struct Checked<'a, R> {
    input: &'a mut R,
    left: u64,
    crc: u32,
}

impl<R: Read> Read for Checked<'_, R> {
    fn read(&mut self, bytes: &mut [u8]) -> std::io::Result<usize> {
        let count = bytes.len().min(self.left as usize);
        if count == 0 {
            return Ok(0);
        }
        let read = self.input.read(&mut bytes[..count])?;
        self.left -= read as u64;
        self.crc = crc32c::crc32c_append(self.crc, &bytes[..read]);
        Ok(read)
    }
}

pub fn scan(family: &Path, since_ns: u64, state: &mut ObservedState) -> Result<()> {
    if state.schema != 0 && (state.schema != 1 || state.family != family) {
        return Err("execution study cursor has a different schema or WAL family".into());
    }
    let segments = engine_wal::segments(family)?;
    if segments.is_empty() {
        return Err("execution study WAL family is empty".into());
    }
    if state.schema == 0 {
        state.schema = 1;
        state.family = family.to_owned();
        state.since_ns = since_ns;
        state.segment = segments
            .iter()
            .find(|(_, path)| {
                path.metadata()
                    .and_then(|m| m.modified())
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .is_some_and(|t| t.as_nanos() >= u128::from(since_ns))
            })
            .unwrap_or_else(|| segments.last().expect("nonempty"))
            .0;
        state.offset = 8;
    }
    if !segments.iter().any(|(index, _)| *index == state.segment) {
        return Err("execution study cursor segment disappeared".into());
    }
    let newest = segments.last().expect("nonempty").0;
    for (index, path) in segments {
        if index < state.segment {
            continue;
        }
        if index > state.segment {
            if index != state.segment + 1 {
                return Err("execution study WAL segment gap".into());
            }
            state.segment = index;
            state.offset = 8;
        }
        let mut reader = BufReader::new(File::open(path)?);
        let size = reader.get_ref().metadata()?.len();
        let mut magic = [0; 8];
        reader.read_exact(&mut magic)?;
        if &magic != b"EWAL0001" || size < state.offset {
            return Err("execution study WAL header changed or input shrank".into());
        }
        reader.seek(SeekFrom::Start(state.offset))?;
        while state.offset + 8 <= size {
            let offset = state.offset;
            let mut header = [0; 8];
            reader.read_exact(&mut header)?;
            let length = u32::from_le_bytes(header[..4].try_into()?) as u64;
            let expected = u32::from_le_bytes(header[4..].try_into()?);
            if length == 0 || length > 512 * 1024 * 1024 {
                return Err(format!("invalid execution study WAL frame length {length}").into());
            }
            if offset + 8 + length > size {
                if index != newest {
                    return Err("incomplete frame in archived WAL segment".into());
                }
                return Ok(());
            }
            let mut checked = Checked {
                input: &mut reader,
                left: length,
                crc: 0,
            };
            let row: Row = serde_json::from_reader(BufReader::new(&mut checked))?;
            if checked.left != 0 || checked.crc != expected {
                return Err(
                    format!("execution study WAL checksum mismatch at {index}:{offset}").into(),
                );
            }
            apply(state, row, index, offset)?;
            state.offset = offset + 8 + length;
            state.records_read += 1;
        }
        if state.offset != size && index != newest {
            return Err("partial frame header in archived WAL segment".into());
        }
    }
    Ok(())
}

fn apply(state: &mut ObservedState, mut row: Row, segment: u64, offset: u64) -> Result<()> {
    if row.kind.starts_with("recovered_fill") {
        let id = row
            .client_order_id
            .clone()
            .ok_or("recovered fill lacks order identity")?;
        if let Some(order) = state.orders.get_mut(&id) {
            if row.exec_id.as_ref().is_none_or(String::is_empty)
                || row.venue_ts_ms.is_none_or(|at| at <= 0)
            {
                order.unidentified_fill_rows += 1;
                return Ok(());
            }
            insert_fill(
                order,
                ActualFill {
                    exec_id: row
                        .exec_id
                        .take()
                        .ok_or("recovered fill lacks execution id")?,
                    at_ns: u64::try_from(row.venue_ts_ms.ok_or("recovered fill lacks time")?)?
                        * 1_000_000,
                    qty: row.qty.ok_or("recovered fill lacks quantity")?,
                    price: row.px.ok_or("recovered fill lacks price")?,
                    fee: row.fee,
                    maker: row.is_maker.ok_or("recovered fill lacks maker flag")?,
                },
            )?;
        }
    }
    if let Some(names) = row.symbols {
        state.symbols = names;
    }
    if let Some(names) = row.strategies {
        state.strategies = names;
    }
    if row.kind == "boot" {
        state.process_epoch_ms = row.wall_ts_ms.unwrap_or_default();
        state.engine_commit = row.commit.filter(|s| !s.is_empty());
    }
    if let Some(catalog) = row.instrument_catalog.or(row.checkpoint) {
        state.rules.extend(catalog.rules);
    }
    if row.kind == "order_sent" || row.kind == "order_sent_v2" {
        let request: OrderRequest =
            serde_json::from_value(row.request.ok_or("order_sent has no request")?)?;
        let symbol = state
            .symbols
            .get(request.symbol.0 as usize)
            .ok_or("order symbol is unnamed")?
            .clone();
        let sleeve = request
            .sleeve_owner()
            .and_then(|id| state.strategies.get(id.0 as usize).cloned())
            .unwrap_or_else(|| "portfolio".into());
        let id = request.client_order_id.clone();
        let rule = state.rules.get(&symbol).copied();
        if let Some(old) = state.orders.get(&id) {
            if old.request != request {
                return Err(format!("conflicting observed order {id}").into());
            }
        } else {
            state.orders.insert(
                id,
                ObservedOrder {
                    request,
                    intent: row.dispatch.map(|d| d.intent),
                    symbol,
                    sleeve,
                    engine_commit: state.engine_commit.clone(),
                    source_segment: segment,
                    source_offset: offset,
                    process_epoch_ms: state.process_epoch_ms,
                    wire_mono_ns: row.wire_ns.unwrap_or_default(),
                    decision_ns: None,
                    socket_write_ns: None,
                    transport_rtt_ns: None,
                    arrival_mid: row.arrival_mid.unwrap_or_default(),
                    rule,
                    fills: BTreeMap::new(),
                    unidentified_fill_rows: 0,
                    terminal: None,
                    amends: 0,
                    cancels: 0,
                },
            );
        }
    }
    if row.kind == "venue_timing" && row.operation.as_deref() == Some("place") {
        if let Some(order) = row
            .client_order_id
            .as_ref()
            .and_then(|id| state.orders.get_mut(id))
        {
            if let (Some(mono), Some(wall), Some(write)) = (
                row.core_handled_ns,
                row.core_handled_wall_ns,
                row.socket_write_ns,
            ) {
                let decision = order
                    .intent
                    .as_ref()
                    .map(|i| i.decided_ns)
                    .unwrap_or(order.wire_mono_ns);
                if wall > 0
                    && decision <= write
                    && write <= mono
                    && order.process_epoch_ms == state.process_epoch_ms
                {
                    order.decision_ns = wall.checked_sub(mono - decision);
                    order.socket_write_ns = wall.checked_sub(mono - write);
                    order.transport_rtt_ns = row.ack_ns.and_then(|ack| ack.checked_sub(write));
                } else {
                    state.unresolved_clocks += 1;
                }
            }
        }
    }
    if let Some(update) = row.update {
        match update {
            OrderUpdate::Fill {
                exec_id,
                client_order_id,
                qty,
                px,
                fee,
                is_maker,
                venue_ts_ms,
                ..
            } => {
                if let Some(order) = state.orders.get_mut(&client_order_id) {
                    if exec_id.is_empty() || venue_ts_ms <= 0 {
                        order.unidentified_fill_rows += 1;
                        return Ok(());
                    }
                    let fill = ActualFill {
                        exec_id,
                        at_ns: venue_ts_ms as u64 * 1_000_000,
                        qty,
                        price: px,
                        fee,
                        maker: is_maker,
                    };
                    insert_fill(order, fill)?;
                }
            }
            OrderUpdate::Cancelled {
                client_order_id, ..
            } => {
                if let Some(o) = state.orders.get_mut(&client_order_id) {
                    o.terminal = Some("cancelled".into());
                }
            }
            OrderUpdate::Reject {
                client_order_id, ..
            } => {
                if let Some(o) = state.orders.get_mut(&client_order_id) {
                    o.terminal = Some("rejected".into());
                }
            }
            _ => {}
        }
    }
    if let Some(order) = row
        .client_order_id
        .as_ref()
        .and_then(|id| state.orders.get_mut(id))
    {
        if row.kind.starts_with("amend_sent") {
            order.amends += 1;
        }
        if row.kind == "cancel_sent" {
            order.cancels += 1;
        }
    }
    Ok(())
}

fn insert_fill(order: &mut ObservedOrder, fill: ActualFill) -> Result<()> {
    if fill.exec_id.is_empty()
        || fill.qty <= 0.0
        || !fill.qty.is_finite()
        || fill.price <= 0.0
        || !fill.price.is_finite()
        || fill.fee.is_some_and(|fee| !fee.is_finite())
    {
        return Err("invalid observed execution".into());
    }
    if let Some(old) = order.fills.get(&fill.exec_id) {
        if serde_json::to_value(old)? != serde_json::to_value(&fill)? {
            return Err("conflicting duplicate execution".into());
        }
    } else {
        order.fills.insert(fill.exec_id.clone(), fill);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::io::Write;

    fn frame(value: serde_json::Value) -> Vec<u8> {
        let body = serde_json::to_vec(&value).unwrap();
        let mut out = (body.len() as u32).to_le_bytes().to_vec();
        out.extend(crc32c::crc32c(&body).to_le_bytes());
        out.extend(body);
        out
    }

    fn order() -> serde_json::Value {
        json!({"kind":"order_sent_v2", "request": {
            "client_order_id":"order-a", "strategy":0,"symbol":0,"side":"Buy",
            "qty":2.0,"kind":"Market","stop":null,"reduce_only":false
        },"wire_ns":100,"arrival_mid":10.0})
    }

    #[test]
    fn partial_live_frame_is_retried_and_restart_does_not_duplicate_orders() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let mut file = File::create(&path).unwrap();
        file.write_all(b"EWAL0001").unwrap();
        file.write_all(&frame(
            json!({"kind":"names","strategies":["long"],"symbols":["XUSDT"]}),
        ))
        .unwrap();
        let bytes = frame(order());
        file.write_all(&bytes[..bytes.len() - 3]).unwrap();
        file.flush().unwrap();
        let mut state = ObservedState::default();
        scan(&path, 0, &mut state).unwrap();
        assert_eq!(state.orders.len(), 0);
        let offset = state.offset;
        file.write_all(&bytes[bytes.len() - 3..]).unwrap();
        file.flush().unwrap();
        let mut restored: ObservedState =
            serde_json::from_value(serde_json::to_value(state).unwrap()).unwrap();
        scan(&path, 0, &mut restored).unwrap();
        assert_eq!(restored.orders.len(), 1);
        assert!(restored.offset > offset);
        let count = restored.records_read;
        scan(&path, 0, &mut restored).unwrap();
        assert_eq!(restored.records_read, count);
    }

    #[test]
    fn timing_maps_the_same_order_without_crossing_process_clock_origins() {
        let mut state = ObservedState {
            symbols: vec!["XUSDT".into()],
            strategies: vec!["long".into()],
            ..ObservedState::default()
        };
        apply(&mut state, serde_json::from_value(order()).unwrap(), 1, 8).unwrap();
        let timing = json!({"kind":"venue_timing","operation":"place","client_order_id":"order-a",
            "socket_write_ns":120,"ack_ns":160,"core_handled_ns":180,"core_handled_wall_ns":1000});
        apply(
            &mut state,
            serde_json::from_value(timing.clone()).unwrap(),
            1,
            16,
        )
        .unwrap();
        assert_eq!(state.orders["order-a"].decision_ns, Some(920));
        assert_eq!(state.orders["order-a"].transport_rtt_ns, Some(40));
        state.orders.get_mut("order-a").unwrap().decision_ns = None;
        state.process_epoch_ms = 99;
        apply(&mut state, serde_json::from_value(timing).unwrap(), 1, 24).unwrap();
        assert_eq!(state.orders["order-a"].decision_ns, None);
        assert_eq!(state.unresolved_clocks, 1);
    }

    #[test]
    fn unrelated_requests_do_not_need_order_fields_and_bad_crc_is_refused() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("engine.wal");
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend(frame(
            json!({"kind":"runtime_control_requested", "request":{"name":"something"}}),
        ));
        std::fs::write(&path, &bytes).unwrap();
        let mut state = ObservedState::default();
        scan(&path, 0, &mut state).unwrap();
        let mut corrupt = frame(order());
        corrupt[4] ^= 1;
        std::fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .unwrap()
            .write_all(&corrupt)
            .unwrap();
        assert!(scan(&path, 0, &mut state).is_err());
    }

    #[test]
    fn legacy_fill_without_execution_id_does_not_block_later_identified_fills() {
        let mut state = ObservedState {
            symbols: vec!["XUSDT".into()],
            strategies: vec!["long".into()],
            ..ObservedState::default()
        };
        apply(&mut state, serde_json::from_value(order()).unwrap(), 1, 8).unwrap();
        let mut row = json!({"kind":"order_update","update":{"Fill":{
            "client_order_id":"order-a","symbol":0,"side":"Buy","qty":1.0,"px":10.0,
            "fee":0.01,"is_maker":false,"venue_ts_ms":1000,"recv_ns":20
        }}});
        apply(
            &mut state,
            serde_json::from_value(row.clone()).unwrap(),
            1,
            24,
        )
        .unwrap();
        assert!(state.orders["order-a"].fills.is_empty());
        assert_eq!(state.orders["order-a"].unidentified_fill_rows, 1);
        row["update"]["Fill"]["exec_id"] = json!("identified");
        apply(&mut state, serde_json::from_value(row).unwrap(), 1, 32).unwrap();
        assert_eq!(state.orders["order-a"].fills.len(), 1);
    }

    #[test]
    fn recovered_executions_deduplicate_against_stream_fills_and_preserve_fees() {
        let mut state = ObservedState {
            symbols: vec!["XUSDT".into()],
            strategies: vec!["long".into()],
            ..ObservedState::default()
        };
        apply(&mut state, serde_json::from_value(order()).unwrap(), 1, 8).unwrap();
        let recovered = json!({"kind":"recovered_fill_v3","exec_id":"fill-a","client_order_id":"order-a","qty":1.0,"px":10.0,"fee":0.01,"is_maker":false,"venue_ts_ms":1000});
        for _ in 0..2 {
            apply(
                &mut state,
                serde_json::from_value(recovered.clone()).unwrap(),
                1,
                24,
            )
            .unwrap();
        }
        let stream = json!({"kind":"order_update_v3","update":{"Fill":{
            "exec_id":"fill-a","client_order_id":"order-a","symbol":0,"side":"Buy",
            "qty":1.0,"px":10.0,"fee":0.01,"is_maker":false,"venue_ts_ms":1000,"recv_ns":20
        }}});
        apply(&mut state, serde_json::from_value(stream).unwrap(), 1, 28).unwrap();
        let fills = &state.orders["order-a"].fills;
        assert_eq!(fills.len(), 1);
        assert_eq!(fills["fill-a"].fee, Some(0.01));
        let mut conflicting = recovered;
        conflicting["qty"] = json!(2.0);
        assert!(apply(
            &mut state,
            serde_json::from_value(conflicting).unwrap(),
            1,
            32
        )
        .is_err());
    }
}
