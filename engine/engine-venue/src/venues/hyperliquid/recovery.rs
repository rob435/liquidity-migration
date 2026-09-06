use super::*;

pub(super) struct RecoveryClient {
    history_progress: std::sync::Arc<std::sync::atomic::AtomicU64>,
    http: HttpClient,
    account: String,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &HyperliquidGateway) -> Self {
        Self {
            history_progress: Default::default(),
            http: gateway.http.clone(),
            account: gateway.address_text(),
        }
    }
    async fn info_as<T: serde::de::DeserializeOwned>(&self, body: Value) -> Result<T, VenueError> {
        let text =
            serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.http
            .post_as(PATH_INFO, text, "application/json", &[])
            .await
    }
    async fn open_orders(&self) -> Result<Box<serde_json::value::RawValue>, VenueError> {
        self.info_as(json!({
            "type": "frontendOpenOrders",
            "user": self.account,
        }))
        .await
    }
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for RecoveryClient {
    fn execution_history_progress(&self) -> Option<std::sync::Arc<std::sync::atomic::AtomicU64>> {
        Some(self.history_progress.clone())
    }
    async fn account_view(&self, symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        // Two reads, issued together: the account, and the open orders that
        // say which positions carry a stop. This venue keeps no stop on the
        // position row, so one read cannot answer both.
        let state = self.info_as::<Box<serde_json::value::RawValue>>(json!({
            "type": "clearinghouseState",
            "user": self.account,
        }));
        let orders = self.open_orders();
        let (observed_ns, reply) =
            account_scan(futures_util::future::try_join(state, orders)).await;
        let (raw_state, raw_orders) = reply?;
        let (exact_amounts, exact_positions) =
            crate::account_numbers::hyperliquid(raw_state.get())?;
        let state = serde_json::from_str(raw_state.get())
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        let orders = serde_json::from_str(raw_orders.get())
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        let exact_stops = crate::account_stops::hyperliquid(raw_orders.get())?;

        let (equity_usdt, available_usdt) = parse_margin(&state)?;
        let stops = stops_by_coin(&orders)?;
        let ids = crate::account_recovery::ids(symbols)?;
        let resolve = |name: &str| ids.get(name).copied();
        let mut positions = parse_positions(&state, &stops, &resolve)?;
        crate::account_numbers::assign(&mut positions, exact_positions, symbols)?;
        let exact_by_symbol: std::collections::HashMap<_, _> = exact_stops
            .into_iter()
            .filter_map(|(coin, stop)| {
                resolve(&super::super::assets::symbol_of(&coin)).map(|symbol| (symbol, stop))
            })
            .collect();
        for position in &mut positions {
            let quantity = position
                .quantity()
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            crate::account_stops::assign(
                position,
                exact_by_symbol
                    .get(&position.symbol)
                    .and_then(|stop| stop.covering(position.side, &quantity)),
            )?;
        }

        Ok(AccountView {
            exact_amounts: Some(Box::new(exact_amounts)),
            equity_usdt,
            available_usdt,
            positions,
            observed_ns,
        })
    }
    async fn executions(
        &self,
        _symbols: &[Symbol],
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        // The venue answers at most 2000 fills per query and returns the
        // oldest first, so a long window walks forward from the last fill seen
        // rather than assuming one reply covered it.
        const PAGE_LIMIT: usize = 2000;
        let mut out =
            engine_types::ExecutionHistoryBuilder::with_progress(self.history_progress.clone());
        let mut from = start_ms;
        let mut boundary = std::collections::BTreeSet::new();
        loop {
            if from > end_ms {
                return crate::account_recovery::finish_history(out).await;
            }
            let page: Vec<Box<serde_json::value::RawValue>> = self
                .info_as(json!({
                    "type": "userFillsByTime",
                    "user": self.account,
                    "startTime": from,
                    "endTime": end_ms,
                }))
                .await?;
            let rows = page
                .iter()
                .map(|row| super::super::execution::decode_raw(row.get()))
                .collect::<Result<Vec<_>, _>>()?;
            let count = rows.len();
            let newest = rows.iter().map(|r| r.venue_ts_ms).max();
            let next_boundary = rows
                .iter()
                .filter(|row| Some(row.venue_ts_ms) == newest)
                .map(|row| row.exec_id.clone())
                .collect();
            let page = rows
                .into_iter()
                .filter(|row| {
                    row.venue_ts_ms >= start_ms
                        && row.venue_ts_ms <= end_ms
                        && !(row.venue_ts_ms == from && boundary.contains(&row.exec_id))
                })
                .collect();
            out = crate::account_recovery::append_history(out, page).await?;
            boundary = next_boundary;
            if count < PAGE_LIMIT {
                return crate::account_recovery::finish_history(out).await;
            }
            match newest {
                // Every fill in a full page shares one millisecond: stepping
                // past it would drop fills, and not stepping loops forever.
                Some(newest) if newest > from => from = newest,
                _ => {
                    return Err(VenueError::BadReply(
                        "a full page of fills shares one timestamp, so the history cannot be \
                         walked without losing some"
                            .to_string(),
                    ))
                }
            }
        }
    }
}
