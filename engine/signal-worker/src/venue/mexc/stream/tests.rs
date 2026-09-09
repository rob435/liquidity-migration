use super::*;

use crate::venue::mexc::ContractRow;

/// Recorded live from `wss://contract.mexc.com/edge` on 2026-09-09: both
/// subscription replies, the refusal for a contract that does not exist, the
/// keep-alive reply, and `push.ticker`/`push.kline` for a
/// 0.0001-per-contract name and a 10000000-per-contract one.
const FRAMES: &str = include_str!("../../../../tests/fixtures/mexc/stream_frames.json");

const BAR: i64 = 1_788_944_400_000;

fn frames() -> Vec<String> {
    serde_json::from_str(FRAMES).expect("recorded frames are JSON")
}

fn contracts() -> Arc<ContractTable> {
    let mut table = ContractTable::default();
    for (symbol, venue_symbol, contract_size, cycle, settle) in [
        (
            "BTCUSDT",
            "BTC_USDT",
            0.0001,
            Some(8),
            Some(1_788_969_600_000),
        ),
        (
            "PEPEUSDT",
            "PEPE_USDT",
            10_000_000.0,
            Some(8),
            Some(1_788_969_600_000),
        ),
        ("XRPUSDT", "XRP_USDT", 1.0, None, None),
    ] {
        table.insert(
            symbol.to_owned(),
            ContractRow {
                venue_symbol: venue_symbol.to_owned(),
                contract_size,
                cycle_hours: cycle,
                next_settle_ms: settle,
            },
        );
    }
    Arc::new(table)
}

fn parsed(text: &str, received_at_ms: i64) -> ParsedMessage {
    parse_frame(text, &contracts(), received_at_ms).expect("recorded frame must parse")
}

fn kline_frame(venue_symbol: &str, open_ts_ms: i64, close: f64, volume_contracts: f64) -> String {
    serde_json::json!({
        "symbol": venue_symbol,
        "channel": "push.kline",
        "ts": open_ts_ms,
        "data": {
            "symbol": venue_symbol,
            "interval": "Min60",
            "t": open_ts_ms / 1_000,
            "o": close, "c": close, "h": close, "l": close,
            "a": 100.0, "q": volume_contracts,
            "ro": close, "rc": close, "rh": close, "rl": close,
        }
    })
    .to_string()
}

#[test]
fn the_recorded_replies_and_refusal_parse_into_the_neutral_vocabulary() {
    let mut seen = Vec::new();
    for frame in frames() {
        seen.push(parsed(&frame, BAR + HOUR_MS));
    }
    assert!(seen.contains(&ParsedMessage::TickerAck { accepted: true }));
    assert!(seen.contains(&ParsedMessage::KlineAck { accepted: true }));
    assert!(seen.contains(&ParsedMessage::Pong));
    // The refusal names the contract and not the channel, which is why a
    // refused contract is quarantined on both of its subscriptions.
    let refusal = seen
        .iter()
        .find(|message| matches!(message, ParsedMessage::Refusal { .. }))
        .expect("the recording carries a refusal");
    let ParsedMessage::Refusal { venue_symbol, why } = refusal else {
        unreachable!()
    };
    assert_eq!(venue_symbol.as_deref(), Some("NOPE_USDT"));
    assert!(why.contains("not exists"), "{why}");

    // A reply that is not the venue's own "success" is not an acceptance.
    assert_eq!(
        parsed(r#"{"channel":"rs.sub.ticker","data":"denied","ts":1}"#, 1),
        ParsedMessage::TickerAck { accepted: false }
    );
}

#[test]
fn a_recorded_ticker_push_carries_the_mark_the_touch_and_a_converted_quantity() {
    let now = 1_788_947_200_000_i64;
    let frame = frames()
        .into_iter()
        .find(|frame| frame.contains("push.ticker") && frame.contains("BTC_USDT"))
        .unwrap();
    let ParsedMessage::Ticker(row) = parsed(&frame, now) else {
        panic!("expected a ticker");
    };
    let recorded: Value = serde_json::from_str(&frame).unwrap();
    let data = &recorded["data"];
    assert_eq!(row.symbol, "BTCUSDT");
    assert_eq!(row.mark_price.as_ref(), Some(&data["fairPrice"]));
    assert_eq!(row.index_price.as_ref(), Some(&data["indexPrice"]));
    assert_eq!(row.last_price.as_ref(), Some(&data["lastPrice"]));
    assert_eq!(row.bid1_price.as_ref(), Some(&data["bid1"]));
    assert_eq!(row.ask1_price.as_ref(), Some(&data["ask1"]));
    assert_eq!(row.turnover24h.as_ref(), Some(&data["amount24"]));
    assert_eq!(row.funding_rate.as_ref(), Some(&data["fundingRate"]));
    let hold = value_f64(&data["holdVol"], "holdVol").unwrap();
    assert_eq!(
        row.open_interest,
        base_quantity(Some(&data["holdVol"]), 0.0001)
    );
    assert!(hold > 0.0);
    // The channel states no size and no open-interest notional.
    assert!(row.bid1_size.is_none() && row.ask1_size.is_none());
    assert!(row.open_interest_value.is_none());
    // The channel carries no settlement clock: it comes from the cycle and
    // stamp the funding page gave the contract table.
    assert_eq!(
        row.next_funding_time,
        Some(Value::from(1_788_969_600_000_i64))
    );

    // A sub-cent price is a JSON number in scientific notation and still a
    // positive price.
    let pepe = frames()
        .into_iter()
        .find(|frame| frame.contains("push.ticker") && frame.contains("PEPE_USDT"))
        .unwrap();
    let ParsedMessage::Ticker(row) = parsed(&pepe, now) else {
        panic!("expected a ticker");
    };
    assert_eq!(row.symbol, "PEPEUSDT");
    let observed = crate::normalize::normalize_ticker_strict(now, now, &row).unwrap();
    assert!(observed.mark_price.is_some_and(|price| price > 0.0));
    assert!(observed.volume_24h.is_some_and(|volume| volume > 0.0));
}

#[test]
fn a_recorded_kline_push_is_the_traded_bar_in_milliseconds_and_base_coin() {
    let frame = frames()
        .into_iter()
        .find(|frame| frame.contains("push.kline") && frame.contains("BTC_USDT"))
        .unwrap();
    let ParsedMessage::Kline(bar) = parsed(&frame, BAR + HOUR_MS) else {
        panic!("expected a kline");
    };
    let recorded: Value = serde_json::from_str(&frame).unwrap();
    let data = &recorded["data"];
    assert_eq!(bar.symbol, "BTCUSDT");
    assert_eq!(bar.open_ts_ms, BAR);
    assert_eq!(bar.row[0], Value::from(BAR));
    assert_eq!(bar.row[1], data["ro"]);
    assert_eq!(bar.row[2], data["rh"]);
    assert_eq!(bar.row[3], data["rl"]);
    assert_eq!(bar.row[4], data["rc"]);
    assert_eq!(bar.row[5], base_quantity(Some(&data["q"]), 0.0001).unwrap());
    assert_eq!(bar.row[6], data["a"]);
    // `o`/`c`/`h`/`l` are the stitched series whose open is the previous bar's
    // close; the recorded frame has an `o` that is not the traded open.
    assert_ne!(data["o"], data["ro"]);
    assert_ne!(bar.row[1], data["o"]);

    // Another interval on the same channel is not this stream's bar.
    let minute = frame.replace(r#""interval": "Min60""#, r#""interval": "Min1""#);
    let minute = minute.replace(r#""interval":"Min60""#, r#""interval":"Min1""#);
    assert_eq!(parsed(&minute, BAR + HOUR_MS), ParsedMessage::Ignore);
}

#[test]
fn a_frame_for_a_contract_the_table_does_not_name_is_ignored() {
    let frame = kline_frame("NOPE_USDT", BAR, 100.0, 1.0);
    assert_eq!(parsed(&frame, BAR + HOUR_MS), ParsedMessage::Ignore);
    let ticker = serde_json::json!({
        "symbol": "NOPE_USDT", "channel": "push.ticker", "ts": 1,
        "data": {"symbol": "NOPE_USDT", "fairPrice": 1.0}
    })
    .to_string();
    assert_eq!(parsed(&ticker, BAR + HOUR_MS), ParsedMessage::Ignore);
}

#[test]
fn a_bar_off_the_hour_or_a_frame_whose_symbols_disagree_is_a_fault() {
    let mut frame: Value = serde_json::from_str(&kline_frame("BTC_USDT", BAR, 100.0, 1.0)).unwrap();
    frame["data"]["t"] = Value::from((BAR + 1_800_000) / 1_000);
    let error = parse_frame(&frame.to_string(), &contracts(), BAR + HOUR_MS).unwrap_err();
    assert!(error.contains("not an hour boundary"), "{error}");

    let mut frame: Value = serde_json::from_str(&kline_frame("BTC_USDT", BAR, 100.0, 1.0)).unwrap();
    frame["symbol"] = Value::from("PEPE_USDT");
    let error = parse_frame(&frame.to_string(), &contracts(), BAR + HOUR_MS).unwrap_err();
    assert!(error.contains("symbols disagree"), "{error}");
}

fn test_worker(symbols: &[&str]) -> (StreamWorker, mpsc::Receiver<StreamEvent>) {
    let symbols = normalize_symbols(symbols.iter().map(|s| (*s).to_owned()).collect()).unwrap();
    let (events_tx, events) = mpsc::channel(stream_event_capacity(symbols.len()));
    let (control_tx, _control) = watch::channel(ControlState::default());
    let options = StreamOptions::production(1_000, 1);
    let shared = Arc::new(Mutex::new(SharedState::continuing(
        &symbols,
        StreamContinuity::default(),
    )));
    (
        StreamWorker {
            url: "ws://127.0.0.1:1".to_owned(),
            symbols,
            contracts: contracts(),
            quarantined: BTreeSet::new(),
            open_bars: BTreeMap::new(),
            published_through: BTreeMap::new(),
            shared,
            events: events_tx,
            control: control_tx,
            options,
            epoch: 1,
            backoff: options.backoff_start,
            next_ping_at: Instant::now() + options.ping_interval,
            pong_deadline: None,
            last_data_at: Instant::now(),
            next_quarantine_reprobe_at: Instant::now() + options.quarantine_reprobe_interval,
        },
        events,
    )
}

/// The venue re-sends the open bar on every trade and marks no bar confirmed.
#[tokio::test]
async fn a_held_bar_is_published_once_when_a_later_bar_arrives() {
    let (mut worker, _events) = test_worker(&["BTCUSDT"]);
    let inside_the_hour = BAR + 600_000;
    // Three updates of the same open bar publish nothing.
    for close in [100.0, 101.0, 102.0] {
        let ParsedMessage::Kline(frame) =
            parsed(&kline_frame("BTC_USDT", BAR, close, 1.0), inside_the_hour)
        else {
            panic!("expected a kline");
        };
        assert!(worker
            .hold_kline(*frame, inside_the_hour)
            .unwrap()
            .is_empty());
    }
    // The first frame of the next hour makes the held bar final, with its last
    // seen values and the instant this process learned it was final.
    let next_hour = BAR + HOUR_MS;
    let ParsedMessage::Kline(frame) = parsed(
        &kline_frame("BTC_USDT", next_hour, 103.0, 1.0),
        next_hour + 1_000,
    ) else {
        panic!("expected a kline");
    };
    let published = worker.hold_kline(*frame, next_hour + 1_000).unwrap();
    assert_eq!(published.len(), 1);
    assert_eq!(published[0].symbol, "BTCUSDT");
    assert_eq!(published[0].row[0], Value::from(BAR));
    assert_eq!(published[0].row[4], Value::from(102.0));
    assert_eq!(published[0].available_at_ms, next_hour + 1_000);

    // A repeat of the closed bar, and of the bar now open, publish nothing
    // more: each bar leaves once.
    for (open, at) in [(BAR, next_hour + 2_000), (next_hour, next_hour + 3_000)] {
        let ParsedMessage::Kline(frame) = parsed(&kline_frame("BTC_USDT", open, 104.0, 1.0), at)
        else {
            panic!("expected a kline");
        };
        assert!(worker.hold_kline(*frame, at).unwrap().is_empty());
    }
}

/// A contract that trades rarely may only be heard from after its hour has
/// gone. That frame is itself a closed bar.
#[tokio::test]
async fn a_frame_whose_own_hour_has_elapsed_is_published_immediately() {
    let (mut worker, _events) = test_worker(&["BTCUSDT"]);
    let late = BAR + HOUR_MS + 5_000;
    let ParsedMessage::Kline(frame) = parsed(&kline_frame("BTC_USDT", BAR, 100.0, 2.0), late)
    else {
        panic!("expected a kline");
    };
    let published = worker.hold_kline(*frame, late).unwrap();
    assert_eq!(published.len(), 1);
    assert_eq!(published[0].row[0], Value::from(BAR));
    assert_eq!(published[0].row[5], Value::from(0.0002));
    assert!(worker.open_bars.is_empty(), "a closed bar is not held open");

    // And it is not published a second time when the venue repeats it.
    let ParsedMessage::Kline(frame) =
        parsed(&kline_frame("BTC_USDT", BAR, 100.0, 2.0), late + 1_000)
    else {
        panic!("expected a kline");
    };
    assert!(worker.hold_kline(*frame, late + 1_000).unwrap().is_empty());
}

/// A bar whose hour has not elapsed by this process's clock is never
/// published, the same guard the Bybit stream puts on a confirmed frame.
#[tokio::test]
async fn a_bar_whose_hour_has_not_elapsed_here_is_never_published() {
    let (mut worker, _events) = test_worker(&["BTCUSDT"]);
    // A venue clock ahead of ours: a frame for the next bar arrives while the
    // held bar's hour has not finished here.
    let ParsedMessage::Kline(held) = parsed(&kline_frame("BTC_USDT", BAR, 100.0, 1.0), BAR + 1_000)
    else {
        panic!("expected a kline");
    };
    assert!(worker.hold_kline(*held, BAR + 1_000).unwrap().is_empty());
    let ParsedMessage::Kline(next) = parsed(
        &kline_frame("BTC_USDT", BAR + HOUR_MS, 101.0, 1.0),
        BAR + 2_000,
    ) else {
        panic!("expected a kline");
    };
    assert!(worker.hold_kline(*next, BAR + 2_000).unwrap().is_empty());
    assert_eq!(worker.published_through.get("BTCUSDT"), None);
}

#[tokio::test]
async fn the_ticker_cache_ages_each_field_and_only_counts_a_covered_symbol() {
    let (mut worker, _events) = test_worker(&["BTCUSDT", "PEPEUSDT"]);
    let now = 1_788_947_200_000_i64;
    for frame in frames() {
        if !frame.contains("push.ticker") {
            continue;
        }
        let ParsedMessage::Ticker(row) = parsed(&frame, now) else {
            panic!("expected a ticker");
        };
        worker.hold_ticker(*row, now);
    }
    let state = worker.shared.lock().unwrap();
    assert_eq!(state.tickers.len(), 2);
    assert_eq!(state.tickers.capacity(), 2);
    let rows = state.tickers.sample(now + 1_000, 30_000);
    assert_eq!(rows.len(), 2);
    assert!(rows.iter().all(|row| row.mark_price.is_some()));
    assert!(rows.iter().all(|row| row.mark_observed_ts_ms == Some(now)));
    // Past the freshness window nothing is a current observation.
    assert!(state.tickers.sample(now + 60_000, 30_000).is_empty());
    // Coverage needs the subscription counts too, which no epoch has set.
    assert!(state.tickers.ws_coverage_complete());
    assert!(!state.ws_ticker_coverage_complete());
}

#[test]
fn a_rest_row_fills_only_the_fields_the_socket_has_not_refreshed() {
    let allowed = ["BTCUSDT".to_owned()].into_iter().collect();
    let mut cache = TickerCache::new(allowed);
    let socket_row = BybitTickerWire {
        symbol: "BTCUSDT".into(),
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: Some(Value::from(100.0)),
        mark_price: Some(Value::from(101.0)),
        index_price: None,
        bid1_price: None,
        ask1_price: None,
        bid1_size: None,
        ask1_size: None,
        open_interest: None,
        open_interest_value: None,
        turnover24h: None,
        volume24h: None,
        funding_rate: None,
        next_funding_time: None,
    };
    let mut rest_row = socket_row.clone();
    cache.apply(socket_row, 2_000);
    rest_row.mark_price = Some(Value::from(999.0));
    rest_row.index_price = Some(Value::from(102.0));
    // The REST read started before the socket frame landed, so its mark is
    // older news and its index price is new news.
    cache.reconcile_rest(rest_row, 1_000, 3_000);
    let rows = cache.sample(3_000, 30_000);
    assert_eq!(rows[0].mark_price, Some(Value::from(101.0)));
    assert_eq!(rows[0].index_price, Some(Value::from(102.0)));
}

#[test]
fn the_keep_alive_and_the_subscriptions_are_the_frames_the_venue_asks_for() {
    assert_eq!(PING_PAYLOAD, r#"{"method":"ping"}"#);
    let [ticker, kline] = subscription_frames("BTC_USDT");
    assert_eq!(
        serde_json::from_str::<Value>(&ticker).unwrap(),
        serde_json::json!({"method": "sub.ticker", "param": {"symbol": "BTC_USDT"}})
    );
    assert_eq!(
        serde_json::from_str::<Value>(&kline).unwrap(),
        serde_json::json!({
            "method": "sub.kline",
            "param": {"symbol": "BTC_USDT", "interval": "Min60"}
        })
    );
}

#[test]
fn the_stream_url_is_the_realm_tables_websocket_and_not_its_rest_host() {
    assert_eq!(public_stream_url(), "wss://contract.mexc.com/edge");
    assert!(!public_stream_url().contains(crate::venue::mexc::rest_host()));
}

#[test]
fn event_queue_has_a_hard_cap_and_a_symbol_must_be_alphanumeric() {
    assert_eq!(stream_event_capacity(1), 64);
    assert_eq!(stream_event_capacity(150), 300);
    assert_eq!(stream_event_capacity(10_000), MAX_STREAM_EVENTS);
    assert!(normalize_symbols(vec!["btcusdt".into()])
        .unwrap()
        .contains("BTCUSDT"));
    assert!(normalize_symbols(vec!["BTC_USDT".into()]).is_err());
    assert!(normalize_symbols(Vec::new()).is_err());
}

#[test]
fn the_refused_contract_is_read_out_of_the_venues_own_wording() {
    assert_eq!(
        bracketed("Contract [NOPE_USDT] not exists").as_deref(),
        Some("NOPE_USDT")
    );
    assert_eq!(bracketed("Contract [] not exists"), None);
    assert_eq!(bracketed("no reason"), None);
}

async fn serve_epoch(listener: &tokio::net::TcpListener, refuse: &'static str) {
    let (stream, _) = listener.accept().await.unwrap();
    let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
    let mut answered = 0;
    while answered < 4 {
        let request = socket.next().await.unwrap().unwrap().into_text().unwrap();
        let request: Value = serde_json::from_str(&request).unwrap();
        let method = request["method"].as_str().unwrap().to_owned();
        let symbol = request["param"]["symbol"].as_str().unwrap().to_owned();
        answered += 1;
        let reply = if symbol == refuse {
            serde_json::json!({
                "channel": "rs.error",
                "data": format!("Contract [{symbol}] not exists"),
                "ts": 1
            })
        } else {
            serde_json::json!({
                "channel": format!("rs.{method}"),
                "data": "success",
                "ts": 1
            })
        };
        socket.send(Message::text(reply.to_string())).await.unwrap();
    }
    let now = wall_ms().unwrap();
    let bar = (now / HOUR_MS - 1) * HOUR_MS;
    socket
        .send(Message::text(
            serde_json::json!({
                "symbol": "BTC_USDT", "channel": "push.ticker", "ts": now,
                "data": {"symbol": "BTC_USDT", "lastPrice": 100.0, "fairPrice": 101.0,
                         "indexPrice": 102.0, "bid1": 99.0, "ask1": 101.5,
                         "volume24": 10.0, "amount24": 1000.0, "holdVol": 5.0,
                         "fundingRate": 0.0001, "timestamp": now}
            })
            .to_string(),
        ))
        .await
        .unwrap();
    socket
        .send(Message::text(kline_frame("BTC_USDT", bar, 100.0, 1.0)))
        .await
        .unwrap();
    while socket.next().await.is_some() {}
}

async fn next(stream: &mut MexcPublicStream) -> StreamEvent {
    tokio::time::timeout(Duration::from_secs(3), stream.next_event())
        .await
        .unwrap()
        .unwrap()
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
        backoff_start: Duration::from_secs(10),
        backoff_max: Duration::from_secs(10),
    }
}

/// The venue answers each request once and its successes are anonymous, so the
/// accepted counts come from the two reply channels and the quarantine from
/// the contract the refusal names.
#[tokio::test(start_paused = true)]
async fn one_refused_contract_is_quarantined_without_disabling_the_others() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move { serve_epoch(&listener, "PEPE_USDT").await });
    let mut stream = MexcPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BTCUSDT".into(), "PEPEUSDT".into()],
        contracts(),
        test_options(),
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
    assert!(health.gap_open, "a fresh epoch owes REST repair");
    assert_eq!(health.ticker_topics_accepted, 1);
    assert_eq!(health.kline_topics_accepted, 1);
    assert_eq!(health.ticker_topics_quarantined, 1);
    assert_eq!(health.kline_topics_quarantined, 1);
    assert!(stream.mark_gap_repaired(1));
    assert!(!stream.health().gap_open);
    assert!(
        !stream.mark_gap_repaired(2),
        "another epoch's repair is not this one's"
    );

    loop {
        if matches!(next(&mut stream).await, StreamEvent::KlineClosed(_)) {
            break;
        }
    }
    let sample = stream
        .sample_tickers(wall_ms().unwrap(), 30_000)
        .expect("the accepted contract has a ticker");
    assert_eq!(sample.rows.len(), 1);
    assert_eq!(sample.rows[0].symbol, "BTCUSDT");
    // One of two contracts is quarantined, so coverage is not complete.
    assert!(!stream.health().ticker_coverage_complete);

    // A source fault clears the cache, opens the gap and counts.
    let before = stream.health().fault_count;
    stream.mark_source_fault(wall_ms().unwrap());
    let health = stream.health();
    assert_eq!(health.fault_count, before + 1);
    assert!(health.gap_open);
    assert_eq!(health.ticker_rows, 0);
}

#[tokio::test(start_paused = true)]
async fn every_contract_accepted_and_marked_makes_coverage_complete() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move { serve_epoch(&listener, "NONE").await });
    let mut stream = MexcPublicStream::with_url(
        format!("ws://127.0.0.1:{port}"),
        vec!["BTCUSDT".into(), "PEPEUSDT".into()],
        contracts(),
        test_options(),
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
    assert_eq!(health.ticker_topics_accepted, 2);
    assert_eq!(health.kline_topics_accepted, 2);
    assert_eq!(health.ticker_topics_quarantined, 0);
    // The socket has one of the two marks; the REST reconciliation brings the
    // other and completes the coverage.
    let now = wall_ms().unwrap();
    assert!(stream.sample_tickers(now, 30_000).is_some());
    assert!(!stream.health().ticker_coverage_complete);
    let pepe = BybitTickerWire {
        symbol: "PEPEUSDT".into(),
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: Some(Value::from(1.0)),
        mark_price: Some(Value::from(1.0)),
        index_price: None,
        bid1_price: None,
        ask1_price: None,
        bid1_size: None,
        ask1_size: None,
        open_interest: None,
        open_interest_value: None,
        turnover24h: None,
        volume24h: None,
        funding_rate: None,
        next_funding_time: None,
    };
    assert!(!stream.reconcile_tickers(1, std::slice::from_ref(&pepe), now, now));
    assert!(!stream.reconcile_tickers(9, std::slice::from_ref(&pepe), now, now));
    let sample = stream.sample_tickers(now, 30_000).unwrap();
    assert_eq!(sample.rows.len(), 2);
    // A REST row alone is not the socket's own snapshot, so coverage stays
    // incomplete until the socket has seen the symbol.
    assert!(!stream.health().ticker_coverage_complete);
}
