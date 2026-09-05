use super::*;

pub(super) struct RecoveryClient {
    rest: RestClient,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &BybitGateway) -> Self {
        Self {
            rest: gateway.rest.clone(),
        }
    }
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for RecoveryClient {
    async fn account_view(&self, symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        // Stamp the beginning of the scan. A private fill received while the
        // REST requests are in flight is not proven present in their snapshot
        // and must remain in the risk kernel's recent-fill overlay.
        let observed_ns = mono_ns();
        // Wallet and positions are separate endpoints, so this is two round
        // trips however it is written — they at least go out together.
        let wallet = self.rest.get_signed(PATH_WALLET, "accountType=UNIFIED");
        let positions = self.rest.get_signed_as::<Box<serde_json::value::RawValue>>(
            PATH_POSITIONS,
            "category=linear&settleCoin=USDT&limit=200",
        );
        let (wallet, positions) = futures_util::future::try_join(wallet, positions).await?;
        let (equity_usdt, available_usdt) = parse_wallet(&venue_result(wallet)?)?;

        let ids = crate::account_recovery::ids(symbols)?;
        let resolve = |name: &str| ids.get(name).copied();
        let parse_page = |raw: Box<serde_json::value::RawValue>| -> Result<_, VenueError> {
            let body =
                serde_json::from_str(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
            let (mut rows, cursor) = parse_positions(&venue_result(body)?, &resolve)?;
            let stops = crate::account_stops::bybit(raw.get())?;
            for row in &mut rows {
                let name = symbols
                    .get(row.symbol.idx())
                    .ok_or_else(|| VenueError::BadReply("position has unknown symbol id".into()))?;
                crate::account_stops::assign(
                    row,
                    stops.get(name).and_then(|stop| stop.for_side(row.side)),
                )?;
            }
            Ok((rows, cursor))
        };
        let (mut open, mut cursor) = parse_page(positions)?;
        let mut pages = 1;
        while !cursor.is_empty() && pages < MAX_PAGES {
            let query = format!(
                "category=linear&settleCoin=USDT&limit=200&cursor={}",
                percent_encode(&cursor)
            );
            let more = self.rest.get_signed_as(PATH_POSITIONS, &query).await?;
            let (rows, next) = parse_page(more)?;
            open.extend(rows);
            cursor = next;
            pages += 1;
        }
        // A truncated position list under-counts exposure and can hide an
        // unprotected position — this read fails closed, like instruments.
        if !cursor.is_empty() {
            return Err(VenueError::BadReply(format!(
                "position listing still had pages after {MAX_PAGES}"
            )));
        }

        Ok(AccountView {
            equity_usdt,
            available_usdt,
            positions: open,
            observed_ns,
        })
    }
    async fn executions(
        &self,
        _symbols: &[Symbol],
        start_ms: i64,
        end_ms: i64,
    ) -> Result<Vec<VenueExecution>, VenueError> {
        // The venue caps one query's window at 7 days, so a longer ask walks
        // in slices. `settleCoin` rather than a symbol, for the same reason
        // as working_orders: this read exists to find what the log missed,
        // and asking only about known symbols would hide exactly that.
        const SLICE_MS: i64 = 6 * 86_400_000;
        let mut out = Vec::new();
        let mut from = start_ms;
        while from < end_ms {
            let to = (from + SLICE_MS).min(end_ms);
            let mut cursor = String::new();
            let mut pages = 0;
            loop {
                if pages >= MAX_PAGES {
                    // A truncated history would quietly leave fills missing,
                    // which is the one answer this read must never give.
                    return Err(VenueError::BadReply(format!(
                        "execution listing still had pages after {MAX_PAGES}"
                    )));
                }
                let query = if cursor.is_empty() {
                    format!(
                        "category={CATEGORY}&settleCoin=USDT&startTime={from}&endTime={to}&limit=100"
                    )
                } else {
                    format!(
                        "category={CATEGORY}&settleCoin=USDT&startTime={from}&endTime={to}\
                         &limit=100&cursor={}",
                        percent_encode(&cursor)
                    )
                };
                let envelope: engine_public::numeric_wire::RawObject<
                    super::super::execution::HistoryReply,
                > = self.rest.get_signed_as(PATH_EXECUTIONS, &query).await?;
                let (rows, next) = envelope.0.executions()?;
                out.extend(rows);
                if next.is_empty() {
                    break;
                }
                cursor = next;
                pages += 1;
            }
            from = to;
        }
        Ok(out)
    }
}
