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
    let (replayed, torn_tail) = engine_wal::replay_scan(path)?;
    let records: Vec<WalRecord> = replayed.into_iter().map(|(_, r)| r).collect();
    Ok(describe(&records, torn_tail))
}

pub fn describe(records: &[WalRecord], torn_tail: bool) -> ReplayReport {
    let mut ledger = LedgerOfOrders::default();
    let mut lines = Vec::with_capacity(records.len());
    let mut last_in_flight: Vec<String> = Vec::new();

    for (index, record) in records.iter().enumerate() {
        lines.push(format!("{:>6}  {}", index + 1, one_line(record)));
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

pub fn one_line(record: &WalRecord) -> String {
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
            "wants      strategy {} {:?} {} of symbol {} {} [{}]",
            intent.strategy.0,
            intent.side,
            intent.qty,
            intent.symbol.0,
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
        WalRecord::OrderSent { request, wire_ns } => format!(
            "sent       {} {:?} {} of symbol {} {} (at +{} from start)",
            request.client_order_id,
            request.side,
            request.qty,
            request.symbol.0,
            kind_words(&request.kind),
            pretty(*wire_ns)
        ),
        WalRecord::OrderUpdate { update } => format!("news       {}", update_words(update)),
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
        WalRecord::Note { source, text } => format!("note       [{source}] {text}"),
        WalRecord::ControlAnchor { source, state } => {
            format!("anchor     [{source}] {state}")
        }
    }
}

fn kind_words(kind: &OrderKind) -> String {
    match kind {
        OrderKind::Market => "at market".to_string(),
        OrderKind::Limit { px, tif } => format!("at {px} ({tif:?})"),
    }
}

fn update_words(update: &engine_types::OrderUpdate) -> String {
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
        } => format!("stop on symbol {} at {trigger_px}", symbol.0),
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
            }],
            true,
        );
        assert_eq!(report.in_flight, vec!["b".to_string()]);
        assert!(report.lines.last().unwrap().contains("part-way"));
    }
}
