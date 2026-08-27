//! The pooled HTTPS client every venue sends through: one warm keep-alive
//! connection per host, HTTP/1.1 only, Nagle off.
//!
//! Only the wire lives here. Signing belongs to each venue, because the four
//! do not agree on what is signed or where the proof rides — Bybit signs the
//! exact bytes and puts a hex HMAC in a header, Hyperliquid signs a hash of a
//! msgpack action and puts the signature *inside* the body, Lighter signs a
//! field-element hash and sends the transaction as a form field. What they do
//! agree on is that a signature covers the bytes that actually go out, so a
//! venue hands finished bytes down here and nothing re-serializes them on the
//! way.

use std::time::Duration;

use bytes::Bytes;
use engine_types::VenueError;
use http_body_util::{BodyExt, Full, Limited};
use hyper::body::Body;
use hyper::{Request, Response};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::Value;

/// A reply this slow is no use to a trading loop; the caller is told the
/// send failed and the log already holds the intent.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);
const MAX_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

pub(crate) struct HttpClient {
    client: Client<HttpsConnector<HttpConnector>, Full<Bytes>>,
    base: String,
    request_timeout: Duration,
}

impl HttpClient {
    pub(crate) fn new(base: impl Into<String>) -> Self {
        crate::tls::install_crypto_provider();

        let mut http = HttpConnector::new();
        // The order path is small writes on a warm socket: do not wait to
        // coalesce them.
        http.set_nodelay(true);
        http.set_keepalive(Some(Duration::from_secs(60)));
        // The TLS connector owns scheme checking; the inner one must not
        // refuse https URIs.
        http.enforce_http(false);

        let https = HttpsConnectorBuilder::new()
            .with_webpki_roots()
            .https_or_http()
            .enable_http1()
            .wrap_connector(http);

        // No http2 feature is compiled in, so this is HTTP/1.1 by
        // construction. Idle sockets are kept long enough that a quiet spell
        // does not cost a fresh TLS handshake on the next order.
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(600))
            .pool_max_idle_per_host(8)
            .build(https);

        Self {
            client,
            base: base.into(),
            request_timeout: REQUEST_TIMEOUT,
        }
    }

    /// The host this client actually sends to. Read back by the live gateway
    /// constructors to check the realm and the host agree.
    pub(crate) fn base(&self) -> &str {
        &self.base
    }

    pub(crate) fn url(&self, path: &str, query: &str) -> String {
        if query.is_empty() {
            format!("{}{}", self.base, path)
        } else {
            format!("{}{}?{}", self.base, path, query)
        }
    }

    pub(crate) async fn get(
        &self,
        path: &str,
        query: &str,
        headers: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        let mut req = Request::builder().method("GET").uri(self.url(path, query));
        for (name, value) in headers {
            req = req.header(*name, value);
        }
        let req = req
            .body(Full::new(Bytes::new()))
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.send(req).await
    }

    /// POST the exact bytes given. The caller has already signed them, so
    /// nothing here may touch them.
    pub(crate) async fn post(
        &self,
        path: &str,
        body: String,
        content_type: &str,
        headers: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        let mut req = Request::builder()
            .method("POST")
            .uri(self.url(path, ""))
            .header("Content-Type", content_type);
        for (name, value) in headers {
            req = req.header(*name, value);
        }
        let req = req
            .body(Full::new(Bytes::from(body)))
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.send(req).await
    }

    async fn send(&self, req: Request<Full<Bytes>>) -> Result<Value, VenueError> {
        let exchange = async {
            let resp = self
                .client
                .request(req)
                .await
                .map_err(|e| VenueError::Transport(e.to_string()))?;
            read_json(resp).await
        };
        match tokio::time::timeout(self.request_timeout, exchange).await {
            Ok(result) => result,
            Err(_) => {
                Err(VenueError::Transport(format!(
                    "request did not complete within {:?}",
                    self.request_timeout
                )))
            }
        }
    }
}

async fn read_json<B>(resp: Response<B>) -> Result<Value, VenueError>
where
    B: Body<Data = Bytes>,
    B::Error: std::error::Error + Send + Sync + 'static,
{
    let status = resp.status();
    let bytes = Limited::new(resp.into_body(), MAX_RESPONSE_BYTES)
        .collect()
        .await
        .map_err(|e| VenueError::Transport(format!("reply body: {e}")))?
        .to_bytes();

    // Venues answer business failures with HTTP 200 and their own status
    // field; a non-2xx status is the edge or the rate limiter, so it is
    // transport. Each venue's parse owns the 200-with-an-error case.
    if !status.is_success() {
        return Err(VenueError::Transport(format!(
            "HTTP {}: {}",
            status.as_u16(),
            snippet(&bytes)
        )));
    }

    serde_json::from_slice(&bytes)
        .map_err(|e| VenueError::BadReply(format!("{e}: {}", snippet(&bytes))))
}

fn snippet(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    text.chars().take(200).collect()
}

/// Percent-encode one query-string value.
///
/// Cursors and addresses come back with characters that mean something in a
/// query string, and a signature covers the query exactly as sent.
pub(crate) fn percent_encode(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for byte in raw.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char)
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    #[test]
    fn query_values_are_escaped_for_the_query_string() {
        assert_eq!(percent_encode("next%3D"), "next%253D");
        assert_eq!(percent_encode("a b&c=d"), "a%20b%26c%3Dd");
        assert_eq!(percent_encode("plain-Cursor_1.0~"), "plain-Cursor_1.0~");
    }

    #[test]
    fn a_url_with_no_query_carries_no_question_mark() {
        let client = HttpClient::new("http://127.0.0.1:1");
        assert_eq!(client.url("/v5/x", ""), "http://127.0.0.1:1/v5/x");
        assert_eq!(client.url("/v5/x", "a=1"), "http://127.0.0.1:1/v5/x?a=1");
    }

    #[tokio::test]
    async fn an_oversized_reply_is_rejected_before_json_parsing() {
        let body = Full::new(Bytes::from(vec![b'x'; MAX_RESPONSE_BYTES + 1]));
        let err = read_json(Response::new(body)).await.unwrap_err();
        assert!(matches!(err, VenueError::Transport(_)));
    }

    #[tokio::test]
    async fn the_deadline_includes_a_stalled_reply_body() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = [0_u8; 1024];
            let _ = socket.read(&mut request).await.unwrap();
            socket
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n")
                .await
                .unwrap();
            std::future::pending::<()>().await;
        });

        let mut client = HttpClient::new(format!("http://{addr}"));
        client.request_timeout = Duration::from_millis(50);
        let err = client.get("/stall", "", &[]).await.unwrap_err();
        assert!(matches!(err, VenueError::Transport(ref text) if text.contains("did not complete")));
        server.abort();
    }
}
