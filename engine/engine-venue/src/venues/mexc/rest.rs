//! MEXC's signed REST calls. Signing happens here so the bytes that are
//! signed are the bytes that go on the wire; the socket itself belongs to
//! [`crate::http`].
//!
//! The GET path takes its parameters as pairs rather than a ready-made query
//! string, because MEXC signs the query **sorted by key** and a caller that
//! built the string itself could sort it differently from the signer. One
//! function builds it and signs it, so the two cannot disagree.

use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock, Weak};
use std::time::Duration;

use engine_types::VenueError;
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::time::Instant;

use crate::creds::Credentials;
use crate::http::HttpClient;
use crate::venues::mexc::sign::{
    query_string, rest_signature, HEADER_KEY, HEADER_RECV_WINDOW, HEADER_SIGN, HEADER_TIMESTAMP,
    RECV_WINDOW_S,
};
use crate::wall_ms;

/// Conservative process-local budget. Most private endpoints allow 20/2s;
/// position stop placement allows only 5/2s. Keep headroom below both and
/// protect four aggregate slots from recovery, entries and administration.
/// Endpoint policy checked against official MEXC documentation 2026-09-09.
const QUOTA_WINDOW: Duration = Duration::from_secs(2);
const QUOTA_REQUESTS: usize = 16;
const SAFETY_RESERVE: usize = 4;
const STOP_WRITE_REQUESTS: usize = 4;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum OperationClass {
    Recovery,
    Trading,
    Administration,
    Protection,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum QuotaGroup {
    General,
    StopWrite,
}

/// A rolling window over every signed request this key sends, shared by every
/// clone of the client: the gateway, its recovery reader and the probe all
/// spend the same budget. Observed live on 2026-09-08: an unpaced sweep of
/// 132 symbols drew `510 Requests are too frequent` on every recovery pass.
#[derive(Debug, Default)]
pub(crate) struct Pacer {
    admissions: tokio::sync::Mutex<VecDeque<(Instant, QuotaGroup)>>,
    wait_by_class_ns: [AtomicU64; 4],
}

impl Pacer {
    pub(crate) async fn reserve_for(&self, class: OperationClass, group: QuotaGroup) -> Duration {
        let started = Instant::now();
        let limit = if class == OperationClass::Protection {
            QUOTA_REQUESTS
        } else {
            QUOTA_REQUESTS - SAFETY_RESERVE
        };
        loop {
            let wait = {
                let mut admissions = self.admissions.lock().await;
                let now = Instant::now();
                while admissions
                    .front()
                    .is_some_and(|(at, _)| now.duration_since(*at) >= QUOTA_WINDOW)
                {
                    admissions.pop_front();
                }
                let stop_blocked = group == QuotaGroup::StopWrite
                    && admissions
                        .iter()
                        .filter(|(_, group)| *group == QuotaGroup::StopWrite)
                        .count()
                        >= STOP_WRITE_REQUESTS;
                if admissions.len() < limit && !stop_blocked {
                    admissions.push_back((now, group));
                    let elapsed = started.elapsed();
                    saturating_add(&self.wait_by_class_ns[class as usize], nanos(elapsed));
                    return elapsed;
                }
                // Both allowances are reserved together, never while sleeping.
                let aggregate_wait = if admissions.len() >= limit {
                    admissions[0].0 + QUOTA_WINDOW - now
                } else {
                    Duration::ZERO
                };
                let stop_wait = if stop_blocked {
                    admissions
                        .iter()
                        .find(|(_, group)| *group == QuotaGroup::StopWrite)
                        .expect("counted a stop admission")
                        .0
                        + QUOTA_WINDOW
                        - now
                } else {
                    Duration::ZERO
                };
                aggregate_wait.max(stop_wait)
            };
            tokio::time::sleep(wait).await;
        }
    }
}

fn nanos(duration: Duration) -> u64 {
    duration.as_nanos().min(u64::MAX as u128) as u64
}

fn saturating_add(counter: &AtomicU64, value: u64) {
    let _ = counter.fetch_update(Ordering::Relaxed, Ordering::Relaxed, |previous| {
        Some(previous.saturating_add(value))
    });
}

/// Independent clients for the same endpoint/key share the same local quota.
/// This is deliberately not a cross-process or IP-wide rate-limit claim.
fn shared_pacer(base: &str, key: &str) -> Arc<Pacer> {
    type Scope = (String, [u8; 32]);
    static PACERS: OnceLock<std::sync::Mutex<HashMap<Scope, Weak<Pacer>>>> = OnceLock::new();
    let mut pacers = PACERS
        .get_or_init(Default::default)
        .lock()
        .expect("MEXC pacer registry poisoned");
    pacers.retain(|_, pacer| pacer.strong_count() != 0);
    let scope = (
        base.trim_end_matches('/').to_owned(),
        Sha256::digest(key.as_bytes()).into(),
    );
    if let Some(pacer) = pacers.get(&scope).and_then(Weak::upgrade) {
        return pacer;
    }
    let pacer = Arc::new(Pacer::default());
    pacers.insert(scope, Arc::downgrade(&pacer));
    pacer
}

#[derive(Clone)]
pub(crate) struct RestClient {
    http: HttpClient,
    creds: Credentials,
    pacer: Arc<Pacer>,
    mutation_wait_ns: Arc<AtomicU64>,
}

impl RestClient {
    pub(crate) fn new(base: impl Into<String>, creds: Credentials) -> Self {
        let base = base.into();
        let pacer = shared_pacer(&base, creds.key());
        Self {
            http: HttpClient::new(base),
            creds,
            pacer,
            mutation_wait_ns: Default::default(),
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
        self.get_signed_for(path, params, OperationClass::Recovery)
            .await
    }

    pub(crate) async fn get_signed_for(
        &self,
        path: &str,
        params: &[(&str, String)],
        class: OperationClass,
    ) -> Result<Value, VenueError> {
        self.admit(class, QuotaGroup::General).await;
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
        self.get_signed_as_for(path, params, OperationClass::Recovery)
            .await
    }

    pub(crate) async fn get_signed_as_for<T: serde::de::DeserializeOwned>(
        &self,
        path: &str,
        params: &[(&str, String)],
        class: OperationClass,
    ) -> Result<T, VenueError> {
        self.admit(class, QuotaGroup::General).await;
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
        let (class, group) = if path.starts_with("/api/v1/private/stoporder/") {
            (OperationClass::Protection, QuotaGroup::StopWrite)
        } else if path == "/api/v1/private/order/cancel_with_external"
            || path == "/api/v1/private/order/create"
                && body.get("reduceOnly").and_then(Value::as_bool) == Some(true)
        {
            (OperationClass::Protection, QuotaGroup::General)
        } else if path == "/api/v1/private/position/change_leverage" {
            (OperationClass::Administration, QuotaGroup::General)
        } else {
            (OperationClass::Trading, QuotaGroup::General)
        };
        let body =
            serde_json::to_string(body).map_err(|e| VenueError::BadRequest(e.to_string()))?;
        self.admit(class, group).await;
        let ts = wall_ms();
        let sign = rest_signature(self.creds.secret(), self.creds.key(), ts, &body);
        self.http
            .post(path, body, "application/json", &self.headers(ts, sign))
            .await
    }

    async fn admit(&self, class: OperationClass, group: QuotaGroup) {
        let waited_ns = nanos(self.pacer.reserve_for(class, group).await);
        if matches!(class, OperationClass::Trading | OperationClass::Protection) {
            saturating_add(&self.mutation_wait_ns, waited_ns);
        }
        tracing::debug!(operation_class=?class, quota_group=?group, waited_ns,
            "MEXC local quota admission");
    }

    pub(crate) fn take_mutation_wait_ns(&self) -> u64 {
        self.mutation_wait_ns.swap(0, Ordering::Relaxed)
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
    async fn clones_and_independent_clients_share_scope_and_background_wait_is_not_mutation_time() {
        let credentials =
            || Credentials::new("mexc_mainnet", false, "quota-fixture-key", "fixture-secret");
        let first = RestClient::new("http://127.0.0.1:1", credentials());
        let cloned = first.clone();
        let independent = RestClient::new("http://127.0.0.1:1/", credentials());
        let other = RestClient::new("http://127.0.0.1:2", credentials());
        assert!(Arc::ptr_eq(&first.pacer, &cloned.pacer));
        assert!(Arc::ptr_eq(&first.pacer, &independent.pacer));
        assert!(!Arc::ptr_eq(&first.pacer, &other.pacer));
        for _ in 0..QUOTA_REQUESTS - SAFETY_RESERVE {
            first
                .admit(OperationClass::Recovery, QuotaGroup::General)
                .await;
        }
        let started = Instant::now();
        independent
            .admit(OperationClass::Recovery, QuotaGroup::General)
            .await;
        assert_eq!(started.elapsed(), QUOTA_WINDOW);
        assert_eq!(first.take_mutation_wait_ns(), 0);
        assert_eq!(independent.take_mutation_wait_ns(), 0);
        assert_eq!(
            first.pacer.wait_by_class_ns[OperationClass::Recovery as usize].load(Ordering::Relaxed),
            nanos(QUOTA_WINDOW)
        );
    }

    #[tokio::test(start_paused = true)]
    async fn recovery_saturation_preserves_four_protective_slots() {
        let pacer = Arc::new(Pacer::default());
        for _ in 0..QUOTA_REQUESTS - SAFETY_RESERVE {
            pacer
                .reserve_for(OperationClass::Recovery, QuotaGroup::General)
                .await;
        }
        let reader = pacer.clone();
        let blocked = tokio::spawn(async move {
            reader
                .reserve_for(OperationClass::Recovery, QuotaGroup::General)
                .await
        });
        tokio::task::yield_now().await;
        assert!(
            !blocked.is_finished(),
            "recovery consumed the protected allowance"
        );
        for _ in 0..SAFETY_RESERVE {
            assert_eq!(
                pacer
                    .reserve_for(OperationClass::Protection, QuotaGroup::General)
                    .await,
                Duration::ZERO
            );
        }
        blocked.abort();
        assert!(
            pacer
                .reserve_for(OperationClass::Protection, QuotaGroup::General)
                .await
                >= QUOTA_WINDOW
        );
    }

    #[tokio::test(start_paused = true)]
    async fn position_stop_writes_obey_their_smaller_endpoint_budget_without_delaying_cancels() {
        let pacer = Arc::new(Pacer::default());
        for _ in 0..STOP_WRITE_REQUESTS {
            pacer
                .reserve_for(OperationClass::Protection, QuotaGroup::StopWrite)
                .await;
        }
        let writer = pacer.clone();
        let blocked = tokio::spawn(async move {
            writer
                .reserve_for(OperationClass::Protection, QuotaGroup::StopWrite)
                .await
        });
        tokio::task::yield_now().await;
        assert!(
            !blocked.is_finished(),
            "stop writes ignored the endpoint-specific quota"
        );
        assert_eq!(
            pacer
                .reserve_for(OperationClass::Protection, QuotaGroup::General)
                .await,
            Duration::ZERO,
            "waiting stop writes consumed a cancellation slot"
        );
        assert!(blocked.await.unwrap() >= QUOTA_WINDOW);
    }

    #[tokio::test(start_paused = true)]
    async fn the_pacer_admits_the_quota_at_once_and_holds_the_next_request_for_the_window() {
        let pacer = Pacer::default();
        let started = Instant::now();
        for _ in 0..QUOTA_REQUESTS {
            pacer
                .reserve_for(OperationClass::Protection, QuotaGroup::General)
                .await;
        }
        assert_eq!(
            started.elapsed(),
            Duration::ZERO,
            "the quota itself is not paced"
        );
        pacer
            .reserve_for(OperationClass::Protection, QuotaGroup::General)
            .await;
        assert!(
            started.elapsed() >= QUOTA_WINDOW,
            "the request past the quota went out after {:?}",
            started.elapsed()
        );
        // The window rolls: a full window later the whole quota is free again.
        let resumed = Instant::now();
        for _ in 0..QUOTA_REQUESTS - 1 {
            pacer
                .reserve_for(OperationClass::Protection, QuotaGroup::General)
                .await;
        }
        assert_eq!(resumed.elapsed(), Duration::ZERO);
    }
}
