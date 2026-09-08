use super::*;

#[derive(Debug)]
pub(super) enum Phase {
    Retry(Instant),
    Ready,
    Pong,
}

struct TimeDriver(tokio::task::JoinHandle<()>);
impl Drop for TimeDriver {
    fn drop(&mut self) {
        self.0.abort();
    }
}

#[derive(Clone, Copy)]
enum TimerPlan {
    Retries,
    SilentEpochs(usize),
    FirstQuoteAfter(Duration),
}

fn drive_timers(feed: &mut BybitPublicFeed, plan: TimerPlan) -> TimeDriver {
    let (tx, mut rx) = tokio::sync::mpsc::channel(16);
    feed.test_phases = Some(tx);
    let ping_interval = feed.timing.ping_interval;
    TimeDriver(tokio::spawn(async move {
        let mut epochs = 0;
        while let Some(phase) = rx.recv().await {
            let delay = match phase {
                Phase::Retry(at) => Some(at.saturating_duration_since(Instant::now())),
                Phase::Ready => {
                    epochs += 1;
                    match plan {
                        TimerPlan::SilentEpochs(count) if epochs <= count => Some(ping_interval),
                        TimerPlan::FirstQuoteAfter(delay) if epochs == 1 => Some(delay),
                        _ => None,
                    }
                }
                Phase::Pong => match plan {
                    TimerPlan::SilentEpochs(count) if epochs <= count => Some(ping_interval),
                    _ => None,
                },
            };
            if let Some(mut remaining) = delay {
                // The caller can cancel its losing feed future at each flush tick.
                while !remaining.is_zero() {
                    let step = remaining.min(Duration::from_millis(250));
                    tokio::time::advance(step).await;
                    tokio::task::yield_now().await;
                    remaining -= step;
                }
            }
        }
    }))
}

fn subs() -> Vec<Subscription> {
    vec![
        Subscription {
            symbol: "btcusdt".into(),
            feed: Feed::Quote,
        },
        Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Ticker,
        },
        Subscription {
            symbol: "ETHUSDT".into(),
            feed: Feed::Quote,
        },
    ]
}

#[test]
fn subscriptions_become_topics_once_each() {
    let feed = BybitPublicFeed::new(&subs());
    assert_eq!(
        feed.topics(),
        [
            "orderbook.1.BTCUSDT",
            "orderbook.1.ETHUSDT",
            "tickers.BTCUSDT"
        ]
    );
    assert_eq!(feed.symbols().len(), 2);
}

#[tokio::test]
async fn a_book_gap_refreshes_only_its_topic_and_invalidates_its_quote() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let request = socket.next().await.unwrap().unwrap();
        let (id, _) = subscribe_request(&request);
        socket
            .send(Message::text(subscribe_reply(&id, true, "")))
            .await
            .unwrap();
        for symbol in ["BTCUSDT", "ETHUSDT"] {
            socket
                .send(Message::text(snapshot_for(symbol, 1, 10.0, 10.1)))
                .await
                .unwrap();
        }
        socket
            .send(Message::text(
                snapshot_for("ETHUSDT", 3, 20.0, 20.1).replace("\"snapshot\"", "\"delta\""),
            ))
            .await
            .unwrap();
        let request = socket.next().await.unwrap().unwrap();
        let (op, id, topics) = operation_request(&request);
        assert_eq!(op, "unsubscribe");
        assert_eq!(topics, ["orderbook.1.ETHUSDT"]);
        socket
            .send(Message::text(snapshot_for("BTCUSDT", 2, 30.0, 30.1)))
            .await
            .unwrap();
        socket
            .send(Message::text(unsubscribe_reply(&id, true, "")))
            .await
            .unwrap();
        let request = socket.next().await.unwrap().unwrap();
        let (id, topics) = subscribe_request(&request);
        assert_eq!(topics, ["orderbook.1.ETHUSDT"]);
        socket
            .send(Message::text(subscribe_reply(&id, true, "")))
            .await
            .unwrap();
        socket
            .send(Message::text(snapshot_for("ETHUSDT", 4, 40.0, 40.1)))
            .await
            .unwrap();
        std::future::pending::<()>().await;
    });
    let mut feed = BybitPublicFeed::with_url(
        format!("ws://{address}"),
        &[
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "ETHUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    );
    let mut healthy_updated = false;
    let mut invalidated = false;
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match feed.next_event().await.unwrap() {
                MarketEvent::FeedReset { .. } => panic!("one book gap reset every symbol"),
                MarketEvent::Quote {
                    symbol: SymbolId(1),
                    quote,
                } if quote.bid_px == 0.0 => invalidated = true,
                MarketEvent::Quote {
                    symbol: SymbolId(0),
                    quote,
                } if quote.bid_px == 30.0 => healthy_updated = true,
                MarketEvent::Quote {
                    symbol: SymbolId(1),
                    quote,
                } if quote.bid_px == 40.0 => break,
                _ => {}
            }
        }
    })
    .await
    .expect("one-topic recovery completes");
    server.abort();
    assert!(invalidated, "the broken book remained tradable");
    assert!(
        healthy_updated,
        "healthy traffic stopped during resubscription"
    );
}

#[test]
fn l50_and_public_trades_use_the_exact_bybit_topics() {
    assert_eq!(
        topic_for(&Subscription {
            symbol: "ONTUSDT".into(),
            feed: Feed::Depth,
        }),
        "orderbook.50.ONTUSDT",
    );
    assert_eq!(
        topic_for(&Subscription {
            symbol: "ONTUSDT".into(),
            feed: Feed::Trades,
        }),
        "publicTrade.ONTUSDT",
    );
}

#[test]
fn only_l1_quotes_have_a_per_topic_idle_contract() {
    assert!(is_quote_topic("orderbook.1.BTCUSDT"));
    assert!(!is_quote_topic("orderbook.50.BTCUSDT"));
    assert!(!is_quote_topic("tickers.BTCUSDT"));
    assert!(!is_quote_topic("publicTrade.BTCUSDT"));
}

#[test]
fn the_subscribe_frame_is_the_venue_shape() {
    let topics = vec!["orderbook.1.BTCUSDT".to_string(), "tickers.BTCUSDT".into()];
    assert_eq!(
        subscribe_payload(&topics, "7"),
        r#"{"args":["orderbook.1.BTCUSDT","tickers.BTCUSDT"],"op":"subscribe","req_id":"7"}"#
    );
}

#[test]
fn subscription_response_code_is_parsed_as_an_integer() {
    let message = Message::text(
        r#"{"retCode":10404,"retMsg":"op type is not found","reqId":"7","op":"subscribe"}"#,
    );
    let ack = subscription_ack(&message).expect("subscription response");
    assert_eq!(ack.request_id, "7");
    assert_eq!(ack.code, Some(10404));
    assert!(!ack.success);
    assert_eq!(ack.ret_msg, "op type is not found");
}

#[test]
fn subscription_refusals_are_scoped_by_code_and_specific_topic_text() {
    assert_eq!(
        subscription_refusal_kind(None, "Invalid symbol"),
        SubscriptionRefusalKind::Topic
    );
    assert_eq!(
        subscription_refusal_kind(None, "args params error"),
        SubscriptionRefusalKind::Topic
    );
    assert_eq!(
        subscription_refusal_kind(Some(10404), "op type is not found"),
        SubscriptionRefusalKind::Global
    );
    assert_eq!(
        subscription_refusal_kind(None, "request handler not found"),
        SubscriptionRefusalKind::Transient
    );
    assert_eq!(
        subscription_refusal_kind(None, "not found"),
        SubscriptionRefusalKind::Transient
    );
    assert_eq!(
        subscription_refusal_kind(Some(10429), "system level frequency protection"),
        SubscriptionRefusalKind::Transient
    );
    assert_eq!(
        subscription_refusal_kind(Some(10016), "service is restarting"),
        SubscriptionRefusalKind::Transient
    );
}

#[test]
fn the_feed_dials_the_realm_tables_public_stream_and_nothing_else() {
    // The literal is pinned in `engine-venue`'s own fence, which is the
    // one place a venue host may be written down. What this crate has to
    // promise is only that it reads it from there — a host spelled out
    // here would be one the fence never sees.
    assert_eq!(
        bybit_public_linear_url(),
        engine_public::VenueRealm::Demo.public_ws()
    );
    assert!(bybit_public_linear_url().starts_with("wss://"));
    assert!(bybit_public_linear_url().ends_with("/v5/public/linear"));
}

#[test]
fn the_clock_moves_forward() {
    let clock = MonoClock::new();
    let first = clock.now_ns();
    let second = clock.now_ns();
    assert!(second >= first);
}

#[tokio::test(start_paused = true)]
async fn the_handoff_coalesces_l1_without_crossing_an_epoch_reset() {
    let _io = crate::test_io::IoProgress::new();
    let handoff = Handoff::new();
    let quote = |symbol, bid_px| MarketEvent::Quote {
        symbol: SymbolId(symbol),
        quote: engine_types::Quote {
            bid_px,
            ..engine_types::Quote::default()
        },
    };
    assert!(handoff.push(Ok(quote(0, 10.0))));
    assert!(handoff.push(Ok(quote(0, 11.0))));
    assert!(handoff.push(Ok(quote(1, 20.0))));
    assert!(handoff.push(Ok(MarketEvent::FeedReset { recv_ns: 7 })));
    assert!(handoff.push(Ok(quote(0, 12.0))));

    assert!(
        matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(0), quote })) if quote.bid_px == 11.0)
    );
    assert!(
        matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(1), quote })) if quote.bid_px == 20.0)
    );
    assert!(matches!(
        handoff.recv().await,
        Some(Ok(MarketEvent::FeedReset { recv_ns: 7 }))
    ));
    assert!(
        matches!(handoff.recv().await, Some(Ok(MarketEvent::Quote { symbol: SymbolId(0), quote })) if quote.bid_px == 12.0)
    );
}

#[tokio::test(start_paused = true)]
async fn the_handoff_adds_trade_flow_instead_of_dropping_bursts() {
    let _io = crate::test_io::IoProgress::new();
    let handoff = Handoff::new();
    let flow = |buy_qty, sell_qty, seq| MarketEvent::Trades {
        symbol: SymbolId(0),
        trades: engine_types::TradeFlow {
            buy_qty,
            sell_qty,
            last_px: seq as f64,
            trade_count: 1,
            seq,
            recv_ns: seq,
            ..engine_types::TradeFlow::default()
        },
    };
    assert!(handoff.push(Ok(flow(1.0, 0.0, 1))));
    assert!(handoff.push(Ok(flow(0.0, 2.0, 2))));
    let Some(Ok(MarketEvent::Trades { trades, .. })) = handoff.recv().await else {
        panic!("expected merged trade flow");
    };
    assert_eq!(trades.buy_qty, 1.0);
    assert_eq!(trades.sell_qty, 2.0);
    assert_eq!(trades.trade_count, 2);
    assert_eq!(trades.last_px, 2.0);
    assert_eq!(trades.seq, 2);
}

fn operation_request(message: &Message) -> (String, String, Vec<String>) {
    let value: serde_json::Value =
        serde_json::from_str(message.to_text().expect("text request")).expect("request JSON");
    let operation = value["op"].as_str().expect("request operation").to_owned();
    let request_id = value["req_id"].as_str().expect("request id").to_owned();
    let topics = value["args"]
        .as_array()
        .expect("request topics")
        .iter()
        .map(|topic| topic.as_str().expect("topic").to_owned())
        .collect();
    (operation, request_id, topics)
}

fn subscribe_request(message: &Message) -> (String, Vec<String>) {
    let (operation, request_id, topics) = operation_request(message);
    assert_eq!(operation, "subscribe");
    (request_id, topics)
}

fn subscribe_reply(request_id: &str, success: bool, ret_msg: &str) -> String {
    serde_json::json!({
        "success": success,
        "ret_msg": ret_msg,
        "conn_id": "x",
        "req_id": request_id,
        "op": "subscribe"
    })
    .to_string()
}

fn subscribe_reply_with_code(request_id: &str, code: i64, ret_msg: &str) -> String {
    serde_json::json!({
        "success": code == 0,
        "retCode": code,
        "retMsg": ret_msg,
        "conn_id": "x",
        "reqId": request_id,
        "op": "subscribe"
    })
    .to_string()
}

fn unsubscribe_reply(request_id: &str, success: bool, ret_msg: &str) -> String {
    serde_json::json!({
        "success": success,
        "ret_msg": ret_msg,
        "conn_id": "x",
        "req_id": request_id,
        "op": "unsubscribe"
    })
    .to_string()
}

fn snapshot(update_id: u64, bid: f64, ask: f64) -> String {
    snapshot_for("BTCUSDT", update_id, bid, ask)
}

fn snapshot_for(symbol: &str, update_id: u64, bid: f64, ask: f64) -> String {
    format!(
        r#"{{"topic":"orderbook.1.{symbol}","ts":10,"type":"snapshot","data":{{"s":"{symbol}","b":[["{bid}","1"]],"a":[["{ask}","1"]],"u":{update_id}}},"cts":9}}"#
    )
}

async fn next(feed: &mut BybitPublicFeed) -> MarketEvent {
    tokio::time::timeout(Duration::from_secs(10), feed.next_event())
        .await
        .expect("an event before the deadline")
        .expect("feed stays healthy")
}

/// Accept one connection, echo the subscribe request back to the test,
/// send the scripted frames, then hang up.
async fn serve_once(
    listener: &tokio::net::TcpListener,
    frames: &[String],
    subscribes: &tokio::sync::mpsc::UnboundedSender<String>,
) {
    let (stream, _) = listener.accept().await.expect("accepts");
    let mut ws = tokio_tungstenite::accept_async(stream)
        .await
        .expect("handshakes");
    if let Some(Ok(message)) = ws.next().await {
        subscribes
            .send(message.to_text().expect("text request").to_owned())
            .expect("test is listening");
        let (request_id, _) = subscribe_request(&message);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("sends ack");
    }
    for frame in frames {
        ws.send(Message::text(frame.clone())).await.expect("sends");
    }
    ws.close(None).await.expect("closes");
}

/// A dropped socket must resubscribe, announce the break, and only then
/// deliver the new epoch's prices.
#[tokio::test(start_paused = true)]
async fn a_dropped_socket_resubscribes_and_announces_the_reset() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (tx, mut subscribes) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        serve_once(&listener, &[snapshot(100, 10.0, 10.1)], &tx).await;
        serve_once(&listener, &[snapshot(500, 11.0, 11.1)], &tx).await;
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    match next(&mut feed).await {
        MarketEvent::Quote { quote, .. } => assert_eq!(quote.bid_px, 10.0),
        other => panic!("expected the first epoch's quote, got {other:?}"),
    }
    // The break is announced before any price from the new socket.
    assert!(
        matches!(next(&mut feed).await, MarketEvent::FeedReset { .. }),
        "the reconnect must announce itself first"
    );
    match next(&mut feed).await {
        MarketEvent::Quote { quote, .. } => assert_eq!(quote.bid_px, 11.0),
        other => panic!("expected the second epoch's quote, got {other:?}"),
    }

    // Both sockets were told what to send.
    for _ in 0..2 {
        let sent = subscribes.recv().await.expect("a subscribe per connection");
        assert!(sent.contains("orderbook.1.BTCUSDT"), "subscribe was {sent}");
    }
}

#[tokio::test(start_paused = true)]
async fn ping_responses_cannot_hide_a_market_silent_socket() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (ping_tx, mut pings) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("first socket");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("first handshake");
        let request = ws.next().await.expect("first request").expect("read");
        let (request_id, _) = subscribe_request(&request);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("accepts first subscription");
        while let Some(Ok(message)) = ws.next().await {
            let Ok(text) = message.to_text() else {
                continue;
            };
            let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
                continue;
            };
            if value["op"] == "ping" {
                ping_tx.send(()).expect("test listens");
                ws.send(Message::text(
                    r#"{"success":true,"ret_msg":"pong","op":"ping"}"#,
                ))
                .await
                .expect("answers application ping");
                ws.send(Message::text(r#"{"op":"notice"}"#))
                    .await
                    .expect("sends an ignored control frame");
                ws.send(Message::text("not-json"))
                    .await
                    .expect("sends an unreadable frame");
            }
        }

        let (stream, _) = listener.accept().await.expect("second socket");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("second handshake");
        let request = ws.next().await.expect("second request").expect("read");
        let (request_id, _) = subscribe_request(&request);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("accepts second subscription");
        ws.send(Message::text(snapshot(200, 20.0, 20.1)))
            .await
            .expect("fresh epoch publishes market traffic");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    )
    .with_timing(
        Duration::from_millis(30),
        Duration::from_millis(25),
        Duration::from_millis(200),
    );
    let _timers = drive_timers(&mut feed, TimerPlan::SilentEpochs(1));

    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::FeedReset { .. }
    ));
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0
    ));
    let mut answered = 0;
    while pings.try_recv().is_ok() {
        answered += 1;
    }
    assert!(
        answered >= 2,
        "the first socket did not stay ping-responsive"
    );
}

#[tokio::test(start_paused = true)]
async fn ack_and_pong_only_epochs_escalate_reconnect_backoff() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (attempts_tx, mut attempts) = tokio::sync::mpsc::unbounded_channel();
    let (pings_tx, mut pings) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        for _ in 0..3 {
            let (stream, _) = listener.accept().await.expect("accepts epoch");
            attempts_tx
                .send(Instant::now())
                .expect("test tracks attempts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            let request = ws.next().await.expect("subscription").expect("read");
            let (request_id, _) = subscribe_request(&request);
            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                .await
                .expect("acknowledges subscription");
            while let Some(Ok(message)) = ws.next().await {
                let Ok(text) = message.to_text() else {
                    continue;
                };
                let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
                    continue;
                };
                if value["op"] == "ping" {
                    pings_tx.send(()).expect("test tracks pings");
                    ws.send(Message::text(
                        r#"{"success":true,"ret_msg":"pong","op":"ping"}"#,
                    ))
                    .await
                    .expect("answers application ping");
                }
            }
        }
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    )
    .with_timing(
        Duration::from_millis(30),
        Duration::from_millis(25),
        Duration::from_millis(150),
    );
    let _timers = drive_timers(&mut feed, TimerPlan::SilentEpochs(2));
    let waiting = tokio::spawn(async move {
        for _ in 0..2 {
            assert!(matches!(
                feed.next_event().await,
                Ok(MarketEvent::FeedReset { .. })
            ));
        }
    });

    let stamps = tokio::time::timeout(Duration::from_secs(4), async {
        let mut stamps = Vec::new();
        for _ in 0..3 {
            stamps.push(attempts.recv().await.expect("worker keeps reconnecting"));
        }
        stamps
    })
    .await
    .expect("three paced epochs before the deadline");
    waiting.await.expect("feed consumer");

    assert!(
        stamps[1].duration_since(stamps[0]) >= BACKOFF_START * 2,
        "the first market-silent epoch did not earn a 500ms retry: {stamps:?}"
    );
    assert!(
        stamps[2].duration_since(stamps[1]) >= BACKOFF_START * 4,
        "ACK/pong traffic reset the escalating retry delay: {stamps:?}"
    );
    let mut answered = 0;
    while pings.try_recv().is_ok() {
        answered += 1;
    }
    assert!(answered >= 4, "the silent epochs were not pong-responsive");
}

#[tokio::test(start_paused = true)]
async fn accepted_market_traffic_refreshes_the_idle_deadline() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let request = ws.next().await.expect("request").expect("read");
        let (request_id, _) = subscribe_request(&request);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("accepts subscription");
        tokio::time::sleep(Duration::from_millis(180)).await;
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("first market event");
        tokio::time::sleep(Duration::from_millis(180)).await;
        ws.send(Message::text(snapshot(101, 11.0, 11.1)))
            .await
            .expect("second market event");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    )
    .with_timing(
        Duration::from_secs(1),
        Duration::from_secs(1),
        Duration::from_millis(300),
    );
    let _timers = drive_timers(
        &mut feed,
        TimerPlan::FirstQuoteAfter(Duration::from_millis(180)),
    );

    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
    ));
    tokio::time::advance(Duration::from_millis(180)).await;
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0
    ));
}

#[tokio::test(start_paused = true)]
async fn one_frozen_quote_is_refreshed_without_interrupting_healthy_topics() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("one stable socket");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let request = ws.next().await.expect("boot request").expect("read");
        let (request_id, topics) = subscribe_request(&request);
        assert_eq!(topics, ["orderbook.1.BTCUSDT", "orderbook.1.ETHUSDT"]);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("accepts both quotes");
        ws.send(Message::text(snapshot_for("BTCUSDT", 1, 10.0, 10.1)))
            .await
            .expect("initial BTC quote");
        ws.send(Message::text(snapshot_for("ETHUSDT", 1, 20.0, 20.1)))
            .await
            .expect("initial ETH quote");
        let eth_last_event_at = Instant::now();

        let mut updates = 2_u64;
        let mut ticks = tokio::time::interval(Duration::from_millis(20));
        ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut eth_unsubscribed = false;
        loop {
            tokio::select! {
                _ = ticks.tick() => {
                    ws.send(Message::text(snapshot_for(
                        "BTCUSDT",
                        updates,
                        1_000.0 + updates as f64,
                        1_000.1 + updates as f64,
                    )))
                    .await
                    .expect("healthy BTC quote");
                    updates += 1;
                }
                incoming = ws.next() => {
                    let message = incoming.expect("refresh request").expect("reads request");
                    let (operation, request_id, topics) = operation_request(&message);
                    assert_eq!(topics, ["orderbook.1.ETHUSDT"]);
                    match operation.as_str() {
                        "unsubscribe" => {
                            assert!(!eth_unsubscribed, "ETH was unsubscribed twice");
                            assert!(
                                eth_last_event_at.elapsed() >= Duration::from_millis(140),
                                "ETH refreshed before its idle deadline"
                            );
                            eth_unsubscribed = true;
                            ws.send(Message::text(unsubscribe_reply(&request_id, true, "")))
                                .await
                                .expect("accepts ETH unsubscribe");
                        }
                        "subscribe" => {
                            assert!(eth_unsubscribed, "ETH resubscribed before unsubscribe");
                            ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                                .await
                                .expect("accepts ETH resubscribe");
                            ws.send(Message::text(snapshot_for(
                                "ETHUSDT", 2, 222.0, 222.1,
                            )))
                            .await
                            .expect("fresh ETH snapshot");
                            return;
                        }
                        other => panic!("unexpected operation {other}"),
                    }
                }
            }
        }
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "ETHUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    )
    .with_timing(
        Duration::from_secs(1),
        Duration::from_secs(1),
        Duration::from_millis(160),
    )
    .with_topic_timing(Duration::from_secs(1), Duration::from_millis(5));
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    let mut initial = Vec::with_capacity(2);
    for _ in 0..2 {
        match next(&mut feed).await {
            MarketEvent::Quote { quote, .. } => initial.push(quote.bid_px),
            other => panic!("expected an initial quote, got {other:?}"),
        }
    }
    assert!(
        initial.contains(&20.0),
        "initial ETH quote was not delivered"
    );
    assert!(
        initial.iter().any(|bid| *bid == 10.0 || *bid >= 1_000.0),
        "initial or coalesced healthy BTC quote was not delivered"
    );

    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let mut healthy_btc_updates = 0;
    loop {
        tokio::time::advance(Duration::from_millis(20)).await;
        let event = tokio::time::timeout_at(deadline, feed.next_event())
            .await
            .expect("frozen ETH recovers before the deadline")
            .expect("feed remains healthy");
        match event {
            MarketEvent::Quote { quote, .. } if quote.bid_px == 222.0 => break,
            MarketEvent::Quote { quote, .. } if quote.bid_px >= 1_000.0 => {
                healthy_btc_updates += 1;
            }
            MarketEvent::FeedReset { .. } => {
                panic!("a single frozen quote reconnected the whole feed")
            }
            _ => {}
        }
    }
    assert!(
        healthy_btc_updates > 0,
        "healthy BTC traffic stopped while ETH recovered"
    );
    server.await.expect("stable-socket server");
}

#[tokio::test(start_paused = true)]
async fn market_data_before_the_subscribe_ack_is_delivered() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let request = ws.next().await.expect("request").expect("read request");
        let (request_id, _) = subscribe_request(&request);
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("sends the early quote");
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("sends ack");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    match next(&mut feed).await {
        MarketEvent::Quote { quote, .. } => assert_eq!(quote.bid_px, 10.0),
        other => panic!("expected the quote that preceded the ack, got {other:?}"),
    }
}

#[tokio::test(start_paused = true)]
async fn request_wide_10404_is_surfaced_without_topic_bisection() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (requests_tx, mut requests) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        while let Some(Ok(request @ Message::Text(_))) = ws.next().await {
            let (request_id, topics) = subscribe_request(&request);
            requests_tx.send(topics).expect("test listens");
            ws.send(Message::text(subscribe_reply_with_code(
                &request_id,
                10404,
                "op type is not found",
            )))
            .await
            .expect("returns the request-wide refusal");
        }
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "ETHUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    let error = tokio::time::timeout(Duration::from_secs(2), feed.next_event())
        .await
        .expect("the global refusal is surfaced")
        .expect_err("the feed must not run after a global protocol refusal");
    assert!(
        matches!(error, FeedError::BadMessage(ref message) if message.contains("10404")),
        "unexpected refusal: {error}"
    );
    assert_eq!(
        requests.recv().await.expect("the original request"),
        ["orderbook.1.BTCUSDT", "orderbook.1.ETHUSDT"]
    );
    tokio::time::advance(Duration::from_millis(100)).await;
    let second = requests.try_recv();
    assert!(
        second.is_err(),
        "a request-wide refusal was incorrectly bisected"
    );
}

#[tokio::test(start_paused = true)]
async fn a_stray_refusal_cannot_break_an_active_feed() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (first_seen, continue_after_first) = tokio::sync::oneshot::channel();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let request = ws.next().await.expect("request").expect("read");
        let (request_id, _) = subscribe_request(&request);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("accepts subscription");
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("first quote");
        continue_after_first.await.expect("client saw first quote");

        ws.send(Message::text(subscribe_reply(
            &request_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("sends a delayed refusal for the completed request");
        ws.send(Message::text(snapshot(101, 11.0, 11.1)))
            .await
            .expect("active topic keeps flowing");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
    ));
    first_seen.send(()).expect("server still waits");
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0
    ));
}

#[tokio::test(start_paused = true)]
async fn a_delayed_ack_cannot_activate_a_refused_topic_or_release_its_early_frame() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");

        let combined = ws.next().await.expect("combined request").expect("read");
        let (combined_id, combined_topics) = subscribe_request(&combined);
        assert_eq!(combined_topics.len(), 2);
        ws.send(Message::text(subscribe_reply(
            &combined_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("splits the group");

        let bad = ws.next().await.expect("bad singleton").expect("read");
        let (bad_id, bad_topics) = subscribe_request(&bad);
        assert_eq!(bad_topics, ["orderbook.1.BADUSDT"]);
        ws.send(Message::text(subscribe_reply(&combined_id, true, "")))
            .await
            .expect("sends delayed old ack");
        ws.send(Message::text(snapshot_for("BADUSDT", 10, 666.0, 667.0)))
            .await
            .expect("sends refused topic data before its reply");
        ws.send(Message::text(subscribe_reply(
            &bad_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("refuses bad singleton");

        let good = ws.next().await.expect("good singleton").expect("read");
        let (good_id, good_topics) = subscribe_request(&good);
        assert_eq!(good_topics, ["orderbook.1.BTCUSDT"]);
        ws.send(Message::text(subscribe_reply(&good_id, true, "")))
            .await
            .expect("accepts good singleton");
        ws.send(Message::text(snapshot(20, 10.0, 10.1)))
            .await
            .expect("sends good quote");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[
            Subscription {
                symbol: "BADUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0),
        "a delayed ACK or refused topic frame escaped activation"
    );
}

#[tokio::test(start_paused = true)]
async fn a_quarantine_survives_a_later_setup_disconnect() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (requests_tx, mut requests) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("first socket");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("first handshake");
        let combined = ws.next().await.expect("combined request").expect("read");
        let (combined_id, combined_topics) = subscribe_request(&combined);
        requests_tx.send(combined_topics).expect("test listens");
        ws.send(Message::text(subscribe_reply(
            &combined_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("splits group");
        let bad = ws.next().await.expect("bad singleton").expect("read");
        let (bad_id, bad_topics) = subscribe_request(&bad);
        requests_tx.send(bad_topics).expect("test listens");
        ws.send(Message::text(subscribe_reply(
            &bad_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("quarantines bad");
        let good = ws.next().await.expect("good singleton").expect("read");
        let (_, good_topics) = subscribe_request(&good);
        requests_tx.send(good_topics).expect("test listens");
        drop(ws);

        let (stream, _) = listener.accept().await.expect("second socket");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("second handshake");
        let retried = ws.next().await.expect("retry request").expect("read");
        let (retry_id, retry_topics) = subscribe_request(&retried);
        requests_tx.send(retry_topics).expect("test listens");
        ws.send(Message::text(subscribe_reply(&retry_id, true, "")))
            .await
            .expect("accepts healthy retry");
        ws.send(Message::text(snapshot(30, 12.0, 12.1)))
            .await
            .expect("sends healthy quote");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[
            Subscription {
                symbol: "BADUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::FeedReset { .. }
    ));
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 12.0)
    );
    assert_eq!(requests.recv().await.expect("combined").len(), 2);
    assert_eq!(requests.recv().await.expect("bad"), ["orderbook.1.BADUSDT"]);
    assert_eq!(
        requests.recv().await.expect("good"),
        ["orderbook.1.BTCUSDT"]
    );
    assert_eq!(
        requests.recv().await.expect("retry"),
        ["orderbook.1.BTCUSDT"],
        "an incrementally quarantined topic returned on the immediate reconnect"
    );
}

#[tokio::test(start_paused = true)]
async fn active_topic_traffic_bypasses_a_slow_late_subscription() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (flooded, flood_done) = tokio::sync::oneshot::channel();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let boot = ws.next().await.expect("boot request").expect("read");
        let (boot_id, _) = subscribe_request(&boot);
        ws.send(Message::text(subscribe_reply(&boot_id, true, "")))
            .await
            .expect("accepts boot topic");
        ws.send(Message::text(snapshot(1, 10.0, 10.1)))
            .await
            .expect("sends boot quote");

        let admission = ws.next().await.expect("late admission").expect("read");
        let (admission_id, admission_topics) = subscribe_request(&admission);
        assert_eq!(admission_topics, ["orderbook.1.ETHUSDT"]);
        for update in 0..=MAX_SUBSCRIPTION_PENDING_MESSAGES {
            ws.send(Message::text(snapshot(
                100 + update as u64,
                20.0 + update as f64,
                20.1 + update as f64,
            )))
            .await
            .expect("active quote flood stays writable");
        }
        ws.send(Message::text(subscribe_reply(&admission_id, true, "")))
            .await
            .expect("eventually accepts late topic");
        ws.send(Message::text(snapshot(10_000, 99_999.0, 99_999.1)))
            .await
            .expect("sends post-ack sentinel");
        let _ = flooded.send(());
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
    );
    feed.admit("ETHUSDT", Feed::Quote);
    tokio::time::timeout(Duration::from_secs(10), flood_done)
        .await
        .expect("the accepted topic did not stall the socket")
        .expect("server completed the flood");
    let deadline = tokio::time::Instant::now() + Duration::from_secs(10);
    loop {
        let event = tokio::time::timeout_at(deadline, feed.next_event())
            .await
            .expect("sentinel before deadline")
            .expect("feed stays live through the slow ACK");
        match event {
            MarketEvent::Quote { quote, .. } if quote.bid_px == 99_999.0 => break,
            MarketEvent::FeedReset { .. } => {
                panic!("active traffic overflowed subscription staging and reconnected")
            }
            _ => {}
        }
    }
}

/// Serve one epoch: accept, wait for the subscribe, ack, send the frames,
/// hang up. A connection that dies before it says anything is a dial the
/// caller abandoned — drop it and wait for the next one.
async fn serve_epoch(listener: &tokio::net::TcpListener, frames: &[String]) {
    loop {
        let Ok((stream, _)) = listener.accept().await else {
            continue;
        };
        let Ok(mut ws) = tokio_tungstenite::accept_async(stream).await else {
            continue;
        };
        let Some(Ok(request @ Message::Text(_))) = ws.next().await else {
            continue;
        };
        let (request_id, _) = subscribe_request(&request);
        if ws
            .send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .is_err()
        {
            continue;
        }
        for frame in frames {
            if ws.send(Message::text(frame.clone())).await.is_err() {
                break;
            }
        }
        let _ = ws.close(None).await;
        return;
    }
}

/// The engine core waits on the feed inside a `select!` and throws away
/// the future of every branch that did not win; its flush tick fires
/// every 250ms. Drive the feed exactly that way — a fresh `next_event`
/// future each time round — and the reconnect must still land.
#[tokio::test(start_paused = true)]
async fn a_reconnect_lands_while_the_caller_cancels_every_250ms() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    tokio::spawn(async move {
        serve_epoch(&listener, &[snapshot(100, 10.0, 10.1)]).await;
        serve_epoch(&listener, &[snapshot(500, 11.0, 11.1)]).await;
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    // The core's flush cadence, first tick immediate, same as the loop.
    let mut flush_tick = tokio::time::interval(Duration::from_millis(250));
    let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
    let mut second_epoch = false;
    let mut seen: Vec<String> = Vec::new();

    while !second_epoch && tokio::time::Instant::now() < deadline {
        tokio::select! {
            event = feed.next_event() => match event {
                Ok(event) => {
                    seen.push(format!("{event:?}"));
                    if let MarketEvent::Quote { quote, .. } = event {
                        second_epoch = quote.bid_px == 11.0;
                    }
                }
                Err(e) => {
                    seen.push(format!("error: {e}"));
                    break;
                }
            },
            _ = flush_tick.tick() => {}
            _ = tokio::time::sleep_until(deadline) => {}
        }
    }

    assert!(
        second_epoch,
        "the second epoch's quote never arrived; the feed produced {seen:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn failed_first_dials_are_backed_off() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (tx, mut attempts) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        for _ in 0..3 {
            let (stream, _) = listener.accept().await.expect("accepts");
            tx.send(Instant::now()).expect("test is listening");
            drop(stream);
        }
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    let waiting = tokio::spawn(async move { feed.next_event().await });
    let stamps = tokio::time::timeout(Duration::from_secs(4), async {
        let mut stamps = Vec::new();
        for _ in 0..3 {
            stamps.push(attempts.recv().await.expect("worker keeps retrying"));
        }
        stamps
    })
    .await
    .expect("three paced attempts before the deadline");
    waiting.abort();

    assert!(
        stamps[1].duration_since(stamps[0]) >= BACKOFF_START * 2,
        "the second initial dial was not backed off: {stamps:?}"
    );
    assert!(
        stamps[2].duration_since(stamps[1]) >= BACKOFF_START * 4,
        "the third initial dial did not increase its backoff: {stamps:?}"
    );
}

/// One retired topic is isolated and omitted on reconnect. Healthy books
/// keep flowing across both epochs.
#[tokio::test(start_paused = true)]
async fn a_refused_topic_does_not_end_the_feed() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (requests_tx, mut requests) = tokio::sync::mpsc::unbounded_channel();

    tokio::spawn(async move {
        for epoch in 0..2 {
            let (stream, _) = listener.accept().await.expect("accepts");
            let mut ws = tokio_tungstenite::accept_async(stream)
                .await
                .expect("handshakes");
            while let Some(Ok(request @ Message::Text(_))) = ws.next().await {
                let (request_id, names) = subscribe_request(&request);
                requests_tx.send(names.clone()).expect("test listens");
                if names.iter().any(|topic| topic.contains("BADUSDT")) {
                    ws.send(Message::text(subscribe_reply(
                        &request_id,
                        false,
                        "Invalid symbol",
                    )))
                    .await
                    .expect("refuses bad");
                    continue;
                }
                ws.send(Message::text(subscribe_reply(&request_id, true, "")))
                    .await
                    .expect("accepts healthy");
                ws.send(Message::text(snapshot(
                    100 + epoch * 100,
                    10.0 + epoch as f64,
                    10.1 + epoch as f64,
                )))
                .await
                .expect("sends healthy quote");
                break;
            }
            ws.close(None).await.expect("closes epoch");
        }
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[
            Subscription {
                symbol: "BADUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
    );
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::FeedReset { .. }
    ));
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0)
    );

    let first = requests.recv().await.expect("initial group");
    let isolated_bad = requests.recv().await.expect("bad half");
    let isolated_good = requests.recv().await.expect("good half");
    let after_reconnect = requests.recv().await.expect("next epoch");
    assert_eq!(first.len(), 2);
    assert_eq!(isolated_bad, ["orderbook.1.BADUSDT"]);
    assert_eq!(isolated_good, ["orderbook.1.BTCUSDT"]);
    assert_eq!(after_reconnect, ["orderbook.1.BTCUSDT"]);
}

#[tokio::test(start_paused = true)]
async fn a_refused_late_admission_keeps_books_live_and_can_be_reprobed() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let boot = ws.next().await.expect("boot request").expect("read");
        let (boot_request_id, _) = subscribe_request(&boot);
        ws.send(Message::text(subscribe_reply(&boot_request_id, true, "")))
            .await
            .expect("accepts boot topic");
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("sends boot quote");

        let admission = ws.next().await.expect("admission arrives").expect("read");
        assert!(
            admission.to_text().expect("text").contains("BADUSDT"),
            "unexpected admission {admission:?}"
        );
        let (admission_request_id, _) = subscribe_request(&admission);
        ws.send(Message::text(subscribe_reply(
            &admission_request_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("refuses only the new topic");
        ws.send(Message::text(snapshot(101, 11.0, 11.1)))
            .await
            .expect("existing topic continues");

        let retry = ws.next().await.expect("retry arrives").expect("read");
        let (retry_request_id, retry_topics) = subscribe_request(&retry);
        assert_eq!(retry_topics, ["orderbook.1.BADUSDT"]);
        ws.send(Message::text(subscribe_reply(&retry_request_id, true, "")))
            .await
            .expect("accepts explicit retry");
        ws.send(Message::text(snapshot_for("BADUSDT", 102, 20.0, 20.1)))
            .await
            .expect("retried topic becomes live");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
    );
    feed.admit("BADUSDT", Feed::Quote);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0)
    );
    feed.admit("BADUSDT", Feed::Quote);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0)
    );
}

#[tokio::test(start_paused = true)]
async fn a_quarantined_topic_reprobes_itself_on_the_same_socket() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("one stable socket");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let boot = ws.next().await.expect("boot request").expect("read");
        let (boot_id, boot_topics) = subscribe_request(&boot);
        assert_eq!(boot_topics, ["orderbook.1.BTCUSDT"]);
        ws.send(Message::text(subscribe_reply(&boot_id, true, "")))
            .await
            .expect("accepts boot quote");
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("initial BTC quote");

        let admission = ws.next().await.expect("one admission").expect("read");
        let (admission_id, admission_topics) = subscribe_request(&admission);
        assert_eq!(admission_topics, ["orderbook.1.BADUSDT"]);
        ws.send(Message::text(subscribe_reply(
            &admission_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("quarantines the late topic");
        let quarantined_at = Instant::now();
        ws.send(Message::text(snapshot(101, 11.0, 11.1)))
            .await
            .expect("healthy quote remains live");

        let retry = tokio::time::timeout(Duration::from_secs(1), ws.next())
            .await
            .expect("the feed schedules its own retry")
            .expect("retry request")
            .expect("reads retry");
        assert!(
            quarantined_at.elapsed() >= Duration::from_millis(70),
            "quarantine retried before its cooldown"
        );
        let (retry_id, retry_topics) = subscribe_request(&retry);
        assert_eq!(retry_topics, ["orderbook.1.BADUSDT"]);
        ws.send(Message::text(subscribe_reply(&retry_id, true, "")))
            .await
            .expect("accepts the timed retry");
        ws.send(Message::text(snapshot_for("BADUSDT", 102, 20.0, 20.1)))
            .await
            .expect("retried topic becomes live");
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    )
    .with_timing(
        Duration::from_secs(1),
        Duration::from_secs(1),
        Duration::from_millis(500),
    )
    .with_topic_timing(Duration::from_millis(80), Duration::from_millis(5));
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);

    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0
    ));
    feed.admit("BADUSDT", Feed::Quote);

    let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
    let mut healthy_quote_seen = false;
    loop {
        let event = tokio::time::timeout_at(deadline, feed.next_event())
            .await
            .expect("timed re-probe completes")
            .expect("feed remains healthy");
        match event {
            MarketEvent::Quote { quote, .. } if quote.bid_px == 20.0 => break,
            MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0 => {
                healthy_quote_seen = true;
                tokio::time::advance(Duration::from_millis(80)).await;
            }
            MarketEvent::FeedReset { .. } => {
                panic!("the timed topic re-probe opened a new socket")
            }
            _ => {}
        }
    }
    assert!(
        healthy_quote_seen,
        "the accepted topic stopped during quarantine"
    );
    server.await.expect("stable-socket server");
}

#[tokio::test(start_paused = true)]
async fn repeated_admission_cannot_turn_one_quarantine_into_a_request_storm() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();

    let (retry_refused, refused) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let boot = ws.next().await.expect("boot request").expect("read");
        let (boot_id, _) = subscribe_request(&boot);
        ws.send(Message::text(subscribe_reply(&boot_id, true, "")))
            .await
            .expect("accepts boot topic");
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("sends boot quote");

        let admission = ws.next().await.expect("admission").expect("read");
        let (admission_id, admission_topics) = subscribe_request(&admission);
        assert_eq!(admission_topics, ["orderbook.1.BADUSDT"]);
        ws.send(Message::text(subscribe_reply(
            &admission_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("refuses new topic");
        ws.send(Message::text(snapshot(101, 11.0, 11.1)))
            .await
            .expect("signals quarantine completion");

        let retry = ws.next().await.expect("one explicit retry").expect("read");
        let (retry_id, retry_topics) = subscribe_request(&retry);
        assert_eq!(retry_topics, ["orderbook.1.BADUSDT"]);
        ws.send(Message::text(subscribe_reply(
            &retry_id,
            false,
            "Invalid symbol",
        )))
        .await
        .expect("refuses retry");
        retry_refused.send(()).unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(200), ws.next())
                .await
                .is_err(),
            "repeated admission sent another probe inside the cooldown"
        );
        ws.send(Message::text(snapshot(102, 12.0, 12.1)))
            .await
            .expect("healthy topic remains live");
        while ws.next().await.is_some() {}
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 10.0)
    );
    feed.admit("BADUSDT", Feed::Quote);
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { quote, .. } if quote.bid_px == 11.0)
    );
    for _ in 0..32 {
        feed.admit("BADUSDT", Feed::Quote);
    }
    let advance_cooldown = async {
        refused.await.unwrap();
        tokio::time::advance(Duration::from_millis(200)).await;
    };
    let (event, ()) = tokio::join!(next(&mut feed), advance_cooldown);
    assert!(matches!(event, MarketEvent::Quote { quote, .. } if quote.bid_px == 12.0));
}

/// Dropping the feed must take the socket with it, or a dead engine would
/// leave a task reading prices nobody wants.
#[tokio::test(start_paused = true)]
async fn dropping_the_feed_lets_go_of_the_socket() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("binds");
    let port = listener.local_addr().expect("has an address").port();
    let (hung_up, closed) = tokio::sync::oneshot::channel();

    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.expect("accepts");
        let mut ws = tokio_tungstenite::accept_async(stream)
            .await
            .expect("handshakes");
        let request = ws.next().await.expect("request").expect("read");
        let (request_id, _) = subscribe_request(&request);
        ws.send(Message::text(subscribe_reply(&request_id, true, "")))
            .await
            .expect("sends ack");
        ws.send(Message::text(snapshot(100, 10.0, 10.1)))
            .await
            .expect("sends");
        // Runs out when the other end goes away.
        while ws.next().await.is_some() {}
        let _ = hung_up.send(());
    });

    let mut feed = BybitPublicFeed::with_url(
        format!("ws://127.0.0.1:{port}"),
        &[Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        }],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(matches!(next(&mut feed).await, MarketEvent::Quote { .. }));

    drop(feed);
    tokio::time::timeout(Duration::from_secs(5), closed)
        .await
        .expect("the socket closed when the feed did")
        .expect("the server was still listening");
}

/// Connects to the real public stream. Off by default; run with
/// `cargo test -p engine-marketdata -- --ignored live_public_feed`.
#[tokio::test(start_paused = true)]
#[ignore = "needs network"]
async fn live_public_feed_delivers_quotes_and_tickers() {
    let mut feed = BybitPublicFeed::new(&[
        Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Quote,
        },
        Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Ticker,
        },
    ]);
    let mut quotes = 0;
    let mut tickers = 0;
    let deadline = tokio::time::Instant::now() + Duration::from_secs(15);
    while (quotes < 5 || tickers < 1) && tokio::time::Instant::now() < deadline {
        let event = tokio::time::timeout_at(deadline, feed.next_event())
            .await
            .expect("a frame within the deadline")
            .expect("feed stays healthy");
        match event {
            MarketEvent::Quote { quote, .. } => {
                assert!(quote.ask_px > quote.bid_px, "crossed book: {quote:?}");
                assert!(quote.recv_ns > 0);
                assert!(quote.venue_ts_ms > 1_700_000_000_000);
                quotes += 1;
            }
            MarketEvent::Ticker { ticker, .. } => {
                assert!(ticker.mark_px > 0.0, "no mark price: {ticker:?}");
                tickers += 1;
            }
            MarketEvent::FeedReset { .. }
            | MarketEvent::Depth { .. }
            | MarketEvent::Trades { .. } => {}
        }
    }
    assert!(quotes >= 5, "only {quotes} quotes arrived");
    assert!(tickers >= 1, "only {tickers} tickers arrived");
}

#[tokio::test(start_paused = true)]
#[ignore = "needs network"]
async fn live_public_feed_delivers_l50_and_aggressor_trades() {
    let mut feed = BybitPublicFeed::new(&[
        Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Depth,
        },
        Subscription {
            symbol: "BTCUSDT".into(),
            feed: Feed::Trades,
        },
    ]);
    let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
    let mut depth_seen = false;
    let mut trades_seen = false;
    while (!depth_seen || !trades_seen) && tokio::time::Instant::now() < deadline {
        let event = tokio::time::timeout_at(deadline, feed.next_event())
            .await
            .expect("a frame within the deadline")
            .expect("feed stays healthy");
        match event {
            MarketEvent::Depth { depth, .. } => {
                assert!(depth.bid_len > 1, "not a deep book: {depth:?}");
                assert!(depth.ask_len > 1, "not a deep book: {depth:?}");
                assert!(depth.best_ask().unwrap().px > depth.best_bid().unwrap().px);
                depth_seen = true;
            }
            MarketEvent::Trades { trades, .. } => {
                assert!(trades.trade_count > 0);
                assert!(trades.last_px > 0.0);
                trades_seen = true;
            }
            MarketEvent::FeedReset { .. }
            | MarketEvent::Quote { .. }
            | MarketEvent::Ticker { .. } => {}
        }
    }
    assert!(depth_seen, "no L50 event arrived");
    assert!(trades_seen, "no public trade event arrived");
}

#[tokio::test(start_paused = true)]
async fn retired_topics_leave_the_live_socket_and_reconnect_without_renumbering_ids() {
    let _io = crate::test_io::IoProgress::new();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let message = socket.next().await.unwrap().unwrap();
        let (id, topics) = subscribe_request(&message);
        assert_eq!(topics, vec!["orderbook.1.BTCUSDT", "orderbook.1.ETHUSDT"]);
        socket
            .send(Message::text(subscribe_reply(&id, true, "")))
            .await
            .unwrap();
        socket
            .send(Message::text(snapshot(1, 10.0, 10.1)))
            .await
            .unwrap();
        tokio::time::timeout(Duration::from_secs(1), socket.next())
            .await
            .expect("retirement closes the old socket");
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        let message = socket.next().await.unwrap().unwrap();
        let (id, topics) = subscribe_request(&message);
        assert_eq!(topics, vec!["orderbook.1.ETHUSDT"]);
        socket
            .send(Message::text(subscribe_reply(&id, true, "")))
            .await
            .unwrap();
        socket
            .send(Message::text(
                snapshot(2, 20.0, 20.1).replace("BTCUSDT", "ETHUSDT"),
            ))
            .await
            .unwrap();
        std::future::pending::<()>().await;
    });
    let mut feed = BybitPublicFeed::with_url(
        format!("ws://{address}"),
        &[
            Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            },
            Subscription {
                symbol: "ETHUSDT".into(),
                feed: Feed::Quote,
            },
        ],
    );
    let _timers = drive_timers(&mut feed, TimerPlan::Retries);
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::Quote {
            symbol: SymbolId(0),
            ..
        }
    ));
    assert!(MarketFeed::retire(&mut feed, "BTCUSDT", Feed::Quote));
    assert!(matches!(
        next(&mut feed).await,
        MarketEvent::FeedReset { .. }
    ));
    assert!(
        matches!(next(&mut feed).await, MarketEvent::Quote { symbol: SymbolId(1), quote } if quote.bid_px == 20.0)
    );
    assert_eq!(feed.symbols().get("BTCUSDT"), Some(SymbolId(0)));
    assert_eq!(feed.symbols().get("ETHUSDT"), Some(SymbolId(1)));
    assert_eq!(feed.admit("BTCUSDT", Feed::Quote), SymbolId(0));
    drop(feed);
    server.abort();
}
