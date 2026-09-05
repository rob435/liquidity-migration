//! What must be true of the venue, the log and the engine when a run ends,
//! whatever was injected along the way.

use std::collections::{BTreeMap, BTreeSet};

use engine_types::{
    AccountView, OrderUpdate, Side, SymbolId, VenueExecution, VenueOrder, WalRecord,
};

use crate::backtest::venue::Accounting;

#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Check {
    pub name: &'static str,
    pub passed: bool,
    pub detail: String,
}

impl Check {
    fn pass(name: &'static str, detail: impl Into<String>) -> Self {
        Check {
            name,
            passed: true,
            detail: detail.into(),
        }
    }

    fn fail(name: &'static str, detail: impl Into<String>) -> Self {
        Check {
            name,
            passed: false,
            detail: detail.into(),
        }
    }

    fn judge(name: &'static str, problems: Vec<String>, when_clean: impl Into<String>) -> Self {
        if problems.is_empty() {
            Check::pass(name, when_clean)
        } else {
            Check::fail(name, problems.join("; "))
        }
    }
}

/// Everything the judges read. `engine_*` is `None` when the last segment
/// ended in an error rather than a stop, so the engine's memory is gone.
pub struct Evidence<'a> {
    pub records: &'a [WalRecord],
    pub venue_view: &'a AccountView,
    pub venue_orders: &'a [VenueOrder],
    pub venue_executions: &'a [VenueExecution],
    pub venue_accounting: &'a Accounting,
    pub engine_in_flight: Option<&'a [String]>,
    pub engine_account: Option<&'a AccountView>,
    pub stopped_by: Option<&'a str>,
    pub engine_error: Option<&'a str>,
    /// Σ `net_usdt` over the round trips the log closes, as `engine fills`
    /// reads them; `None` when it closes none.
    pub ledger_net_usdt: Option<f64>,
}

pub fn all(e: &Evidence<'_>) -> Vec<Check> {
    vec![
        engine_ran_clean(e),
        stopped_by_feed_closed(e),
        positions_agree(e),
        every_fill_journaled(e),
        no_orphan_orders(e),
        cash_flow_agrees_when_flat(e),
        ledger_agrees_when_flat(e),
        numbers_finite(e),
    ]
}

fn engine_ran_clean(e: &Evidence<'_>) -> Check {
    match e.engine_error {
        None => Check::pass("engine_ran_clean", "no boot or run error"),
        Some(error) => Check::fail("engine_ran_clean", error),
    }
}

fn stopped_by_feed_closed(e: &Evidence<'_>) -> Check {
    match e.stopped_by {
        Some("FeedClosed") => Check::pass("stopped_by_feed_closed", "the tape ended"),
        Some(other) => Check::fail("stopped_by_feed_closed", format!("stopped by {other}")),
        None => Check::fail("stopped_by_feed_closed", "the loop never stopped cleanly"),
    }
}

fn signed(side: Side, qty: f64) -> f64 {
    match side {
        Side::Buy => qty,
        Side::Sell => -qty,
    }
}

fn positions_agree(e: &Evidence<'_>) -> Check {
    let logged = match crate::reconcile::logged_exposure(e.records) {
        Ok(logged) => logged,
        Err(error) => {
            return Check::fail(
                "positions_agree",
                format!("the log's exposure is unreadable: {error}"),
            )
        }
    };
    let mut venue: BTreeMap<SymbolId, f64> = BTreeMap::new();
    for p in &e.venue_view.positions {
        *venue.entry(p.symbol).or_insert(0.0) += signed(p.side, p.qty);
    }
    let symbols: BTreeSet<SymbolId> = logged.keys().chain(venue.keys()).copied().collect();
    let mut problems = Vec::new();
    for symbol in &symbols {
        let log_qty = logged.get(symbol).copied().unwrap_or(0.0);
        let venue_qty = venue.get(symbol).copied().unwrap_or(0.0);
        if (log_qty - venue_qty).abs() > 1e-9 {
            problems.push(format!(
                "symbol {}: log {log_qty} venue {venue_qty}",
                symbol.0
            ));
        }
    }
    Check::judge(
        "positions_agree",
        problems,
        format!("{} symbols compared", symbols.len()),
    )
}

fn wal_exec_ids(records: &[WalRecord]) -> BTreeSet<&str> {
    records
        .iter()
        .filter_map(|record| match record {
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill { exec_id, .. },
                ..
            }
            | WalRecord::OrderUpdate {
                update: OrderUpdate::FastFill { exec_id, .. },
                ..
            }
            | WalRecord::FastExecution { exec_id, .. }
            | WalRecord::RecoveredFill { exec_id, .. } => Some(exec_id.as_str()),
            _ => None,
        })
        .collect()
}

fn every_fill_journaled(e: &Evidence<'_>) -> Check {
    let venue: BTreeSet<&str> = e
        .venue_executions
        .iter()
        .map(|x| x.exec_id.as_str())
        .collect();
    let logged = wal_exec_ids(e.records);
    let mut problems = Vec::new();
    let missing: Vec<&str> = venue.difference(&logged).copied().collect();
    if !missing.is_empty() {
        problems.push(format!(
            "{} venue fills never reached the log: {}",
            missing.len(),
            missing
                .iter()
                .take(5)
                .copied()
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    let invented: Vec<&str> = logged.difference(&venue).copied().collect();
    if !invented.is_empty() {
        problems.push(format!(
            "{} logged fills the venue never made: {}",
            invented.len(),
            invented
                .iter()
                .take(5)
                .copied()
                .collect::<Vec<_>>()
                .join(", ")
        ));
    }
    Check::judge(
        "every_fill_journaled",
        problems,
        format!("{} executions, all in the log", venue.len()),
    )
}

fn no_orphan_orders(e: &Evidence<'_>) -> Check {
    let Some(in_flight) = e.engine_in_flight else {
        return Check::pass(
            "no_orphan_orders",
            "not judged: the engine did not stop cleanly",
        );
    };
    let known: BTreeSet<&str> = in_flight.iter().map(String::as_str).collect();
    let orphans: Vec<&str> = e
        .venue_orders
        .iter()
        .map(|o| o.client_order_id.as_str())
        .filter(|id| !known.contains(id))
        .collect();
    let problems = if orphans.is_empty() {
        Vec::new()
    } else {
        vec![format!(
            "{} orders working at the venue that the engine does not hold: {}",
            orphans.len(),
            orphans
                .iter()
                .take(5)
                .copied()
                .collect::<Vec<_>>()
                .join(", ")
        )]
    };
    Check::judge(
        "no_orphan_orders",
        problems,
        format!(
            "{} working at the venue, {} in flight in the engine",
            e.venue_orders.len(),
            in_flight.len()
        ),
    )
}

/// Every fill in the log, once each, as money: sells in, buys out, fees
/// out. With no position left, that sum is the venue's realized P&L net of
/// every fee, whatever was dropped, repeated or recovered on the way.
fn cash_flow_agrees_when_flat(e: &Evidence<'_>) -> Check {
    let name = "cash_flow_agrees_when_flat";
    if e.venue_accounting.open_positions != 0 {
        return Check::pass(
            name,
            format!(
                "not judged: {} positions open at the end",
                e.venue_accounting.open_positions
            ),
        );
    }
    // Per execution id, preferring a record that states the fee.
    let mut fills: BTreeMap<&str, (Side, f64, f64, Option<f64>)> = BTreeMap::new();
    for record in e.records {
        let (exec_id, row) = match record {
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        exec_id,
                        side,
                        qty,
                        px,
                        fee,
                        ..
                    },
                ..
            }
            | WalRecord::RecoveredFill {
                exec_id,
                side,
                qty,
                px,
                fee,
                ..
            } => (exec_id.as_str(), (*side, *qty, *px, *fee)),
            WalRecord::FastExecution {
                exec_id,
                side,
                qty,
                px,
                ..
            }
            | WalRecord::OrderUpdate {
                update:
                    OrderUpdate::FastFill {
                        exec_id,
                        side,
                        qty,
                        px,
                        ..
                    },
                ..
            } => (exec_id.as_str(), (*side, *qty, *px, None)),
            _ => continue,
        };
        match fills.get(exec_id) {
            Some((_, _, _, Some(_))) => {}
            _ => {
                fills.insert(exec_id, row);
            }
        }
    }
    let mut engine = 0.0;
    let mut without_fee = 0usize;
    for (side, qty, px, fee) in fills.values() {
        engine += signed(side.flipped(), qty * px);
        match fee {
            Some(fee) => engine -= fee,
            None => without_fee += 1,
        }
    }
    if without_fee > 0 {
        return Check::pass(
            name,
            format!("not judged: {without_fee} logged fills state no fee"),
        );
    }
    let a = e.venue_accounting;
    let venue = a.realized_pnl_usdt - a.fees_paid_usdt;
    let difference = engine - venue;
    let tolerance = 1e-6 * venue.abs().max(1.0);
    if difference.abs() <= tolerance {
        Check::pass(
            name,
            format!("{} fills; engine {engine:.6} venue {venue:.6}", fills.len()),
        )
    } else {
        Check::fail(
            name,
            format!(
                "{} fills; engine {engine:.6} venue {venue:.6} differ by {difference:.6}",
                fills.len()
            ),
        )
    }
}

fn ledger_agrees_when_flat(e: &Evidence<'_>) -> Check {
    let name = "ledger_agrees_when_flat";
    if e.venue_accounting.open_positions != 0 {
        return Check::pass(
            name,
            format!(
                "not judged: {} positions open at the end",
                e.venue_accounting.open_positions
            ),
        );
    }
    let Some(engine_net) = e.ledger_net_usdt else {
        return Check::pass(name, "not judged: the log closes no round trip");
    };
    let a = e.venue_accounting;
    let venue_net = a.realized_pnl_usdt - (a.fees_paid_usdt - a.open_entry_fees_usdt);
    let difference = engine_net - venue_net;
    let tolerance = 1e-6 * engine_net.abs().max(1.0);
    if difference.abs() <= tolerance {
        Check::pass(name, format!("engine {engine_net:.6} venue {venue_net:.6}"))
    } else {
        Check::fail(
            name,
            format!("engine {engine_net:.6} venue {venue_net:.6} differ by {difference:.6}"),
        )
    }
}

fn numbers_finite(e: &Evidence<'_>) -> Check {
    let a = e.venue_accounting;
    let mut problems = Vec::new();
    for (name, value) in [
        ("cash", a.cash_usdt),
        ("realized", a.realized_pnl_usdt),
        ("fees", a.fees_paid_usdt),
        ("funding", a.funding_paid_usdt),
        ("unrealized", a.unrealized_usdt),
        ("equity", a.equity_usdt),
    ] {
        if !value.is_finite() {
            problems.push(format!("venue {name} is {value}"));
        }
    }
    if let Some(account) = e.engine_account {
        for (name, value) in [
            ("equity", account.equity_usdt),
            ("available", account.available_usdt),
        ] {
            if !value.is_finite() {
                problems.push(format!("engine account {name} is {value}"));
            }
        }
        for p in &account.positions {
            if !p.qty.is_finite() || !p.entry_px.is_finite() {
                problems.push(format!(
                    "engine position {} has a non-finite figure",
                    p.symbol.0
                ));
            }
        }
    }
    Check::judge("numbers_finite", problems, "every figure is finite")
}
