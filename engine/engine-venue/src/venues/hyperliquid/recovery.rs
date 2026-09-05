use super::*;

pub(super) struct RecoveryClient {
    http: HttpClient,
    account: String,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &HyperliquidGateway) -> Self {
        Self {
            http: gateway.http.clone(),
            account: gateway.address_text(),
        }
    }
    async fn info(&self, body: Value) -> Result<Value, VenueError> {
        let text =
            serde_json::to_string(&body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.http
            .post(PATH_INFO, text, "application/json", &[])
            .await
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
    async fn account_view(&self, symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        // Two reads, issued together: the account, and the open orders that
        // say which positions carry a stop. This venue keeps no stop on the
        // position row, so one read cannot answer both.
        let state = self.info(json!({
            "type": "clearinghouseState",
            "user": self.account,
        }));
        let orders = self.open_orders();
        let (observed_ns, reply) =
            account_scan(futures_util::future::try_join(state, orders)).await;
        let (state, raw_orders) = reply?;
        let orders = serde_json::from_str(raw_orders.get())
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        let exact_stops = crate::account_stops::hyperliquid(raw_orders.get())?;

        let (equity_usdt, available_usdt) = parse_margin(&state)?;
        let stops = stops_by_coin(&orders)?;
        let ids = crate::account_recovery::ids(symbols)?;
        let resolve = |name: &str| ids.get(name).copied();
        let mut positions = parse_positions(&state, &stops, &resolve)?;
        let exact_by_symbol: std::collections::HashMap<_, _> = exact_stops
            .into_iter()
            .filter_map(|(coin, stop)| {
                resolve(&super::super::assets::symbol_of(&coin)).map(|symbol| (symbol, stop))
            })
            .collect();
        for position in &mut positions {
            crate::account_stops::assign(
                position,
                exact_by_symbol
                    .get(&position.symbol)
                    .and_then(|stop| stop.for_side(position.side)),
            )?;
        }

        Ok(AccountView {
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
    ) -> Result<Vec<VenueExecution>, VenueError> {
        // The venue answers at most 2000 fills per query and returns the
        // oldest first, so a long window walks forward from the last fill seen
        // rather than assuming one reply covered it.
        const PAGE_LIMIT: usize = 2000;
        const MAX_PAGES: usize = 20;
        let mut out: Vec<VenueExecution> = Vec::new();
        let mut from = start_ms;
        for _ in 0..MAX_PAGES {
            if from > end_ms {
                return Ok(out);
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
            // Fills already held are dropped by their own id, so a page that
            // overlaps the last one does not double-count.
            for row in rows {
                if !out.iter().any(|held| held.exec_id == row.exec_id) {
                    out.push(row);
                }
            }
            if count < PAGE_LIMIT {
                return Ok(out);
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
        // A truncated history would quietly leave fills missing, which is the
        // one answer this read must never give.
        Err(VenueError::BadReply(format!(
            "fill history still had pages after {MAX_PAGES}"
        )))
    }
}
