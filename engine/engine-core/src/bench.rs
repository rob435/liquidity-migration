//! `engine bench`: the whole chain, on this box, with the real loop.
//!
//! Nothing here is a simulation of the engine — it is the engine, running its
//! own `run()` against parts that stand in for the outside world:
//!
//! - a scripted quote stream instead of Bybit's socket, at a rate you pick;
//! - a **real** venue: a small HTTP server on its own thread, so an order
//!   really is signed, really goes out over a socket, and really waits for a
//!   reply parsed from bytes. It is plain HTTP on localhost, not TLS, so the
//!   venue-side number is short by roughly one TLS record's worth of work;
//! - the file log with its real fsync in the measured chain;
//! - a risk kernel that allows everything, so the number is the loop's, not a
//!   policy's.
//!
//! What it proves: our side of the wire. The venue round trip from this box
//! is about 175 ms and no rebuild changes that.

use std::io::ErrorKind;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::time::Duration;

use engine_types::{
    AccountView, AmendSpec, EngineEvent, Feed, FeedError, InstrumentRule, Intent, MarketEvent,
    MarketFeed, OrderAck, OrderFeed, OrderKind, OrderRequest, OrderUpdate, Quote, RiskKernel,
    RiskVerdict, Side, StopSpec, Strategy, StrategyCtx, StrategyId, Subscription, Symbol, SymbolId,
    VenueCaps, VenueError, VenueGateway, Wal, WalRecord,
};
use sha2::{Digest, Sha256};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::clock;
use crate::config::EngineSection;
use crate::engine::{Engine, EngineError};

use crate::ledger::{pretty, LatencyLedger, Quantiles, Segment};

#[derive(Clone, Debug)]
pub struct BenchOptions {
    pub events: u64,
    /// Quotes per second. Zero means as fast as the loop will take them.
    pub rate: u64,
    /// One order every this many quotes.
    pub every_nth: u64,
    pub symbols: Vec<String>,
    pub wal_path: PathBuf,
}

impl Default for BenchOptions {
    fn default() -> Self {
        BenchOptions {
            events: 20_000,
            rate: 0,
            every_nth: 20,
            symbols: vec!["BTCUSDT".to_string()],
            wal_path: PathBuf::from("engine-bench.wal"),
        }
    }
}

pub struct BenchResult {
    pub events: u64,
    pub orders: u64,
    pub segments: Vec<(Segment, Quantiles)>,
}

impl BenchResult {
    pub fn table(&self) -> String {
        let mut out = String::from(
            "  what happened            count   typical(p50)  slow 1 in 10  slow 1 in 100      worst\n",
        );
        for (segment, q) in &self.segments {
            out.push_str(&format!(
                "  {:<22} {:>7}  {:>12}  {:>12}  {:>13}  {:>9}\n",
                segment.plain_name(),
                q.count,
                pretty(q.p50_ns),
                pretty(q.p90_ns),
                pretty(q.p99_ns),
                pretty(q.max_ns)
            ));
        }
        out
    }

    pub fn as_json(&self) -> String {
        let cells: Vec<String> = self
            .segments
            .iter()
            .map(|(segment, q)| {
                format!(
                    "\"{}\":{{\"count\":{},\"p50_ns\":{},\"p90_ns\":{},\"p99_ns\":{},\"max_ns\":{}}}",
                    segment.plain_name(),
                    q.count,
                    q.p50_ns,
                    q.p90_ns,
                    q.p99_ns,
                    q.max_ns
                )
            })
            .collect();
        format!(
            "{{\"events\":{},\"orders\":{},{}}}",
            self.events,
            self.orders,
            cells.join(",")
        )
    }
}

/// Build everything, run the real loop, read the histograms.
pub async fn run(options: &BenchOptions) -> Result<BenchResult, EngineError> {
    let venue_addr = start_mock_venue()?;
    let settings = EngineSection {
        wal_path: options.wal_path.clone(),
        group_flush_ms: 250,
        account_view_max_age_ms: 60_000,
        shadow: false,
        // Shadow off on purpose: the point is to measure a real send. The
        // venue on the other end is the pretend one started just above.
        target_book_path: None,
    };
    // The real log, so the measured barrier is the shipping fsync path.
    let (wal, _replayed) = engine_wal::WalWriter::open(&options.wal_path)?;
    let strategy = BenchStrategy::new(&options.symbols, options.every_nth);
    let mut engine = Engine::boot(
        &settings,
        &format!("bench-{}-{}", options.events, options.every_nth),
        wal,
        AllowEverything,
        HttpVenue::new(venue_addr, options.symbols.clone()),
        vec![Box::new(strategy)],
        &[],
    )
    .await?;

    let symbols: Vec<SymbolId> = options
        .symbols
        .iter()
        .filter_map(|name| engine.market().table.get(name))
        .collect();
    let mut feed = ScriptedFeed::new(symbols, options.events, options.rate);
    let mut orders = SilentOrderFeed;

    let outcome = engine
        .run(&mut feed, &mut orders, std::future::pending::<()>())
        .await?;

    let result = summarise(engine.ledger(), outcome.market_events, outcome.orders_sent);
    engine.wal.append(&WalRecord::Note {
        source: "bench".into(),
        text: result.as_json(),
    })?;
    engine.wal.flush()?;
    Ok(result)
}

fn summarise(ledger: &LatencyLedger, events: u64, orders: u64) -> BenchResult {
    let segments = [
        Segment::Decide,
        Segment::Durable,
        Segment::Wire,
        Segment::Ack,
        Segment::EndToEnd,
    ]
    .into_iter()
    .map(|segment| (segment, ledger.quantiles(segment)))
    .collect();
    BenchResult {
        events,
        orders,
        segments,
    }
}

// ---------------------------------------------------------------- the parts

/// A quote stream with no venue behind it.
pub struct ScriptedFeed {
    symbols: Vec<SymbolId>,
    remaining: u64,
    sent: u64,
    gap: Option<Duration>,
    start: Option<tokio::time::Instant>,
    px: f64,
}

impl ScriptedFeed {
    pub fn new(symbols: Vec<SymbolId>, events: u64, rate: u64) -> Self {
        ScriptedFeed {
            symbols,
            remaining: events,
            sent: 0,
            gap: if rate > 0 {
                Some(Duration::from_nanos(1_000_000_000 / rate.max(1)))
            } else {
                None
            },
            start: None,
            px: 30_000.0,
        }
    }
}

impl MarketFeed for ScriptedFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self.remaining == 0 || self.symbols.is_empty() {
            return Err(FeedError::Closed);
        }
        if let Some(gap) = self.gap {
            let start = *self.start.get_or_insert_with(tokio::time::Instant::now);
            tokio::time::sleep_until(start + gap * self.sent as u32).await;
        }
        self.remaining -= 1;
        self.sent += 1;
        // A small saw-tooth so the price is not constant.
        self.px += if self.sent.is_multiple_of(2) { 0.5 } else { -0.5 };
        let symbol = self.symbols[(self.sent as usize) % self.symbols.len()];
        Ok(MarketEvent::Quote {
            symbol,
            quote: Quote {
                bid_px: self.px,
                bid_qty: 1.5,
                ask_px: self.px + 0.5,
                ask_qty: 1.5,
                venue_ts_ms: clock::wall_ms(),
                recv_ns: clock::now_ns(),
                seq: self.sent,
            },
        })
    }
}

/// No private stream in the bench: every reply comes back from the send.
pub struct SilentOrderFeed;

impl OrderFeed for SilentOrderFeed {
    async fn next_update(&mut self) -> Result<OrderUpdate, FeedError> {
        std::future::pending().await
    }
}

/// Allows everything, so the bench measures the loop and not a policy.
pub struct AllowEverything;

impl RiskKernel for AllowEverything {
    fn assess(&mut self, intent: &Intent, _account: &AccountView) -> RiskVerdict {
        RiskVerdict::Allow { qty: intent.qty }
    }

    fn on_update(&mut self, _update: &OrderUpdate) {}
}

/// Buys a little every Nth quote, with a stop attached.
pub struct BenchStrategy {
    symbols: Vec<String>,
    every_nth: u64,
    seen: u64,
}

impl BenchStrategy {
    pub fn new(symbols: &[String], every_nth: u64) -> Self {
        BenchStrategy {
            symbols: symbols.to_vec(),
            every_nth: every_nth.max(1),
            seen: 0,
        }
    }
}

impl Strategy for BenchStrategy {
    fn name(&self) -> &str {
        "bench"
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        self.symbols
            .iter()
            .map(|symbol| Subscription {
                symbol: symbol.clone(),
                feed: Feed::Quote,
            })
            .collect()
    }

    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        let EngineEvent::Market(MarketEvent::Quote { symbol, quote }) = event else {
            return;
        };
        self.seen += 1;
        if !self.seen.is_multiple_of(self.every_nth) {
            return;
        }
        ctx.place(Intent {
            strategy: StrategyId(0),
            symbol: *symbol,
            side: Side::Buy,
            qty: 0.01,
            kind: OrderKind::Market,
            stop: Some(StopSpec {
                trigger_px: quote.bid_px * 0.99,
            }),
            reduce_only: false,
            tag: "bench".into(),
            decided_ns: ctx.now_ns(),
        });
    }
}

// ------------------------------------------------------- the pretend venue

/// The client side: signs, writes to a warm socket, reads the reply.
pub struct HttpVenue {
    addr: SocketAddr,
    stream: Option<tokio::net::TcpStream>,
    buf: Vec<u8>,
    symbols: Vec<Symbol>,
    key: Vec<u8>,
}

impl HttpVenue {
    pub fn new(addr: SocketAddr, symbols: Vec<Symbol>) -> Self {
        HttpVenue {
            addr,
            stream: None,
            buf: Vec::with_capacity(8 * 1024),
            symbols,
            key: b"bench-secret-key".to_vec(),
        }
    }

    async fn connect(&mut self) -> Result<(), VenueError> {
        if self.stream.is_some() {
            return Ok(());
        }
        let stream = tokio::net::TcpStream::connect(self.addr)
            .await
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        stream
            .set_nodelay(true)
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        self.stream = Some(stream);
        Ok(())
    }

    async fn call(&mut self, path: &str, body: &str) -> Result<serde_json::Value, VenueError> {
        for attempt in 0..2 {
            self.connect().await?;
            match self.try_call(path, body).await {
                Ok(value) => return Ok(value),
                Err(e) if attempt == 0 => {
                    // A keep-alive connection can be closed under us; one
                    // reconnect, then the error stands.
                    self.stream = None;
                    tracing::debug!(error = %e, "reconnecting to the bench venue");
                }
                Err(e) => return Err(e),
            }
        }
        Err(VenueError::Transport("unreachable".into()))
    }

    async fn try_call(&mut self, path: &str, body: &str) -> Result<serde_json::Value, VenueError> {
        let timestamp = clock::wall_ms();
        let signature = sign(&self.key, &format!("{timestamp}bench5000{body}"));
        let request = format!(
            "POST {path} HTTP/1.1\r\nHost: bench\r\nContent-Type: application/json\r\n\
             X-BAPI-API-KEY: bench\r\nX-BAPI-TIMESTAMP: {timestamp}\r\nX-BAPI-SIGN: {signature}\r\n\
             Content-Length: {}\r\n\r\n{body}",
            body.len()
        );
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| VenueError::Transport("no connection".into()))?;
        stream
            .write_all(request.as_bytes())
            .await
            .map_err(|e| VenueError::Transport(e.to_string()))?;

        self.buf.clear();
        let body = read_http_body(self.stream.as_mut().unwrap(), &mut self.buf).await?;
        serde_json::from_slice(&body).map_err(|e| VenueError::BadReply(e.to_string()))
    }
}

impl VenueGateway for HttpVenue {
    /// The same answers Bybit gives, so the bench walks the same paths the
    /// shipping gateway walks.
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            native_position_stop: true,
            amend_in_place: true,
            post_only: true,
            batch_orders: false,
        }
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let px = match req.kind {
            OrderKind::Limit { px, .. } => px,
            OrderKind::Market => 0.0,
        };
        let body = format!(
            "{{\"category\":\"linear\",\"symbol\":\"{}\",\"side\":\"{:?}\",\"qty\":\"{}\",\
             \"price\":\"{px}\",\"orderLinkId\":\"{}\",\"reduceOnly\":{}}}",
            req.symbol.0, req.side, req.qty, req.client_order_id, req.reduce_only
        );
        let reply = self.call("/v5/order/create", &body).await?;
        let ack_ns = clock::now_ns();
        let code = reply.get("retCode").and_then(|v| v.as_i64()).unwrap_or(-1);
        if code != 0 {
            return Err(VenueError::Rejected {
                code,
                message: reply
                    .get("retMsg")
                    .and_then(|v| v.as_str())
                    .unwrap_or("no message")
                    .to_string(),
            });
        }
        let venue_order_id = reply
            .pointer("/result/orderId")
            .and_then(|v| v.as_str())
            .ok_or_else(|| VenueError::BadReply("no orderId".into()))?
            .to_string();
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id,
            ack_ns,
        })
    }

    async fn cancel_order(&mut self, _symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.call("/v5/order/cancel", &format!("{{\"orderLinkId\":\"{id}\"}}"))
            .await
            .map(|_| ())
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        let px = spec.px.map(|px| format!(",\"price\":\"{px}\"")).unwrap_or_default();
        let qty = spec.qty.map(|qty| format!(",\"qty\":\"{qty}\"")).unwrap_or_default();
        self.call(
            "/v5/order/amend",
            &format!("{{\"orderLinkId\":\"{id}\"{px}{qty}}}"),
        )
        .await
        .map(|_| ())
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        self.call(
            "/v5/position/trading-stop",
            &format!("{{\"symbol\":\"{}\",\"stopLoss\":\"{trigger_px}\"}}", symbol.0),
        )
        .await
        .map(|_| ())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        let reply = self.call("/v5/account/wallet-balance", "{}").await?;
        Ok(AccountView {
            equity_usdt: reply
                .pointer("/result/equity")
                .and_then(|v| v.as_f64())
                .unwrap_or(10_000.0),
            available_usdt: reply
                .pointer("/result/available")
                .and_then(|v| v.as_f64())
                .unwrap_or(10_000.0),
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        self.call("/v5/market/instruments-info", "{}").await?;
        Ok(self
            .symbols
            .iter()
            .map(|symbol| {
                (
                    symbol.clone(),
                    InstrumentRule {
                        tick_size: 0.5,
                        qty_step: 0.001,
                        min_qty: 0.001,
                        min_notional: 5.0,
                    },
                )
            })
            .collect())
    }
}

/// Start the pretend venue on its own thread, so it is somewhere else the way
/// a real venue is somewhere else, and the engine's thread does only its own
/// work.
pub fn start_mock_venue() -> Result<SocketAddr, EngineError> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0")
        .map_err(|e| EngineError::Boot(format!("cannot open the bench venue socket: {e}")))?;
    let addr = listener
        .local_addr()
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    listener
        .set_nonblocking(true)
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    std::thread::Builder::new()
        .name("bench-venue".into())
        .spawn(move || {
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("bench venue runtime");
            runtime.block_on(async move {
                let listener = tokio::net::TcpListener::from_std(listener).expect("listener");
                loop {
                    match listener.accept().await {
                        Ok((socket, _)) => {
                            tokio::spawn(serve(socket));
                        }
                        Err(e) => {
                            tracing::warn!(error = %e, "bench venue accept failed");
                            return;
                        }
                    }
                }
            });
        })
        .map_err(|e| EngineError::Boot(e.to_string()))?;
    Ok(addr)
}

async fn serve(mut socket: tokio::net::TcpStream) {
    let _ = socket.set_nodelay(true);
    let mut buf = Vec::with_capacity(8 * 1024);
    let mut orders = 0u64;
    loop {
        buf.clear();
        let request = match read_http_body(&mut socket, &mut buf).await {
            Ok(body) => body,
            Err(_) => return,
        };
        orders += 1;
        let link = serde_json::from_slice::<serde_json::Value>(&request)
            .ok()
            .and_then(|v| {
                v.get("orderLinkId")
                    .and_then(|s| s.as_str())
                    .map(|s| s.to_string())
            })
            .unwrap_or_default();
        let body = format!(
            "{{\"retCode\":0,\"retMsg\":\"OK\",\"result\":{{\"orderId\":\"v{orders}\",\
             \"orderLinkId\":\"{link}\",\"equity\":10000.0,\"available\":9000.0}}}}"
        );
        let reply = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{body}",
            body.len()
        );
        if socket.write_all(reply.as_bytes()).await.is_err() {
            return;
        }
    }
}

/// Read one HTTP message (request or response) and return its body.
async fn read_http_body(
    socket: &mut tokio::net::TcpStream,
    buf: &mut Vec<u8>,
) -> Result<Vec<u8>, VenueError> {
    let mut chunk = [0u8; 4096];
    loop {
        if let Some(head_end) = find_head_end(buf) {
            let length = content_length(&buf[..head_end]);
            let total = head_end + length;
            if buf.len() >= total {
                let body = buf[head_end..total].to_vec();
                buf.drain(..total);
                return Ok(body);
            }
        }
        let read = socket
            .read(&mut chunk)
            .await
            .map_err(|e| VenueError::Transport(e.to_string()))?;
        if read == 0 {
            return Err(VenueError::Transport(
                std::io::Error::from(ErrorKind::UnexpectedEof).to_string(),
            ));
        }
        buf.extend_from_slice(&chunk[..read]);
    }
}

fn find_head_end(buf: &[u8]) -> Option<usize> {
    buf.windows(4).position(|w| w == b"\r\n\r\n").map(|p| p + 4)
}

fn content_length(head: &[u8]) -> usize {
    let text = String::from_utf8_lossy(head);
    for line in text.split("\r\n") {
        let mut parts = line.splitn(2, ':');
        if let (Some(name), Some(value)) = (parts.next(), parts.next()) {
            if name.eq_ignore_ascii_case("content-length") {
                return value.trim().parse().unwrap_or(0);
            }
        }
    }
    0
}

/// HMAC-SHA256, the same signing work the real gateway does, so the bench
/// carries that cost too.
fn sign(key: &[u8], message: &str) -> String {
    let mut block = [0u8; 64];
    if key.len() > 64 {
        let digest = Sha256::digest(key);
        block[..32].copy_from_slice(&digest);
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let mut inner_pad = [0x36u8; 64];
    let mut outer_pad = [0x5cu8; 64];
    for i in 0..64 {
        inner_pad[i] ^= block[i];
        outer_pad[i] ^= block[i];
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(message.as_bytes());
    let inner_digest = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner_digest);
    hex::encode(outer.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signing_is_hmac_sha256() {
        // RFC 4231 test case 1.
        let key = [0x0bu8; 20];
        assert_eq!(
            sign(&key, "Hi There"),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"
        );
    }

    #[test]
    fn http_head_and_length_are_parsed() {
        let head = b"HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\n";
        assert_eq!(find_head_end(head), Some(head.len()));
        assert_eq!(content_length(head), 17);
    }
}
