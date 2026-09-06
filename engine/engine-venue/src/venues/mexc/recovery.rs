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
                if execution_page_complete(name, page, raw_count)? {
                    break;
                }
                self.history_progress
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                page = page.checked_add(1).ok_or_else(|| {
                    VenueError::BadReply("execution history page number exhausted".into())
                })?;
            }
        }
        crate::account_recovery::finish_history(out).await
    }
}
