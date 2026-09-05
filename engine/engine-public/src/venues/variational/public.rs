use super::realm::VariationalRealm;
use crate::http::HttpClient;
use engine_types::VenueError;
use serde_json::Value;

pub struct StatsClient {
    http: HttpClient,
}
impl StatsClient {
    pub fn new(realm: VariationalRealm) -> Self {
        Self::for_test(realm.rest_base())
    }
    pub fn for_test(base_url: &str) -> Self {
        Self {
            http: HttpClient::new(base_url),
        }
    }
    pub fn base(&self) -> &str {
        self.http.base()
    }
    pub async fn stats(&self) -> Result<Value, VenueError> {
        self.http.get("/metadata/stats", "", &[]).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};

    #[tokio::test]
    async fn public_stats_retries_after_http_failure_without_private_headers() {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let client = StatsClient::for_test(&format!("http://{}", listener.local_addr().unwrap()));
        let server = tokio::spawn(async move {
            for status in [503, 200] {
                let (mut stream, _) = listener.accept().await.unwrap();
                let mut request = Vec::new();
                while !request.ends_with(b"\r\n\r\n") {
                    let byte = stream.read_u8().await.unwrap();
                    request.push(byte);
                }
                let request = String::from_utf8(request).unwrap();
                assert!(request.starts_with("GET /metadata/stats HTTP/1.1\r\n"));
                let headers = request.to_ascii_lowercase();
                assert!(!headers.contains("authorization:"));
                assert!(!headers.contains("api-key:"));
                let body = r#"{"listings":[],"unknown":"escaped \"field\""}"#;
                let response = format!("HTTP/1.1 {status} Response\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
                stream.write_all(response.as_bytes()).await.unwrap();
            }
        });
        assert!(
            matches!(client.stats().await, Err(VenueError::Transport(text)) if text.starts_with("HTTP 503"))
        );
        assert_eq!(
            client.stats().await.unwrap(),
            serde_json::json!({"listings":[], "unknown":"escaped \"field\""})
        );
        server.await.unwrap();
    }
}
