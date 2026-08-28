#![allow(dead_code)]
//! A local HTTP/1.1 server for the request-shape tests.
//!
//! It speaks just enough HTTP to record what the gateway sent and answer with
//! a canned body, including keep-alive so the pooled client behaves the way it
//! does against the venue. Written on raw TCP so the tests need no dependency
//! the crate does not already have.

use std::net::SocketAddr;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;

#[derive(Clone, Debug)]
pub struct Recorded {
    pub method: String,
    pub path: String,
    pub query: String,
    pub headers: Vec<(String, String)>,
    pub body: String,
}

impl Recorded {
    pub fn header(&self, name: &str) -> Option<&str> {
        let name = name.to_ascii_lowercase();
        self.headers
            .iter()
            .find(|(k, _)| *k == name)
            .map(|(_, v)| v.as_str())
    }

    pub fn json(&self) -> serde_json::Value {
        serde_json::from_str(&self.body)
            .unwrap_or_else(|e| panic!("body is not JSON ({e}): {}", self.body))
    }
}

/// Answers a request. The second argument is how many requests this path has
/// already had, which is how the pagination test serves two pages.
pub type Handler = Arc<dyn Fn(&Recorded, usize) -> (u16, String) + Send + Sync>;

pub struct TestServer {
    pub addr: SocketAddr,
    seen: Arc<Mutex<Vec<Recorded>>>,
    connections: Arc<AtomicUsize>,
    peak_in_flight: Arc<AtomicUsize>,
}

impl TestServer {
    pub async fn start<F>(handler: F) -> TestServer
    where
        F: Fn(&Recorded, usize) -> (u16, String) + Send + Sync + 'static,
    {
        Self::start_delayed(handler, Duration::ZERO).await
    }

    /// As [`Self::start`], but hold each complete request for `response_delay`
    /// before acknowledging it. This gives concurrency tests a deterministic
    /// wire-level overlap window without blocking a Tokio worker thread.
    pub async fn start_delayed<F>(handler: F, response_delay: Duration) -> TestServer
    where
        F: Fn(&Recorded, usize) -> (u16, String) + Send + Sync + 'static,
    {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        let seen: Arc<Mutex<Vec<Recorded>>> = Arc::new(Mutex::new(Vec::new()));
        let handler: Handler = Arc::new(handler);

        let accepted = seen.clone();
        let connections = Arc::new(AtomicUsize::new(0));
        let counted = connections.clone();
        let in_flight = Arc::new(AtomicUsize::new(0));
        let active = in_flight.clone();
        let peak_in_flight = Arc::new(AtomicUsize::new(0));
        let peak = peak_in_flight.clone();
        tokio::spawn(async move {
            while let Ok((stream, _)) = listener.accept().await {
                counted.fetch_add(1, Ordering::SeqCst);
                let seen = accepted.clone();
                let handler = handler.clone();
                let in_flight = active.clone();
                let peak_in_flight = peak.clone();
                tokio::spawn(async move {
                    serve(
                        stream,
                        seen,
                        handler,
                        response_delay,
                        in_flight,
                        peak_in_flight,
                    )
                    .await
                });
            }
        });

        TestServer {
            addr,
            seen,
            connections,
            peak_in_flight,
        }
    }

    /// How many TCP connections were opened. Two requests over one
    /// connection is the pool doing its job.
    pub fn connections(&self) -> usize {
        self.connections.load(Ordering::SeqCst)
    }

    pub fn peak_in_flight(&self) -> usize {
        self.peak_in_flight.load(Ordering::SeqCst)
    }

    pub fn base_url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn requests(&self) -> Vec<Recorded> {
        self.seen.lock().unwrap().clone()
    }

    pub fn to_path(&self, path: &str) -> Vec<Recorded> {
        self.requests()
            .into_iter()
            .filter(|r| r.path == path)
            .collect()
    }

    /// The one request to this path; panics if there was not exactly one.
    pub fn only(&self, path: &str) -> Recorded {
        let mut found = self.to_path(path);
        assert_eq!(
            found.len(),
            1,
            "expected one request to {path}, saw {}",
            found.len()
        );
        found.remove(0)
    }
}

async fn serve(
    mut stream: TcpStream,
    seen: Arc<Mutex<Vec<Recorded>>>,
    handler: Handler,
    response_delay: Duration,
    in_flight: Arc<AtomicUsize>,
    peak_in_flight: Arc<AtomicUsize>,
) {
    let mut buf: Vec<u8> = Vec::new();
    loop {
        // Head first, then however many body bytes it declares.
        let head_end = loop {
            if let Some(at) = find(&buf, b"\r\n\r\n") {
                break at + 4;
            }
            if !read_more(&mut stream, &mut buf).await {
                return;
            }
        };
        let head = String::from_utf8_lossy(&buf[..head_end]).to_string();
        let length = content_length(&head);
        while buf.len() < head_end + length {
            if !read_more(&mut stream, &mut buf).await {
                return;
            }
        }
        let body = String::from_utf8_lossy(&buf[head_end..head_end + length]).to_string();
        buf.drain(..head_end + length);

        let request = parse_head(&head, body);
        let prior = seen
            .lock()
            .unwrap()
            .iter()
            .filter(|r| r.path == request.path)
            .count();
        let (status, reply) = handler(&request, prior);
        seen.lock().unwrap().push(request);

        let active = in_flight.fetch_add(1, Ordering::SeqCst) + 1;
        peak_in_flight.fetch_max(active, Ordering::SeqCst);
        if !response_delay.is_zero() {
            tokio::time::sleep(response_delay).await;
        }

        let response = format!(
            "HTTP/1.1 {status} X\r\ncontent-type: application/json\r\ncontent-length: {}\r\n\r\n{reply}",
            reply.len()
        );
        let sent = stream.write_all(response.as_bytes()).await;
        in_flight.fetch_sub(1, Ordering::SeqCst);
        if sent.is_err() {
            return;
        }
    }
}

async fn read_more(stream: &mut TcpStream, buf: &mut Vec<u8>) -> bool {
    let mut chunk = [0u8; 4096];
    match stream.read(&mut chunk).await {
        Ok(0) | Err(_) => false,
        Ok(n) => {
            buf.extend_from_slice(&chunk[..n]);
            true
        }
    }
}

fn parse_head(head: &str, body: String) -> Recorded {
    let mut lines = head.lines();
    let request_line = lines.next().unwrap_or_default();
    let mut parts = request_line.split_whitespace();
    let method = parts.next().unwrap_or_default().to_string();
    let target = parts.next().unwrap_or_default().to_string();
    let (path, query) = match target.split_once('?') {
        Some((p, q)) => (p.to_string(), q.to_string()),
        None => (target, String::new()),
    };
    let headers = lines
        .filter_map(|line| line.split_once(':'))
        .map(|(k, v)| (k.trim().to_ascii_lowercase(), v.trim().to_string()))
        .collect();
    Recorded {
        method,
        path,
        query,
        headers,
        body,
    }
}

fn content_length(head: &str) -> usize {
    head.lines()
        .filter_map(|line| line.split_once(':'))
        .find(|(k, _)| k.trim().eq_ignore_ascii_case("content-length"))
        .and_then(|(_, v)| v.trim().parse().ok())
        .unwrap_or(0)
}

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}
