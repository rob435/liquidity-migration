//! MEXC's signed REST calls. Signing happens here so the bytes that are
//! signed are the bytes that go on the wire; the socket itself belongs to
//! [`crate::http`].
//!
//! The GET path takes its parameters as pairs rather than a ready-made query
//! string, because MEXC signs the query **sorted by key** and a caller that
//! built the string itself could sort it differently from the signer. One
//! function builds it and signs it, so the two cannot disagree.

use std::collections::VecDeque;
use std::sync::Arc;
use std::time::Duration;

use engine_types::VenueError;
use serde_json::Value;
use tokio::time::Instant;

use crate::creds::Credentials;
use crate::http::HttpClient;
use crate::venues::mexc::sign::{
    query_string, rest_signature, HEADER_KEY, HEADER_RECV_WINDOW, HEADER_SIGN, HEADER_TIMESTAMP,
    RECV_WINDOW_S,
};
use crate::wall_ms;

/// MEXC's published quota for its private endpoints: 20 requests per 2
/// seconds, per key. The pacer admits fewer than that so a clock boundary
/// between here and the venue cannot turn an exact admission into a 510.
const QUOTA_WINDOW: Duration = Duration::from_secs(2);
const QUOTA_REQUESTS: usize = 16;

/// A rolling window over every signed request this key sends, shared by every
/// clone of the client: the gateway, its recovery reader and the probe all
/// spend the same budget. Observed live on 2026-09-08: an unpaced sweep of
/// 132 symbols drew `510 Requests are too frequent` on every recovery pass.
#[derive(Debug, Default)]
pub(crate) struct Pacer {
    admissions: tokio::sync::Mutex<VecDeque<Instant>>,
}

impl Pacer {
    /// Wait until one more request fits in the window, then take the slot.
    pub(crate) async fn reserve(&self) {
        loop {
            let wait = {
                let mut admissions = self.admissions.lock().await;
                let now = Instant::now();
                while admissions
                    .front()
                    .is_some_and(|at| now.duration_since(*at) >= QUOTA_WINDOW)
                {
                    admissions.pop_front();
                }
                if admissions.len() < QUOTA_REQUESTS {
                    admissions.push_back(now);
                    return;
                }
                admissions[0] + QUOTA_WINDOW - now
            };
            tokio::time::sleep(wait).await;
        }
    }
}

#[derive(Clone)]
pub(crate) struct RestClient {
    http: HttpClient,
    creds: Credentials,
    pacer: Arc<Pacer>,
}

impl RestClient {
    pub(crate) fn new(base: impl Into<String>, creds: Credentials) -> Self {
        Self {
            http: HttpClient::new(base),
            creds,
            pacer: Arc::new(Pacer::default()),
        }
    }

    /// The host this client actually sends to. Read back by the live gateway
    /// constructor to check the realm and the host agree.
    pub(crate) fn base(&self) -> &str {
        self.http.base()
    }

    /// The key these requests are signed with. Not a secret — it goes out in
    /// a header on every signed call.
    pub(crate) fn api_key(&self) -> &str {
        self.creds.key()
    }

    pub(crate) async fn get_public_as<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        query: &str,
    ) -> Result<T, VenueError> {
        self.http.get_as(path, query, &[]).await
    }

    /// Signed GET. The parameters are sorted once, here, and the same string
    /// is both signed and sent.
    pub(crate) async fn get_signed(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<Value, VenueError> {
        self.pacer.reserve().await;
        let query = query_string(params);
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), self.creds.key(), ts, &query);
        self.http.get(path, &query, &self.headers(ts, sign)).await
    }

    pub(crate) async fn get_signed_as<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        params: &[(&str, String)],
    ) -> Result<T, VenueError> {
        self.pacer.reserve().await;
        let query = query_string(params);
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), self.creds.key(), ts, &query);
        self.http
            .get_as(path, &query, &self.headers(ts, sign))
            .await
    }

    /// Signed POST. The signature covers the exact body bytes sent — the
    /// serialized string, not a re-serialization of the value.
    pub(crate) async fn post_signed(&self, path: &str, body: &Value) -> Result<Value, VenueError> {
        self.pacer.reserve().await;
        let body =
            serde_json::to_string(body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), self.creds.key(), ts, &body);
        self.http
            .post(path, body, "application/json", &self.headers(ts, sign))
            .await
    }

    fn headers(&self, ts: i64, sign: String) -> [(&'static str, String); 4] {
        [
            (HEADER_KEY, self.creds.key().to_string()),
            (HEADER_TIMESTAMP, ts.to_string()),
            (HEADER_RECV_WINDOW, RECV_WINDOW_S.to_string()),
            (HEADER_SIGN, sign),
        ]
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test(start_paused = true)]
    async fn the_pacer_admits_the_quota_at_once_and_holds_the_next_request_for_the_window() {
        let pacer = Pacer::default();
        let started = Instant::now();
        for _ in 0..QUOTA_REQUESTS {
            pacer.reserve().await;
        }
        assert_eq!(
            started.elapsed(),
            Duration::ZERO,
            "the quota itself is not paced"
        );
        pacer.reserve().await;
        assert!(
            started.elapsed() >= QUOTA_WINDOW,
            "the request past the quota went out after {:?}",
            started.elapsed()
        );
        // The window rolls: a full window later the whole quota is free again.
        let resumed = Instant::now();
        for _ in 0..QUOTA_REQUESTS - 1 {
            pacer.reserve().await;
        }
        assert_eq!(resumed.elapsed(), Duration::ZERO);
    }
}
