//! Reading the log back in words.
//!
//! One line per record, and whenever the set of orders that are still out
//! there changes, a line saying what it is now. This is the audit surface: if
//! the engine died mid-send, the last such line names the order it cannot
//! account for.

use std::path::Path;

use engine_types::{OrderKind, WalRecord};

use crate::inflight::LedgerOfOrders;
use crate::ledger::pretty;

pub struct ReplayReport {
    pub lines: Vec<String>,
    pub records: usize,
    pub in_flight: Vec<String>,
    pub torn_tail: bool,
}

pub fn read(path: &Path) -> Result<ReplayReport, engine_types::WalError> {
    // The whole family: a rotated log is its segments read in order, and a
    // log that never rotated is a family of one, read exactly as before.
    let (replayed, torn_tail) = engine_wal::replay_chain(path)?;
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    Ok(describe(&records, torn_tail))
}

pub fn describe(records: &[WalRecord], torn_tail: bool) -> ReplayReport {
    let mut ledger = LedgerOfOrders::default();
    let mut lines = Vec::with_capacity(records.len());
    let mut last_in_flight: Vec<String> = Vec::new();

    // Ids are positions, so a record's `strategy` and `symbol` fields mean
    // nothing without the tables the run was using. The log says so as it
    // goes; ids are only appended, so the newest table is the fullest.
    let mut names = LogNames::default();
    for (index, record) in records.iter().enumerate() {
        names.learn(record);
        lines.push(format!("{:>6}  {}", index + 1, one_line(record, &names)));
        ledger.apply(record);
        let now: Vec<String> = ledger.in_flight_ids().iter().map(|s| s.to_string()).collect();
        if now != last_in_flight {
            lines.push(format!("        still out there: {}", listed(&now)));
            last_in_flight = now;
        }
    }
    if torn_tail {
        lines.push("        the log ends part-way through a record; the rest was dropped".into());
    }

    ReplayReport {
        lines,
        records: records.len(),
        in_flight: last_in_flight,
        torn_tail,
    }
}

/// A long list of ids helps nobody: name a few and count the rest.
pub fn listed(ids: &[String]) -> String {
    match ids.len() {
        0 => "nothing".to_string(),
        1..=5 => ids.join(", "),
        n => format!("{} and {} more", ids[..5].join(", "), n - 5),
    }
}

/// What the ids in a log are called, learned from the log itself.
///
/// A log written before the engine recorded its tables still reads — as
/// numbers, which is what it always was.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct LogNames {
    pub strategies: Vec<String>,
    pub symbols: Vec<String>,
}

impl LogNames {
    /// Take the tables from a record that carries them, and ignore the rest.
    /// Ids are only appended, so the newest record is the fullest. A segment
    /// restatement carries the same tables and counts the same way.
    pub fn learn(&mut self, record: &WalRecord) {
        match record {
            WalRecord::Names { strategies, symbols }
            | WalRecord::SegmentBase { strategies, symbols, .. } => {
                self.strategies = strategies.clone();
                self.symbols = symbols.clone();
            }
            _ => {}
        }
    }

    pub fn of_log(records: &[WalRecord]) -> Self {
        let mut me = LogNames::default();
        for record in records {
            me.learn(record);
        }
        me
    }

    pub fn strategy(&self, id: engine_types::StrategyId) -> String {
        self.strategies
            .get(id.0 as usize)
            .cloned()
            .unwrap_or_else(|| format!("strategy {}", id.0))
    }

    pub fn symbol(&self, id: engine_types::SymbolId) -> String {
        self.symbols
            .get(id.0 as usize)
            .cloned()
            .unwrap_or_else(|| format!("symbol {}", id.0))
    }
}

pub fn one_line(record: &WalRecord, names: &LogNames) -> String {
    match record {
        WalRecord::Boot {
            version,
            config_sha256,
            wall_ts_ms,
        } => format!(
            "started    {version}, config {}, at {wall_ts_ms}",
            &config_sha256[..config_sha256.len().min(12)]
        ),
        WalRecord::Intent { intent } => format!(
            "wants      {} {:?} {} of {} {} [{}]",
            names.strategy(intent.strategy),
            intent.side,
            intent.qty,
            names.symbol(intent.symbol),
            kind_words(&intent.kind),
            intent.tag
        ),
        WalRecord::Verdict {
            client_order_id,
            verdict,
        } => match verdict {
            engine_types::RiskVerdict::Allow { qty } => format!(
                "allowed    {} for {qty}",
                client_order_id.as_deref().unwrap_or("(no id)")
            ),
            engine_types::RiskVerdict::Deny { reason } => format!("refused    {reason:?}"),
        },
        WalRecord::OrderSent {
            request,
            wire_ns,
            arrival_mid,
        } => format!(
            "sent       {} {:?} {} of {} {} (at +{} from start){}",
            request.client_order_id,
            request.side,
            request.qty,
            names.symbol(request.symbol),
            kind_words(&request.kind),
            pretty(*wire_ns),
            // The price this order will be judged against. Silent when the
            // book could not be read, which is what a zero here means.
            if *arrival_mid > 0.0 {
                format!(", mid then {arrival_mid}")
            } else {
                String::new()
            }
        ),
        WalRecord::OrderUpdate { update } => format!("news       {}", update_words(update, names)),
        WalRecord::StopSet {
            symbol,
            trigger_px,
            wall_ts_ms,
        } => format!(
            "restopped  {} to {trigger_px} (at {wall_ts_ms})",
            names.symbol(*symbol)
        ),
        WalRecord::CancelSent {
            symbol,
            client_order_id,
            wire_ns,
        } => format!(
            "pulled     {client_order_id} on {} (at +{} from start)",
            names.symbol(*symbol),
            pretty(*wire_ns)
        ),
        WalRecord::AmendSent {
            symbol,
            client_order_id,
            spec,
            wire_ns,
        } => format!(
            "moved      {client_order_id} on {} to{}{} (at +{} from start)",
            names.symbol(*symbol),
            match spec.px {
                Some(px) => format!(" price {px}"),
                None => String::new(),
            },
            match spec.qty {
                Some(qty) => format!(" size {qty}"),
                None => String::new(),
            },
            pretty(*wire_ns)
        ),
        WalRecord::LatencyLedger {
            window_s,
            events,
            decide_p50_ns,
            decide_p99_ns,
            wire_p50_ns,
            wire_p99_ns,
        } => format!(
            "latency    {window_s}s, {events} messages; think {} / {}, out the door {} / {}",
            pretty(*decide_p50_ns),
            pretty(*decide_p99_ns),
            pretty(*wire_p50_ns),
            pretty(*wire_p99_ns)
        ),
        WalRecord::Markout {
            client_order_id,
            horizon_ms,
            signed_markout_bps,
            actual_horizon_ms,
            ..
        } => format!(
            "markout    {client_order_id} after {}s: {}{}",
            horizon_ms / 1_000,
            match signed_markout_bps {
                // Said in words, because the sign is the opposite of every
                // other number in this log and a bare figure invites the
                // wrong reading.
                Some(bps) if *bps >= 0.0 => format!("{bps:.2} bp our way"),
                Some(bps) => format!("{:.2} bp against us", -bps),
                None => "no readable book, so never measured".to_string(),
            },
            match actual_horizon_ms.saturating_sub(*horizon_ms) {
                0 => String::new(),
                late => format!(" (read {late} ms late)"),
            }
        ),
        WalRecord::Names { strategies, symbols } => format!(
            "names      {} sleeve(s): {}; {} symbol(s): {}",
            strategies.len(),
            listed(strategies),
            symbols.len(),
            listed(symbols)
        ),
        WalRecord::Note { source, text } => format!("note       [{source}] {text}"),
        WalRecord::ControlAnchor { source, state } => {
            format!("anchor     [{source}] {state}")
        }
        WalRecord::RecoveredFill { client_order_id, symbol, side, qty, px, .. } => format!(
            "recovered  {side:?} {qty} of {} at {px}{} — the stream never delivered this fill",
            names.symbol(*symbol),
            match client_order_id.as_str() {
                "" => " (no order of ours: a venue stop or a hand trade)".to_string(),
                id => format!(" for {id}"),
            }
        ),
        WalRecord::ClaimsDropped { rows, .. } => format!(
            "unclaimed  the venue held nothing in these, so the sleeve claims on them end: {}",
            rows.iter()
                .map(|row| format!(
                    "{} {} {}",
                    names.strategy(row.strategy),
                    names.symbol(row.symbol),
                    row.signed_qty
                ))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        WalRecord::LatchCleared { note, restated_exposure, findings, .. } => format!(
            "cleared    an operator looked at the log: may-open resets, exposure restated \
             over {} symbol(s) ({note}){}",
            restated_exposure.len(),
            findings.iter().map(|f| format!("\n             absorbed: {f}")).collect::<String>()
        ),
        WalRecord::Reconciled { findings, may_open, .. } => format!(
            "reconciled {} finding(s), may open: {may_open}{}",
            findings.len(),
            findings.iter().map(|f| format!("\n             {f}")).collect::<String>()
        ),
        WalRecord::SegmentBase {
            wall_ts_ms,
            symbols,
            may_open,
            open_orders,
            ..
        } => format!(
            "rotation   this segment restates the ones before it (at {wall_ts_ms}): \
             {} symbol(s), {} open order(s), may open: {may_open}",
            symbols.len(),
            open_orders.len()
        ),
    }
}

fn kind_words(kind: &OrderKind) -> String {
    match kind {
        OrderKind::Market => "at market".to_string(),
        OrderKind::Limit { px, tif } => format!("at {px} ({tif:?})"),
    }
}

fn update_words(update: &engine_types::OrderUpdate, names: &LogNames) -> String {
    use engine_types::OrderUpdate as U;
    match update {
        U::Ack(ack) => format!("{} accepted as {}", ack.client_order_id, ack.venue_order_id),
        U::Reject {
            client_order_id,
            code,
            reason,
        } => format!("{client_order_id} rejected ({code}): {reason}"),
        U::Fill {
            client_order_id,
            qty,
            px,
            fee,
            ..
        } => format!("{client_order_id} filled {qty} at {px}, fee {fee}"),
        U::Cancelled { client_order_id, .. } => format!("{client_order_id} cancelled"),
        U::StopAttached {
            symbol, trigger_px, ..
        } => format!("stop on {} at {trigger_px}", names.symbol(*symbol)),
        U::StreamReset { .. } => "private stream reconnected; gap possible".to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderRequest, OrderUpdate, Side, StrategyId, SymbolId};

    fn request(id: &str) -> OrderRequest {
        OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty: 1.0,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
        }
    }

    #[test]
    fn the_in_flight_line_appears_when_the_set_changes() {
        let records = vec![
            WalRecord::Boot {
                version: "test".into(),
                config_sha256: "abcdef1234567890".into(),
                wall_ts_ms: 1,
            },
            WalRecord::OrderSent {
                request: request("a"),
                wire_ns: 10,
                arrival_mid: 0.0,
            },
            WalRecord::OrderUpdate {
                update: OrderUpdate::Cancelled {
                    client_order_id: "a".into(),
                    recv_ns: 20,
                },
            },
        ];
        let report = describe(&records, false);
        let text = report.lines.join("\n");
        assert!(text.contains("still out there: a"), "{text}");
        assert!(text.contains("still out there: nothing"), "{text}");
        assert!(report.in_flight.is_empty());
        assert_eq!(report.records, 3);
    }

    #[test]
    fn an_unanswered_send_is_still_out_there_at_the_end() {
        let report = describe(
            &[WalRecord::OrderSent {
                request: request("b"),
                wire_ns: 1,
                arrival_mid: 0.0,
            }],
            true,
        );
        assert_eq!(report.in_flight, vec!["b".to_string()]);
        assert!(report.lines.last().unwrap().contains("part-way"));
    }
}
