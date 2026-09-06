use super::*;

pub(super) struct RecoveryClient {
    history_progress: std::sync::Arc<std::sync::atomic::AtomicU64>,
    rest: RestClient,
}
impl RecoveryClient {
    pub(super) fn new(gateway: &BybitGateway) -> Self {
        Self {
            history_progress: Default::default(),
            rest: gateway.rest.clone(),
        }
    }
}

#[engine_types::async_trait]
impl engine_types::orders::AccountRecoveryClient for RecoveryClient {
    fn execution_history_progress(&self) -> Option<std::sync::Arc<std::sync::atomic::AtomicU64>> {
        Some(self.history_progress.clone())
    }
    async fn account_view(&self, symbols: &[Symbol]) -> Result<AccountView, VenueError> {
        // Stamp the beginning of the scan. A private fill received while the
        // REST requests are in flight is not proven present in their snapshot
        // and must remain in the risk kernel's recent-fill overlay.
        let observed_ns = mono_ns();
        // Wallet and positions are separate endpoints, so this is two round
        // trips however it is written — they at least go out together.
        let wallet = self
            .rest
            .get_signed_as::<Box<serde_json::value::RawValue>>(PATH_WALLET, "accountType=UNIFIED");
        let positions = self.rest.get_signed_as::<Box<serde_json::value::RawValue>>(
            PATH_POSITIONS,
            "category=linear&settleCoin=USDT&limit=200",
        );
        let (wallet, positions) = futures_util::future::try_join(wallet, positions).await?;
        let exact_amounts = crate::account_numbers::bybit_wallet(wallet.get())?;
        let wallet =
            serde_json::from_str(wallet.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
        let (equity_usdt, available_usdt) = parse_wallet(&venue_result(wallet)?)?;

        let ids = crate::account_recovery::ids(symbols)?;
        let resolve = |name: &str| ids.get(name).copied();
        let parse_page = |raw: Box<serde_json::value::RawValue>| -> Result<_, VenueError> {
            let body =
                serde_json::from_str(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
            let (mut rows, cursor) = parse_positions(&venue_result(body)?, &resolve)?;
            crate::account_numbers::assign(
                &mut rows,
                crate::account_numbers::bybit_positions(raw.get())?,
                symbols,
            )?;
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
            exact_amounts: Some(Box::new(exact_amounts)),
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
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        // The venue caps one query's window at 7 days, so a longer ask walks
        // in slices. `settleCoin` rather than a symbol, for the same reason
        // as working_orders: this read exists to find what the log missed,
        // and asking only about known symbols would hide exactly that.
        const SLICE_MS: i64 = 6 * 86_400_000;
        let mut out =
            engine_types::ExecutionHistoryBuilder::with_progress(self.history_progress.clone());
        let mut from = start_ms;
        while from < end_ms {
            let to = (from + SLICE_MS).min(end_ms);
            let mut cursor = String::new();
            let mut pages = crate::account_recovery::PageProgress::default();
            let mut payloads = crate::account_recovery::PageProgress::default();
            loop {
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
                let (rows, next, digest) = envelope.0.executions()?;
                if !next.is_empty() {
                    pages.cursor(&next)?;
                    payloads.advance(digest)?;
                }
                out = crate::account_recovery::append_history(out, rows).await?;
                if next.is_empty() {
                    break;
                }
                self.history_progress
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                cursor = next;
            }
            from = to;
        }
        crate::account_recovery::finish_history(out).await
    }
}

#[cfg(test)]
mod history_progress_tests {
    use super::*;
    use engine_types::orders::AccountRecoveryClient;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn repeated_history_payloads_cannot_keep_a_changing_cursor_alive() {
        for exec_type in ["Trade", "Funding"] {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let server = tokio::spawn(async move {
                for page in 1..=3 {
                    let (mut stream, _) = listener.accept().await.unwrap();
                    let mut request = Vec::new();
                    while !request.ends_with(b"\r\n\r\n") {
                        let mut bytes = [0; 1024];
                        let size = stream.read(&mut bytes).await.unwrap();
                        assert!(size > 0 && request.len() < 16_384);
                        request.extend_from_slice(&bytes[..size]);
                    }
                    let row = serde_json::json!({"execType":exec_type,"execId":"same-execution","orderLinkId":"eng-1800000000000-1","symbol":"BTCUSDT","side":"Buy","execQty":"1","execPrice":"100","execFee":"0","isMaker":false,"execTime":"1000"});
                    let cursor = if page == 3 {
                        String::new()
                    } else {
                        format!("different-cursor-{page}")
                    };
                    let body = serde_json::json!({"retCode":0,"result":{"list":[row],"nextPageCursor":cursor}}).to_string();
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
                    crate::creds::Credentials::new("demo", false, "fixture-key", "fixture-secret"),
                ),
            };
            let result = tokio::time::timeout(
                std::time::Duration::from_secs(2),
                client.executions(&[], 1, 2000),
            )
            .await
            .unwrap();
            server.abort();
            assert!(
                matches!(result, Err(VenueError::BadReply(ref error)) if error.contains("repeated a page")),
                "{exec_type} page loop was accepted"
            );
        }
    }
}
