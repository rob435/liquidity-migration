use std::sync::Arc;
use std::time::Duration;

use bytes::Bytes;
use http_body_util::{BodyExt, Full, Limited};
use hyper::body::Body;
use hyper::{Request, Response};
use hyper_rustls::{HttpsConnector, HttpsConnectorBuilder};
use hyper_util::client::legacy::connect::HttpConnector;
use hyper_util::client::legacy::Client;
use hyper_util::rt::TokioExecutor;
use serde_json::Value;
use tokio::sync::Semaphore;

use crate::worker::WorkerError;

const MAX_RESPONSE_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone)]
pub struct PublicHttpClient {
    client: Client<HttpsConnector<HttpConnector>, Full<Bytes>>,
    base: String,
    timeout: Duration,
    retries: usize,
    retry_base: Duration,
    request_budget: Arc<Semaphore>,
}

impl PublicHttpClient {
    pub fn new(
        host: &str,
        timeout_ms: u64,
        retries: usize,
        retry_base_ms: u64,
        request_budget: Arc<Semaphore>,
    ) -> Result<Self, WorkerError> {
        let _ = rustls::crypto::ring::default_provider().install_default();
        if host.is_empty() || host.contains('/') {
            return Err(WorkerError::config("public HTTP host is invalid"));
        }
        if request_budget.available_permits() == 0 {
            return Err(WorkerError::config(
                "public HTTP request budget must be positive",
            ));
        }
        let mut http = HttpConnector::new();
        http.set_nodelay(true);
        http.set_keepalive(Some(Duration::from_secs(60)));
        http.enforce_http(false);
        let https = HttpsConnectorBuilder::new()
            .with_webpki_roots()
            .https_only()
            .enable_http1()
            .wrap_connector(http);
        let client = Client::builder(TokioExecutor::new())
            .pool_idle_timeout(Duration::from_secs(600))
            .pool_max_idle_per_host(16)
            .build(https);
        Ok(Self {
            client,
            base: format!("https://{host}"),
            timeout: Duration::from_millis(timeout_ms),
            retries,
            retry_base: Duration::from_millis(retry_base_ms),
            request_budget,
        })
    }

    pub async fn get(&self, path: &str, query: &str) -> Result<(Value, i64), WorkerError> {
        let url = if query.is_empty() {
            format!("{}{}", self.base, path)
        } else {
            format!("{}{}?{}", self.base, path, query)
        };
        let mut last = None;
        for attempt in 0..self.retries {
            let request = Request::builder()
                .method("GET")
                .uri(&url)
                .header("Accept", "application/json")
                .header("User-Agent", "liquidity-migration-signal-worker/1")
                .body(Full::new(Bytes::new()))
                .map_err(|error| WorkerError::network(format!("build public request: {error}")))?;
            let exchange = async {
                let response =
                    self.client.request(request).await.map_err(|error| {
                        WorkerError::network(format!("public request: {error}"))
                    })?;
                read_json(response).await
            };
            let outcome = {
                let _request_permit = self
                    .request_budget
                    .acquire()
                    .await
                    .map_err(|_| WorkerError::state("public HTTP request budget closed"))?;
                tokio::time::timeout(self.timeout, exchange).await
            };
            match outcome {
                Ok(Ok(value)) => {
                    if let Some(error) = public_api_error(&value) {
                        last = Some(error);
                    } else {
                        return Ok((value, wall_ms()?));
                    }
                }
                Ok(Err(error)) => last = Some(error),
                Err(_) => {
                    last = Some(WorkerError::network(format!(
                        "public request exceeded {:?}",
                        self.timeout
                    )))
                }
            }
            if attempt + 1 < self.retries {
                let multiplier = 1_u32 << u32::try_from(attempt.min(8)).unwrap_or(8);
                tokio::time::sleep(self.retry_base.saturating_mul(multiplier)).await;
            }
        }
        Err(last.unwrap_or_else(|| WorkerError::network("public request had no attempts")))
    }
}

fn public_api_error(payload: &Value) -> Option<WorkerError> {
    let code = payload.get("retCode")?;
    if code.as_i64() == Some(0) {
        return None;
    }
    Some(WorkerError::network(format!(
        "Bybit retCode={} retMsg={}",
        code,
        payload.get("retMsg").unwrap_or(&Value::Null)
    )))
}

async fn read_json<B>(response: Response<B>) -> Result<Value, WorkerError>
where
    B: Body<Data = Bytes>,
    B::Error: std::error::Error + Send + Sync + 'static,
{
    let status = response.status();
    let bytes = Limited::new(response.into_body(), MAX_RESPONSE_BYTES)
        .collect()
        .await
        .map_err(|error| WorkerError::network(format!("public response body: {error}")))?
        .to_bytes();
    if !status.is_success() {
        return Err(WorkerError::network(format!(
            "public HTTP {}: {}",
            status.as_u16(),
            String::from_utf8_lossy(&bytes)
                .chars()
                .take(200)
                .collect::<String>()
        )));
    }
    serde_json::from_slice(&bytes)
        .map_err(|error| WorkerError::network(format!("parse public response: {error}")))
}

pub fn percent_encode(raw: &str) -> String {
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

pub fn wall_ms() -> Result<i64, WorkerError> {
    let elapsed = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|error| WorkerError::state(format!("wall clock precedes Unix epoch: {error}")))?;
    i64::try_from(elapsed.as_millis())
        .map_err(|_| WorkerError::state("wall clock milliseconds exceed i64"))
}

#[cfg(test)]
mod tests {
    use super::{public_api_error, read_json, PublicHttpClient, MAX_RESPONSE_BYTES};
    use bytes::Bytes;
    use http_body_util::Full;
    use hyper::Response;

    #[test]
    fn bybit_application_failures_are_retryable_request_failures() {
        assert!(public_api_error(&serde_json::json!({"retCode": 0})).is_none());
        assert!(public_api_error(&serde_json::json!([])).is_none());
        let error = public_api_error(&serde_json::json!({
            "retCode": 10006,
            "retMsg": "Too many visits"
        }))
        .expect("nonzero Bybit result must fail");
        assert!(error.to_string().contains("10006"));
    }

    #[tokio::test]
    async fn clients_and_lane_clones_share_one_request_concurrency_budget() {
        let budget = std::sync::Arc::new(tokio::sync::Semaphore::new(2));
        let client =
            PublicHttpClient::new("example.com", 1_000, 1, 1, std::sync::Arc::clone(&budget))
                .unwrap();
        let other_source = PublicHttpClient::new("example.org", 1_000, 1, 1, budget).unwrap();
        let clone = other_source.clone();
        let first = client.request_budget.acquire().await.unwrap();
        let second = clone.request_budget.acquire().await.unwrap();
        assert!(client.request_budget.try_acquire().is_err());
        drop(first);
        assert!(clone.request_budget.try_acquire().is_ok());
        drop(second);
    }

    #[tokio::test]
    async fn response_body_cap_refuses_the_next_byte() {
        let response = Response::builder()
            .status(200)
            .body(Full::new(Bytes::from(vec![b' '; MAX_RESPONSE_BYTES + 1])))
            .unwrap();
        let error = read_json(response).await.unwrap_err();
        assert!(error.to_string().contains("public response body"));
    }
}
