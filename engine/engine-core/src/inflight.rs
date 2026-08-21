//! What the log says is still out there.
//!
//! An order is in flight from the moment its OrderSent record is durable
//! until the log shows it finished: rejected, cancelled, or filled for the
//! whole size. An acknowledgement is not an ending. A send whose reply never
//! arrived stays in flight, which is the honest reading — the engine does not
//! know what the venue did with it, and guessing is how you send twice.

use std::collections::BTreeMap;

use engine_types::{OrderRequest, OrderUpdate, StrategyId, WalRecord};

const QTY_EPS: f64 = 1e-9;

/// How a never-sent order's note starts. Read from the log, never written:
/// no run skips the send now. The marker cannot be deleted because the logs
/// that already hold it would otherwise read back as runs that abandoned
/// every order they ever wrote.
pub const NEVER_SENT_PREFIX: &str = "no send: ";

#[derive(Clone, Debug, PartialEq)]
pub enum Ending {
    Rejected { code: i64, reason: String },
    Cancelled,
    Filled,
    /// Written down and never sent. Only ever read from an older log.
    NeverSent,
}

#[derive(Clone, Debug)]
pub struct OrderRec {
    pub request: OrderRequest,
    pub wire_ns: u64,
    pub acked: bool,
    pub filled_qty: f64,
    pub ending: Option<Ending>,
    /// The midpoint when this order left, carried so a fill arriving a minute
    /// later can still be priced against it. Zero when the book could not be
    /// read then. Kept here rather than looked up because the order may have
    /// been sent in an earlier boot, and this ledger is what a boot rebuilds
    /// from the log.
    pub arrival_mid: f64,
}

impl OrderRec {
    pub fn in_flight(&self) -> bool {
        self.ending.is_none()
    }
}

/// The order book of the log itself, rebuilt by reading records in order.
#[derive(Default, Debug)]
pub struct LedgerOfOrders {
    pub orders: BTreeMap<String, OrderRec>,
    pub boots: u32,
}

impl LedgerOfOrders {
    pub fn from_records(records: &[WalRecord]) -> Self {
        let mut me = Self::default();
        for record in records {
            me.apply(record);
        }
        me
    }

    pub fn apply(&mut self, record: &WalRecord) {
        match record {
            WalRecord::Boot { .. } => self.boots += 1,
            WalRecord::OrderSent {
                request,
                wire_ns,
                arrival_mid,
            } => {
                self.orders.insert(
                    request.client_order_id.clone(),
                    OrderRec {
                        request: request.clone(),
                        wire_ns: *wire_ns,
                        acked: false,
                        filled_qty: 0.0,
                        ending: None,
                        arrival_mid: *arrival_mid,
                    },
                );
            }
            WalRecord::OrderUpdate { update } => self.apply_update(update),
            // A fill the private stream never delivered, read back from the
            // venue's own history. It ends its order exactly like a delivered
            // one: without this the working-order pass never retires a filled
            // order and keeps cancelling something the venue has already
            // finished with.
            WalRecord::RecoveredFill {
                client_order_id,
                qty,
                ..
            } => {
                if let Some(rec) = self.orders.get_mut(client_order_id.as_str()) {
                    rec.acked = true;
                    rec.filled_qty += qty;
                    if rec.filled_qty + QTY_EPS >= rec.request.qty {
                        rec.ending = Some(Ending::Filled);
                    }
                }
            }
            WalRecord::Note { source, text } if source == "shadow" => {
                if let Some(rest) = text.strip_prefix(NEVER_SENT_PREFIX) {
                    let id = rest.split_whitespace().next().unwrap_or_default();
                    if let Some(order) = self.orders.get_mut(id) {
                        order.ending = Some(Ending::NeverSent);
                    }
                }
            }
            // A rotation restated every order still in flight. Set, not add:
            // in a chain read each row equals what this ledger already says
            // at that point, and in a fresh segment it is all there is.
            // Orders that ENDED before the rotation are not restated, so a
            // very late fill for one of them reads as a stranger's after the
            // next restart — charged to nobody, reconcile's to notice.
            WalRecord::SegmentBase { open_orders, .. } => {
                for open in open_orders {
                    self.orders.insert(
                        open.request.client_order_id.clone(),
                        OrderRec {
                            request: open.request.clone(),
                            wire_ns: open.wire_ns,
                            acked: open.acked,
                            filled_qty: open.filled_qty,
                            ending: None,
                            arrival_mid: open.arrival_mid,
                        },
                    );
                }
            }
            _ => {}
        }
    }

    pub fn apply_update(&mut self, update: &OrderUpdate) {
        let id = client_order_id(update);
        let Some(id) = id else { return };
        let Some(rec) = self.orders.get_mut(id) else { return };
        match update {
            OrderUpdate::Ack(_) => rec.acked = true,
            OrderUpdate::Reject { code, reason, .. } => {
                rec.ending = Some(Ending::Rejected {
                    code: *code,
                    reason: reason.clone(),
                })
            }
            OrderUpdate::Cancelled { .. } => rec.ending = Some(Ending::Cancelled),
            OrderUpdate::Fill { qty, .. } => {
                rec.filled_qty += qty;
                if rec.filled_qty + QTY_EPS >= rec.request.qty {
                    rec.ending = Some(Ending::Filled);
                }
            }
            OrderUpdate::StopAttached { .. } | OrderUpdate::StreamReset { .. } => {}
        }
    }

    pub fn contains(&self, client_order_id: &str) -> bool {
        self.orders.contains_key(client_order_id)
    }

    /// Which strategy placed an order. Wider than [`OrderRegistry::owner_of`],
    /// which knows only the ids this boot minted and the ones that were in
    /// flight when it started: every order the log ever recorded is here, and
    /// each one carries its own strategy. A fill can still arrive for an order
    /// that ended in an earlier boot, and it must land on the right strategy.
    pub fn owner_of(&self, client_order_id: &str) -> Option<StrategyId> {
        self.orders
            .get(client_order_id)
            .map(|order| order.request.strategy)
    }

    pub fn in_flight(&self) -> Vec<&OrderRec> {
        self.orders.values().filter(|o| o.in_flight()).collect()
    }

    pub fn in_flight_ids(&self) -> Vec<&str> {
        self.orders
            .iter()
            .filter(|(_, o)| o.in_flight())
            .map(|(id, _)| id.as_str())
            .collect()
    }
}

/// Which order an update is about. `StopAttached` names a symbol and
/// `StreamReset` names nothing, so they belong to nobody here.
pub fn client_order_id(update: &OrderUpdate) -> Option<&str> {
    match update {
        OrderUpdate::Ack(ack) => Some(&ack.client_order_id),
        OrderUpdate::Reject { client_order_id, .. } => Some(client_order_id),
        OrderUpdate::Fill { client_order_id, .. } => Some(client_order_id),
        OrderUpdate::Cancelled { client_order_id, .. } => Some(client_order_id),
        OrderUpdate::StopAttached { .. } | OrderUpdate::StreamReset { .. } => None,
    }
}

/// Who owns an order id. Ids minted this boot share a prefix; ids recovered
/// from an earlier boot's log keep their own and are still routed, so a
/// strategy hears the end of an order it placed before a restart.
#[derive(Default, Debug)]
pub struct OrderRegistry {
    boot_prefix: String,
    owner: BTreeMap<String, StrategyId>,
}

impl OrderRegistry {
    pub fn new(boot_prefix: String) -> Self {
        OrderRegistry {
            boot_prefix,
            owner: BTreeMap::new(),
        }
    }

    pub fn own(&mut self, client_order_id: &str, strategy: StrategyId) {
        self.owner.insert(client_order_id.to_string(), strategy);
    }

    pub fn owner_of(&self, client_order_id: &str) -> Option<StrategyId> {
        self.owner.get(client_order_id).copied()
    }

    /// Did this engine mint the id during this boot?
    pub fn is_ours(&self, client_order_id: &str) -> bool {
        client_order_id.starts_with(&self.boot_prefix)
    }

    pub fn prefix(&self) -> &str {
        &self.boot_prefix
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{OrderAck, OrderKind, Side, SymbolId};

    fn request(id: &str, qty: f64) -> OrderRequest {
        OrderRequest {
            client_order_id: id.into(),
            strategy: StrategyId(0),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            kind: OrderKind::Market,
            stop: None,
            reduce_only: false,
        }
    }

    fn sent(id: &str, qty: f64) -> WalRecord {
        WalRecord::OrderSent {
            request: request(id, qty),
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    fn fill(id: &str, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                client_order_id: id.into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty,
                px: 100.0,
                fee: 0.0,
                is_maker: false,
                venue_ts_ms: 0,
                recv_ns: 0,
            },
        }
    }

    #[test]
    fn an_ack_alone_leaves_the_order_in_flight() {
        let log = vec![
            sent("a", 1.0),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Ack(OrderAck {
                    client_order_id: "a".into(),
                    venue_order_id: "v".into(),
                    ack_ns: 5,
                }),
            },
        ];
        let ledger = LedgerOfOrders::from_records(&log);
        assert_eq!(ledger.in_flight_ids(), vec!["a"]);
        assert!(ledger.orders["a"].acked);
    }

    fn recovered(id: &str, qty: f64) -> WalRecord {
        WalRecord::RecoveredFill {
            exec_id: format!("e-{id}-{qty}"),
            client_order_id: id.into(),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            px: 100.0,
            fee: 0.0,
            is_maker: false,
            venue_ts_ms: 0,
            recovered_wall_ts_ms: 0,
        }
    }

    #[test]
    fn a_fill_recovered_from_the_venues_history_ends_its_order_too() {
        // The stream never delivered it, so nothing else can end the order —
        // and an order that never ends is one the working-order pass keeps
        // cancelling at a venue that finished with it long ago.
        let ledger = LedgerOfOrders::from_records(&[sent("a", 1.0), recovered("a", 1.0)]);
        assert!(ledger.in_flight_ids().is_empty());
        assert_eq!(ledger.orders["a"].ending, Some(Ending::Filled));

        // Partly recovered is still in flight, exactly like a partial fill.
        let ledger = LedgerOfOrders::from_records(&[sent("a", 1.0), recovered("a", 0.4)]);
        assert_eq!(ledger.in_flight_ids(), vec!["a"]);
    }

    #[test]
    fn a_part_fill_stays_in_flight_and_the_rest_ends_it() {
        let ledger = LedgerOfOrders::from_records(&[sent("a", 1.0), fill("a", 0.4)]);
        assert_eq!(ledger.in_flight_ids(), vec!["a"]);
        let ledger = LedgerOfOrders::from_records(&[sent("a", 1.0), fill("a", 0.4), fill("a", 0.6)]);
        assert!(ledger.in_flight_ids().is_empty());
        assert_eq!(ledger.orders["a"].ending, Some(Ending::Filled));
    }

    #[test]
    fn rejects_and_cancels_end_it() {
        let ledger = LedgerOfOrders::from_records(&[
            sent("a", 1.0),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Reject {
                    client_order_id: "a".into(),
                    code: 7,
                    reason: "no".into(),
                },
            },
            sent("b", 1.0),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Cancelled {
                    client_order_id: "b".into(),
                    recv_ns: 3,
                },
            },
        ]);
        assert!(ledger.in_flight_ids().is_empty());
    }

    #[test]
    fn the_prefix_says_whether_an_id_is_ours() {
        let mut reg = OrderRegistry::new("eng-1700000000000-".into());
        reg.own("eng-1700000000000-1", StrategyId(2));
        assert_eq!(reg.owner_of("eng-1700000000000-1"), Some(StrategyId(2)));
        assert!(reg.is_ours("eng-1700000000000-1"));
        assert!(!reg.is_ours("hand-placed-42"));
        assert_eq!(reg.owner_of("hand-placed-42"), None);
    }
}
