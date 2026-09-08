use super::*;

pub(super) struct RecoveryClient {
    history_progress: std::sync::Arc<std::sync::atomic::AtomicU64>,
    rest: RestClient,
    catalog: std::sync::RwLock<std::sync::Arc<Contracts>>,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &MexcGateway) -> Self {
        Self {
            history_progress: Default::default(),
            rest: gateway.rest.clone(),
            catalog: std::sync::RwLock::new(std::sync::Arc::new(gateway.contracts.clone())),
        }
    }
    async fn stop_records(&self) -> Result<Box<serde_json::value::RawValue>, VenueError> {
        self.rest.get_signed_as(PATH_STOP_OPEN, &[]).await
    }
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for RecoveryClient {
    fn execution_history_progress(&self) -> Option<std::sync::Arc<std::sync::atomic::AtomicU64>> {
        Some(self.history_progress.clone())
    }
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
        if snapshot.base != self.rest.base() {
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
        let contracts = self
            .catalog
            .read()
            .map_err(|_| VenueError::BadReply("recovery catalog lock poisoned".into()))?
            .clone();
        let (observed_ns, reply) = account_scan(async {
            let raw_assets = self
                .rest
                .get_signed_as::<Box<serde_json::value::RawValue>>(PATH_ASSETS, &[])
                .await?;
            let exact_amounts = crate::account_numbers::mexc_assets(raw_assets.get())?;
            let assets = serde_json::from_str(raw_assets.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let (equity_usdt, available_usdt) = parse_assets(venue_result(&assets)?)?;
            let raw_positions = self
                .rest
                .get_signed_as::<Box<serde_json::value::RawValue>>(PATH_POSITIONS, &[])
                .await?;
            let exact_positions =
                crate::account_numbers::mexc_positions(raw_positions.get(), &contracts)?;
            let positions_body = serde_json::from_str(raw_positions.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let positions_data = venue_result(&positions_body)?.clone();
            // The position rows say nothing about stops, so the stop book is read
            // alongside and joined in. Without it every position would report
            // itself unprotected, and the engine would act on that.
            let raw_stops = self.stop_records().await?;
            let stop_body = serde_json::from_str(raw_stops.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let stops = parse_position_stops(venue_result(&stop_body)?);
            let exact_stops = crate::account_stops::mexc(raw_stops.get())?;
            let ids = crate::account_recovery::ids(symbols)?;

            let mut positions = parse_positions(&positions_data, &contracts, &ids, &stops)?;
            crate::account_numbers::assign(&mut positions, exact_positions, symbols)?;
            let held_rows: Vec<_> = positions_data
                .as_array()
                .ok_or_else(|| VenueError::BadReply("position list missing".into()))?
                .iter()
                .filter(|row| crate::json::num_field(row, "holdVol").is_ok_and(|qty| qty != 0.0))
                .collect();
            if held_rows.len() != positions.len() {
                return Err(VenueError::BadReply(
                    "native position stop join is incomplete".into(),
                ));
            }
            for (position, row) in positions.iter_mut().zip(held_rows) {
                let position_id = super::super::parse::id_text(row, "positionId");
                crate::account_stops::assign(
                    position,
                    position_id
                        .as_ref()
                        .and_then(|id| exact_stops.get(id))
                        .and_then(|stop| stop.for_side(position.side)),
                )?;
            }
            Ok::<_, VenueError>((equity_usdt, available_usdt, positions, exact_amounts))
        })
        .await;
        let (equity_usdt, available_usdt, positions, exact_amounts) = reply?;
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
        symbols: &[Symbol],
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        // `symbol` is required here, so the sweep is per symbol rather than
        // account-wide. The engine asks about the symbols it follows.
        let names = symbols;
        let contracts = self
            .catalog
            .read()
            .map_err(|_| VenueError::BadReply("recovery catalog lock poisoned".into()))?
            .clone();
        let mut out =
            engine_types::ExecutionHistoryBuilder::with_progress(self.history_progress.clone());
        for name in names {
            let venue_symbol = contracts
                .any(name)
                .ok_or_else(|| {
                    VenueError::BadReply(format!(
                        "configured symbol {name} is absent from the MEXC contract table"
                    ))
                })?
                .venue_symbol
                .clone();
            let mut page = 1u32;
            let mut progress = crate::account_recovery::PageProgress::default();
            loop {
                let body: engine_public::numeric_wire::RawObject<
                    super::super::execution::HistoryReply,
                > = self
                    .rest
                    .get_signed_as(
                        PATH_DEALS,
                        &[
                            ("symbol", venue_symbol.clone()),
                            ("start_time", start_ms.to_string()),
                            ("end_time", end_ms.to_string()),
                            ("page_num", page.to_string()),
                            ("page_size", PAGE_SIZE.to_string()),
                        ],
                    )
                    .await?;

                let (rows, raw_count) = body.0.executions(&contracts)?;
                if raw_count > 0 {
                    progress.rows(&rows)?;
                }
                out = crate::account_recovery::append_history(out, rows).await?;
                // One answered page is progress, rows or no rows. The engine
                // abandons a history read that leaves this counter still for
                // MUTATION_DRAIN_TIMEOUT; this sweep is one signed request per
                // followed symbol, paced at QUOTA_REQUESTS per QUOTA_WINDOW, so
                // an account with no fills in the window would report nothing
                // for the whole sweep and never finish one.
                self.history_progress
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                if execution_page_complete(name, page, raw_count)? {
                    break;
                }
                page = page.checked_add(1).ok_or_else(|| {
                    VenueError::BadReply("execution history page number exhausted".into())
                })?;
            }
        }
        crate::account_recovery::finish_history(out).await
    }
}

#[cfg(test)]
mod history_progress_tests {
    use super::*;
    use engine_types::orders::AccountRecoveryClient;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    /// Three contracts, shaped like `GET /api/v1/contract/detail`.
    const DETAIL: &str = r#"{"success":true,"code":0,"data":[
      {"symbol":"BTC_USDT","baseCoin":"BTC","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":0.0001,"priceUnit":0.1,"minVol":1,"maxVol":400000,
       "limitMaxVol":2500000,"maxLeverage":500,"apiAllowed":true},
      {"symbol":"ETH_USDT","baseCoin":"ETH","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":0.01,"priceUnit":0.01,"minVol":1,"maxVol":400000,
       "limitMaxVol":2500000,"maxLeverage":200,"apiAllowed":true},
      {"symbol":"SOL_USDT","baseCoin":"SOL","quoteCoin":"USDT","settleCoin":"USDT",
       "contractSize":1,"priceUnit":0.01,"minVol":1,"maxVol":400000,
       "limitMaxVol":2500000,"maxLeverage":100,"apiAllowed":true}]}"#;

    /// A sweep over an account with no fills in the window must still report
    /// progress, one count per answered page. The engine's recovery watchdog
    /// reads this counter and abandons a read that leaves it still for
    /// MUTATION_DRAIN_TIMEOUT; the sweep is one paced signed request per
    /// followed symbol, so on the live 132-symbol MEXC realm a sweep that
    /// reported only rows never finished a single pass.
    #[tokio::test]
    async fn an_empty_sweep_reports_progress_for_every_symbol_it_asks_about() {
        let symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"];
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let served = std::sync::Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let counted = served.clone();
        let server = tokio::spawn(async move {
            loop {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                while !request.ends_with(b"\r\n\r\n") {
                    let mut bytes = [0; 1024];
                    let size = stream.read(&mut bytes).await.unwrap();
                    assert!(size > 0 && request.len() < 16_384);
                    request.extend_from_slice(&bytes[..size]);
                }
                counted.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                let body = r#"{"success":true,"code":0,"data":{"resultList":[]}}"#;
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                stream.write_all(response.as_bytes()).await.unwrap();
            }
        });
        let client = RecoveryClient {
            history_progress: Default::default(),
            rest: RestClient::new(
                format!("http://{address}"),
                crate::creds::Credentials::new("mainnet", false, "fixture-key", "fixture-secret"),
            ),
            catalog: std::sync::RwLock::new(std::sync::Arc::new(
                Contracts::parse_raw(DETAIL).unwrap(),
            )),
        };
        let names: Vec<String> = symbols.iter().map(|name| (*name).to_owned()).collect();
        let history = client.executions(&names, 1, 2000).await.unwrap();
        server.abort();
        assert_eq!(history.len(), 0, "the fixture pages carry no executions");
        assert_eq!(
            served.load(std::sync::atomic::Ordering::Relaxed),
            symbols.len(),
            "the sweep asked once per symbol"
        );
        assert_eq!(
            client
                .history_progress
                .load(std::sync::atomic::Ordering::Relaxed),
            symbols.len() as u64,
            "a rowless sweep reported no progress and the engine would abandon it"
        );
    }
}
