use super::*;
use engine_types::{Feed, StrategyId, Subscription};

fn observation() -> SignalObservation {
    let mut observation = SignalObservation {
        schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "carry-v1".to_string(),
        destination: StrategyId(2),
        source: "carry-worker".to_string(),
        sequence: 1,
        observation_id: "funding-1".to_string(),
        kind: "settled_funding".to_string(),
        observed_wall_ts_ms: 10,
        available_wall_ts_ms: 11,
        subscriptions: vec![Subscription {
            symbol: "BTCUSDT".to_string(),
            feed: Feed::Ticker,
        }],
        payload: br#"{"rate":"0.0001"}"#.to_vec(),
        content_sha256: String::new(),
    };
    observation.content_sha256 = content_sha256(&observation);
    observation
}

fn spool_path(directory: &Path, observation: &SignalObservation) -> PathBuf {
    directory.join(format!(
        "{:020}-{}.json",
        observation.sequence, observation.content_sha256
    ))
}

#[test]
fn exact_hash_covers_subscriptions_and_payload() {
    let observation = observation();
    validate(&observation).unwrap();
    let mut changed = observation;
    changed.payload.push(b' ');
    assert!(validate(&changed).unwrap_err().contains("content hash"));
}

#[tokio::test(start_paused = true)]
async fn bounded_sender_never_waits() {
    let (sender, _receiver) = signal_channel();
    for _ in 0..SIGNAL_CHANNEL_CAPACITY {
        sender.try_send(observation()).unwrap();
    }
    assert!(matches!(
        sender.try_send(observation()),
        Err(SignalSendError::Full(_))
    ));
}

#[tokio::test(start_paused = true)]
async fn spool_retires_only_the_previously_returned_file() {
    let directory = crate::testpath::temp_path("signal-spool");
    std::fs::create_dir(directory.path()).unwrap();
    let first = observation();
    let mut second = first.clone();
    second.sequence = 2;
    second.observation_id = "funding-2".into();
    second.content_sha256 = content_sha256(&second);
    let first_path = spool_path(directory.path(), &first);
    let second_path = spool_path(directory.path(), &second);
    publish_test_row(&first_path, &serde_json::to_vec(&first).unwrap());
    publish_test_row(&second_path, &serde_json::to_vec(&second).unwrap());

    let mut feed =
        SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
    assert_eq!(feed.next_observation().await.unwrap(), first);
    assert!(
        first_path.exists(),
        "not retired before core can barrier it"
    );
    feed.acknowledge_last().unwrap();
    assert_eq!(feed.next_observation().await.unwrap(), second);
    assert!(
        !first_path.exists(),
        "the durable prior row leaves the scan"
    );
    assert!(
        second_path.exists(),
        "the row just returned is still recoverable"
    );

    std::fs::remove_file(second_path).unwrap();
    std::fs::remove_dir(directory.path()).unwrap();
}

#[tokio::test(start_paused = true)]
async fn invalid_spool_row_is_never_retired() {
    let directory = crate::testpath::temp_path("bad-signal-spool");
    std::fs::create_dir(directory.path()).unwrap();
    let mut bad = observation();
    bad.content_sha256 = "0".repeat(64);
    let path = spool_path(directory.path(), &bad);
    publish_test_row(&path, &serde_json::to_vec(&bad).unwrap());
    let mut feed = SpoolSignalFeed::new(directory.path());
    assert!(feed.next_observation().await.is_err());
    assert!(
        path.exists(),
        "a failed admission source stays for inspection"
    );
    std::fs::remove_file(path).unwrap();
    std::fs::remove_dir(directory.path()).unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_file_deleted_between_scan_and_read_is_skipped() {
    let directory = crate::testpath::temp_path("signal-spool-delete-race");
    std::fs::create_dir(directory.path()).unwrap();
    let missing = observation();
    let mut live = missing.clone();
    live.sequence = 2;
    live.observation_id = "funding-2".into();
    live.content_sha256 = content_sha256(&live);
    let missing_path = spool_path(directory.path(), &missing);
    let live_path = spool_path(directory.path(), &live);
    publish_test_row(&missing_path, &serde_json::to_vec(&missing).unwrap());
    publish_test_row(&live_path, &serde_json::to_vec(&live).unwrap());

    let mut feed =
        SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
    let scanned = SpoolScanner::page(directory.path(), None, None).unwrap();
    assert_eq!(scanned.len(), 2);
    std::fs::remove_file(&missing_path).unwrap();
    assert!(SpoolSignalFeed::read_one(&missing_path).unwrap().is_none());
    assert_eq!(feed.next_observation().await.unwrap(), live);

    std::fs::remove_file(live_path).unwrap();
    std::fs::remove_dir(directory.path()).unwrap();
}

#[tokio::test(start_paused = true)]
async fn each_pop_merges_new_lower_sequences_from_an_independent_lane() {
    let directory = crate::testpath::temp_path("signal-spool-independent-lanes");
    std::fs::create_dir(directory.path()).unwrap();
    let mut high = observation();
    high.sequence = 100;
    high.observation_id = "long-100".into();
    high.content_sha256 = content_sha256(&high);
    let high_path = spool_path(directory.path(), &high);
    publish_test_row(&high_path, &serde_json::to_vec(&high).unwrap());

    let mut feed =
        SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
    assert_eq!(feed.next_observation().await.unwrap(), high);

    let mut low = observation();
    low.destination = StrategyId(3);
    low.sequence = 1;
    low.observation_id = "carry-1".into();
    low.content_sha256 = content_sha256(&low);
    let low_path = spool_path(directory.path(), &low);
    publish_test_row(&low_path, &serde_json::to_vec(&low).unwrap());

    feed.acknowledge_last().unwrap();
    assert_eq!(feed.next_observation().await.unwrap(), low);
    assert!(!high_path.exists());
    assert!(low_path.exists());

    std::fs::remove_file(low_path).unwrap();
    std::fs::remove_dir(directory.path()).unwrap();
}

#[test]
fn consumed_universe_changes_keep_earlier_subscriptions_through_rotation() {
    let mut first = observation();
    first.destination = StrategyId(1);
    first.subscriptions[0].symbol = "HELDUSDT".into();
    first.content_sha256 = content_sha256(&first);
    let mut second = first.clone();
    second.sequence = 2;
    second.observation_id = "universe-2".into();
    second.subscriptions.clear();
    second.content_sha256 = content_sha256(&second);
    let records = vec![
        WalRecord::SignalObservation {
            wall_ts_ms: 1,
            observation: first.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: 2,
            strategy: StrategyId(1),
            source: first.source.clone(),
            sequence: 1,
            observation_id: first.observation_id.clone(),
        },
        WalRecord::SignalObservation {
            wall_ts_ms: 3,
            observation: second.clone(),
        },
        WalRecord::SignalObservationConsumed {
            wall_ts_ms: 4,
            strategy: StrategyId(1),
            source: second.source.clone(),
            sequence: 2,
            observation_id: second.observation_id.clone(),
        },
    ];
    let expected = vec![Subscription {
        symbol: "HELDUSDT".into(),
        feed: Feed::Ticker,
    }];
    assert_eq!(active_subscriptions(&records), expected);

    let rotated = WalRecord::SegmentBase {
        portfolio_control: Default::default(),
        pending_order_dispatches: Vec::new(),
        signal_producers: Vec::new(),
        identities: None,
        instrument_catalog: None,
        signal_suspensions: Vec::new(),
        portfolio: Some(Default::default()),
        strategy_processes: Vec::new(),
        strategy_callback_queues: Vec::new(),
        strategy_callback_sources: Vec::new(),
        signal_callback_deliveries: Vec::new(),
        strategy_callbacks: Vec::new(),
        wall_ts_ms: 5,
        strategies: vec!["long".into(), "carry".into()],
        symbols: vec!["HELDUSDT".into()],
        may_open: true,
        control_anchors: vec![],
        attribution: vec![],
        logged_exposure: vec![],
        intended_stops: vec![],
        recent_execution_ids: vec![],
        execution_history_through_ms: None,
        target_book_latches: vec![],
        strategy_checkpoints: vec![],
        strategy_global_checkpoints: vec![],
        strategy_events: vec![],
        signal_observations: vec![],
        signal_gaps: vec![],
        strategy_effects: Default::default(),
        signal_cursors: vec![engine_types::SignalCursor {
            source: second.source.clone(),
            sequence: 2,
            content_sha256: second.content_sha256,
        }],
        signal_subscriptions: vec![SignalSubscriptionState {
            source: first.source,
            destination: StrategyId(1),
            subscriptions: expected.clone(),
        }],
        runtime_control_requests: vec![],
        runtime_control_consumed: vec![],
        open_orders: vec![],
        rolling_loss_rows: vec![],
    };
    assert_eq!(active_subscriptions(&[rotated]), expected);
}

fn short_test_dir(tag: &str) -> PathBuf {
    let dir = PathBuf::from(format!("/tmp/lm-{}-{}", tag, std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[tokio::test(start_paused = true)]
async fn unix_signal_feed_streams_observations() {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let directory = short_test_dir("ux-sig");
    let sock_path = directory.join("stream.sock");

    let mut feed = UnixSignalFeed::bind(&sock_path).unwrap();

    let mut obs = observation();
    obs.sequence = 42;
    obs.content_sha256 = content_sha256(&obs);

    let body = serde_json::to_vec(&obs).unwrap();
    let len = (body.len() as u32).to_le_bytes();

    let handle = tokio::spawn(async move { feed.next_observation().await });

    tokio::time::sleep(Duration::from_millis(10)).await;
    let mut client = UnixStream::connect(&sock_path).unwrap();
    client.write_all(&len).unwrap();
    client.write_all(&body).unwrap();
    client.flush().unwrap();

    let received = handle.await.unwrap().unwrap();
    assert_eq!(received, obs);

    drop(client);
    let _ = std::fs::remove_dir_all(&directory);
}

#[tokio::test(start_paused = true)]
async fn hybrid_signal_feed_drains_spool_then_receives_socket() {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let directory = short_test_dir("hy-sig");

    let mut first = observation();
    first.sequence = 1;
    first.content_sha256 = content_sha256(&first);
    let first_path = spool_path(&directory, &first);
    publish_test_row(&first_path, &serde_json::to_vec(&first).unwrap());

    let mut feed = HybridSignalFeed::new(&directory)
        .unwrap()
        .with_poll_interval(Duration::from_millis(5));

    let obs1 = feed.next_observation().await.unwrap();
    assert_eq!(obs1, first);

    let mut second = observation();
    second.sequence = 2;
    second.content_sha256 = content_sha256(&second);
    let body = serde_json::to_vec(&second).unwrap();
    let len = (body.len() as u32).to_le_bytes();
    publish_test_row(&spool_path(&directory, &second), &body);

    let sock_path = directory.join("stream.sock");
    let mut client = UnixStream::connect(&sock_path).unwrap();
    client.write_all(&len).unwrap();
    client.write_all(&body).unwrap();
    client.flush().unwrap();

    feed.acknowledge_last().unwrap();
    let obs2 = feed.next_observation().await.unwrap();
    assert_eq!(obs2, second);

    assert!(!first_path.exists());

    drop(client);
    let _ = std::fs::remove_dir_all(&directory);
}

/// The core's `select!` drops the feed future whenever another branch
/// wins. A frame whose length prefix was read before that and whose body
/// arrives after it is one frame, not a length followed by `{"sc`.
#[tokio::test(start_paused = true)]
async fn a_frame_split_by_a_dropped_future_is_still_one_frame() {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let directory = short_test_dir("ux-split");
    let sock_path = directory.join("stream.sock");
    let mut feed = UnixSignalFeed::bind(&sock_path).unwrap();

    let mut obs = observation();
    obs.sequence = 7;
    obs.content_sha256 = content_sha256(&obs);
    let body = serde_json::to_vec(&obs).unwrap();

    let mut client = UnixStream::connect(&sock_path).unwrap();
    client
        .write_all(&(body.len() as u32).to_le_bytes())
        .unwrap();
    client.flush().unwrap();

    let dropped = tokio::time::timeout(Duration::from_millis(50), feed.next_observation()).await;
    assert!(
        dropped.is_err(),
        "no body has arrived, so there is nothing to return"
    );

    client.write_all(&body).unwrap();
    client.flush().unwrap();
    let received = tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
        .await
        .expect("the body completes the frame the length began")
        .unwrap();
    assert_eq!(received, obs);

    drop(client);
    let _ = std::fs::remove_dir_all(&directory);
}

#[tokio::test]
async fn a_client_that_dies_mid_frame_costs_only_its_own_frame() {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let directory = short_test_dir("ux-eof");
    let sock_path = directory.join("stream.sock");
    let mut feed = UnixSignalFeed::bind(&sock_path).unwrap();

    let mut lost = observation();
    lost.sequence = 7;
    lost.content_sha256 = content_sha256(&lost);
    let lost_body = serde_json::to_vec(&lost).unwrap();
    let mut first = UnixStream::connect(&sock_path).unwrap();
    first
        .write_all(&(lost_body.len() as u32).to_le_bytes())
        .unwrap();
    first.write_all(&lost_body[..lost_body.len() / 2]).unwrap();
    first.flush().unwrap();
    drop(first);

    let mut whole = observation();
    whole.sequence = 8;
    whole.content_sha256 = content_sha256(&whole);
    let whole_body = serde_json::to_vec(&whole).unwrap();
    let mut second = UnixStream::connect(&sock_path).unwrap();
    second
        .write_all(&(whole_body.len() as u32).to_le_bytes())
        .unwrap();
    second.write_all(&whole_body).unwrap();
    second.flush().unwrap();

    let received = tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
        .await
        .unwrap()
        .unwrap();
    assert_eq!(received, whole);

    drop(second);
    let _ = std::fs::remove_dir_all(&directory);
}

/// The worker writes a row before it sends the frame. A row still on disk
/// with a lower sequence than an arriving frame was written before it and
/// goes first; the frame's own row is retired after the barrier like any
/// other returned envelope.
#[tokio::test(start_paused = true)]
async fn a_frame_waits_for_the_row_written_before_it_and_retires_its_own() {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let directory = short_test_dir("hy-order");

    let mut first = observation();
    first.sequence = 1;
    first.content_sha256 = content_sha256(&first);
    let first_path = spool_path(&directory, &first);
    publish_test_row(&first_path, &serde_json::to_vec(&first).unwrap());

    let mut second = observation();
    second.sequence = 2;
    second.content_sha256 = content_sha256(&second);
    let second_path = spool_path(&directory, &second);
    let second_body = serde_json::to_vec(&second).unwrap();
    publish_test_row(&second_path, &second_body);

    // A poll long enough that only the socket can wake the feed.
    let mut feed = HybridSignalFeed::new(&directory)
        .unwrap()
        .with_poll_interval(Duration::from_secs(30));
    let mut client = UnixStream::connect(directory.join("stream.sock")).unwrap();
    client
        .write_all(&(second_body.len() as u32).to_le_bytes())
        .unwrap();
    client.write_all(&second_body).unwrap();
    client.flush().unwrap();

    assert_eq!(feed.next_observation().await.unwrap(), first);
    feed.acknowledge_last().unwrap();
    assert_eq!(feed.next_observation().await.unwrap(), second);
    assert!(!first_path.exists(), "the row before the frame is retired");
    assert!(
        second_path.exists(),
        "the frame's row waits for the barrier"
    );

    feed.acknowledge_last().unwrap();
    let quiet = tokio::time::timeout(Duration::from_millis(200), feed.next_observation()).await;
    assert!(quiet.is_err(), "nothing else was written");
    assert!(
        !second_path.exists(),
        "the next poll retires the frame's own row"
    );

    drop(client);
    let _ = std::fs::remove_dir_all(&directory);
}

/// The core drops the feed future whenever another `select!` branch
/// wins. A row whose read was in flight is still the next row out.
#[test]
fn a_row_whose_read_the_core_dropped_is_still_delivered_first() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(1)
        .build()
        .unwrap();
    runtime.block_on(async {
        let directory = crate::testpath::temp_path("signal-spool-dropped-read");
        std::fs::create_dir(directory.path()).unwrap();
        let first = observation();
        let mut second = first.clone();
        second.sequence = 2;
        second.observation_id = "funding-2".into();
        second.content_sha256 = content_sha256(&second);
        let first_path = spool_path(directory.path(), &first);
        let second_path = spool_path(directory.path(), &second);
        publish_test_row(&first_path, &serde_json::to_vec(&first).unwrap());
        publish_test_row(&second_path, &serde_json::to_vec(&second).unwrap());

        let mut feed =
            SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));

        // The only blocking thread is busy, so the row read cannot finish
        // inside the one poll the core gives the feed before dropping it.
        let (release, held) = std::sync::mpsc::channel::<()>();
        let hold = tokio::task::spawn_blocking(move || {
            let _ = held.recv();
        });
        let dropped = tokio::time::timeout(Duration::ZERO, feed.next_observation()).await;
        assert!(dropped.is_err(), "the read is still in flight");
        release.send(()).unwrap();
        hold.await.unwrap();

        let received = tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
            .await
            .expect("the in-flight read completes")
            .unwrap();
        assert_eq!(received, first, "the dropped read's row goes first");
        feed.acknowledge_last().unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), second);
        assert!(!first_path.exists(), "the delivered row is retired");

        std::fs::remove_file(second_path).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    });
}

/// A frame the engine cannot take is dropped with its stream; the row
/// is on disk, so the feed keeps running and the next client is heard.
#[tokio::test(start_paused = true)]
async fn an_oversize_frame_length_costs_its_stream_and_nothing_else() {
    use std::io::Write;
    use std::os::unix::net::UnixStream;

    let directory = short_test_dir("ux-oversize");
    let sock_path = directory.join("stream.sock");
    let mut feed = UnixSignalFeed::bind(&sock_path).unwrap();

    let mut fat = UnixStream::connect(&sock_path).unwrap();
    fat.write_all(&((MAX_SIGNAL_OBSERVATION_BYTES as u32 + 1).to_le_bytes()))
        .unwrap();
    fat.flush().unwrap();
    let waiting = tokio::time::timeout(Duration::from_millis(100), feed.next_observation()).await;
    assert!(
        waiting.is_err(),
        "an oversize length is not an error the core sees: {waiting:?}"
    );
    assert!(feed.active_stream.is_none(), "the fat stream is dropped");

    let mut obs = observation();
    obs.sequence = 8;
    obs.content_sha256 = content_sha256(&obs);
    let body = serde_json::to_vec(&obs).unwrap();
    let mut client = UnixStream::connect(&sock_path).unwrap();
    client
        .write_all(&(body.len() as u32).to_le_bytes())
        .unwrap();
    client.write_all(&body).unwrap();
    client.flush().unwrap();
    let received = tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
        .await
        .expect("the next client's frame is read")
        .unwrap();
    assert_eq!(received, obs);

    drop(client);
    drop(fat);
    let _ = std::fs::remove_dir_all(&directory);
}
