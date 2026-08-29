//! Persistent Bybit WebSocket order entry.

use std::net::SocketAddr;
use std::time::{Duration, Instant};

use engine_types::VenueError;
use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::{lookup_host, TcpStream};
use tokio::sync::{mpsc, oneshot};
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message};
use tokio_tungstenite::{client_async_tls_with_config, MaybeTlsStream, WebSocketStream};

use super::sign::{ws_signature, RECV_WINDOW_MS};
use crate::creds::Credentials;
use crate::{mono_ns, wall_ms};

type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);
const REPLY_TIMEOUT: Duration = Duration::from_secs(10);
const WRITE_TIMEOUT: Duration = Duration::from_secs(10);
const PING_EVERY: Duration = Duration::from_secs(20);
const AUTH_WINDOW_MS: i64 = 5_000;
const CHANNEL_DEPTH: usize = 256;

pub(crate) struct TradeReply {
    pub(crate) data: Value,
    pub(crate) ret_ext_info: Value,
    pub(crate) sent_ns: u64,
    pub(crate) ack_ns: u64,
    /// What the venue says this account's per-second quota for this endpoint
    /// is, from the acknowledgement's own header. `None` when the header is
    /// absent or unreadable, which is the only honest answer: the adapter
    /// then keeps pacing to the documented default rather than to a guess.
    pub(crate) quota_per_second: Option<usize>,
}

enum Command {
    Warm(oneshot::Sender<Result<(), VenueError>>),
    Request {
        req_id: String,
        operation: &'static str,
        args: Vec<Value>,
        reply: oneshot::Sender<Result<TradeReply, VenueError>>,
    },
}

pub(crate) struct TradeClient {
    url: String,
    creds: Credentials,
    commands: Option<mpsc::Sender<Command>>,
    next_request: u64,
}

impl TradeClient {
    pub(crate) fn new(url: &str, creds: Credentials) -> Self {
        Self {
            url: url.to_string(),
            creds,
            commands: None,
            next_request: 1,
        }
    }

    pub(crate) async fn warm(&mut self) -> Result<(), VenueError> {
        let sender = self.sender();
        let (reply, receive) = oneshot::channel();
        sender
            .send(Command::Warm(reply))
            .await
            .map_err(|_| stopped())?;
        receive.await.map_err(|_| stopped())?
    }

    pub(crate) async fn request(
        &mut self,
        operation: &'static str,
        args: Vec<Value>,
    ) -> Result<TradeReply, VenueError> {
        let req_id = format!("eng-{}", self.next_request);
        self.next_request = self.next_request.wrapping_add(1).max(1);
        let sender = self.sender();
        let (reply, receive) = oneshot::channel();
        sender
            .send(Command::Request {
                req_id,
                operation,
                args,
                reply,
            })
            .await
            .map_err(|_| stopped())?;
        receive.await.map_err(|_| stopped())?
    }

    fn sender(&mut self) -> mpsc::Sender<Command> {
        if let Some(sender) = &self.commands {
            return sender.clone();
        }
        let (sender, receiver) = mpsc::channel(CHANNEL_DEPTH);
        let worker = Worker {
            url: self.url.clone(),
            creds: self.creds.clone(),
        };
        tokio::spawn(worker.run(receiver));
        self.commands = Some(sender.clone());
        sender
    }
}

struct Worker {
    url: String,
    creds: Credentials,
}

impl Worker {
    async fn run(self, mut commands: mpsc::Receiver<Command>) {
        let mut socket: Option<Socket> = None;
        let mut next_ping = Instant::now() + PING_EVERY;
        loop {
            if socket.is_none() {
                let Some(command) = commands.recv().await else {
                    return;
                };
                match self.connect().await {
                    Ok(connected) => {
                        socket = Some(connected);
                        next_ping = Instant::now() + PING_EVERY;
                    }
                    Err(error) => {
                        answer_error(command, error);
                        continue;
                    }
                }
                if !self
                    .handle(command, socket.as_mut().expect("connected"))
                    .await
                {
                    socket = None;
                }
                continue;
            }

            let connected = socket.as_mut().expect("checked above");
            tokio::select! {
                command = commands.recv() => {
                    let Some(command) = command else { return; };
                    if !self.handle(command, connected).await {
                        socket = None;
                    }
                }
                frame = connected.next() => {
                    if !idle_frame(connected, frame).await {
                        socket = None;
                    }
                }
                _ = tokio::time::sleep_until(tokio::time::Instant::from_std(next_ping)) => {
                    if send(connected, Message::text(r#"{"op":"ping"}"#)).await.is_err() {
                        socket = None;
                    }
                    next_ping = Instant::now() + PING_EVERY;
                }
            }
        }
    }

    async fn handle(&self, command: Command, socket: &mut Socket) -> bool {
        match command {
            Command::Warm(reply) => {
                let _ = reply.send(Ok(()));
                true
            }
            Command::Request {
                req_id,
                operation,
                args,
                reply,
            } => match request(socket, &req_id, operation, args).await {
                Ok(answer) => {
                    let _ = reply.send(Ok(answer));
                    true
                }
                Err(error) => {
                    // A rejection is an answer. The socket carried it, the
                    // exchange of frames stayed in step, and the next request
                    // can go down the same connection — so an order the venue
                    // declined must not cost the one after it a reconnect and
                    // a re-authentication. Only a transport or decode failure
                    // leaves the stream in a state worth abandoning.
                    let keep = matches!(error, VenueError::Rejected { .. });
                    let _ = reply.send(Err(error));
                    keep
                }
            },
        }
    }

    async fn connect(&self) -> Result<Socket, VenueError> {
        crate::tls::install_crypto_provider();
        let mut socket = tokio::time::timeout(CONNECT_TIMEOUT, connect_ipv4(&self.url))
            .await
            .map_err(|_| VenueError::Transport("trade socket dial timed out".to_string()))??;
        let expires = wall_ms() + AUTH_WINDOW_MS;
        let auth = json!({
            "op": "auth",
            "args": [self.creds.key(), expires, ws_signature(self.creds.secret(), expires)],
        });
        send(&mut socket, Message::text(auth.to_string())).await?;
        let reply = next_json(&mut socket, REPLY_TIMEOUT).await?;
        if successful(&reply) && reply.get("op").and_then(Value::as_str) == Some("auth") {
            tracing::info!("Bybit trade socket authenticated");
            Ok(socket)
        } else {
            Err(reply_error(&reply, "trade socket auth refused"))
        }
    }
}

async fn connect_ipv4(url: &str) -> Result<Socket, VenueError> {
    let request = url
        .into_client_request()
        .map_err(|error| VenueError::Transport(error.to_string()))?;
    let host = request
        .uri()
        .host()
        .ok_or_else(|| VenueError::Transport("trade socket URL has no host".to_string()))?
        .to_string();
    let port = request
        .uri()
        .port_u16()
        .or_else(|| match request.uri().scheme_str() {
            Some("wss") => Some(443),
            Some("ws") => Some(80),
            _ => None,
        })
        .ok_or_else(|| VenueError::Transport("trade socket URL has no port".to_string()))?;

    let addresses = lookup_host((host.as_str(), port))
        .await
        .map_err(|error| VenueError::Transport(error.to_string()))?;
    let mut last_error = None;
    let mut stream = None;
    for address in ipv4_only(addresses) {
        match TcpStream::connect(address).await {
            Ok(connected) => {
                stream = Some(connected);
                break;
            }
            Err(error) => last_error = Some(error),
        }
    }
    let stream = stream.ok_or_else(|| {
        let detail = last_error
            .map(|error| error.to_string())
            .unwrap_or_else(|| "DNS returned no IPv4 address".to_string());
        VenueError::Transport(format!(
            "trade socket IPv4 dial failed for {host}: {detail}"
        ))
    })?;
    stream
        .set_nodelay(true)
        .map_err(|error| VenueError::Transport(error.to_string()))?;
    let peer = stream.peer_addr().ok();
    let (socket, _) = client_async_tls_with_config(request, stream, None, None)
        .await
        .map_err(|error| VenueError::Transport(error.to_string()))?;
    tracing::info!(peer = ?peer, "Bybit trade socket connected over IPv4");
    Ok(socket)
}

fn ipv4_only(addresses: impl Iterator<Item = SocketAddr>) -> impl Iterator<Item = SocketAddr> {
    addresses.filter(SocketAddr::is_ipv4)
}

async fn request(
    socket: &mut Socket,
    req_id: &str,
    operation: &'static str,
    args: Vec<Value>,
) -> Result<TradeReply, VenueError> {
    let timestamp = wall_ms();
    let frame = json!({
        "reqId": req_id,
        "header": {
            "X-BAPI-TIMESTAMP": timestamp.to_string(),
            "X-BAPI-RECV-WINDOW": RECV_WINDOW_MS,
        },
        "op": operation,
        "args": args,
    });
    send(socket, Message::text(frame.to_string())).await?;
    let sent_ns = mono_ns();
    let deadline = tokio::time::Instant::now() + REPLY_TIMEOUT;
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        let reply = next_json(socket, remaining).await?;
        if reply.get("reqId").and_then(Value::as_str) != Some(req_id) {
            continue;
        }
        let ack_ns = mono_ns();
        if successful(&reply) {
            return Ok(TradeReply {
                data: reply.get("data").cloned().unwrap_or(Value::Null),
                ret_ext_info: reply.get("retExtInfo").cloned().unwrap_or(Value::Null),
                sent_ns,
                ack_ns,
                quota_per_second: quota_in(&reply),
            });
        }
        return Err(reply_error(&reply, operation));
    }
}

async fn next_json(socket: &mut Socket, timeout: Duration) -> Result<Value, VenueError> {
    tokio::time::timeout(timeout, async {
        loop {
            let frame = socket
                .next()
                .await
                .ok_or_else(|| VenueError::Transport("trade socket ended".to_string()))?
                .map_err(|error| VenueError::Transport(error.to_string()))?;
            match frame {
                Message::Text(text) => {
                    let value: Value = serde_json::from_str(text.as_str())
                        .map_err(|error| VenueError::BadReply(error.to_string()))?;
                    if matches!(
                        value.get("op").and_then(Value::as_str),
                        Some("ping" | "pong")
                    ) {
                        continue;
                    }
                    return Ok(value);
                }
                Message::Ping(payload) => send(socket, Message::Pong(payload)).await?,
                Message::Pong(_) => {}
                Message::Close(_) => {
                    return Err(VenueError::Transport("trade socket closed".to_string()));
                }
                _ => {}
            }
        }
    })
    .await
    .map_err(|_| VenueError::Transport("trade socket reply timed out".to_string()))?
}

async fn idle_frame(
    socket: &mut Socket,
    frame: Option<Result<Message, tokio_tungstenite::tungstenite::Error>>,
) -> bool {
    match frame {
        Some(Ok(Message::Ping(payload))) => send(socket, Message::Pong(payload)).await.is_ok(),
        Some(Ok(Message::Close(_))) | Some(Err(_)) | None => false,
        _ => true,
    }
}

async fn send(socket: &mut Socket, message: Message) -> Result<(), VenueError> {
    tokio::time::timeout(WRITE_TIMEOUT, socket.send(message))
        .await
        .map_err(|_| VenueError::Transport("trade socket write timed out".to_string()))?
        .map_err(|error| VenueError::Transport(error.to_string()))
}

/// The per-second request quota the venue attaches to an acknowledgement.
///
/// Bybit answers order entry with a `header` block carrying the account's own
/// limit for the endpoint that was called. Reading it is how a market-maker
/// tier stops being invisible: without it the adapter paces forever to the
/// documented default, whatever the account was actually granted.
///
/// Deliberately forgiving about shape. The value is a string today and the
/// header is the venue's to change; anything unreadable is `None`, which
/// leaves the pacing exactly where it was.
fn quota_in(reply: &Value) -> Option<usize> {
    let header = reply.get("header")?;
    let value = ["X-Bapi-Limit", "x-bapi-limit"]
        .into_iter()
        .find_map(|key| header.get(key))?;
    let limit = match value {
        Value::String(text) => text.trim().parse::<u64>().ok()?,
        Value::Number(number) => number.as_u64()?,
        _ => return None,
    };
    (limit > 0).then_some(limit as usize)
}

fn successful(reply: &Value) -> bool {
    reply.get("retCode").and_then(Value::as_i64) == Some(0)
        || reply.get("success").and_then(Value::as_bool) == Some(true)
}

fn reply_error(reply: &Value, context: &str) -> VenueError {
    let code = reply.get("retCode").and_then(Value::as_i64).unwrap_or(-1);
    let message = reply
        .get("retMsg")
        .or_else(|| reply.get("ret_msg"))
        .and_then(Value::as_str)
        .unwrap_or(context)
        .to_string();
    VenueError::Rejected { code, message }
}

fn answer_error(command: Command, error: VenueError) {
    match command {
        Command::Warm(reply) => {
            let _ = reply.send(Err(error));
        }
        Command::Request { reply, .. } => {
            let _ = reply.send(Err(error));
        }
    }
}

fn stopped() -> VenueError {
    VenueError::Transport("trade socket task stopped".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::venues::bybit::realm::VenueRealm;
    use tokio_tungstenite::accept_async;

    #[tokio::test]
    async fn one_authenticated_socket_carries_the_warmup_and_order_request() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("listen");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("connection");
            let mut socket = accept_async(stream).await.expect("websocket");
            let auth = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let auth: Value = serde_json::from_str(&auth).unwrap();
            assert_eq!(auth["op"], "auth");
            assert_eq!(auth["args"][0], "key");
            socket
                .send(Message::text(r#"{"op":"auth","retCode":0}"#))
                .await
                .unwrap();

            let order = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let order: Value = serde_json::from_str(&order).unwrap();
            assert_eq!(order["op"], "order.create");
            assert_eq!(order["args"][0]["symbol"], "BTCUSDT");
            assert!(order["header"]["X-BAPI-TIMESTAMP"].as_str().is_some());
            let req_id = order["reqId"].as_str().unwrap();
            socket
                .send(Message::text(
                    json!({
                        "reqId": req_id,
                        "op": "order.create",
                        "retCode": 0,
                        "data": {"orderId": "venue-1", "orderLinkId": "client-1"},
                        "retExtInfo": {}
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
        });

        let creds = VenueRealm::Mainnet.credentials_for_test("key", "secret");
        let mut client = TradeClient::new(&format!("ws://{address}"), creds);
        client.warm().await.expect("authenticated warm socket");
        let reply = client
            .request(
                "order.create",
                vec![json!({"category":"linear", "symbol":"BTCUSDT"})],
            )
            .await
            .expect("order reply");
        assert_eq!(reply.data["orderId"], "venue-1");
        assert!(reply.sent_ns > 0);
        assert!(reply.ack_ns >= reply.sent_ns);
        server.await.unwrap();
    }

    #[test]
    fn the_acknowledgement_states_this_accounts_own_request_quota() {
        // Bybit answers order entry with the account's limit for the endpoint
        // it just called. An account on a market-maker tier says a bigger
        // number here than the documented default, and reading it is the only
        // way the adapter learns it may go faster.
        assert_eq!(
            quota_in(&json!({
                "reqId": "eng-1",
                "retCode": 0,
                "header": {
                    "X-Bapi-Limit": "20",
                    "X-Bapi-Limit-Status": "19",
                    "X-Bapi-Limit-Reset-Timestamp": "1672217748000"
                }
            })),
            Some(20)
        );
        // The venue owns this header's shape. Anything unreadable is no
        // information, never a guess, because a wrong number here paces the
        // whole order path.
        assert_eq!(quota_in(&json!({"retCode": 0})), None);
        assert_eq!(quota_in(&json!({"header": {}})), None);
        assert_eq!(quota_in(&json!({"header": {"X-Bapi-Limit": ""}})), None);
        assert_eq!(quota_in(&json!({"header": {"X-Bapi-Limit": "0"}})), None);
        assert_eq!(quota_in(&json!({"header": {"X-Bapi-Limit": "lots"}})), None);
        assert_eq!(quota_in(&json!({"header": {"X-Bapi-Limit": -5}})), None);
        // A number rather than a string reads the same, so a venue that stops
        // quoting the value does not silently switch the pacing back to the
        // documented default.
        assert_eq!(
            quota_in(&json!({"header": {"X-Bapi-Limit": 100}})),
            Some(100)
        );
    }

    #[tokio::test]
    async fn a_rejected_order_leaves_the_socket_up_for_the_next_one() {
        // A declined order is an answer, not a broken pipe. Dropping the
        // connection over one would make the next order pay a reconnect and a
        // re-authentication for somebody else's mistake.
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("listen");
        let address = listener.local_addr().expect("address");
        let server = tokio::spawn(async move {
            let (stream, _) = listener.accept().await.expect("connection");
            let mut socket = accept_async(stream).await.expect("websocket");
            // One auth, and then both orders down the same socket: a second
            // connection would leave this accept loop waiting forever.
            let auth = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let auth: Value = serde_json::from_str(&auth).unwrap();
            assert_eq!(auth["op"], "auth");
            socket
                .send(Message::text(r#"{"op":"auth","retCode":0}"#))
                .await
                .unwrap();

            for (code, order_id) in [(110001, ""), (0, "venue-2")] {
                let text = socket.next().await.unwrap().unwrap().into_text().unwrap();
                let sent: Value = serde_json::from_str(&text).unwrap();
                let req_id = sent["reqId"].as_str().unwrap();
                socket
                    .send(Message::text(
                        json!({
                            "reqId": req_id,
                            "op": "order.create",
                            "retCode": code,
                            "retMsg": "order does not exist",
                            "data": {"orderId": order_id, "orderLinkId": "client-1"},
                            "retExtInfo": {}
                        })
                        .to_string(),
                    ))
                    .await
                    .unwrap();
            }
        });

        let creds = VenueRealm::Mainnet.credentials_for_test("key", "secret");
        let mut client = TradeClient::new(&format!("ws://{address}"), creds);
        client.warm().await.expect("authenticated warm socket");
        let refused = client
            .request("order.create", vec![json!({"symbol": "BTCUSDT"})])
            .await;
        match refused {
            Err(VenueError::Rejected { code, .. }) => assert_eq!(code, 110001),
            Err(other) => panic!("expected a venue rejection, got {other}"),
            Ok(_) => panic!("the venue declined this order"),
        }
        // Bounded, because the failure this pins is a reconnect: the test
        // server accepts once, so a client that drops the socket waits for an
        // accept that never comes. Without the bound that reads as a hung
        // suite instead of a failed assertion.
        let accepted = tokio::time::timeout(
            Duration::from_secs(5),
            client.request("order.create", vec![json!({"symbol": "BTCUSDT"})]),
        )
        .await
        .expect("the rejection dropped the socket and the next order is waiting to reconnect")
        .expect("the socket that carried the rejection carries the next order");
        assert_eq!(accepted.data["orderId"], "venue-2");
        server.await.unwrap();
    }

    #[test]
    fn business_errors_keep_the_venue_code_and_message() {
        let error = reply_error(
            &json!({"retCode": 110001, "retMsg": "order does not exist"}),
            "amend",
        );
        assert!(matches!(
            error,
            VenueError::Rejected { code: 110001, ref message }
                if message == "order does not exist"
        ));
    }

    #[test]
    fn the_trade_dialer_discards_every_ipv6_address() {
        let addresses = [
            "[2600:9000::1]:443".parse().unwrap(),
            "192.0.2.1:443".parse().unwrap(),
        ];
        assert_eq!(
            ipv4_only(addresses.into_iter()).collect::<Vec<_>>(),
            addresses[1..]
        );
    }
}
