//! The pooled HTTPS client: one warm keep-alive connection per host, HTTP/1.1
//! only, Nagle off. Signing happens here so the bytes that are signed are the
//! bytes that go on the wire.

use std::time::Duration;

use bytes::Bytes;
use engine_types::VenueError;
use http_body_util::{BodyExt, Full};
use hyper::{Request, Response};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::Value;

use crate::clock::wall_ms;
use crate::creds::Credentials;
use crate::sign::{
    rest_signature, HEADER_KEY, HEADER_RECV_WINDOW, HEADER_SIGN, HEADER_TIMESTAMP, RECV_WINDOW_MS,
};

/// A reply this slow is no use to a trading loop; the caller is told the
/// send failed and the log already holds the intent.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(10);

pub(crate) struct RestClient {
    client: Client<HttpsConnector<HttpConnector>, Full<Bytes>>,
    base: String,
    creds: Credentials,
}

impl RestClient {
    pub(crate) fn new(base: impl Into<String>, creds: Credentials) -> Self {
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
            creds,
        }
    }

    /// The key these requests are signed with. Not a secret — it goes out in
    /// a header on every signed call — and the account-identity check needs
    /// it to compare against the key the venue says it saw.
    pub(crate) fn api_key(&self) -> &str {
        self.creds.key()
    }

    fn url(&self, path: &str, query: &str) -> String {
        if query.is_empty() {
            format!("{}{}", self.base, path)
        } else {
            format!("{}{}?{}", self.base, path, query)
        }
    }

    /// Unsigned GET, for the public endpoints.
    pub(crate) async fn get_public(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        let req = Request::builder()
            .method("GET")
            .uri(self.url(path, query))
            .body(Full::new(Bytes::new()))
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.send(req).await
    }

    /// Signed GET. The signature covers the raw query string.
    pub(crate) async fn get_signed(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), ts, self.creds.key(), query);
        let req = Request::builder()
            .method("GET")
            .uri(self.url(path, query))
            .header(HEADER_KEY, self.creds.key())
            .header(HEADER_TIMESTAMP, ts.to_string())
            .header(HEADER_RECV_WINDOW, RECV_WINDOW_MS)
            .header(HEADER_SIGN, sign)
            .body(Full::new(Bytes::new()))
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.send(req).await
    }

    /// Signed POST. The signature covers the exact body bytes sent.
    pub(crate) async fn post_signed(&self, path: &str, body: &Value) -> Result<Value, VenueError> {
        let body = serde_json::to_string(body)
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), ts, self.creds.key(), &body);
        let req = Request::builder()
            .method("POST")
            .uri(self.url(path, ""))
            .header(HEADER_KEY, self.creds.key())
            .header(HEADER_TIMESTAMP, ts.to_string())
            .header(HEADER_RECV_WINDOW, RECV_WINDOW_MS)
            .header(HEADER_SIGN, sign)
            .header("Content-Type", "application/json")
            .body(Full::new(Bytes::from(body)))
            .map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.send(req).await
    }

    async fn send(&self, req: Request<Full<Bytes>>) -> Result<Value, VenueError> {
        let sent = self.client.request(req);
        let resp = match tokio::time::timeout(REQUEST_TIMEOUT, sent).await {
            Ok(Ok(resp)) => resp,
            Ok(Err(e)) => return Err(VenueError::Transport(e.to_string())),
            Err(_) => {
                return Err(VenueError::Transport(format!(
                    "no reply within {}s",
                    REQUEST_TIMEOUT.as_secs()
                )))
            }
        };
        read_json(resp).await
    }
}

async fn read_json(resp: Response<hyper::body::Incoming>) -> Result<Value, VenueError> {
    let status = resp.status();
    let bytes = resp
        .into_body()
        .collect()
        .await
        .map_err(|e| VenueError::Transport(format!("reply body: {e}")))?
        .to_bytes();

    // Bybit answers business failures with HTTP 200 and a non-zero retCode;
    // a non-2xx status is the edge or the rate limiter, so it is transport.
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
