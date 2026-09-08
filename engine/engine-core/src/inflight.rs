//! What the log says is still out there.
//!
//! An order is in flight from the moment its OrderSent record is durable
//! until the log shows it finished: rejected, cancelled, or filled for the
//! whole size. An acknowledgement is not an ending. A send whose reply never
//! arrived stays in flight, which is the honest reading — the engine does not
//! know what the venue did with it, and guessing is how you send twice.

use std::collections::BTreeMap;

use engine_types::{OrderRequest, OrderUpdate, Side, StrategyId, SymbolId, WalRecord};

mod quantities;

const QTY_EPS: f64 = 1e-9;

/// How a never-sent order's note starts. Read from the log, never written:
/// no run skips the send now. The marker cannot be deleted because the logs
/// that already hold it would otherwise read back as runs that abandoned
/// every order they ever wrote.
pub const NEVER_SENT_PREFIX: &str = "no send: ";

pub use engine_types::wal::OrderEnding as Ending;

#[derive(Clone, Debug)]
pub struct OrderRec {
    pub request: OrderRequest,
    pub entry_work: Option<engine_types::WorkPolicy>,
    pub wire_ns: u64,
    pub acked: bool,
    pub fill_quantity: engine_types::wal::OrderFillQuantity,
    pub ending: Option<Ending>,
    pub terminal_checkpoint_ms: Option<i64>,
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
    pub exact_price_range: engine_types::wal::ExactPriceRange,
}

impl OrderRec {
    pub fn in_flight(&self) -> bool {
        self.ending.is_none()
    }
    pub fn price_is_ambiguous(&self) -> bool {
        self.exact_price_range.low != self.exact_price_range.high
    }
    pub fn retain_at(&self, now_ms: i64, history_through_ms: i64) -> bool {
        self.in_flight()
            || self.terminal_checkpoint_ms.is_none_or(|ended| {
                ended
                    >= now_ms
                        .min(history_through_ms)
                        .saturating_sub(crate::execution_ids::RETENTION_MS)
            })
    }
    pub fn snapshot(&self, now_ms: i64) -> engine_types::OpenOrderState {
        engine_types::OpenOrderState {
            request: self.request.clone(),
            entry_work: self.entry_work,
            wire_ns: self.wire_ns,
            arrival_mid: self.arrival_mid,
            acked: self.acked,
            filled_qty: self.filled_qty().expect("validated order fill projection"),
            fill_quantity: Some(self.fill_quantity.clone()),
            reservation_low_px: self.reservation_low_px,
            reservation_high_px: self.reservation_high_px,
            exact_price_range: Some(self.exact_price_range.clone()),
            terminal: self
                .ending
                .clone()
                .map(|ending| engine_types::wal::TerminalOrderState {
                    ending,
                    retained_since_ms: self.terminal_checkpoint_ms.unwrap_or(now_ms),
                }),
        }
    }
}

/// A total-order wrapper for a venue stop price. WAL payloads cannot contain
/// NaNs, but `total_cmp` also makes replay deterministic for every finite bit
/// pattern, including the two representations of zero.
#[derive(Clone, Copy, Debug)]
struct StopPrice(f64);

impl PartialEq for StopPrice {
    fn eq(&self, other: &Self) -> bool {
        self.cmp(other).is_eq()
    }
}

impl Eq for StopPrice {}

impl PartialOrd for StopPrice {
    fn partial_cmp(&self, other: &Self) -> Option<std::cmp::Ordering> {
        Some(self.cmp(other))
    }
}

impl Ord for StopPrice {
    fn cmp(&self, other: &Self) -> std::cmp::Ordering {
        self.0.total_cmp(&other.0)
    }
}

/// The order book of the log itself, rebuilt by reading records in order.
#[derive(Default, Debug)]
pub struct LedgerOfOrders {
    pub orders: BTreeMap<String, OrderRec>,
    terminal_cache_dirty: bool,
    terminal_cache_limits: Option<(usize, usize)>,
    #[cfg(test)]
    terminal_cache_scanned_rows: usize,
    pub boots: u32,
    /// Count of live opening orders per sleeve/symbol. Counts, rather than a
    /// set, ensure one terminal sibling cannot hide another still-live order.
    opening_symbols: BTreeMap<(StrategyId, SymbolId), usize>,
    /// Live opening-stop prices grouped by (symbol, is-short). The engine asks
    /// for the tightest level before every placement batch. Keeping the
    /// multiset here makes that query proportional to active symbols instead
    /// of rescanning the process's entire order history on every decision.
    opening_stop_levels: BTreeMap<(SymbolId, Side), BTreeMap<StopPrice, usize>>,
}

impl LedgerOfOrders {
    pub fn from_records(records: &[WalRecord]) -> Self {
        Self::try_from_records(records).expect("validated order ledger")
    }

    pub fn try_from_records(records: &[WalRecord]) -> Result<Self, String> {
        let mut me = Self::default();
        for record in records {
            me.try_apply(record)?;
        }
        Ok(me)
    }

    pub fn try_apply(&mut self, record: &WalRecord) -> Result<(), String> {
        self.validate_record_quantities(record)?;
        if matches!(
            record,
            WalRecord::OrderSent { .. }
                | WalRecord::OrderLineageRestored { .. }
                | WalRecord::OrderUpdate { .. }
                | WalRecord::RecoveredFill { .. }
                | WalRecord::AmendSent { .. }
                | WalRecord::AmendResolved { .. }
                | WalRecord::SegmentBase { .. }
        ) {
            self.terminal_cache_dirty = true;
        }
        self.apply_validated(record);
        Ok(())
    }

    pub fn apply(&mut self, record: &WalRecord) {
        self.try_apply(record).expect("validated order record");
    }

    fn apply_validated(&mut self, record: &WalRecord) {
        match record {
            WalRecord::Boot { .. } => self.boots += 1,
            WalRecord::OrderSent {
                dispatch,
                request,
                wire_ns,
                arrival_mid,
                ..
            } => {
                let exact_px = limit_px(request);
                self.insert_live_order(
                    request.client_order_id.clone(),
                    OrderRec {
                        request: request.clone(),
                        entry_work: dispatch.as_ref().and_then(|d| d.intent.work),
                        wire_ns: *wire_ns,
                        acked: false,
                        fill_quantity: quantities::initial(request),
                        ending: None,
                        terminal_checkpoint_ms: None,
                        arrival_mid: *arrival_mid,
                        reservation_low_px: exact_px,
                        reservation_high_px: exact_px,
                        exact_price_range: request_price_range(request),
                    },
                );
            }
            WalRecord::OrderUpdate { update, .. } => self.apply_update_validated(update),
            // A fill the private stream never delivered, read back from the
            // venue's own history. It ends its order exactly like a delivered
            // one: without this the working-order pass never retires a filled
            // order and keeps cancelling something the venue has already
            // finished with.
            WalRecord::RecoveredFill {
                client_order_id,
                qty,
                amounts,
                ..
            } => {
                let ended_indexes = if let Some(rec) = self.orders.get_mut(client_order_id.as_str())
                {
                    let was_live = rec.in_flight();
                    let stop = was_live.then(|| opening_stop(&rec.request)).flatten();
                    let opening = was_live.then(|| opening_key(&rec.request)).flatten();
                    rec.acked = true;
                    rec.commit_fill(*qty, amounts.as_ref());
                    (was_live && !rec.in_flight()).then_some((stop, opening))
                } else {
                    None
                };
                if let Some((stop, opening)) = ended_indexes {
                    if let Some(stop) = stop {
                        self.remove_opening_stop(stop);
                    }
                    if let Some(opening) = opening {
                        self.remove_opening_symbol(opening);
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
                        if let engine_types::OrderKind::Limit { .. } = rec.request.kind {
                            // The request may have reached the venue even if
                            // the process died before its answer. Preserve the
                            // full plausible range: high prices dominate
                            // notional, low prices can dominate short-stop loss.
                            let requested = spec
                                .exact_terms
                                .as_deref()
                                .and_then(|terms| terms.limit_price.clone())
                                .unwrap_or_else(|| {
                                    engine_types::numeric::Exact::from_legacy_f64(requested_px)
                                        .expect("validated amendment price")
                                });
                            rec.exact_price_range.low =
                                rec.exact_price_range.low.clone().min(requested.clone());
                            rec.exact_price_range.high =
                                rec.exact_price_range.high.clone().max(requested);
                            rec.reservation_low_px = rec
                                .exact_price_range
                                .low
                                .to_f64()
                                .expect("validated price range");
                            rec.reservation_high_px = rec
                                .exact_price_range
                                .high
                                .to_f64()
                                .expect("validated price range");
                        }
                    }
                }
            }
            WalRecord::AmendResolved {
                client_order_id,
                effective_px,
                exact_effective_px,
            } => {
                if let Some(rec) = self.orders.get_mut(client_order_id) {
                    if rec.in_flight() {
                        if let engine_types::OrderKind::Limit { tif, .. } = rec.request.kind {
                            rec.request.kind = engine_types::OrderKind::Limit {
                                px: *effective_px,
                                tif,
                            };
                            if let Some(terms) = &mut rec.request.exact_terms {
                                terms.limit_price = Some(
                                    exact_effective_px
                                        .as_ref()
                                        .map(|number| number.value.clone())
                                        .unwrap_or_else(|| {
                                            engine_types::numeric::Exact::from_legacy_f64(
                                                *effective_px,
                                            )
                                            .expect("validated effective price")
                                        }),
                                );
                            }
                            rec.reservation_low_px = *effective_px;
                            rec.reservation_high_px = *effective_px;
                            let price = exact_effective_px
                                .as_ref()
                                .map(|number| number.value.clone())
                                .unwrap_or_else(|| {
                                    engine_types::numeric::Exact::from_legacy_f64(*effective_px)
                                        .expect("validated effective price")
                                });
                            rec.exact_price_range = engine_types::wal::ExactPriceRange {
                                low: price.clone(),
                                high: price,
                            };
                        }
                    }
                }
            }
            WalRecord::Note { source, text } if source == "shadow" => {
                if let Some(rest) = text.strip_prefix(NEVER_SENT_PREFIX) {
                    let id = rest.split_whitespace().next().unwrap_or_default();
                    let ended_indexes = if let Some(order) = self.orders.get_mut(id) {
                        let was_live = order.in_flight();
                        let stop = was_live.then(|| opening_stop(&order.request)).flatten();
                        let opening = was_live.then(|| opening_key(&order.request)).flatten();
                        order.ending = Some(Ending::NeverSent);
                        was_live.then_some((stop, opening))
                    } else {
                        None
                    };
                    if let Some((stop, opening)) = ended_indexes {
                        if let Some(stop) = stop {
                            self.remove_opening_stop(stop);
                        }
                        if let Some(opening) = opening {
                            self.remove_opening_symbol(opening);
                        }
                    }
                }
            }
            WalRecord::OrderLineageRestored { order } => self.restore_snapshot_order(order),
            WalRecord::SegmentBase { open_orders, .. } => {
                self.orders.clear();
                self.opening_symbols.clear();
                self.opening_stop_levels.clear();
                for open in open_orders {
                    self.restore_snapshot_order(open);
                }
            }
            _ => {}
        }
    }

    pub fn try_apply_update(&mut self, update: &OrderUpdate) -> Result<(), String> {
        self.try_apply(&WalRecord::OrderUpdate {
            callbacks: None,
            update: update.clone(),
        })
    }

    fn apply_update_validated(&mut self, update: &OrderUpdate) {
        let id = client_order_id(update);
        let Some(id) = id else { return };
        let ended_indexes = {
            let Some(rec) = self.orders.get_mut(id) else {
                return;
            };
            let was_live = rec.in_flight();
            let stop = was_live.then(|| opening_stop(&rec.request)).flatten();
            let opening = was_live.then(|| opening_key(&rec.request)).flatten();
            match update {
                OrderUpdate::Ack(_) => rec.acked = true,
                OrderUpdate::Reject { code, reason, .. } => {
                    rec.terminal_checkpoint_ms = None;
                    rec.ending = Some(Ending::Rejected {
                        code: *code,
                        reason: reason.clone(),
                    })
                }
                OrderUpdate::Cancelled { .. } => {
                    rec.terminal_checkpoint_ms = None;
                    rec.ending = Some(Ending::Cancelled);
                }
                OrderUpdate::Fill { qty, amounts, .. } => {
                    rec.commit_fill(*qty, amounts.as_deref());
                }
                OrderUpdate::FastFill { .. }
                | OrderUpdate::StopAttached { .. }
                | OrderUpdate::StreamReset { .. } => {}
                // News, not bookkeeping. What an amend left the order at is
                // written down by `AmendResolved`, which also narrows the
                // reservation the amend widened; doing half of that here
                // would leave a replay whose price and reservation disagree.
                OrderUpdate::Amended { .. } => {}
            }
            (was_live && !rec.in_flight()).then_some((stop, opening))
        };
        if let Some((stop, opening)) = ended_indexes {
            if let Some(stop) = stop {
                self.remove_opening_stop(stop);
            }
            if let Some(opening) = opening {
                self.remove_opening_symbol(opening);
            }
        }
    }

    fn restore_snapshot_order(&mut self, open: &engine_types::OpenOrderState) {
        let exact_px = limit_px(&open.request);
        self.insert_live_order(
            open.request.client_order_id.clone(),
            OrderRec {
                request: open.request.clone(),
                entry_work: open.entry_work,
                wire_ns: open.wire_ns,
                acked: open.acked,
                fill_quantity: quantities::restore(open),
                ending: open.terminal.as_ref().map(|state| state.ending.clone()),
                terminal_checkpoint_ms: open.terminal.as_ref().map(|state| state.retained_since_ms),
                arrival_mid: open.arrival_mid,
                reservation_low_px: positive_or(open.reservation_low_px, exact_px),
                reservation_high_px: positive_or(open.reservation_high_px, exact_px),
                exact_price_range: restored_price_range(open),
            },
        );
    }

    fn insert_live_order(&mut self, id: String, record: OrderRec) {
        let stop = record
            .in_flight()
            .then(|| opening_stop(&record.request))
            .flatten();
        let opening = record
            .in_flight()
            .then(|| opening_key(&record.request))
            .flatten();
        if let Some(previous) = self.orders.insert(id, record) {
            if previous.in_flight() {
                if let Some(stop) = opening_stop(&previous.request) {
                    self.remove_opening_stop(stop);
                }
                if let Some(opening) = opening_key(&previous.request) {
                    self.remove_opening_symbol(opening);
                }
            }
        }
        if let Some(stop) = stop {
            self.add_opening_stop(stop);
        }
        if let Some(opening) = opening {
            self.add_opening_symbol(opening);
        }
    }

    fn add_opening_symbol(&mut self, key: (StrategyId, SymbolId)) {
        *self.opening_symbols.entry(key).or_default() += 1;
    }

    fn remove_opening_symbol(&mut self, key: (StrategyId, SymbolId)) {
        let remove_key = if let Some(count) = self.opening_symbols.get_mut(&key) {
            if *count > 1 {
                *count -= 1;
                false
            } else {
                true
            }
        } else {
            false
        };
        if remove_key {
            self.opening_symbols.remove(&key);
        }
    }

    fn add_opening_stop(&mut self, (key, price): ((SymbolId, Side), StopPrice)) {
        *self
            .opening_stop_levels
            .entry(key)
            .or_default()
            .entry(price)
            .or_default() += 1;
    }

    fn remove_opening_stop(&mut self, (key, price): ((SymbolId, Side), StopPrice)) {
        let remove_key = if let Some(levels) = self.opening_stop_levels.get_mut(&key) {
            if let Some(count) = levels.get_mut(&price) {
                if *count > 1 {
                    *count -= 1;
                } else {
                    levels.remove(&price);
                }
            }
            levels.is_empty()
        } else {
            false
        };
        if remove_key {
            self.opening_stop_levels.remove(&key);
        }
    }

    pub(crate) fn trim_terminal_cache(
        &mut self,
        capacity: usize,
        byte_capacity: usize,
    ) -> Result<Vec<String>, String> {
        if !self.terminal_cache_dirty
            && self.terminal_cache_limits == Some((capacity, byte_capacity))
        {
            return Ok(Vec::new());
        }
        let mut terminals = self
            .orders
            .iter()
            .filter(|(_, row)| !row.in_flight())
            .map(|(id, row)| {
                Ok((
                    row.wire_ns,
                    id.clone(),
                    serde_json::to_vec(&row.snapshot(0))
                        .map_err(|error| error.to_string())?
                        .len(),
                ))
            })
            .collect::<Result<Vec<_>, String>>()?;
        #[cfg(test)]
        {
            self.terminal_cache_scanned_rows += terminals.len();
        }
        terminals.sort_unstable();
        let mut bytes: usize = terminals.iter().map(|(_, _, size)| size).sum();
        let mut count = terminals.len();
        let mut removed = Vec::new();
        for (_, id, size) in terminals {
            if count <= capacity && bytes <= byte_capacity {
                break;
            }
            self.orders.remove(&id);
            count -= 1;
            bytes -= size;
            removed.push(id);
        }
        self.terminal_cache_dirty = false;
        self.terminal_cache_limits = Some((capacity, byte_capacity));
        Ok(removed)
    }

    #[cfg(test)]
    pub(crate) fn terminal_cache_scanned_rows(&self) -> usize {
        self.terminal_cache_scanned_rows
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
            .and_then(|order| order.request.sleeve_owner())
    }

    /// Check an authoritative fill before it can change any order, risk, or
    /// position state. An unknown id is handled by reconciliation; a known id
    /// has a stronger contract because the WAL says exactly what was sent.
    pub fn validate_fill(
        &self,
        client_order_id: &str,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
    ) -> Result<(), String> {
        if !qty.is_finite() || qty <= 0.0 {
            return Err(format!(
                "reported quantity {qty} is not finite and positive"
            ));
        }
        if !px.is_finite() || px <= 0.0 {
            return Err(format!("reported price {px} is not finite and positive"));
        }

        let Some(order) = self.orders.get(client_order_id) else {
            if client_order_id.starts_with("eng-") {
                return Err(
                    "engine order lineage is archived or unavailable; execution requires recovery"
                        .into(),
                );
            }
            return Ok(());
        };
        if order.request.symbol != symbol {
            return Err(format!(
                "reported symbol {} does not match sent symbol {}",
                symbol.0, order.request.symbol.0
            ));
        }
        if order.request.side != side {
            return Err(format!(
                "reported side {side:?} does not match sent side {:?}",
                order.request.side
            ));
        }
        if !order.request.qty.is_finite() || order.request.qty <= 0.0 || order.filled_qty().is_err()
        {
            return Err("the sent order carries invalid quantity state".to_string());
        }
        let remaining = order.remaining_qty()?;
        if qty > remaining + QTY_EPS {
            return Err(format!(
                "reported quantity {qty} exceeds the order's remaining quantity {remaining}"
            ));
        }
        Ok(())
    }

    pub fn in_flight(&self) -> Vec<&OrderRec> {
        self.iter_in_flight().collect()
    }

    pub fn iter_in_flight(&self) -> impl Iterator<Item = &OrderRec> + '_ {
        self.orders.values().filter(|order| order.in_flight())
    }

    pub fn in_flight_ids(&self) -> Vec<&str> {
        self.orders
            .iter()
            .filter(|(_, o)| o.in_flight())
            .map(|(id, _)| id.as_str())
            .collect()
    }

    /// Distinct sleeve/symbol pairs with at least one live opening order.
    /// Cost is bounded by current live exposure, never WAL/account history.
    pub fn opening_symbols(&self) -> impl Iterator<Item = (StrategyId, SymbolId)> + '_ {
        self.opening_symbols.keys().copied()
    }

    pub fn opening_owned_by_another(&self, mine: StrategyId, symbol: SymbolId) -> bool {
        self.opening_symbols()
            .any(|(owner, held)| held == symbol && owner != mine)
    }

    /// Tightest live opening-order stop per (symbol, is-short). Long
    /// protection tightens upward; short protection tightens downward.
    pub fn tightest_opening_stops(&self) -> impl Iterator<Item = ((SymbolId, Side), f64)> + '_ {
        self.opening_stop_levels.iter().filter_map(|(key, levels)| {
            let (price, _) = if key.1 == Side::Sell {
                levels.first_key_value()
            } else {
                levels.last_key_value()
            }?;
            Some((*key, price.0))
        })
    }
}

fn opening_stop(request: &OrderRequest) -> Option<((SymbolId, Side), StopPrice)> {
    request
        .stop
        .filter(|_| !request.reduce_only)
        .map(|stop| ((request.symbol, request.side), StopPrice(stop.trigger_px)))
}

fn opening_key(request: &OrderRequest) -> Option<(StrategyId, SymbolId)> {
    (!request.is_sleeve_reduction()).then_some((request.strategy, request.symbol))
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
        OrderUpdate::FastFill {
            client_order_id, ..
        } => Some(client_order_id),
        OrderUpdate::Cancelled {
            client_order_id, ..
        } => Some(client_order_id),
        OrderUpdate::Amended {
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
    pub(crate) fn forget(&mut self, client_order_id: &str) {
        self.owner.remove(client_order_id);
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

    pub(crate) fn set_boot_epoch(&mut self, epoch_ms: i64) {
        self.boot_prefix = Self::boot_prefix(epoch_ms);
    }

    pub fn prefix(&self) -> &str {
        &self.boot_prefix
    }
}

fn request_price_range(request: &OrderRequest) -> engine_types::wal::ExactPriceRange {
    let price = request
        .exact_terms
        .as_deref()
        .and_then(|terms| terms.limit_price.clone())
        .unwrap_or_else(|| {
            engine_types::numeric::Exact::from_legacy_f64(limit_px(request))
                .expect("validated limit price")
        });
    engine_types::wal::ExactPriceRange {
        low: price.clone(),
        high: price,
    }
}

fn restored_price_range(open: &engine_types::OpenOrderState) -> engine_types::wal::ExactPriceRange {
    if let Some(range) = &open.exact_price_range {
        return range.clone();
    }
    let request = request_price_range(&open.request);
    let restore = |value: f64, canonical: &engine_types::numeric::Exact| {
        if value <= 0.0 || canonical.to_f64().ok() == Some(value) {
            canonical.clone()
        } else {
            engine_types::numeric::Exact::from_legacy_f64(value)
                .expect("validated legacy price range")
        }
    };
    engine_types::wal::ExactPriceRange {
        low: restore(open.reservation_low_px, &request.low),
        high: restore(open.reservation_high_px, &request.high),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{AmendSpec, OrderAck, OrderKind, Side, StopSpec, SymbolId, TimeInForce};

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
            exact_terms: None,
            sleeve_effect: None,
            close_position: false,
        }
    }

    fn sent(id: &str, qty: f64) -> WalRecord {
        WalRecord::OrderSent {
            dispatch: None,
            request: request(id, qty),
            wire_ns: 1,
            arrival_mid: 0.0,
        }
    }

    fn opening_sent(id: &str, symbol: u16, side: Side, stop: f64) -> WalRecord {
        let mut request = request(id, 1.0);
        request.symbol = SymbolId(symbol);
        request.side = side;
        request.stop = Some(StopSpec { trigger_px: stop });
        WalRecord::OrderSent {
            dispatch: None,
            request,
            wire_ns: 1,
            arrival_mid: 100.0,
        }
    }

    fn fill(id: &str, qty: f64) -> WalRecord {
        WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: String::new(),
                client_order_id: id.into(),
                symbol: SymbolId(0),
                side: Side::Buy,
                qty,
                px: 100.0,
                fee: Some(0.0),
                is_maker: false,
                forced_close: None,
                venue_ts_ms: 0,
                recv_ns: 0,
            },
        }
    }

    fn exact_order_and_fill(qty: &str, part: &str, recovery: bool) -> (WalRecord, WalRecord) {
        use engine_types::numeric::{AssetId, Exact, ExactNumber, ExecutionAmounts};
        use engine_types::order_terms::{ExactOrderTerms, OrderInputPolicy};
        let mut order = sent("exact", qty.parse().unwrap());
        if let WalRecord::OrderSent { request, .. } = &mut order {
            let terms = ExactOrderTerms {
                quantity: Exact::parse_decimal(qty).unwrap(),
                limit_price: None,
                stop_trigger_price: None,
                physical_stop_trigger_price: None,
                input_policy: OrderInputPolicy::StrategyShortestDecimal,
            };
            terms.apply_projection(request).unwrap();
        }
        let amounts = ExecutionAmounts {
            quantity: ExactNumber::venue_decimal(part).unwrap(),
            price: ExactNumber::venue_decimal("100").unwrap(),
            fee: None,
            settlement_asset: AssetId::Unknown,
        };
        let mut execution = if recovery {
            recovered("exact", part.parse().unwrap())
        } else {
            fill("exact", part.parse().unwrap())
        };
        match &mut execution {
            WalRecord::OrderUpdate {
                update:
                    OrderUpdate::Fill {
                        amounts: row, fee, ..
                    },
                ..
            } => {
                *row = Some(Box::new(amounts));
                *fee = None;
            }
            WalRecord::RecoveredFill {
                amounts: row, fee, ..
            } => {
                *row = Some(amounts);
                *fee = None;
            }
            _ => unreachable!(),
        }
        (order, execution)
    }

    #[test]
    fn mixed_fill_frontiers_preserve_snapshot_projection_and_nan_rejection() {
        for (first_exact, second_exact, expected) in [
            (true, true, 0.3_f64),
            (true, false, 0.1_f64 + 0.2),
            (false, true, 0.1_f64 + 0.2),
            (false, false, 0.1_f64 + 0.2),
        ] {
            let (mut order, exact_first) = exact_order_and_fill("1", "0.1", false);
            if !first_exact {
                let WalRecord::OrderSent { request, .. } = &mut order else {
                    unreachable!()
                };
                request.exact_terms = None;
            }
            let first = if first_exact {
                exact_first
            } else {
                fill("exact", 0.1)
            };
            let second = if second_exact {
                exact_order_and_fill("1", "0.2", true).1
            } else {
                recovered("exact", 0.2)
            };
            let mut ledger = LedgerOfOrders::try_from_records(&[order, first, second]).unwrap();
            ledger
                .try_apply(&WalRecord::OrderUpdate {
                    callbacks: None,
                    update: OrderUpdate::Cancelled {
                        client_order_id: "exact".into(),
                        recv_ns: 7,
                    },
                })
                .unwrap();
            let snapshot = ledger.orders["exact"].snapshot(42);
            assert_eq!(snapshot.filled_qty.to_bits(), expected.to_bits());
            assert!(
                matches!(snapshot.terminal.as_ref(), Some(row) if row.ending == Ending::Cancelled)
            );
            let encoded = serde_json::to_string(&snapshot).unwrap();
            println!("projection-{first_exact}-{second_exact}: {encoded}");
            let decoded = serde_json::from_str(&encoded).unwrap();
            let restored = LedgerOfOrders::try_from_records(&[WalRecord::OrderLineageRestored {
                order: decoded,
            }])
            .unwrap();
            assert_eq!(restored.orders["exact"].snapshot(42), snapshot);
            for invalid in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY] {
                let mut corrupt = snapshot.clone();
                corrupt.filled_qty = invalid;
                corrupt.fill_quantity =
                    Some(engine_types::wal::OrderFillQuantity::LegacyBinary64 {
                        quantity: invalid,
                    });
                assert!(
                    LedgerOfOrders::try_from_records(&[WalRecord::OrderLineageRestored {
                        order: corrupt
                    }])
                    .is_err()
                );
            }
        }
    }

    #[test]
    fn exact_partial_fills_do_not_finish_a_small_order_at_the_legacy_epsilon() {
        for recovery in [false, true] {
            let (order, part) = exact_order_and_fill("0.0000000001", "0.00000000004", recovery);
            let mut ledger = LedgerOfOrders::from_records(&[order, part]);
            assert_eq!(
                ledger.in_flight_ids(),
                ["exact"],
                "a real unfilled remainder disappeared after recovery={recovery}"
            );
            assert!(
                ledger.opening_owned_by_another(StrategyId(1), SymbolId(0)),
                "remaining ownership must survive a partial fill"
            );
            let (_, rest) = exact_order_and_fill("0.0000000001", "0.00000000006", !recovery);
            ledger.apply(&rest);
            assert!(ledger.in_flight_ids().is_empty());
        }
    }

    #[tokio::test(start_paused = true)]
    async fn exact_partial_fill_rotation_retains_frontier_and_refuses_corruption() {
        let (engine, _) = crate::tests::lifecycle_test_fixture(vec![]).await;
        let (sent, fill) = exact_order_and_fill("0.0000000001", "0.00000000004", false);
        let ledger = LedgerOfOrders::try_from_records(&[sent, fill]).unwrap();
        let order = &ledger.orders["exact"];
        let mut base = engine.rotation_base(engine_types::clock::wall_ms());
        if let WalRecord::SegmentBase { open_orders, .. } = &mut base {
            open_orders.push(engine_types::OpenOrderState {
                entry_work: None,
                request: order.request.clone(),
                wire_ns: order.wire_ns,
                acked: order.acked,
                filled_qty: order.filled_qty().unwrap(),
                fill_quantity: Some(order.fill_quantity.clone()),
                arrival_mid: order.arrival_mid,
                reservation_low_px: order.reservation_low_px,
                reservation_high_px: order.reservation_high_px,
                exact_price_range: Some(order.exact_price_range.clone()),
                terminal: None,
            });
        }
        let base: WalRecord = serde_json::from_slice(&serde_json::to_vec(&base).unwrap()).unwrap();
        let mut restored = LedgerOfOrders::try_from_records(std::slice::from_ref(&base)).unwrap();
        assert_eq!(
            restored.orders["exact"].remaining_qty().unwrap(),
            0.00000000006
        );
        let (_, too_large) = exact_order_and_fill("0.0000000001", "0.000000000061", true);
        assert!(restored.try_apply(&too_large).is_err());
        assert_eq!(
            restored.orders["exact"].remaining_qty().unwrap(),
            0.00000000006
        );
        let (_, rest) = exact_order_and_fill("0.0000000001", "0.00000000006", true);
        restored.try_apply(&rest).unwrap();
        assert!(restored.in_flight_ids().is_empty());
        let mut corrupt = base.clone();
        if let WalRecord::SegmentBase { open_orders, .. } = &mut corrupt {
            open_orders[0].filled_qty = 0.0;
        }
        assert!(LedgerOfOrders::try_from_records(&[corrupt]).is_err());
        let mut legacy = base;
        if let WalRecord::SegmentBase { open_orders, .. } = &mut legacy {
            open_orders[0].fill_quantity = None;
        }
        let legacy = LedgerOfOrders::try_from_records(&[legacy]).unwrap();
        assert!(
            matches!(legacy.orders["exact"].fill_quantity, engine_types::wal::OrderFillQuantity::LegacyBinary64 { quantity } if quantity == 0.00000000004)
        );
    }

    #[test]
    fn an_ack_alone_leaves_the_order_in_flight() {
        let log = vec![
            sent("a", 1.0),
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Ack(OrderAck {
                    client_order_id: "a".into(),
                    venue_order_id: "v".into(),
                    sent_ns: 0,
                    ack_ns: 5,
                }),
            },
        ];
        let ledger = LedgerOfOrders::from_records(&log);
        assert_eq!(ledger.in_flight_ids(), vec!["a"]);
        assert!(ledger.orders["a"].acked);
    }

    #[test]
    fn in_flight_keeps_live_orders_in_key_order_with_terminal_rows_retained() {
        assert!(LedgerOfOrders::default().in_flight().is_empty());
        let ledger = LedgerOfOrders::from_records(&[
            sent("z-sent", 2.0),
            sent("m-partial", 2.0),
            sent("a-acked", 1.0),
            sent("b-filled", 1.0),
            sent("c-cancelled", 1.0),
            sent("d-rejected", 1.0),
            fill("m-partial", 1.0),
            fill("b-filled", 1.0),
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Ack(OrderAck {
                    client_order_id: "a-acked".into(),
                    venue_order_id: "venue-a".into(),
                    sent_ns: 1,
                    ack_ns: 2,
                }),
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Cancelled {
                    client_order_id: "c-cancelled".into(),
                    recv_ns: 3,
                },
            },
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Reject {
                    client_order_id: "d-rejected".into(),
                    code: 7,
                    reason: "rejected".into(),
                },
            },
        ]);
        assert_eq!(ledger.orders.len(), 6);
        assert_eq!(
            ledger
                .in_flight()
                .iter()
                .map(|order| order.request.client_order_id.as_str())
                .collect::<Vec<_>>(),
            ["a-acked", "m-partial", "z-sent"]
        );
    }

    fn recovered(id: &str, qty: f64) -> WalRecord {
        WalRecord::RecoveredFill {
            callbacks: None,
            allocation: None,
            amounts: None,
            exec_id: format!("e-{id}-{qty}"),
            client_order_id: id.into(),
            symbol: SymbolId(0),
            side: Side::Buy,
            qty,
            px: 100.0,
            fee: Some(0.0),
            is_maker: false,
            forced_close: None,
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
    fn a_known_fill_must_match_the_order_before_it_can_mutate_state() {
        let ledger = LedgerOfOrders::from_records(&[sent("a", 1.0)]);
        assert!(ledger
            .validate_fill("a", SymbolId(0), Side::Buy, 1.0, 100.0)
            .is_ok());

        for (symbol, side, qty, px, reason) in [
            (SymbolId(1), Side::Buy, 1.0, 100.0, "reported symbol"),
            (SymbolId(0), Side::Sell, 1.0, 100.0, "reported side"),
            (SymbolId(0), Side::Buy, f64::NAN, 100.0, "reported quantity"),
            (SymbolId(0), Side::Buy, 1.0, f64::INFINITY, "reported price"),
        ] {
            let error = ledger
                .validate_fill("a", symbol, side, qty, px)
                .expect_err("malformed fill was trusted");
            assert!(error.contains(reason), "{error}");
        }
    }

    #[test]
    fn cumulative_fills_cannot_exceed_the_quantity_that_was_sent() {
        let ledger = LedgerOfOrders::from_records(&[sent("a", 1.0), fill("a", 0.6)]);
        assert!(ledger
            .validate_fill("a", SymbolId(0), Side::Buy, 0.4, 100.0)
            .is_ok());
        let error = ledger
            .validate_fill("a", SymbolId(0), Side::Buy, 0.400_000_01, 100.0)
            .expect_err("an overfill was trusted");
        assert!(error.contains("remaining quantity 0.4"), "{error}");
    }

    #[test]
    fn rejects_and_cancels_end_it() {
        let ledger = LedgerOfOrders::from_records(&[
            sent("a", 1.0),
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Reject {
                    client_order_id: "a".into(),
                    code: 7,
                    reason: "no".into(),
                },
            },
            sent("b", 1.0),
            WalRecord::OrderUpdate {
                callbacks: None,
                update: OrderUpdate::Cancelled {
                    client_order_id: "b".into(),
                    recv_ns: 3,
                },
            },
        ]);
        assert!(ledger.in_flight_ids().is_empty());
    }

    #[test]
    fn live_stop_index_tracks_the_tightest_level_and_order_endings() {
        let mut ledger = LedgerOfOrders::from_records(&[
            opening_sent("long-loose", 3, Side::Buy, 90.0),
            opening_sent("long-tight-a", 3, Side::Buy, 95.0),
            opening_sent("long-tight-b", 3, Side::Buy, 95.0),
            opening_sent("short-loose", 3, Side::Sell, 110.0),
            opening_sent("short-tight", 3, Side::Sell, 105.0),
        ]);
        assert_eq!(
            ledger.tightest_opening_stops().collect::<Vec<_>>(),
            vec![
                ((SymbolId(3), Side::Buy), 95.0),
                ((SymbolId(3), Side::Sell), 105.0)
            ]
        );

        ledger.apply(&fill("long-tight-a", 1.0));
        assert_eq!(
            ledger.tightest_opening_stops().collect::<Vec<_>>(),
            vec![
                ((SymbolId(3), Side::Buy), 95.0),
                ((SymbolId(3), Side::Sell), 105.0)
            ],
            "a duplicate tight level remains live"
        );
        ledger.apply(&fill("long-tight-b", 1.0));
        ledger.apply(&WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Cancelled {
                client_order_id: "short-tight".into(),
                recv_ns: 3,
            },
        });
        assert_eq!(
            ledger.tightest_opening_stops().collect::<Vec<_>>(),
            vec![
                ((SymbolId(3), Side::Buy), 90.0),
                ((SymbolId(3), Side::Sell), 110.0)
            ]
        );

        ledger.apply(&WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Reject {
                client_order_id: "long-loose".into(),
                code: 7,
                reason: "no".into(),
            },
        });
        ledger.apply(&recovered("short-loose", 1.0));
        assert!(ledger.tightest_opening_stops().next().is_none());
    }

    #[test]
    fn live_opening_symbol_index_counts_siblings_and_drops_every_ending() {
        let mut reduce = request("reduce", 1.0);
        reduce.strategy = StrategyId(4);
        reduce.symbol = SymbolId(9);
        reduce.reduce_only = true;
        let reduce = WalRecord::OrderSent {
            dispatch: None,
            request: reduce,
            wire_ns: 1,
            arrival_mid: 100.0,
        };
        let mut ledger = LedgerOfOrders::from_records(&[
            opening_sent("first", 3, Side::Buy, 90.0),
            opening_sent("second", 3, Side::Buy, 91.0),
            reduce,
        ]);
        assert_eq!(
            ledger.opening_symbols().collect::<Vec<_>>(),
            vec![(StrategyId(0), SymbolId(3))],
            "duplicate siblings produce one bounded heartbeat row and reductions produce none"
        );

        ledger.apply(&fill("first", 1.0));
        assert_eq!(
            ledger.opening_symbols().collect::<Vec<_>>(),
            vec![(StrategyId(0), SymbolId(3))],
            "ending one sibling must not hide the other"
        );
        ledger.apply(&WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Cancelled {
                client_order_id: "second".into(),
                recv_ns: 3,
            },
        });
        assert!(ledger.opening_symbols().next().is_none());

        ledger.apply(&opening_sent("rejected", 7, Side::Sell, 105.0));
        ledger.apply(&WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Reject {
                client_order_id: "rejected".into(),
                code: 7,
                reason: "no".into(),
            },
        });
        ledger.apply(&opening_sent("recovered", 8, Side::Sell, 106.0));
        ledger.apply(&recovered("recovered", 1.0));
        assert!(
            ledger.opening_symbols().next().is_none(),
            "reject and recovered fill both retire their index rows"
        );
    }

    #[test]
    fn replay_reserves_the_worst_price_until_an_amend_is_resolved() {
        let mut original = request("a", 1.0);
        original.kind = OrderKind::Limit {
            px: 100.0,
            tif: TimeInForce::Gtc,
        };
        let sent = WalRecord::OrderSent {
            dispatch: None,
            request: original,
            wire_ns: 1,
            arrival_mid: 100.0,
        };
        let amend = WalRecord::AmendSent {
            symbol: SymbolId(0),
            client_order_id: "a".into(),
            spec: AmendSpec {
                exact_terms: None,
                px: Some(1_000.0),
                qty: None,
            },
            wire_ns: 2,
        };
        let unresolved = LedgerOfOrders::from_records(&[sent.clone(), amend.clone()]);
        assert!(matches!(
            unresolved.orders["a"].request.kind,
            OrderKind::Limit { px: 100.0, .. }
        ));
        assert_eq!(unresolved.orders["a"].reservation_low_px, 100.0);
        assert_eq!(unresolved.orders["a"].reservation_high_px, 1_000.0);

        let rejected = LedgerOfOrders::from_records(&[
            sent.clone(),
            amend.clone(),
            WalRecord::AmendResolved {
                client_order_id: "a".into(),
                effective_px: 100.0,
                exact_effective_px: None,
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
                exact_effective_px: None,
            },
        ]);
        assert!(matches!(
            accepted.orders["a"].request.kind,
            OrderKind::Limit { px: 1_000.0, .. }
        ));
        assert_eq!(accepted.orders["a"].reservation_low_px, 1_000.0);
        assert_eq!(accepted.orders["a"].reservation_high_px, 1_000.0);
    }

    #[tokio::test(start_paused = true)]
    async fn canonical_amend_price_range_survives_rotation_and_rejects_false_bounds() {
        use engine_types::numeric::{Exact, ExactNumber};
        use engine_types::order_terms::{ExactAmendTerms, ExactOrderTerms, OrderInputPolicy};
        let d = |value: &str| Exact::parse_decimal(value).unwrap();
        let (engine, _) = crate::tests::lifecycle_test_fixture(vec![]).await;
        let mut original = request("precise-amend", 1.0);
        original.kind = OrderKind::Limit {
            px: 100.0,
            tif: TimeInForce::Gtc,
        };
        ExactOrderTerms {
            quantity: Exact::one(),
            limit_price: Some(d("100.000000000000000001")),
            stop_trigger_price: None,
            physical_stop_trigger_price: None,
            input_policy: OrderInputPolicy::CanonicalPortfolio,
        }
        .apply_projection(&mut original)
        .unwrap();
        let sent = WalRecord::OrderSent {
            dispatch: None,
            request: original,
            wire_ns: 1,
            arrival_mid: 100.0,
        };
        let amended = WalRecord::AmendSent {
            symbol: SymbolId(0),
            client_order_id: "precise-amend".into(),
            wire_ns: 2,
            spec: AmendSpec {
                qty: None,
                px: Some(100.0),
                exact_terms: Some(Box::new(ExactAmendTerms {
                    quantity: None,
                    limit_price: Some(d("100.000000000000000002")),
                    input_policy: OrderInputPolicy::CanonicalPortfolio,
                })),
            },
        };
        let ledger = LedgerOfOrders::try_from_records(&[sent, amended]).unwrap();
        let order = &ledger.orders["precise-amend"];
        assert!(
            order.price_is_ambiguous(),
            "distinct wire prices collapsed into one compatibility value"
        );
        assert_eq!(order.exact_price_range.low, d("100.000000000000000001"));
        assert_eq!(order.exact_price_range.high, d("100.000000000000000002"));
        let mut base = engine.rotation_base(engine_types::clock::wall_ms());
        if let WalRecord::SegmentBase { open_orders, .. } = &mut base {
            open_orders.push(engine_types::OpenOrderState {
                entry_work: None,
                request: order.request.clone(),
                wire_ns: order.wire_ns,
                arrival_mid: order.arrival_mid,
                acked: order.acked,
                filled_qty: order.filled_qty().unwrap(),
                fill_quantity: Some(order.fill_quantity.clone()),
                reservation_low_px: order.reservation_low_px,
                reservation_high_px: order.reservation_high_px,
                exact_price_range: Some(order.exact_price_range.clone()),
                terminal: None,
            });
        }
        let base: WalRecord = serde_json::from_slice(&serde_json::to_vec(&base).unwrap()).unwrap();
        let mut restored = LedgerOfOrders::try_from_records(std::slice::from_ref(&base)).unwrap();
        assert_eq!(
            restored.orders["precise-amend"].exact_price_range,
            order.exact_price_range
        );
        restored
            .try_apply(&WalRecord::AmendResolved {
                client_order_id: "precise-amend".into(),
                effective_px: 100.0,
                exact_effective_px: Some(
                    ExactNumber::venue_decimal("100.000000000000000002").unwrap(),
                ),
            })
            .unwrap();
        assert!(!restored.orders["precise-amend"].price_is_ambiguous());
        assert_eq!(
            restored.orders["precise-amend"].exact_price_range.low,
            d("100.000000000000000002")
        );
        let mut corrupt = base;
        if let WalRecord::SegmentBase { open_orders, .. } = &mut corrupt {
            let range = open_orders[0].exact_price_range.as_mut().unwrap();
            range.low = d("99.999999999999999999");
            range.high = range.low.clone();
        }
        assert!(
            LedgerOfOrders::try_from_records(&[corrupt]).is_err(),
            "canonical bounds excluded the known request price"
        );
    }

    #[test]
    fn terminal_lineage_expires_only_after_the_history_window_and_missing_engine_ids_are_unresolved(
    ) {
        let mut ledger = LedgerOfOrders::try_from_records(&[sent("eng-terminal-1", 1.0)]).unwrap();
        ledger
            .try_apply_update(&OrderUpdate::Reject {
                client_order_id: "eng-terminal-1".into(),
                code: 1,
                reason: "refused".into(),
            })
            .unwrap();
        let row = ledger.orders.get_mut("eng-terminal-1").unwrap();
        let retired = 1_000_000;
        row.terminal_checkpoint_ms = Some(retired);
        let after = retired + crate::execution_ids::RETENTION_MS + 1;
        assert!(
            row.retain_at(after, retired),
            "history must cover retention before ownership expires"
        );
        assert!(
            row.retain_at(retired, after),
            "a future cursor cannot outrun wall time"
        );
        assert!(!row.retain_at(after, after));
        let snapshot = row.snapshot(after);
        assert_eq!(
            snapshot.terminal.as_ref().unwrap().retained_since_ms,
            retired
        );
        let empty = LedgerOfOrders::default();
        assert!(empty
            .validate_fill("eng-terminal-1", SymbolId(0), Side::Buy, 0.5, 100.0)
            .unwrap_err()
            .contains("lineage is archived or unavailable"));
        assert!(empty
            .validate_fill("manual-fill", SymbolId(0), Side::Buy, 0.5, 100.0)
            .is_ok());
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
