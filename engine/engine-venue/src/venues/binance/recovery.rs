use super::*;

pub(super) struct RecoveryClient {
    rest: RestClient,
    budget: crate::shared_budget::SharedBudget,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &BinanceGateway) -> Self {
        Self {
            rest: gateway.rest.clone(),
            budget: gateway.weight_budget.clone(),
        }
    }
    async fn open_algo_orders_raw(
        &self,
        name: &str,
    ) -> Result<Box<serde_json::value::RawValue>, VenueError> {
        let reservation = self.budget.reserve(WEIGHT_OPEN_ORDERS_SYMBOL).await;
        let reply = self
            .rest
            .get_signed_as(PATH_OPEN_ALGO_ORDERS, &[("symbol", name.to_string())])
            .await;
        drop(reservation);
        reply
    }
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for RecoveryClient {
    async fn account_view(&self, symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        let (observed_ns, reply) = account_scan(async {
            let reservation = self.budget.reserve(WEIGHT_ACCOUNT).await;
            let account = self
                .rest
                .get_signed_as::<Box<serde_json::value::RawValue>>(PATH_ACCOUNT, &[])
                .await;
            drop(reservation);
            let raw_account = account?;
            let (exact_amounts, exact_positions) =
                crate::account_numbers::binance(raw_account.get())?;
            let account = serde_json::from_str(raw_account.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            // The position rows say nothing about stops, so the stop book is
            // read beside them and joined in — one open-algo read per held
            // symbol, because "which symbols" is only known from the account.
            // Without the join every position would report itself
            // unprotected, and the engine would act on that.
            let (equity, available, mut positions) = parse_account(
                &account,
                &crate::account_recovery::ids(symbols)?,
                &HashMap::new(),
            )?;
            crate::account_numbers::assign(&mut positions, exact_positions, symbols)?;
            for position in &mut positions {
                let name = symbols
                    .get(position.symbol.idx())
                    .ok_or_else(|| {
                        VenueError::BadReply("account names an unregistered symbol id".into())
                    })?
                    .clone();
                let raw = self.open_algo_orders_raw(&name).await?;
                let stops = crate::account_stops::binance(raw.get())?;
                crate::account_stops::assign(
                    position,
                    stops
                        .get(name.as_str())
                        .and_then(|stop| stop.for_side(position.side)),
                )?;
            }
            Ok::<_, VenueError>((equity, available, positions, exact_amounts))
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
        _symbols: &[Symbol],
        _start_ms: i64,
        _end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        Err(VenueError::BadRequest(
            "Binance execution recovery is unavailable: account trades require a symbol, while \
             account-wide order discovery and order lookup can omit a fill from an ordinary GTC \
             order created more than 90 days ago; a complete account-wide interval cannot be \
             proved"
                .to_string(),
        ))
    }
}
