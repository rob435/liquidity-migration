use engine_types::strategy_process::{
    CallbackSnapshot, DepthSnapshot, OwnedOrderSnapshot, SymbolSnapshot, MAX_PROCESS_PROPOSAL_BYTES,
};
use engine_types::{StrategyCtx, SymbolId};

use crate::ctx::Ctx;

use super::state::CallbackState;

impl Ctx<'_> {
    pub(crate) fn callback_snapshot(&self) -> Result<CallbackSnapshot, String> {
        let mut snapshot = CallbackSnapshot {
            strategy: self.strategy,
            now_ns: self.now_ns,
            wall_ms: self.wall_ms(),
            entries_enabled: self.entries_enabled(true),
            account: self.account_summary(),
            symbols: Vec::new(),
            orders: Vec::new(),
            global_checkpoint: self.strategy_global_checkpoint().cloned(),
            strategy_names: self.strategy_names.to_vec(),
            strategy_events: Vec::new(),
        };
        // Empty arrays include their brackets; each inserted row adds its
        // encoded bytes and, after the first, one comma.
        let mut bytes = CallbackState::encoded_size(&snapshot)?;
        let mut account_bytes = |value: usize| -> Result<(), String> {
            bytes = bytes
                .checked_add(value)
                .filter(|size| *size <= MAX_PROCESS_PROPOSAL_BYTES)
                .ok_or("strategy callback snapshot exceeds its byte budget")?;
            Ok(())
        };
        for index in 0..self.books.market.table.len() {
            let id = SymbolId(
                u16::try_from(index).map_err(|_| "strategy snapshot symbol overflows its id")?,
            );
            let in_flight = self
                .in_flight_exact(id)
                .map_err(|error| error.to_string())?;
            let row = SymbolSnapshot {
                id,
                name: self.books.market.table.name(id).to_owned(),
                quote: *self.quote(id),
                depth: DepthSnapshot::from(self.depth(id)),
                trades: *self.trade_flow(id),
                ticker: *self.ticker(id),
                instrument: self.instrument(id),
                position: self.position(id),
                foreign_position: self.foreign_position(id),
                my_position: self.my_position(id),
                exact_my_position: Some(Box::new(
                    self.my_position_exact(id)
                        .map_err(|error| error.to_string())?,
                )),
                in_flight: in_flight.to_f64().map_err(|error| error.to_string())?,
                exact_in_flight: Some(Box::new(in_flight)),
                facts: self.my_position_facts(id),
                checkpoint: self.strategy_checkpoint(id).cloned(),
            };
            account_bytes(
                CallbackState::encoded_size(&row)? + usize::from(!snapshot.symbols.is_empty()),
            )?;
            snapshot.symbols.push(row);
        }
        for (id, order) in &self.books.orders.orders {
            if order.request.sleeve_owner() != Some(self.strategy) {
                continue;
            }
            let request = &order.request;
            let row = OwnedOrderSnapshot {
                id: id.clone(),
                symbol: request.symbol,
                side: request.side,
                kind: request.kind,
                qty: request.qty,
                filled_qty: order.filled_qty()?,
                remaining_qty: Some(order.remaining_qty()?),
                reduce_only: request.is_sleeve_reduction(),
                acked: order.acked,
                resting: order.in_flight()
                    && self.books.registry.owner_of(id) == Some(self.strategy),
            };
            account_bytes(
                CallbackState::encoded_size(&row)? + usize::from(!snapshot.orders.is_empty()),
            )?;
            snapshot.orders.push(row);
        }
        for event in self
            .strategy_events
            .values()
            .filter(|event| event.source == self.strategy || event.destination == self.strategy)
        {
            account_bytes(
                CallbackState::encoded_size(event)?
                    + usize::from(!snapshot.strategy_events.is_empty()),
            )?;
            snapshot.strategy_events.push(event.clone());
        }
        Ok(snapshot)
    }
}
