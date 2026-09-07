use super::*;

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    fn required_portfolio_routes(&self) -> Result<Vec<Subscription>, EngineError> {
        let pending = self
            .host
            .pending
            .iter()
            .chain(self.ready_actions.iter().map(|(action, _)| action))
            .chain(self.dispatches.waiting.iter().map(|(action, _)| action))
            .chain(
                self.deferred_actions
                    .values()
                    .flat_map(|rows| rows.iter().map(|(action, _)| action)),
            )
            .filter_map(|action| crate::portfolio_routes::action_symbol(&action.action));
        crate::portfolio_routes::routes(
            self.books
                .attribution
                .all_symbols()
                .chain(
                    (0..self.host.strategies.len())
                        .flat_map(|index| self.books.covers.symbols(StrategyId(index as u16))),
                )
                .chain(
                    self.books
                        .orders
                        .iter_in_flight()
                        .map(|order| order.request.symbol),
                )
                .chain(
                    self.dispatches
                        .orders
                        .values()
                        .map(|order| order.request.symbol),
                )
                .chain(
                    self.books
                        .account
                        .positions
                        .iter()
                        .filter(|position| position.qty != 0.0)
                        .map(|position| position.symbol),
                )
                .chain(
                    self.portfolio_controls
                        .exits
                        .values()
                        .map(|exit| exit.symbol),
                )
                .chain(self.portfolio_controls.emergencies.keys().copied())
                .chain(self.portfolio_controls.native_pending.keys().copied())
                .chain(self.intended_stops.keys().copied())
                .chain(self.stop_repairs_pending.iter().copied())
                .chain(
                    self.host
                        .effects
                        .transitions
                        .values()
                        .flat_map(|transition| {
                            transition
                                .effects
                                .iter()
                                .enumerate()
                                .filter(|(index, _)| !transition.completed.contains(index))
                                .filter_map(|(_, action)| {
                                    crate::portfolio_routes::action_symbol(action)
                                })
                        }),
                )
                .chain(pending),
            |id| {
                (id.idx() < self.books.market.table.len())
                    .then(|| self.books.market.table.name(id).to_string())
            },
        )
        .map_err(EngineError::State)
    }

    pub(super) fn restore_portfolio_routes(&mut self) -> Result<(), EngineError> {
        self.portfolio_subscriptions = self.required_portfolio_routes()?;
        for route in &self.portfolio_subscriptions {
            if !self.subscriptions.contains(route) {
                self.subscriptions.push(route.clone());
            }
        }
        Ok(())
    }

    pub(super) fn maintain_portfolio_routes<M: MarketFeed>(
        &mut self,
        market: &mut M,
    ) -> Result<(), EngineError> {
        let needed = self.required_portfolio_routes()?;
        fn bit(feed: Feed) -> u8 {
            match feed {
                Feed::Quote => 1,
                Feed::Depth => 2,
                Feed::Trades => 4,
                Feed::Ticker => 8,
            }
        }
        const SUBSCRIBED: usize = 0;
        const RETAINED: usize = 1;
        const REQUIRED: usize = 2;
        let mut membership = vec![[0u8; 3]; self.books.market.table.len()];
        for (group, routes) in [&self.subscriptions, &self.portfolio_subscriptions, &needed]
            .into_iter()
            .enumerate()
        {
            for route in routes {
                if let Some(symbol) = self.books.market.table.get(&route.symbol) {
                    membership[symbol.idx()][group] |= bit(route.feed);
                }
            }
        }
        for route in &needed {
            let expected = self
                .books
                .market
                .table
                .get(&route.symbol)
                .expect("validated portfolio route");
            let feeds = &mut membership[expected.idx()];
            let feed = bit(route.feed);
            if feeds[SUBSCRIBED] & feed != 0 {
                continue;
            }
            match market.admit(&route.symbol, route.feed) {
                Some(id) if id == expected => {
                    feeds[SUBSCRIBED] |= feed;
                    self.subscriptions.push(route.clone());
                }
                Some(id) => {
                    return Err(EngineError::State(format!(
                        "portfolio feed assigned {} to {}, expected {}",
                        id.0, route.symbol, expected.0
                    )))
                }
                None if feeds[RETAINED] & feed == 0 => {
                    tracing::warn!(symbol = %route.symbol, feed = ?route.feed, "portfolio market demand is pending feed admission")
                }
                None => {}
            }
        }
        for route in &self.portfolio_subscriptions {
            let symbol = self
                .books
                .market
                .table
                .get(&route.symbol)
                .expect("retained portfolio route");
            if membership[symbol.idx()][REQUIRED] & bit(route.feed) != 0 {
                continue;
            }
            if !self.routing.listens(symbol, route.feed) && market.retire(&route.symbol, route.feed)
            {
                self.subscriptions.retain(|known| known != route);
            }
        }
        self.portfolio_subscriptions = needed;
        Ok(())
    }
    #[cfg(test)]
    pub(super) fn reference_maintain_portfolio_routes<M: MarketFeed>(
        &mut self,
        market: &mut M,
    ) -> Result<(), EngineError> {
        let needed = self.required_portfolio_routes()?;
        for route in &needed {
            if self.subscriptions.contains(route) {
                continue;
            }
            let expected = self
                .books
                .market
                .table
                .get(&route.symbol)
                .expect("validated portfolio route");
            match market.admit(&route.symbol, route.feed) {
                Some(id) if id == expected => self.subscriptions.push(route.clone()),
                Some(id) => {
                    return Err(EngineError::State(format!(
                        "portfolio feed assigned {} to {}, expected {}",
                        id.0, route.symbol, expected.0
                    )))
                }
                None if !self.portfolio_subscriptions.contains(route) => {
                    tracing::warn!(symbol = %route.symbol, feed = ?route.feed, "portfolio market demand is pending feed admission")
                }
                None => {}
            }
        }
        for route in &self.portfolio_subscriptions {
            if needed.contains(route) {
                continue;
            }
            let symbol = self
                .books
                .market
                .table
                .get(&route.symbol)
                .expect("retained portfolio route");
            if !self.routing.listens(symbol, route.feed) && market.retire(&route.symbol, route.feed)
            {
                self.subscriptions.retain(|known| known != route);
            }
        }
        self.portfolio_subscriptions = needed;
        Ok(())
    }
}
