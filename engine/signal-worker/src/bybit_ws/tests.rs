use super::*;

fn ticker(kind: &str, symbol: &str, fields: &str) -> String {
    format!(
        r#"{{"topic":"tickers.{symbol}","type":"{kind}","ts":1,"data":{{"symbol":"{symbol}",{fields}}}}}"#
    )
}

fn kline(symbol: &str, start: i64, confirm: bool) -> String {
    format!(
        r#"{{"topic":"kline.60.{symbol}","type":"snapshot","ts":{},"data":[{{"start":{start},"end":{},"interval":"60","open":"100","high":"110","low":"90","close":"105","volume":"2","turnover":"205","confirm":{confirm},"timestamp":{}}}]}}"#,
        start + HOUR_MS,
        start + HOUR_MS - 1,
        start + HOUR_MS,
    )
}

#[test]
fn parser_keeps_only_confirmed_hourly_klines() {
    let start = 10 * HOUR_MS;
    assert_eq!(
        parse_frame(&kline("BTCUSDT", start, false), start + HOUR_MS).unwrap(),
        ParsedMessage::Klines(Vec::new())
    );
    let ParsedMessage::Klines(rows) =
        parse_frame(&kline("BTCUSDT", start, true), start + HOUR_MS).unwrap()
    else {
        panic!("expected kline rows");
    };
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].symbol, "BTCUSDT");
    assert_eq!(rows[0].row[0], Value::from(start));
    assert_eq!(rows[0].row[4], Value::from("105"));
}

#[test]
fn parser_rejects_semantically_invalid_market_rows_before_the_shared_loop() {
    let bad_ticker = ticker("snapshot", "BTCUSDT", r#""markPrice":"not-a-number""#);
    assert!(parse_frame(&bad_ticker, 10 * HOUR_MS).is_err());

    let start = 10 * HOUR_MS;
    let bad_kline =
        kline("BTCUSDT", start, true).replace(r#""open":"100""#, r#""open":"not-a-number""#);
    assert!(parse_frame(&bad_kline, start + HOUR_MS).is_err());
}

#[test]
fn subscription_ack_parser_preserves_numeric_and_string_return_codes() {
    for frame in [
        r#"{"op":"subscribe","success":false,"retCode":10429,"ret_msg":"busy","req_id":"s1-0"}"#,
        r#"{"op":"subscribe","success":false,"ret_code":"10016","ret_msg":"restart","req_id":"s1-1"}"#,
    ] {
        let ParsedMessage::Ack {
            success, ret_code, ..
        } = parse_frame(frame, 1_000).unwrap()
        else {
            panic!("expected subscribe ack");
        };
        assert!(!success);
        assert!(matches!(ret_code, Some(10429 | 10016)));
    }
    assert!(!topic_local_subscription_refusal(Some(10429), "busy"));
    assert!(!topic_local_subscription_refusal(Some(10016), "restart"));
    assert!(!topic_local_subscription_refusal(Some(10019), "restart"));
    assert!(!topic_local_subscription_refusal(Some(20003), "slow down"));
    assert!(!topic_local_subscription_refusal(
        Some(10404),
        "op type is not found"
    ));
    assert!(!topic_local_subscription_refusal(None, "route not found"));
    assert!(!topic_local_subscription_refusal(None, "invalid operation"));
    assert!(topic_local_subscription_refusal(Some(10001), "bad param"));
    assert!(topic_local_subscription_refusal(None, "invalid topic"));
}

#[test]
fn ticker_deltas_merge_into_a_full_bounded_cache() {
    let allowed = ["BTCUSDT".to_owned(), "ETHUSDT".to_owned()]
        .into_iter()
        .collect();
    let mut cache = TickerCache::new(allowed);
    let frames = [
        ticker(
            "snapshot",
            "BTCUSDT",
            r#""lastPrice":"100","markPrice":"101","fundingRate":"-0.001""#,
        ),
        ticker("delta", "BTCUSDT", r#""markPrice":"102""#),
        ticker("snapshot", "ETHUSDT", r#""markPrice":"50""#),
        ticker("snapshot", "UNKNOWN", r#""markPrice":"999""#),
    ];
    for (index, frame) in frames.iter().enumerate() {
        let ParsedMessage::Ticker(frame) = parse_frame(frame, 1_000 + index as i64).unwrap() else {
            panic!("expected ticker");
        };
        cache.apply(*frame, 1_000 + index as i64);
    }
    for index in 0..10_000 {
        let frame = ticker(
            "delta",
            "BTCUSDT",
            &format!(r#""markPrice":"{}""#, 103 + index),
        );
        let ParsedMessage::Ticker(frame) = parse_frame(&frame, 2_000 + index).unwrap() else {
            panic!("expected ticker");
        };
        cache.apply(*frame, 2_000 + index);
    }
    assert_eq!(cache.len(), 2);
    assert_eq!(cache.capacity(), 2);
    let rows = cache.sample(12_000, 30_000);
    assert_eq!(rows[0].last_price, Some(Value::from("100")));
    assert_eq!(rows[0].mark_price, Some(Value::from("10102")));
    cache.clear();
    assert_eq!(cache.len(), 0);
}

#[test]
fn ticker_fields_age_independently_and_one_stale_symbol_does_not_block_others() {
    let allowed = ["BTCUSDT".to_owned(), "ETHUSDT".to_owned()]
        .into_iter()
        .collect();
    let mut cache = TickerCache::new(allowed);
    let ParsedMessage::Ticker(btc) = parse_frame(
        &ticker(
            "snapshot",
            "BTCUSDT",
            r#""markPrice":"100","fundingRate":"0.001","nextFundingTime":"100000""#,
        ),
        1_000,
    )
    .unwrap() else {
        panic!("expected ticker");
    };
    cache.apply(*btc, 1_000);
    let ParsedMessage::Ticker(eth) =
        parse_frame(&ticker("snapshot", "ETHUSDT", r#""markPrice":"50""#), 1_000).unwrap()
    else {
        panic!("expected ticker");
    };
    cache.apply(*eth, 1_000);
    let ParsedMessage::Ticker(delta) =
        parse_frame(&ticker("delta", "BTCUSDT", r#""markPrice":"101""#), 2_500).unwrap()
    else {
        panic!("expected ticker");
    };
    cache.apply(*delta, 2_500);

    let rows = cache.sample(2_600, 1_000);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].symbol, "BTCUSDT");
    assert_eq!(rows[0].mark_price, Some(Value::from("101")));
    assert_eq!(rows[0].mark_observed_ts_ms, Some(2_500));
    assert_eq!(rows[0].funding_rate, None);
    assert_eq!(rows[0].funding_observed_ts_ms, None);
    assert_eq!(rows[0].next_funding_time, Some(Value::from("100000")));
    assert_eq!(rows[0].schedule_observed_ts_ms, Some(1_000));
    assert_eq!(cache.sample(100_001, 1_000).len(), 0);
}

#[test]
fn fresh_funding_schedule_survives_a_stale_mark() {
    let allowed = ["BTCUSDT".to_owned()].into_iter().collect();
    let mut cache = TickerCache::new(allowed);
    let ParsedMessage::Ticker(snapshot) = parse_frame(
        &ticker("snapshot", "BTCUSDT", r#""markPrice":"100""#),
        1_000,
    )
    .unwrap() else {
        panic!("expected ticker");
    };
    cache.apply(*snapshot, 1_000);
    let ParsedMessage::Ticker(delta) = parse_frame(
        &ticker(
            "delta",
            "BTCUSDT",
            r#""fundingRate":"0.001","nextFundingTime":"100000""#,
        ),
        2_500,
    )
    .unwrap() else {
        panic!("expected ticker");
    };
    cache.apply(*delta, 2_500);

    let rows = cache.sample(2_600, 1_000);
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].mark_price, None);
    assert_eq!(rows[0].mark_observed_ts_ms, None);
    assert_eq!(rows[0].funding_rate, Some(Value::from("0.001")));
    assert_eq!(rows[0].funding_observed_ts_ms, Some(2_500));
    assert_eq!(rows[0].next_funding_time, Some(Value::from("100000")));
}

#[test]
fn rest_reconcile_heals_stale_fields_without_rolling_back_same_millisecond_ws_data() {
    let allowed = ["BTCUSDT".to_owned()].into_iter().collect();
    let mut cache = TickerCache::new(allowed);
    let ParsedMessage::Ticker(snapshot) = parse_frame(
        &ticker("snapshot", "BTCUSDT", r#""markPrice":"100""#),
        1_000,
    )
    .unwrap() else {
        panic!("expected ticker");
    };
    cache.apply(*snapshot, 1_000);
    let ParsedMessage::Ticker(rest_old) =
        parse_frame(&ticker("snapshot", "BTCUSDT", r#""markPrice":"90""#), 2_000).unwrap()
    else {
        panic!("expected ticker");
    };
    cache.reconcile_rest(rest_old.row, 1_500, 2_000);
    assert_eq!(
        cache.sample(2_001, 1_000)[0].mark_price,
        Some(Value::from("90"))
    );

    let ParsedMessage::Ticker(ws_new) =
        parse_frame(&ticker("delta", "BTCUSDT", r#""markPrice":"110""#), 2_500).unwrap()
    else {
        panic!("expected ticker");
    };
    cache.apply(*ws_new, 2_500);
    let ParsedMessage::Ticker(stale_response) =
        parse_frame(&ticker("snapshot", "BTCUSDT", r#""markPrice":"95""#), 3_000).unwrap()
    else {
        panic!("expected ticker");
    };
    cache.reconcile_rest(stale_response.row, 2_500, 3_000);
    let row = &cache.sample(3_001, 1_000)[0];
    assert_eq!(row.mark_price, Some(Value::from("110")));
    assert_eq!(row.mark_observed_ts_ms, Some(2_500));
}

#[test]
fn event_queue_has_a_hard_cap() {
    assert_eq!(stream_event_capacity(1), 64);
    assert_eq!(stream_event_capacity(150), 300);
    assert_eq!(stream_event_capacity(10_000), MAX_STREAM_EVENTS);
}

#[test]
fn public_stream_url_is_the_credential_free_linear_endpoint() {
    assert_eq!(
        public_linear_url(),
        "wss://stream.bybit.com/v5/public/linear"
    );
}

async fn serve_epoch(listener: &tokio::net::TcpListener, start: i64, mark: &str, hold_open: bool) {
    let (stream, _) = listener.accept().await.unwrap();
    let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
    let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
    let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
        .as_str()
        .unwrap()
        .to_owned();
    socket
        .send(Message::text(
            serde_json::json!({
                "success": true,
                "ret_msg": "",
                "conn_id": "x",
                "req_id": req_id,
                "op": "subscribe",
            })
            .to_string(),
        ))
        .await
        .unwrap();
    socket
        .send(Message::text(ticker(
            "snapshot",
            "BTCUSDT",
            &format!(r#""lastPrice":"{mark}","markPrice":"{mark}""#),
        )))
        .await
        .unwrap();
    socket
        .send(Message::text(kline("BTCUSDT", start, true)))
        .await
        .unwrap();
    if hold_open {
        while socket.next().await.is_some() {}
    } else {
        tokio::time::sleep(Duration::from_millis(100)).await;
        socket.close(None).await.unwrap();
    }
}

async fn next(stream: &mut BybitPublicStream) -> StreamEvent {
    tokio::time::timeout(Duration::from_secs(3), stream.next_event())
        .await
        .unwrap()
        .unwrap()
}

#[tokio::test(start_paused = true)]
async fn frames_before_the_final_subscription_ack_are_discarded_on_refusal() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let first = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let first_req = serde_json::from_str::<Value>(&first).unwrap()["req_id"]
            .as_str()
            .unwrap()
            .to_owned();
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": true,
                    "ret_msg": "",
                    "req_id": first_req,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        socket
            .send(Message::text(kline("S000USDT", 10 * HOUR_MS, true)))
            .await
            .unwrap();
        let second = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let second_req = serde_json::from_str::<Value>(&second).unwrap()["req_id"]
            .as_str()
            .unwrap()
            .to_owned();
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": false,
                    "ret_msg": "bad topic",
                    "req_id": second_req,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
    });
    let symbols = (0..60).map(|index| format!("S{index:03}USDT")).collect();
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    };
    let mut stream =
        BybitPublicStream::with_url(format!("ws://127.0.0.1:{port}"), symbols, options).unwrap();
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::GapOpened { epoch: 1, .. }
        ) {
            break;
        }
    }
    while let Ok(event) = stream.events.try_recv() {
        assert!(!matches!(event, StreamEvent::KlineClosed(_)));
    }
    assert_eq!(stream.health().ticker_rows, 0);
    assert!(!stream.health().connected);
}

#[tokio::test(start_paused = true)]
async fn one_refused_ticker_is_quarantined_without_disabling_other_topics() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let mut accepted = BTreeSet::new();
        let mut refused = false;
        while accepted.len() < 3 || !refused {
            let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let request: Value = serde_json::from_str(&request).unwrap();
            let req_id = request["req_id"].as_str().unwrap();
            let args = request["args"].as_array().unwrap();
            let contains_bad_ticker = args
                .iter()
                .any(|topic| topic.as_str() == Some("tickers.BADUSDT"));
            let success = !contains_bad_ticker;
            if success {
                accepted.extend(args.iter().filter_map(Value::as_str).map(str::to_owned));
            } else if args.len() == 1 {
                refused = true;
            }
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": success,
                        "ret_msg": if success { "" } else { "bad topic" },
                        "req_id": req_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
        }
        socket
            .send(Message::text(ticker(
                "snapshot",
                "BTCUSDT",
                r#""markPrice":"100""#,
            )))
            .await
            .unwrap();
        socket
            .send(Message::text(kline("BTCUSDT", 10 * HOUR_MS, true)))
            .await
            .unwrap();
        while socket.next().await.is_some() {}
    });
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    };
    let mut stream = BybitPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BADUSDT".into(), "BTCUSDT".into()],
        options,
    )
    .unwrap();
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::EpochStarted { epoch: 1, .. }
        ) {
            break;
        }
    }
    let health = stream.health();
    assert!(health.connected);
    assert!(health.gap_open);
    assert_eq!(health.ticker_topics_accepted, 1);
    assert_eq!(health.ticker_topics_quarantined, 1);
    assert_eq!(health.kline_topics_accepted, 2);
    assert_eq!(health.kline_topics_quarantined, 0);
    assert!(stream.mark_gap_repaired(1));
    assert!(!stream.health().gap_open);
    loop {
        if matches!(next(&mut stream).await, StreamEvent::KlineClosed(_)) {
            break;
        }
    }
    let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
    assert_eq!(sample.rows.len(), 1);
    assert_eq!(sample.rows[0].symbol, "BTCUSDT");
}

#[tokio::test(start_paused = true)]
async fn quarantined_topic_reprobes_use_unique_ids_and_survive_transient_refusal() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (reprobe_tx, reprobe_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let mut accepted = BTreeSet::new();
        let mut seen_req_ids = BTreeSet::new();
        let mut refused = false;
        while accepted.len() < 3 || !refused {
            let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let request: Value = serde_json::from_str(&request).unwrap();
            let req_id = request["req_id"].as_str().unwrap().to_owned();
            assert!(seen_req_ids.insert(req_id.clone()), "duplicate request id");
            let args = request["args"].as_array().unwrap();
            let contains_bad_ticker = args
                .iter()
                .any(|topic| topic.as_str() == Some("tickers.BADUSDT"));
            let success = !contains_bad_ticker;
            if success {
                accepted.extend(args.iter().filter_map(Value::as_str).map(str::to_owned));
            } else if args.len() == 1 {
                refused = true;
            }
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": success,
                        "retCode": if success { 0 } else { 10001 },
                        "ret_msg": if success { "" } else { "invalid symbol" },
                        "req_id": &req_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
        }
        socket
            .send(Message::text(ticker(
                "snapshot",
                "BTCUSDT",
                r#""markPrice":"100""#,
            )))
            .await
            .unwrap();
        let first_reprobe = tokio::time::timeout(Duration::from_secs(1), socket.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
            .into_text()
            .unwrap();
        let first_reprobe: Value = serde_json::from_str(&first_reprobe).unwrap();
        assert_eq!(
            first_reprobe["args"].as_array().unwrap(),
            &[Value::from("tickers.BADUSDT")]
        );
        let first_reprobe_id = first_reprobe["req_id"].as_str().unwrap().to_owned();
        assert!(
            seen_req_ids.insert(first_reprobe_id.clone()),
            "re-probe reused an earlier request id"
        );
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": false,
                    "retCode": 10429,
                    "ret_msg": "system frequency protection",
                    "req_id": first_reprobe_id,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        socket
            .send(Message::text(ticker(
                "delta",
                "BTCUSDT",
                r#""markPrice":"101""#,
            )))
            .await
            .unwrap();
        let second_reprobe = tokio::time::timeout(Duration::from_secs(1), socket.next())
            .await
            .unwrap()
            .unwrap()
            .unwrap()
            .into_text()
            .unwrap();
        let second_reprobe: Value = serde_json::from_str(&second_reprobe).unwrap();
        assert_eq!(
            second_reprobe["args"].as_array().unwrap(),
            &[Value::from("tickers.BADUSDT")]
        );
        let second_reprobe_id = second_reprobe["req_id"].as_str().unwrap().to_owned();
        assert!(
            seen_req_ids.insert(second_reprobe_id.clone()),
            "second re-probe reused an earlier request id"
        );
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": true,
                    "retCode": 0,
                    "ret_msg": "",
                    "req_id": second_reprobe_id,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        socket
            .send(Message::text(ticker(
                "snapshot",
                "BADUSDT",
                r#""markPrice":"50""#,
            )))
            .await
            .unwrap();
        reprobe_tx.send(()).unwrap();
        while socket.next().await.is_some() {}
    });
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_millis(20),
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    };
    let mut stream = BybitPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BADUSDT".into(), "BTCUSDT".into()],
        options,
    )
    .unwrap();
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::EpochStarted { epoch: 1, .. }
        ) {
            break;
        }
    }
    while stream.health().ticker_rows != 1 {
        tokio::task::yield_now().await;
    }
    tokio::time::advance(Duration::from_millis(20)).await;
    loop {
        let retried = stream
            .sample_tickers(wall_ms().unwrap(), 30_000)
            .is_some_and(|sample| {
                sample
                    .rows
                    .iter()
                    .any(|row| row.mark_price == Some(Value::from("101")))
            });
        if retried {
            break;
        }
        tokio::task::yield_now().await;
    }
    tokio::time::advance(Duration::from_millis(20)).await;
    tokio::time::timeout(Duration::from_secs(1), reprobe_rx)
        .await
        .unwrap()
        .unwrap();
    tokio::time::timeout(Duration::from_secs(1), async {
        loop {
            let health = stream.health();
            if health.ticker_topics_quarantined == 0 && health.ticker_rows == 2 {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    let health = stream.health();
    assert_eq!(health.epoch, 1);
    assert_eq!(health.reconnect_count, 0);
    assert_eq!(health.ticker_topics_accepted, 2);
    assert_eq!(health.ticker_topics_quarantined, 0);
    let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
    assert_eq!(sample.rows.len(), 2);
}

#[tokio::test(start_paused = true)]
async fn transient_subscription_refusal_reconnects_without_quarantining_topics() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (first, _) = listener.accept().await.unwrap();
        let mut first = tokio_tungstenite::accept_async(first).await.unwrap();
        let request = first.next().await.unwrap().unwrap().into_text().unwrap();
        let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
            .as_str()
            .unwrap()
            .to_owned();
        first
            .send(Message::text(
                serde_json::json!({
                    "success": false,
                    "retCode": 10429,
                    "ret_msg": "system frequency protection",
                    "req_id": req_id,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        drop(first);

        let (second, _) = listener.accept().await.unwrap();
        let mut second = tokio_tungstenite::accept_async(second).await.unwrap();
        let request = second.next().await.unwrap().unwrap().into_text().unwrap();
        let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
            .as_str()
            .unwrap()
            .to_owned();
        second
            .send(Message::text(
                serde_json::json!({
                    "success": true,
                    "retCode": 0,
                    "ret_msg": "",
                    "req_id": req_id,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        second
            .send(Message::text(ticker(
                "snapshot",
                "BTCUSDT",
                r#""markPrice":"100""#,
            )))
            .await
            .unwrap();
        while second.next().await.is_some() {}
    });
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_millis(10),
        backoff_max: Duration::from_millis(20),
    };
    let mut stream = BybitPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BTCUSDT".into()],
        options,
    )
    .unwrap();
    loop {
        match next(&mut stream).await {
            StreamEvent::GapOpened { epoch: 1, .. } => {
                tokio::time::advance(Duration::from_millis(20)).await
            }
            StreamEvent::EpochStarted {
                epoch: 2,
                reconnected: true,
                ..
            } => break,
            _ => {}
        }
    }
    let health = stream.health();
    assert!(health.connected);
    assert_eq!(health.ticker_topics_quarantined, 0);
    assert_eq!(health.kline_topics_quarantined, 0);
    assert_eq!(health.reconnect_count, 1);
}

#[tokio::test(start_paused = true)]
async fn global_10404_refusal_never_bisects_or_quarantines_topics() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (request_count_tx, request_count_rx) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
            .as_str()
            .unwrap()
            .to_owned();
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": false,
                    "retCode": 10404,
                    "ret_msg": "op type is not found",
                    "req_id": req_id,
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        let extra = matches!(
            tokio::time::timeout(Duration::from_millis(100), socket.next()).await,
            Ok(Some(Ok(Message::Text(text)))) if text.contains("subscribe")
        );
        let _ = request_count_tx.send(1 + usize::from(extra));
    });
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    };
    let mut stream = BybitPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BTCUSDT".into(), "ETHUSDT".into()],
        options,
    )
    .unwrap();
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::GapOpened { epoch: 1, .. }
        ) {
            break;
        }
    }
    tokio::time::advance(Duration::from_millis(100)).await;
    assert_eq!(request_count_rx.await.unwrap(), 1);
    assert_eq!(stream.health().ticker_topics_quarantined, 0);
    assert_eq!(stream.health().kline_topics_quarantined, 0);
}

#[tokio::test(start_paused = true)]
async fn accepted_topic_flood_bypasses_the_unacknowledged_chunk_buffer() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let first = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let first: Value = serde_json::from_str(&first).unwrap();
        assert!(first["args"]
            .as_array()
            .unwrap()
            .iter()
            .any(|topic| topic == "tickers.S000USDT"));
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": true,
                    "ret_msg": "",
                    "req_id": first["req_id"],
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        let second = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let second: Value = serde_json::from_str(&second).unwrap();
        socket
            .send(Message::text(ticker(
                "snapshot",
                "S000USDT",
                r#""markPrice":"1""#,
            )))
            .await
            .unwrap();
        for index in 1..MAX_STREAM_EVENTS + 32 {
            socket
                .send(Message::text(ticker(
                    "delta",
                    "S000USDT",
                    &format!(r#""markPrice":"{index}""#),
                )))
                .await
                .unwrap();
        }
        socket
            .send(Message::text(
                serde_json::json!({
                    "success": true,
                    "ret_msg": "",
                    "req_id": second["req_id"],
                    "op": "subscribe"
                })
                .to_string(),
            ))
            .await
            .unwrap();
        while socket.next().await.is_some() {}
    });
    let symbols = (0..60).map(|index| format!("S{index:03}USDT")).collect();
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(10),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    };
    let mut stream =
        BybitPublicStream::with_url(format!("ws://127.0.0.1:{port}"), symbols, options).unwrap();
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::EpochStarted { epoch: 1, .. }
        ) {
            break;
        }
    }
    let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
    let row = sample
        .rows
        .iter()
        .find(|row| row.symbol == "S000USDT")
        .unwrap();
    assert_eq!(
        row.mark_price,
        Some(Value::from((MAX_STREAM_EVENTS + 31).to_string()))
    );
    assert_eq!(stream.health().queued_frames, 0);
}

#[tokio::test(start_paused = true)]
async fn ack_and_pong_only_epochs_do_not_reset_reconnect_backoff() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let (accepted_tx, mut accepted_rx) = tokio::sync::mpsc::unbounded_channel();
    tokio::spawn(async move {
        for _ in 0..3 {
            let (stream, _) = listener.accept().await.unwrap();
            accepted_tx.send(Instant::now()).unwrap();
            let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
            let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let req_id = serde_json::from_str::<Value>(&request).unwrap()["req_id"]
                .as_str()
                .unwrap()
                .to_owned();
            socket
                .send(Message::text(
                    serde_json::json!({
                        "success": true,
                        "ret_msg": "",
                        "req_id": req_id,
                        "op": "subscribe"
                    })
                    .to_string(),
                ))
                .await
                .unwrap();
            socket
                .send(Message::text(r#"{"op":"pong"}"#))
                .await
                .unwrap();
            while socket.next().await.is_some() {}
        }
    });
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(1),
        pong_timeout: Duration::from_millis(100),
        data_idle_timeout: Duration::from_millis(15),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_millis(30),
        backoff_max: Duration::from_millis(120),
    };
    let mut stream = BybitPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BTCUSDT".into()],
        options,
    )
    .unwrap();
    let first = tokio::time::timeout(Duration::from_secs(2), accepted_rx.recv())
        .await
        .unwrap()
        .unwrap();
    for expected_epoch in 1..=2 {
        loop {
            if matches!(next(&mut stream).await, StreamEvent::EpochStarted { epoch, .. } if epoch == expected_epoch)
            {
                break;
            }
        }
        tokio::time::advance(Duration::from_millis(15)).await;
        loop {
            if matches!(next(&mut stream).await, StreamEvent::GapOpened { epoch, .. } if epoch == expected_epoch)
            {
                break;
            }
        }
        tokio::time::advance(Duration::from_millis(if expected_epoch == 1 {
            60
        } else {
            120
        }))
        .await;
    }
    let second = tokio::time::timeout(Duration::from_secs(2), accepted_rx.recv())
        .await
        .unwrap()
        .unwrap();
    let third = tokio::time::timeout(Duration::from_secs(2), accepted_rx.recv())
        .await
        .unwrap()
        .unwrap();
    let first_delay = second.duration_since(first);
    let second_delay = third.duration_since(second);
    assert!(first_delay >= Duration::from_millis(60));
    assert!(second_delay >= Duration::from_millis(120));
    assert!(second_delay > first_delay + Duration::from_millis(30));
    drop(stream);
}

#[tokio::test(start_paused = true)]
async fn reconnect_opens_a_gap_and_clears_the_old_epoch_cache() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let start = 10 * HOUR_MS;
    tokio::spawn(async move {
        serve_epoch(&listener, start, "100", false).await;
        serve_epoch(&listener, start + HOUR_MS, "200", true).await;
    });
    let options = StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(2),
        quarantine_reprobe_interval: Duration::from_secs(60),
        backoff_start: Duration::from_millis(10),
        backoff_max: Duration::from_millis(20),
    };
    let mut stream = BybitPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BTCUSDT".into()],
        options,
    )
    .unwrap();

    loop {
        match next(&mut stream).await {
            StreamEvent::EpochStarted { epoch: 1, .. } => break,
            StreamEvent::Fault(_) => {}
            other => panic!("data/control event preceded the first epoch: {other:?}"),
        }
    }
    assert!(matches!(
        next(&mut stream).await,
        StreamEvent::KlineClosed(_)
    ));
    assert!(stream.mark_gap_repaired(1));
    assert!(stream.sample_tickers(wall_ms().unwrap(), 30_000).is_some());
    tokio::time::advance(Duration::from_millis(100)).await;
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::GapOpened { epoch: 1, .. }
        ) {
            break;
        }
    }
    assert!(stream.sample_tickers(wall_ms().unwrap(), 30_000).is_none());
    tokio::time::advance(Duration::from_millis(20)).await;
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::EpochStarted {
                epoch: 2,
                reconnected: true,
                ..
            }
        ) {
            break;
        }
    }
    assert!(matches!(
        next(&mut stream).await,
        StreamEvent::KlineClosed(_)
    ));
    assert!(stream.mark_gap_repaired(2));
    let sample = stream.sample_tickers(wall_ms().unwrap(), 30_000).unwrap();
    assert_eq!(sample.rows[0].mark_price, Some(Value::from("200")));
    assert!(stream.health().queued_frames <= stream.health().queue_capacity);
}

#[test]
fn a_replacement_stream_continues_the_epoch_and_the_gap_clock() {
    // A universe refresh replaces the stream object, and the health record
    // lives in it. A successor that starts fresh reuses epoch numbers a
    // repair lane spawned for the outgoing stream still carries, and dates
    // the gap from the refresh instead of from the outage.
    let symbols = BTreeSet::from(["BTCUSDT".to_owned(), "ETHUSDT".to_owned()]);
    let outgoing = StreamHealth {
        connected: true,
        epoch: 3,
        gap_open: true,
        gap_open_since_ms: Some(1_788_538_993_000),
        reconnect_count: 2,
        fault_count: 5,
        ..StreamHealth::default()
    };

    let restarted = SharedState::continuing(&symbols, StreamContinuity::default());
    assert_eq!(restarted.health.epoch, 0);
    assert_eq!(restarted.health.gap_open_since_ms, None);

    let mut successor = SharedState::continuing(&symbols, StreamContinuity::from(&outgoing));
    assert_eq!(successor.health.reconnect_count, 2);
    assert_eq!(successor.health.fault_count, 5);
    assert!(successor.health.gap_open);
    successor.prepare_epoch(successor.health.epoch + 1, 1_788_546_193_000);

    assert_eq!(
        successor.health.epoch, 4,
        "the successor's first epoch is above every epoch an in-flight repair still holds"
    );
    assert_eq!(
        successor.health.gap_open_since_ms,
        Some(1_788_538_993_000),
        "the gap is as old as the transport's, not as old as the rebuild"
    );
}
