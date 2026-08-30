//! Bybit's signed REST calls. Signing happens here so the bytes that are
//! signed are the bytes that go on the wire; the socket itself belongs to
//! [`crate::http`].

use engine_types::VenueError;
use serde_json::Value;

use crate::creds::Credentials;
use crate::http::HttpClient;
use crate::venues::bybit::sign::{
    rest_signature, HEADER_KEY, HEADER_RECV_WINDOW, HEADER_SIGN, HEADER_TIMESTAMP, RECV_WINDOW_MS,
};
use crate::wall_ms;

pub(crate) struct RestClient {
    http: HttpClient,
    creds: Credentials,
}

impl RestClient {
    pub(crate) fn new(base: impl Into<String>, creds: Credentials) -> Self {
        Self {
            http: HttpClient::new(base),
            creds,
        }
    }

    /// The host this client actually sends to. Read back by the live gateway
    /// constructor to check the realm and the host agree.
    pub(crate) fn base(&self) -> &str {
        self.http.base()
    }

    /// The key these requests are signed with. Not a secret — it goes out in
    /// a header on every signed call — and the account-identity check needs
    /// it to compare against the key the venue says it saw.
    pub(crate) fn api_key(&self) -> &str {
        self.creds.key()
    }

    /// Unsigned GET, for the public endpoints.
    pub(crate) async fn get_public(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        self.http.get(path, query, &[]).await
    }

    /// Signed GET. The signature covers the raw query string.
    pub(crate) async fn get_signed(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), ts, self.creds.key(), query);
        self.http.get(path, query, &self.headers(ts, sign)).await
    }

    /// Signed POST. The signature covers the exact body bytes sent.
    pub(crate) async fn post_signed(&self, path: &str, body: &Value) -> Result<Value, VenueError> {
        let body =
            serde_json::to_string(body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), ts, self.creds.key(), &body);
        self.http
            .post(path, body, "application/json", &self.headers(ts, sign))
            .await
    }

    fn headers(&self, ts: i64, sign: String) -> [(&'static str, String); 4] {
        [
            (HEADER_KEY, self.creds.key().to_string()),
            (HEADER_TIMESTAMP, ts.to_string()),
            (HEADER_RECV_WINDOW, RECV_WINDOW_MS.to_string()),
            (HEADER_SIGN, sign),
        ]
    }
}
