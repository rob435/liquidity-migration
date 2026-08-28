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
    Rejected {
        code: i64,
        reason: String,
    },
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
    /// Exact for an ordinary order; a range while an amend outcome is
    /// unknown. Rotation persists both ends so restart cannot narrow risk.
    pub reservation_low_px: f64,
    pub reservation_high_px: f64,
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
                let exact_px = limit_px(request);
                self.orders.insert(
                    request.client_order_id.clone(),
                    OrderRec {
                        request: request.clone(),
                        wire_ns: *wire_ns,
                        acked: false,
                        filled_qty: 0.0,
                        ending: None,
                        arrival_mid: *arrival_mid,
                        reservation_low_px: exact_px,
                        reservation_high_px: exact_px,
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
            WalRecord::AmendSent {
                client_order_id,
                spec,
                ..
            } => {
                if let (Some(rec), Some(requested_px)) =
                    (self.orders.get_mut(client_order_id), spec.px)
                {
                    if rec.in_flight() {
                        if let engine_types::OrderKind::Limit { px, tif } = rec.request.kind {
                            // The request may have reached the venue even if
                            // the process died before its answer. Preserve the
                            // full plausible range: high prices dominate
                            // notional, low prices can dominate short-stop loss.
                            let prior_low = positive_or(rec.reservation_low_px, px);
                            let prior_high = positive_or(rec.reservation_high_px, px);
                            rec.reservation_low_px = prior_low.min(requested_px);
                            rec.reservation_high_px = prior_high.max(requested_px);
                            rec.request.kind = engine_types::OrderKind::Limit {
                                px: rec.reservation_high_px,
                                tif,
                            };
                        }
                    }
                }
            }
            WalRecord::AmendResolved {
                client_order_id,
                effective_px,
            } => {
                if let Some(rec) = self.orders.get_mut(client_order_id) {
                    if rec.in_flight() {
                        if let engine_types::OrderKind::Limit { tif, .. } = rec.request.kind {
                            rec.request.kind = engine_types::OrderKind::Limit {
                                px: *effective_px,
                                tif,
                            };
                            rec.reservation_low_px = *effective_px;
                            rec.reservation_high_px = *effective_px;
                        }
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
                    let exact_px = limit_px(&open.request);
                    self.orders.insert(
                        open.request.client_order_id.clone(),
                        OrderRec {
                            request: open.request.clone(),
                            wire_ns: open.wire_ns,
                            acked: open.acked,
                            filled_qty: open.filled_qty,
                            ending: None,
                            arrival_mid: open.arrival_mid,
                            reservation_low_px: positive_or(open.reservation_low_px, exact_px),
                            reservation_high_px: positive_or(open.reservation_high_px, exact_px),
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
        let Some(rec) = self.orders.get_mut(id) else {
            return;
        };
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

fn limit_px(request: &OrderRequest) -> f64 {
    match request.kind {
        engine_types::OrderKind::Limit { px, .. } if px.is_finite() && px > 0.0 => px,
        _ => 0.0,
    }
}

fn positive_or(value: f64, fallback: f64) -> f64 {
    if value.is_finite() && value > 0.0 {
        value
    } else {
        fallback
    }
}

/// Which order an update is about. `StopAttached` names a symbol and
/// `StreamReset` names nothing, so they belong to nobody here.
pub fn client_order_id(update: &OrderUpdate) -> Option<&str> {
    match update {
        OrderUpdate::Ack(ack) => Some(&ack.client_order_id),
        OrderUpdate::Reject {
            client_order_id, ..
        } => Some(client_order_id),
        OrderUpdate::Fill {
            client_order_id, ..
        } => Some(client_order_id),
        OrderUpdate::Cancelled {
            client_order_id, ..
        } => Some(client_order_id),
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

    /// Build the boot prefix from a wall-clock stamp.
    ///
    /// The millisecond is dropped here, and this is the only place it is: an
    /// id's stamp has to fit Lighter's 48-bit client order index, which an
    /// absolute millisecond stamp plus a usable counter does not. See
    /// `venues/lighter/order_index.rs`. Without it the venue hands back an id
    /// the engine never minted, and every Lighter fill is charged to nobody
    /// while every resting order of ours reads as a stranger's.
    pub fn boot_prefix(boot_ms: i64) -> String {
        format!("eng-{}-", boot_ms - boot_ms.rem_euclid(1_000))
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
    use engine_types::{AmendSpec, OrderAck, OrderKind, Side, SymbolId, TimeInForce};

    #[test]
    fn a_minted_id_carries_no_millisecond_of_its_own() {
        // Driven through the same function the engine boots with, so a change
        // there is a failure here. Lighter's client order index is 48 bits,
        // which an absolute millisecond stamp plus a usable counter does not
        // fit; without the rounding the venue hands back an id this engine
        // never minted, silently.
        for boot_ms in [
            1_762_000_000_123i64,
            1_762_000_000_999,
            1_762_000_000_000,
            1,
        ] {
            let registry = OrderRegistry::new(OrderRegistry::boot_prefix(boot_ms));
            let mut n = 0u64;
            let id = crate::engine::mint_unused(registry.prefix(), &mut n, |_| false);
            let stamp: i64 = id
                .strip_prefix("eng-")
                .and_then(|rest| rest.split('-').next())
                .and_then(|s| s.parse().ok())
                .unwrap_or_else(|| panic!("{id} is not an engine id"));
            assert_eq!(stamp % 1_000, 0, "{id} carries a millisecond");
            assert_eq!(stamp, boot_ms - boot_ms.rem_euclid(1_000));
            assert!(registry.is_ours(&id));
        }
    }

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
                exec_id: String::new(),
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
        let ledger =
            LedgerOfOrders::from_records(&[sent("a", 1.0), fill("a", 0.4), fill("a", 0.6)]);
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
    fn replay_reserves_the_worst_price_until_an_amend_is_resolved() {
        let mut original = request("a", 1.0);
        original.kind = OrderKind::Limit {
            px: 100.0,
            tif: TimeInForce::Gtc,
        };
        let sent = WalRecord::OrderSent {
            request: original,
            wire_ns: 1,
            arrival_mid: 100.0,
        };
        let amend = WalRecord::AmendSent {
            symbol: SymbolId(0),
            client_order_id: "a".into(),
            spec: AmendSpec {
                px: Some(1_000.0),
                qty: None,
            },
            wire_ns: 2,
        };
        let unresolved = LedgerOfOrders::from_records(&[sent.clone(), amend.clone()]);
        assert!(matches!(
            unresolved.orders["a"].request.kind,
            OrderKind::Limit { px: 1_000.0, .. }
        ));
        assert_eq!(unresolved.orders["a"].reservation_low_px, 100.0);
        assert_eq!(unresolved.orders["a"].reservation_high_px, 1_000.0);

        let rejected = LedgerOfOrders::from_records(&[
            sent.clone(),
            amend.clone(),
            WalRecord::AmendResolved {
                client_order_id: "a".into(),
                effective_px: 100.0,
            },
        ]);
        assert!(matches!(
            rejected.orders["a"].request.kind,
            OrderKind::Limit { px: 100.0, .. }
        ));
        assert_eq!(rejected.orders["a"].reservation_low_px, 100.0);
        assert_eq!(rejected.orders["a"].reservation_high_px, 100.0);

        let accepted = LedgerOfOrders::from_records(&[
            sent,
            amend,
            WalRecord::AmendResolved {
                client_order_id: "a".into(),
                effective_px: 1_000.0,
            },
        ]);
        assert!(matches!(
            accepted.orders["a"].request.kind,
            OrderKind::Limit { px: 1_000.0, .. }
        ));
        assert_eq!(accepted.orders["a"].reservation_low_px, 1_000.0);
        assert_eq!(accepted.orders["a"].reservation_high_px, 1_000.0);
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
