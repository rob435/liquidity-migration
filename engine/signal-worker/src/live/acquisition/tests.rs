use super::*;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::task::JoinHandle;

async fn http_source(
    respond: impl Fn(&str) -> Value + Send + Sync + 'static,
) -> (
    PublicHttpClient,
    mpsc::UnboundedReceiver<String>,
    JoinHandle<()>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let (requests, received) = mpsc::unbounded_channel();
    let server = tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") {
                let byte = socket.read_u8().await.unwrap();
                request.push(byte);
            }
            let request = String::from_utf8(request).unwrap();
            let path = request.split_whitespace().nth(1).unwrap();
            requests.send(path.to_owned()).unwrap();
            let body = respond(path).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            socket.shutdown().await.unwrap();
        }
    });
    let client = PublicHttpClient::for_http_test(base, Arc::new(Semaphore::new(2)));
    (client, received, server)
}

#[tokio::test]
async fn repair_lane_commits_one_symbol_and_stops_without_fetching_the_suffix() {
    let (client, mut requests, server) = http_source(|_| {
        serde_json::json!({"retCode": 0, "result": {"list": [
            [(2 * HOUR_MS).to_string(), "100", "101", "99", "100", "1", "100"]
        ]}})
    })
    .await;
    let (tx, mut rx) = mpsc::channel(1);
    spawn_repair_lane(
        tx,
        client,
        "linear".into(),
        100,
        vec![
            ("BTCUSDT".into(), 2 * HOUR_MS, 3 * HOUR_MS),
            ("ETHUSDT".into(), 2 * HOUR_MS, 3 * HOUR_MS),
        ],
        3 * HOUR_MS,
        Some(7),
    );
    let LaneCompletion::RepairChunk { result, resume } = rx.recv().await.unwrap() else {
        panic!("repair result expected");
    };
    let fetched = result.unwrap();
    assert!(fetched.failures.is_empty());
    assert_eq!(fetched.batches.len(), 1);
    assert_eq!(fetched.batches[0].0, "BTCUSDT");
    assert_eq!(fetched.batches[0].1.checked_from_ms, Some(2 * HOUR_MS));
    assert_eq!(fetched.batches[0].1.checked_through_ms, Some(3 * HOUR_MS));
    assert_eq!(fetched.batches[0].1.rows.len(), 1);
    assert!(requests.recv().await.unwrap().contains("symbol=BTCUSDT"));
    resume.send(false).unwrap();
    assert!(matches!(
        rx.recv().await,
        Some(LaneCompletion::RepairFinished { end_ms, epoch: Some(7) })
            if end_ms == 3 * HOUR_MS
    ));
    assert!(requests.try_recv().is_err());
    server.abort();
}

#[tokio::test]
async fn funding_job_preserves_disjoint_coverage_and_emits_settlements_once() {
    let (client, mut requests, server) = http_source(|_| {
        let rows = [1, 2, 4, 5].map(|hour| {
            serde_json::json!({"fundingRateTimestamp": (hour * HOUR_MS).to_string(), "fundingRate": "0.0001"})
        });
        serde_json::json!({"retCode": 0, "result": {"list": rows}})
    })
    .await;
    let instrument = serde_json::from_value(serde_json::json!({
        "symbol": "BTCUSDT", "observed_ts_ms": 1, "available_at_ms": 1,
        "funding_interval_min": 60, "is_prelisting": false
    }))
    .unwrap();
    let fetched = fetch_funding_job(
        client,
        "linear".into(),
        100,
        ("BTCUSDT".into(), HOUR_MS, 5 * HOUR_MS, true),
        &BTreeMap::from([("BTCUSDT".into(), instrument)]),
    )
    .await
    .unwrap();
    assert!(fetched.failures.is_empty());
    assert_eq!(fetched.batches.len(), 2);
    let first = &fetched.batches[0].1;
    let second = &fetched.batches[1].1;
    assert_eq!(
        (first.checked_from_ms, first.checked_through_ms),
        (Some(HOUR_MS), Some(2 * HOUR_MS))
    );
    assert_eq!(
        (second.checked_from_ms, second.checked_through_ms),
        (Some(4 * HOUR_MS), Some(5 * HOUR_MS))
    );
    assert_eq!(first.rows.len(), 4);
    assert!(first.emit_lifecycle);
    assert!(second.rows.is_empty());
    assert!(!second.emit_lifecycle);
    assert_eq!(first.available_at_ms, second.available_at_ms);
    assert!(requests.recv().await.unwrap().contains("symbol=BTCUSDT"));
    assert!(requests.try_recv().is_err());
    server.abort();
}

#[tokio::test]
async fn funding_lane_continues_after_one_source_failure_and_reports_incomplete() {
    let (client, mut requests, server) = http_source(|path| {
        if path.contains("symbol=BTCUSDT") {
            serde_json::json!({"retCode": 10006, "retMsg": "Too many visits"})
        } else {
            serde_json::json!({"retCode": 0, "result": {"list": []}})
        }
    })
    .await;
    let (tx, mut rx) = mpsc::channel(1);
    spawn_funding_fetch_lane(
        tx,
        client,
        "linear".into(),
        100,
        vec![
            ("BTCUSDT".into(), 0, HOUR_MS, false),
            ("ETHUSDT".into(), 0, HOUR_MS, false),
        ],
        Arc::new(BTreeMap::new()),
    );
    for (symbol, failures) in [("BTCUSDT", 1), ("ETHUSDT", 0)] {
        let LaneCompletion::FundingChunk { result, resume } = rx.recv().await.unwrap() else {
            panic!("funding result expected");
        };
        let fetched = result.unwrap();
        assert_eq!(fetched.failures.len(), failures);
        assert!(requests
            .recv()
            .await
            .unwrap()
            .contains(&format!("symbol={symbol}")));
        if failures > 0 {
            assert!(fetched.failures[0].1.contains("10006"));
        }
        resume.send(true).unwrap();
    }
    assert!(matches!(
        rx.recv().await,
        Some(LaneCompletion::FundingFinished { succeeded: false })
    ));
    assert!(requests.try_recv().is_err());
    server.abort();
}

#[tokio::test]
async fn whale_job_preserves_complete_day_and_its_coverage() {
    let (client, _requests, server) = http_source(|_| {
        Value::Array(
            (0..288)
                .map(|slot| {
                    serde_json::json!({
                        "timestamp": DAY_MS + slot * FIVE_MIN_MS, "longShortRatio": "1.25"
                    })
                })
                .collect(),
        )
    })
    .await;
    let fetched = fetch_whale_job(client, 500, ("BTCUSDT".into(), DAY_MS, 2 * DAY_MS))
        .await
        .unwrap();
    assert_eq!(fetched.rows.len(), 1);
    assert_eq!(fetched.rows[0].symbol, "BTCUSDT");
    assert_eq!(fetched.rows[0].day_end_ms, Value::from(2 * DAY_MS));
    assert_eq!(fetched.rows[0].long_short_ratio, Some(Value::from("1.25")));
    assert_eq!(
        fetched.coverage,
        vec![SourceCoverage {
            symbol: "BTCUSDT".into(),
            checked_from_ms: DAY_MS,
            checked_through_ms: 2 * DAY_MS,
            replace_coverage: false,
        }]
    );
    server.abort();
}

#[tokio::test]
async fn closed_global_budget_remains_a_fatal_error() {
    let budget = Arc::new(Semaphore::new(1));
    let client = PublicHttpClient::for_http_test("http://127.0.0.1:1".into(), Arc::clone(&budget));
    budget.close();
    let result =
        fetch_kline_job(client, "linear".into(), 100, ("BTCUSDT".into(), 0, HOUR_MS)).await;
    assert!(matches!(result, Err(error) if !error.is_lane_local_source_failure()));
}
