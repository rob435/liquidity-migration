use super::*;
use serde_json::json;

/// Recorded from `wss://api.hyperliquid.xyz/ws` on 2026-09-09, one frame per
/// channel the stream reads.
const FRAMES: &str = include_str!("../../../../tests/fixtures/hyperliquid/stream_frames.json");

fn frames() -> BTreeMap<String, Value> {
    serde_json::from_str(FRAMES).expect("recorded frames are JSON")
}

fn frame(name: &str) -> String {
    frames()[name].to_string()
}

fn followed() -> BTreeMap<String, String> {
    BTreeMap::from([
        ("BTCUSDT".to_owned(), "BTC".to_owned()),
        ("KPEPEUSDT".to_owned(), "kPEPE".to_owned()),
    ])
}

fn coins_by_symbol() -> BTreeMap<String, String> {
    followed()
}

fn symbols_by_coin() -> BTreeMap<String, String> {
    followed()
        .into_iter()
        .map(|(symbol, coin)| (coin, symbol))
        .collect()
}

fn parse(text: &str, received_at_ms: i64) -> Result<ParsedMessage, String> {
    parse_frame(&symbols_by_coin(), text, received_at_ms)
}

fn candle(coin: &str, open_ts_ms: i64, close: &str) -> String {
    json!({"channel": "candle", "data": {
        "t": open_ts_ms,
        "T": open_ts_ms + HOUR_MS - 1,
        "s": coin,
        "i": "1h",
        "o": "100", "h": "110", "l": "90", "c": close, "v": "2", "n": 7,
    }})
    .to_string()
}

/// Every recorded frame, read the way the live socket hands it over.
#[test]
fn every_recorded_channel_parses_into_the_workers_wire_shape() {
    let now = 1_788_947_000_000_i64;
    let ParsedMessage::Ticker(row) = parse(&frame("activeAssetCtx_BTC"), now).unwrap() else {
        panic!("expected a ticker");
    };
    assert_eq!(row.symbol, "BTCUSDT");
    assert_eq!(row.mark_price, Some(Value::from("78989.0")));
    assert_eq!(row.index_price, Some(Value::from("79016.0")));
    assert_eq!(row.last_price, Some(Value::from("78980.5")));
    assert_eq!(row.funding_rate, Some(Value::from("0.0000125")));
    assert_eq!(row.bid1_price, None, "impact prices are not the touch");
    assert_eq!(row.ask1_price, None);
    assert_eq!(
        row.next_funding_time,
        Some(Value::from(next_settlement_ms(now)))
    );

    // The venue's own spelling reaches the engine's symbol.
    let ParsedMessage::Ticker(row) = parse(&frame("activeAssetCtx_kPEPE"), now).unwrap() else {
        panic!("expected a ticker");
    };
    assert_eq!(row.symbol, "KPEPEUSDT");

    let ParsedMessage::Candle { row, open_ts_ms } = parse(&frame("candle_BTC"), now).unwrap()
    else {
        panic!("expected a candle");
    };
    assert_eq!(row.symbol, "BTCUSDT");
    assert_eq!(open_ts_ms, 1_788_944_400_000);
    assert_eq!(row.row.len(), 7);
    assert_eq!(row.row[0], Value::from(open_ts_ms));
    assert_eq!(row.row[4], Value::from("78980.0"));
    let ParsedMessage::Candle { row, .. } = parse(&frame("candle_kPEPE"), now).unwrap() else {
        panic!("expected a candle");
    };
    assert_eq!(row.symbol, "KPEPEUSDT");

    for (name, channel, coin) in [
        (
            "subscriptionResponse_activeAssetCtx_BTC",
            Channel::Ticker,
            "BTC",
        ),
        ("subscriptionResponse_candle_BTC", Channel::Candle, "BTC"),
        (
            "subscriptionResponse_activeAssetCtx_kPEPE",
            Channel::Ticker,
            "kPEPE",
        ),
        (
            "subscriptionResponse_candle_kPEPE",
            Channel::Candle,
            "kPEPE",
        ),
    ] {
        let ParsedMessage::Accepted(key) = parse(&frame(name), now).unwrap() else {
            panic!("expected an ack for {name}");
        };
        assert_eq!(
            key,
            SubscriptionKey {
                channel,
                coin: coin.to_owned()
            }
        );
    }

    // The refusal quotes the subscription it refused, which is how one topic
    // is quarantined without disabling the rest.
    let ParsedMessage::Refused { key, reason } = parse(&frame("error"), now).unwrap() else {
        panic!("expected a refusal");
    };
    assert_eq!(
        key,
        SubscriptionKey {
            channel: Channel::Ticker,
            coin: "NOTACOIN".to_owned()
        }
    );
    assert!(reason.contains("Invalid subscription"), "{reason}");

    assert_eq!(parse(&frame("pong"), now).unwrap(), ParsedMessage::Pong);

    // A coin this stream does not follow, and a channel it did not ask for.
    assert_eq!(
        parse(&candle("SOL", 4 * HOUR_MS, "100"), now).unwrap(),
        ParsedMessage::Ignore
    );
    assert_eq!(
        parse(
            &json!({"channel": "l2Book", "data": {"coin": "BTC"}}).to_string(),
            now
        )
        .unwrap(),
        ParsedMessage::Ignore
    );
    // A candle on another interval is not this stream's bar.
    let other = candle("BTC", 4 * HOUR_MS, "100").replace(r#""i":"1h""#, r#""i":"15m""#);
    assert_eq!(parse(&other, now).unwrap(), ParsedMessage::Ignore);
}

/// A refusal or a failure the stream cannot attribute to one topic is a
/// transport failure, because leaving it as one topic's problem would hide it.
#[test]
fn an_unattributable_failure_is_a_transport_failure() {
    let now = 4 * HOUR_MS;
    for refused in [
        json!({"channel": "error", "data": "Too many subscriptions"}).to_string(),
        json!({"channel": "error", "data": "Invalid subscription {\"type\":\"l2Book\",\"coin\":\"BTC\"}"})
            .to_string(),
    ] {
        let error = parse(&refused, now).unwrap_err();
        assert!(error.contains("Hyperliquid public stream error"), "{error}");
    }
    let error = parse("not json", now).unwrap_err();
    assert!(error.contains("invalid JSON"), "{error}");
    let error = parse(
        &json!({"channel": "activeAssetCtx", "data": {"coin": "BTC", "ctx": {"markPx": "0"}}})
            .to_string(),
        now,
    )
    .unwrap_err();
    assert!(error.contains("markPrice"), "{error}");
    let error = parse(&candle("BTC", HOUR_MS + 1, "100"), now).unwrap_err();
    assert!(error.contains("invalid open clock"), "{error}");
}

/// A bar reaches the queue once: when the next bar starts, or once its own
/// hour has been closed for the settle lag.
#[test]
fn a_bar_is_handed_over_once_and_only_after_its_hour() {
    let mut watch = CandleWatch::default();
    let first = 10 * HOUR_MS;
    let running = |open_ts_ms: i64, close: &str| {
        let ParsedMessage::Candle { row, open_ts_ms } =
            parse(&candle("BTC", open_ts_ms, close), open_ts_ms + 1).unwrap()
        else {
            panic!("expected a candle");
        };
        (*row, open_ts_ms)
    };

    let (row, open) = running(first, "101");
    assert!(watch.observe(row, open).is_none(), "a running bar waits");
    // A restatement of the same bar replaces it and closes nothing.
    let (row, open) = running(first, "105");
    assert!(watch.observe(row, open).is_none());
    assert!(
        watch.settled(first + HOUR_MS).is_empty(),
        "the lag has to pass"
    );

    // The next bar starting closes the one before it, with its last values.
    let (row, open) = running(first + HOUR_MS, "200");
    let closed = watch.observe(row, open).expect("the earlier bar closed");
    assert_eq!(closed.row[0], Value::from(first));
    assert_eq!(closed.row[4], Value::from("105"));

    // The bar just closed never closes twice, however often it is restated.
    let (row, open) = running(first, "106");
    assert!(watch.observe(row, open).is_none());
    assert!(watch
        .settled(first + 4 * HOUR_MS)
        .iter()
        .all(|row| row.row[0] != first));

    // A coin that stops trading has no next frame, so the lag closes its bar.
    let mut watch = CandleWatch::default();
    let (row, open) = running(first, "105");
    assert!(watch.observe(row, open).is_none());
    assert!(watch
        .settled(first + HOUR_MS + CANDLE_SETTLE_LAG_MS - 1)
        .is_empty());
    let settled = watch.settled(first + HOUR_MS + CANDLE_SETTLE_LAG_MS);
    assert_eq!(settled.len(), 1);
    assert_eq!(settled[0].row[0], Value::from(first));
    assert!(watch.settled(first + 10 * HOUR_MS).is_empty());
}

/// The topic names are what the heartbeat's `bybit_ws_*_topics_*` counts read.
#[test]
fn each_symbol_carries_a_ticker_and_a_candle_topic() {
    let built = topics(&coins_by_symbol()).unwrap();
    assert_eq!(
        built.iter().map(Topic::name).collect::<Vec<_>>(),
        [
            "activeAssetCtx.BTCUSDT",
            "candle.1h.BTCUSDT",
            "activeAssetCtx.KPEPEUSDT",
            "candle.1h.KPEPEUSDT",
        ]
    );
    // The subscription names the venue's own coin, not the engine's symbol.
    assert_eq!(
        built[0].subscription(),
        json!({"type": "activeAssetCtx", "coin": "BTC"})
    );
    assert_eq!(
        built[3].subscription(),
        json!({"type": "candle", "coin": "kPEPE", "interval": "1h"})
    );

    let accepted = built
        .iter()
        .filter(|topic| topic.symbol == "BTCUSDT")
        .map(Topic::name)
        .collect::<BTreeSet<_>>();
    let quarantined = BTreeSet::from(["activeAssetCtx.KPEPEUSDT".to_owned()]);
    let mut state = SharedState::continuing(
        &["BTCUSDT".to_owned(), "KPEPEUSDT".to_owned()]
            .into_iter()
            .collect(),
        StreamContinuity::default(),
    );
    state.update_topic_counts(&accepted, &quarantined);
    assert_eq!(state.health.ticker_topics_accepted, 1);
    assert_eq!(state.health.kline_topics_accepted, 1);
    assert_eq!(state.health.ticker_topics_quarantined, 1);
    assert_eq!(state.health.kline_topics_quarantined, 0);
    assert!(!state.ws_ticker_coverage_complete());

    assert!(topics(&BTreeMap::new()).is_err());
    assert!(topics(&BTreeMap::from([("btcusdt".to_owned(), "BTC".to_owned())])).is_err());
    assert!(topics(&BTreeMap::from([("BTCUSDT".to_owned(), " ".to_owned())])).is_err());
}

/// A context frame restates the whole context, so it replaces the cached row;
/// a REST reconcile heals a field the socket has not restated.
#[test]
fn context_frames_replace_the_cached_row_and_fields_age_independently() {
    let allowed = ["BTCUSDT".to_owned(), "KPEPEUSDT".to_owned()]
        .into_iter()
        .collect::<BTreeSet<_>>();
    let mut cache = TickerCache::new(allowed);
    assert_eq!(cache.capacity(), 2);
    let ParsedMessage::Ticker(btc) = parse(&frame("activeAssetCtx_BTC"), 1_000).unwrap() else {
        panic!("expected a ticker");
    };
    cache.apply(*btc, 1_000);
    let ParsedMessage::Ticker(kpepe) = parse(&frame("activeAssetCtx_kPEPE"), 1_000).unwrap() else {
        panic!("expected a ticker");
    };
    cache.apply(*kpepe, 1_000);
    assert_eq!(cache.len(), 2);
    assert!(cache.ws_coverage_complete());

    // A coin outside the followed set never enters the cache.
    let ParsedMessage::Ticker(btc) = parse(&frame("activeAssetCtx_BTC"), 2_000).unwrap() else {
        panic!("expected a ticker");
    };
    let mut stranger = *btc;
    stranger.symbol = "SOLUSDT".to_owned();
    cache.apply(stranger, 2_000);
    assert_eq!(cache.len(), 2);

    // One stale symbol does not block the other.
    let rows = cache.sample(1_500, 1_000);
    assert_eq!(rows.len(), 2);
    assert!(rows.iter().all(|row| row.mark_price.is_some()));
    assert_eq!(rows[0].mark_observed_ts_ms, Some(1_000));
    let stale = cache.sample(1_000_000, 1_000);
    assert!(stale.is_empty(), "every field aged out");

    // A REST row heals a cache the socket has gone quiet on, and does not roll
    // back a frame that arrived while the read was in flight.
    let mut fresh = cache.rows["BTCUSDT"].row.clone();
    fresh.mark_price = Some(Value::from("1"));
    cache.reconcile_rest(fresh.clone(), 500, 2_000);
    assert_eq!(
        cache.rows["BTCUSDT"].row.mark_price,
        Some(Value::from("78989.0")),
        "a read that started before the frame does not overwrite it"
    );
    cache.reconcile_rest(fresh, 1_500, 2_000);
    assert_eq!(cache.rows["BTCUSDT"].row.mark_price, Some(Value::from("1")));
    cache.clear();
    assert_eq!(cache.len(), 0);
    assert!(!cache.ws_coverage_complete());
}

/// A settlement clock in the past is not a schedule, whatever the cache holds.
#[test]
fn a_settlement_already_past_is_dropped_from_the_sample() {
    let mut cache = TickerCache::new(["BTCUSDT".to_owned()].into_iter().collect());
    let ParsedMessage::Ticker(row) = parse(&frame("activeAssetCtx_BTC"), HOUR_MS).unwrap() else {
        panic!("expected a ticker");
    };
    cache.apply(*row, HOUR_MS);
    let sampled = cache.sample(HOUR_MS + 1, HOUR_MS);
    assert_eq!(sampled[0].next_funding_time, Some(Value::from(2 * HOUR_MS)));
    assert_eq!(sampled[0].schedule_observed_ts_ms, Some(HOUR_MS));
    let past = cache.sample(3 * HOUR_MS, 3 * HOUR_MS);
    assert_eq!(past[0].next_funding_time, None);
    assert_eq!(past[0].schedule_observed_ts_ms, None);
}

fn test_options() -> StreamOptions {
    StreamOptions {
        connect_timeout: Duration::from_secs(2),
        subscribe_timeout: Duration::from_secs(2),
        write_timeout: Duration::from_secs(2),
        ping_interval: Duration::from_secs(20),
        pong_timeout: Duration::from_secs(2),
        data_idle_timeout: Duration::from_secs(30),
        quarantine_reprobe_interval: Duration::from_secs(60),
        candle_sweep_interval: Duration::from_secs(5),
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    }
}

async fn next(stream: &mut HyperliquidPublicStream) -> StreamEvent {
    tokio::time::timeout(Duration::from_secs(5), stream.next_event())
        .await
        .expect("an event")
        .expect("the worker is alive")
}

/// The whole subscribe handshake against a stand-in venue: one refused coin is
/// quarantined, the rest go live, and the bar that closes reaches the queue.
#[tokio::test(start_paused = true)]
async fn a_refused_coin_is_quarantined_while_the_rest_of_the_epoch_runs() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let open = 10 * HOUR_MS;
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let mut answered = 0;
        while answered < 6 {
            let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let subscription: Value = serde_json::from_str(&request).unwrap();
            let subscription = subscription["subscription"].clone();
            answered += 1;
            // The venue refuses one coin's context and acks everything else.
            if subscription["coin"] == "GONE" && subscription["type"] == "activeAssetCtx" {
                socket
                    .send(Message::text(
                        json!({"channel": "error", "data": format!(
                            "Invalid subscription {subscription}"
                        )})
                        .to_string(),
                    ))
                    .await
                    .unwrap();
                continue;
            }
            socket
                .send(Message::text(
                    json!({"channel": "subscriptionResponse", "data": {
                        "method": "subscribe",
                        "subscription": subscription,
                    }})
                    .to_string(),
                ))
                .await
                .unwrap();
        }
        for frame in [
            frames()["activeAssetCtx_BTC"].to_string(),
            candle("BTC", open, "105"),
            candle("BTC", open + HOUR_MS, "200"),
        ] {
            socket.send(Message::text(frame)).await.unwrap();
        }
        while socket.next().await.is_some() {}
    });

    let coins = BTreeMap::from([
        ("BTCUSDT".to_owned(), "BTC".to_owned()),
        ("GONEUSDT".to_owned(), "GONE".to_owned()),
        ("KPEPEUSDT".to_owned(), "kPEPE".to_owned()),
    ]);
    let mut stream = HyperliquidPublicStream::with_options(
        &format!("ws://127.0.0.1:{port}"),
        coins,
        test_options(),
        StreamContinuity::default(),
    )
    .unwrap();
    assert!(matches!(
        next(&mut stream).await,
        StreamEvent::EpochStarted {
            epoch: 1,
            reconnected: false,
            ..
        }
    ));
    let mut closed = None;
    let mut faults = Vec::new();
    while closed.is_none() {
        match next(&mut stream).await {
            StreamEvent::KlineClosed(row) => closed = Some(row),
            StreamEvent::Fault(error) => faults.push(error),
            event => panic!("unexpected {event:?}"),
        }
    }
    let closed = closed.unwrap();
    assert_eq!(closed.symbol, "BTCUSDT");
    assert_eq!(closed.row[0], Value::from(open));
    assert_eq!(closed.row[4], Value::from("105"));
    assert!(
        faults
            .iter()
            .any(|fault| fault.contains("quarantined 1 refused topics")),
        "{faults:?}"
    );

    let health = stream.health();
    assert!(health.connected);
    assert_eq!(health.epoch, 1);
    assert!(health.gap_open, "every epoch opens a repair gap");
    assert_eq!(health.ticker_topics_accepted, 2);
    assert_eq!(health.ticker_topics_quarantined, 1);
    assert_eq!(health.kline_topics_accepted, 3);
    assert_eq!(health.kline_topics_quarantined, 0);
    assert_eq!(health.ticker_rows, 1);
    assert_eq!(health.ticker_capacity, 3);
    assert!(!health.ticker_coverage_complete);
    assert_eq!(health.fault_count, 1);

    // The gap closes only for the epoch that opened it.
    assert!(!stream.mark_gap_repaired(2));
    assert!(stream.mark_gap_repaired(1));
    assert!(!stream.health().gap_open);
    stream.mark_source_fault(5 * HOUR_MS);
    let health = stream.health();
    assert!(health.gap_open);
    assert_eq!(health.gap_open_since_ms, Some(5 * HOUR_MS));
    assert_eq!(health.ticker_rows, 0, "a source fault empties the cache");
    assert_eq!(health.fault_count, 2);
}

/// A socket that goes away opens a gap and empties the epoch's cache.
#[tokio::test(start_paused = true)]
async fn a_closed_socket_opens_a_gap() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        for _ in 0..2 {
            let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
            let subscription: Value = serde_json::from_str(&request).unwrap();
            socket
                .send(Message::text(
                    json!({"channel": "subscriptionResponse", "data": {
                        "method": "subscribe",
                        "subscription": subscription["subscription"].clone(),
                    }})
                    .to_string(),
                ))
                .await
                .unwrap();
        }
        socket
            .send(Message::text(frames()["activeAssetCtx_BTC"].to_string()))
            .await
            .unwrap();
        tokio::time::sleep(Duration::from_millis(100)).await;
        socket.close(None).await.unwrap();
        while socket.next().await.is_some() {}
    });

    let mut stream = HyperliquidPublicStream::with_options(
        &format!("ws://127.0.0.1:{port}"),
        BTreeMap::from([("BTCUSDT".to_owned(), "BTC".to_owned())]),
        test_options(),
        StreamContinuity::default(),
    )
    .unwrap();
    assert!(matches!(
        next(&mut stream).await,
        StreamEvent::EpochStarted { epoch: 1, .. }
    ));
    // The stand-in venue holds the socket for a moment, then drops it.
    tokio::time::advance(Duration::from_millis(100)).await;
    loop {
        if matches!(
            next(&mut stream).await,
            StreamEvent::GapOpened { epoch: 1, .. }
        ) {
            break;
        }
    }
    let health = stream.health();
    assert!(!health.connected);
    assert!(health.gap_open);
    assert_eq!(health.ticker_rows, 0, "a gap empties the epoch's cache");
}

/// A replacement stream is the same transport as far as the heartbeat and the
/// repair lanes are concerned.
#[test]
fn a_replacement_stream_continues_the_epoch_and_the_gap_clock() {
    let continuity = StreamContinuity {
        epoch: 7,
        gap_open: true,
        gap_open_since_ms: Some(3 * HOUR_MS),
        reconnect_count: 4,
        fault_count: 2,
    };
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_time()
        .build()
        .unwrap();
    let stream = runtime.block_on(async {
        HyperliquidPublicStream::with_options(
            "ws://127.0.0.1:1",
            BTreeMap::from([("BTCUSDT".to_owned(), "BTC".to_owned())]),
            test_options(),
            continuity,
        )
        .unwrap()
    });
    let health = stream.health();
    assert_eq!(health.epoch, 7);
    assert!(health.gap_open);
    assert_eq!(health.gap_open_since_ms, Some(3 * HOUR_MS));
    assert_eq!(health.reconnect_count, 4);
    assert_eq!(health.fault_count, 2);
    assert_eq!(stream.symbols(), &BTreeSet::from(["BTCUSDT".to_owned()]));
    assert_eq!(StreamContinuity::from(&health), continuity);
    // Nothing is sampled or reconciled while the transport is down.
    assert!(stream.sample_tickers(HOUR_MS, HOUR_MS).is_none());
    assert!(!stream.reconcile_tickers(7, &[], 1, 2));
}

#[test]
fn the_event_queue_has_a_hard_cap() {
    assert_eq!(stream_event_capacity(0), 64);
    assert_eq!(stream_event_capacity(100), 200);
    assert_eq!(stream_event_capacity(10_000), MAX_STREAM_EVENTS);
}
