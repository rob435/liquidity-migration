//! Binance's REST calls. Signing happens here so the bytes that are signed
//! are the bytes that go on the wire; the socket itself belongs to
//! [`crate::http`].
//!
//! Every parameter of every method travels in the query string — the venue
//! documents that POST, PUT and DELETE may — so one builder makes the string,
//! one signer covers it, and the two cannot disagree. Three access levels,
//! matching the venue's own security types: public (no key), keyed (the
//! `X-MBX-APIKEY` header alone — the listen-key endpoints), and signed (the
//! header plus `timestamp`, `recvWindow` and the `signature` parameter).
//!
//! Binance answers a business refusal with an HTTP 400 and a `{code, msg}`
//! body — unlike Bybit and MEXC, which say 200 and refuse inside the
//! envelope — so the refusal is recovered from the transport error here,
//! where every reply passes, rather than at each call site.

use engine_types::VenueError;
use serde_json::Value;

use crate::creds::Credentials;
use crate::http::HttpClient;
use crate::venues::binance::parse::refine_rejection;
use crate::venues::binance::sign::{query_string, signed_query, HEADER_KEY, RECV_WINDOW_MS};
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

    /// Unsigned GET, for the public endpoints. Market data needs no key.
    pub(crate) async fn get_public(&self, path: &str, query: &str) -> Result<Value, VenueError> {
        self.http
            .get(path, query, &[])
            .await
            .map_err(refine_rejection)
    }

    /// POST with the API key header and no signature: the venue's
    /// `USER_STREAM` security type, which is the listen-key endpoints only.
    pub(crate) async fn post_keyed(&self, path: &str) -> Result<Value, VenueError> {
        self.http
            .post(path, String::new(), CONTENT_TYPE, &self.key_header())
            .await
            .map_err(refine_rejection)
    }

    /// PUT with the API key header and no signature — the listen-key
    /// keepalive.
    pub(crate) async fn put_keyed(&self, path: &str) -> Result<Value, VenueError> {
        self.http
            .put(path, "", &self.key_header())
            .await
            .map_err(refine_rejection)
    }

    pub(crate) async fn get_signed(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        let query = self.sign(params);
        self.http
            .get(path, &query, &self.key_header())
            .await
            .map_err(refine_rejection)
    }

    /// Signed POST. The parameters ride in the query string and the body is
    /// empty, so the signed bytes are exactly the sent bytes.
    pub(crate) async fn post_signed(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        let query = self.sign(params);
        self.http
            .post(
                &format!("{path}?{query}"),
                String::new(),
                CONTENT_TYPE,
                &self.key_header(),
            )
            .await
            .map_err(refine_rejection)
    }

    pub(crate) async fn put_signed(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        let query = self.sign(params);
        self.http
            .put(path, &query, &self.key_header())
            .await
            .map_err(refine_rejection)
    }

    pub(crate) async fn delete_signed(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        let query = self.sign(params);
        self.http
            .delete(path, &query, &self.key_header())
            .await
            .map_err(refine_rejection)
    }

    /// The full query for one signed request: the caller's parameters, then
    /// `recvWindow` and `timestamp`, then the signature over all of it.
    fn sign(&self, params: &[(&str, String)]) -> String {
        let mut pairs: Vec<(&str, String)> = params.to_vec();
        pairs.push(("recvWindow", RECV_WINDOW_MS.to_string()));
        pairs.push(("timestamp", wall_ms().to_string()));
        signed_query(self.creds.secret(), &query_string(&pairs))
    }

    fn key_header(&self) -> [(&'static str, String); 1] {
        [(HEADER_KEY, self.creds.key().to_string())]
    }
}

const CONTENT_TYPE: &str = "application/x-www-form-urlencoded";
