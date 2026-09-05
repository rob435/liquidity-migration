use super::*;

pub(super) struct RecoveryClient {
    http: HttpClient,
    account: AccountKey,
    secret: Scalar,
    catalog: std::sync::RwLock<std::sync::Arc<Markets>>,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &LighterGateway) -> Self {
        Self {
            http: gateway.http.clone(),
            account: gateway.account,
            secret: gateway.secret,
            catalog: std::sync::RwLock::new(std::sync::Arc::new(gateway.markets.clone())),
        }
    }
    async fn get(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        let reply = self.http.get(path, query, &[]).await?;
        venue_result(reply)
    }
    async fn get_signed_as<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        query: &str,
    ) -> Result<T, VenueError> {
        let token = self.auth_token()?;
        self.http
            .get_as(path, query, &[("Authorization", token)])
            .await
    }
    async fn active_orders(&self) -> Result<Box<serde_json::value::RawValue>, VenueError> {
        // No market named, so the venue answers for every market — which is
        // the point of this read: to find orders nobody here placed.
        let query = format!("account_index={}", self.account.account_index);
        self.get_signed_as(PATH_ACTIVE_ORDERS, &query).await
    }
    fn auth_token(&self) -> Result<String, VenueError> {
        let deadline = wall_ms() / 1000 + AUTH_LIFETIME_S;
        let message = format!(
            "{deadline}:{}:{}",
            self.account.account_index, self.account.api_key_index
        );
        let hashed = hash_to_quintic_extension(&order_index::bytes_as_fields(message.as_bytes()));
        let signature = schnorr::sign(&hashed, &self.secret);
        Ok(format!("{message}:{}", hex::encode(signature.to_bytes())))
    }
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for RecoveryClient {
    fn install_instrument_catalog(
        &self,
        catalog: &engine_types::orders::InstrumentCatalog,
    ) -> Result<(), VenueError> {
        let snapshot = catalog
            .cache
            .as_ref()
            .and_then(|cache| cache.as_ref().as_any().downcast_ref::<CatalogSnapshot>())
            .ok_or_else(|| {
                VenueError::BadRequest("recovery catalog belongs to another adapter".into())
            })?;
        if snapshot.base != self.http.base() {
            return Err(VenueError::BadRequest(
                "recovery catalog belongs to another endpoint".into(),
            ));
        }
        *self
            .catalog
            .write()
            .map_err(|_| VenueError::BadReply("recovery catalog lock poisoned".into()))? =
            std::sync::Arc::new(snapshot.data.clone());
        Ok(())
    }
    async fn account_view(&self, symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        let markets = self
            .catalog
            .read()
            .map_err(|_| VenueError::BadReply("recovery catalog lock poisoned".into()))?
            .clone();
        // Two reads, issued together: the account, and the open orders that
        // say which positions carry a stop.
        let account_query = format!("by=index&value={}", self.account.account_index);
        let account = self.get(PATH_ACCOUNT, &account_query);
        let orders = self.active_orders();
        let (observed_ns, reply) =
            account_scan(futures_util::future::try_join(account, orders)).await;
        let (account, raw_orders) = reply?;
        let orders = venue_result(
            serde_json::from_str(raw_orders.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?,
        )?;
        let exact_stops = crate::account_stops::lighter(raw_orders.get())?;

        let (equity_usdt, available_usdt) = parse_margin(&account)?;
        let stops = stops_by_market(&orders)?;
        let ids = crate::account_recovery::ids(symbols)?;
        let resolve = |name: &str| ids.get(name).copied();
        let mut positions = parse_positions(&account, &markets, &stops, &resolve)?;
        let rows = account
            .get("accounts")
            .and_then(Value::as_array)
            .and_then(|rows| rows.first())
            .and_then(|row| row.get("positions"))
            .and_then(Value::as_array)
            .ok_or_else(|| VenueError::BadReply("account position rows missing".into()))?;
        let held_rows: Vec<_> = rows
            .iter()
            .filter(|row| crate::json::num_field(row, "position").is_ok_and(|qty| qty != 0.0))
            .collect();
        if held_rows.len() != positions.len() {
            return Err(VenueError::BadReply(
                "native position stop join is incomplete".into(),
            ));
        }
        for (position, row) in positions.iter_mut().zip(held_rows) {
            let index = i16::try_from(crate::json::int_field(row, "market_id")?)
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            crate::account_stops::assign(
                position,
                exact_stops
                    .get(&index)
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
        let markets = self
            .catalog
            .read()
            .map_err(|_| VenueError::BadReply("recovery catalog lock poisoned".into()))?
            .clone();
        // The venue answers at most this many per query, oldest first, so a
        // busy window walks forward from the last fill seen. On this venue
        // this read is the ONLY way a fill is ever learned — the private feed
        // paces resyncs and carries no fills of its own — so one truncated
        // page is a fill the log never gets.
        const PAGE_LIMIT: usize = 100;
        const MAX_PAGES: usize = 20;
        let mut out: Vec<VenueExecution> = Vec::new();
        let mut from = start_ms;
        for _ in 0..MAX_PAGES {
            if from > end_ms {
                return Ok(out);
            }
            let query = format!(
                "account_index={}&sort_by=timestamp&sort_dir=asc&from={from}&to={end_ms}\
                 &limit={PAGE_LIMIT}",
                self.account.account_index
            );
            let reply: engine_public::numeric_wire::RawObject<
                super::super::execution::HistoryReply,
            > = self.get_signed_as(PATH_TRADES, &query).await?;
            let (rows, count) = reply.0.executions(self.account.account_index, &markets)?;
            let newest = rows.iter().map(|r| r.venue_ts_ms).max();
            // Fills already held are dropped by their own id, so a page that
            // overlaps the last one does not double-count.
            for row in rows {
                if row.venue_ts_ms < start_ms || row.venue_ts_ms > end_ms {
                    continue;
                }
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
