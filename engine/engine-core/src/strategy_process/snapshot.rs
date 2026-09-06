use engine_types::strategy_process::{
    CallbackSnapshot, DepthSnapshot, OwnedOrderSnapshot, SymbolSnapshot, MAX_PROCESS_PROPOSAL_BYTES,
};
use engine_types::{StrategyCtx, SymbolId};

use crate::ctx::Ctx;

use super::state::CallbackState;

impl Ctx<'_> {
    pub(crate) fn callback_snapshot(&self) -> Result<CallbackSnapshot, String> {
        let mut bytes = 0_usize;
        let mut account_bytes = |value: usize| -> Result<(), String> {
            bytes = bytes
                .checked_add(value)
                .filter(|size| *size <= MAX_PROCESS_PROPOSAL_BYTES)
                .ok_or("strategy callback snapshot exceeds its byte budget")?;
            Ok(())
        };
        let mut symbols = Vec::new();
        for index in 0..self.books.market.table.len() {
            let id = SymbolId(
                u16::try_from(index).map_err(|_| "strategy snapshot symbol overflows its id")?,
            );
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
                in_flight: self
                    .in_flight_exact(id)
                    .and_then(|quantity| quantity.to_f64())
                    .map_err(|error| error.to_string())?,
                exact_in_flight: Some(Box::new(
                    self.in_flight_exact(id)
                        .map_err(|error| error.to_string())?,
                )),
                facts: self.my_position_facts(id),
                checkpoint: self.strategy_checkpoint(id).cloned(),
            };
            account_bytes(CallbackState::encoded_size(&row)?)?;
            symbols.push(row);
        }
        let mut orders = Vec::new();
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
                filled_qty: order.filled_qty,
                remaining_qty: Some(order.remaining_qty()?),
                reduce_only: request.is_sleeve_reduction(),
                acked: order.acked,
                resting: order.in_flight()
                    && self.books.registry.owner_of(id) == Some(self.strategy),
            };
            account_bytes(CallbackState::encoded_size(&row)?)?;
            orders.push(row);
        }
        let mut strategy_events = Vec::new();
        for event in self
            .strategy_events
            .values()
            .filter(|event| event.source == self.strategy || event.destination == self.strategy)
        {
            account_bytes(CallbackState::encoded_size(event)?)?;
            strategy_events.push(event.clone());
        }
        let snapshot = CallbackSnapshot {
            strategy: self.strategy,
            now_ns: self.now_ns,
            wall_ms: self.wall_ms(),
            entries_enabled: self.entries_enabled(true),
            account: self.account_summary(),
            symbols,
            orders,
            global_checkpoint: self.strategy_global_checkpoint().cloned(),
            strategy_names: self.strategy_names.to_vec(),
            strategy_events,
        };
        CallbackState::encoded_size(&snapshot)?;
        Ok(snapshot)
    }
}
