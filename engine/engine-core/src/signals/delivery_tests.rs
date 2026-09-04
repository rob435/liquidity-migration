use super::*;
use engine_types::StrategyId;

fn row(source: &str, sequence: u64) -> SignalObservation {
    let mut row = SignalObservation {
        schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
        decision_fingerprint: "delivery-test".into(),
        destination: StrategyId(0),
        source: source.into(),
        sequence,
        observation_id: format!("{source}-{sequence}"),
        kind: "test".into(),
        observed_wall_ts_ms: 1,
        available_wall_ts_ms: 2,
        subscriptions: Vec::new(),
        payload: b"{}".to_vec(),
        content_sha256: String::new(),
    };
    row.content_sha256 = content_sha256(&row);
    row
}

fn request(source: &str, next_sequence: u64) -> SignalGapRequest {
    SignalGapRequest {
        source: source.into(),
        next_sequence,
    }
}

fn write(feed: &SpoolSignalFeed, row: &SignalObservation) -> PathBuf {
    let path = feed.path_for(row);
    publish_test_row(&path, &serde_json::to_vec(row).unwrap());
    path
}

#[tokio::test]
async fn only_explicit_acknowledgement_can_retire_a_spool_row() {
    let directory = crate::testpath::temp_path("signal-explicit-ack");
    std::fs::create_dir(directory.path()).unwrap();
    let mut feed = SpoolSignalFeed::new(directory.path());
    let expected = row("worker.g1", 3);
    let path = write(&feed, &expected);
    let delivered = feed.next_observation().await.unwrap();
    assert!(feed.next_observation().await.is_err());
    assert!(path.exists());
    feed.defer_last(delivered).unwrap();
    feed.set_gap_requests(&[request("worker.g1", 1)], &[])
        .unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(20), feed.next_observation())
            .await
            .is_err()
    );
    assert!(path.exists());
    drop(feed);
    let mut restarted = SpoolSignalFeed::new(directory.path());
    assert_eq!(restarted.next_observation().await.unwrap(), expected);
    assert!(path.exists());
}

#[tokio::test]
async fn gap_catchup_precedes_new_generations_while_independent_destinations_flow() {
    let directory = crate::testpath::temp_path("signal-catchup");
    std::fs::create_dir(directory.path()).unwrap();
    let mut feed =
        SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
    let future = row("worker.g1", 3);
    let future_path = write(&feed, &future);
    let delivered = feed.next_observation().await.unwrap();
    feed.defer_last(delivered).unwrap();
    feed.set_gap_requests(&[request("worker.g1", 1)], &[StrategyId(0)])
        .unwrap();
    let next_generation = row("worker.g2", 1);
    let next_path = write(&feed, &next_generation);
    let mut independent = row("independent", 1);
    independent.destination = StrategyId(1);
    independent.content_sha256 = content_sha256(&independent);
    write(&feed, &independent);
    assert_eq!(feed.next_observation().await.unwrap(), independent);
    feed.acknowledge_last().unwrap();
    assert!(future_path.exists());
    assert!(next_path.exists());
    assert!(
        tokio::time::timeout(Duration::from_millis(10), feed.next_observation())
            .await
            .is_err()
    );
    for sequence in 1..=2 {
        write(&feed, &row("worker.g1", sequence));
    }
    for sequence in 1..=3 {
        feed.set_gap_requests(&[request("worker.g1", sequence)], &[StrategyId(0)])
            .unwrap();
        assert_eq!(
            feed.next_observation().await.unwrap(),
            row("worker.g1", sequence)
        );
        feed.acknowledge_last().unwrap();
    }
    feed.set_gap_requests(&[], &[]).unwrap();
    assert_eq!(feed.next_observation().await.unwrap(), next_generation);
    feed.acknowledge_last().unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(20), feed.next_observation())
            .await
            .is_err()
    );
    assert!(!future_path.exists());
}

#[test]
fn bounded_spool_pages_find_catchup_beyond_a_saturated_metadata_cache() {
    let directory = crate::testpath::temp_path("signal-paged-catchup");
    std::fs::create_dir(directory.path()).unwrap();
    let feed = SpoolSignalFeed::new(directory.path());
    for sequence in 2..=(SPOOL_METADATA_CAPACITY as u64 + 3) {
        write(&feed, &row("future", sequence));
    }
    let gaps = vec![request("future", 1)];
    let mut scanner = SpoolScanner::default();
    assert!(scanner
        .select(directory.path(), &gaps, &[])
        .unwrap()
        .is_none());
    assert_eq!(scanner.deferred.len(), SPOOL_METADATA_CAPACITY);
    assert_eq!(
        SpoolScanner::page(directory.path(), None, None)
            .unwrap()
            .len(),
        SPOOL_SCAN_PAGE
    );
    let independent = row("other", 50_000);
    write(&feed, &independent);
    assert_eq!(
        scanner
            .select(directory.path(), &gaps, &[])
            .unwrap()
            .unwrap()
            .1,
        independent
    );
    let missing = row("future", 1);
    write(&feed, &missing);
    assert_eq!(
        scanner
            .select(directory.path(), &gaps, &[])
            .unwrap()
            .unwrap()
            .1,
        missing
    );
    assert!(scanner.deferred.len() <= SPOOL_METADATA_CAPACITY);
}

#[tokio::test]
async fn a_full_channel_retains_rows_and_has_one_prefix_recovery_slot() {
    let (sender, mut feed) = signal_channel();
    for sequence in 2..=(SIGNAL_CHANNEL_CAPACITY as u64 + 1) {
        sender.try_send(row("future", sequence)).unwrap();
    }
    feed.set_gap_requests(&[request("future", 1)], &[]).unwrap();
    let ordinary_rejected = row("other", 1);
    let refused = sender.try_send(ordinary_rejected.clone()).unwrap_err();
    assert!(matches!(&refused, SignalSendError::Full(_)));
    assert_eq!(refused.into_inner(), ordinary_rejected);
    sender.try_send(row("future", 1)).unwrap();
    assert!(matches!(
        sender.try_send(row("future", 1)),
        Err(SignalSendError::Full(_))
    ));
    assert_eq!(feed.0.lock().queued.len(), SIGNAL_CHANNEL_CAPACITY + 1);
    assert_eq!(feed.next_observation().await.unwrap(), row("future", 1));
    assert!(feed.next_observation().await.is_err());
    feed.acknowledge_last().unwrap();
    assert!(!feed.0.lock().recovery_used);
    for sequence in 2..=(SIGNAL_CHANNEL_CAPACITY as u64 + 1) {
        feed.set_gap_requests(&[request("future", sequence)], &[])
            .unwrap();
        let delivered = feed.next_observation().await.unwrap();
        assert_eq!(delivered, row("future", sequence));
        if sequence == 2 {
            feed.defer_last(delivered).unwrap();
            assert_eq!(
                feed.next_observation().await.unwrap(),
                row("future", sequence)
            );
        }
        feed.acknowledge_last().unwrap();
    }
    assert_eq!(feed.0.lock().ordinary_rows, 0);
    assert_eq!(feed.0.lock().ordinary_bytes, 0);
    drop(sender);
    assert!(matches!(
        feed.next_observation().await,
        Err(SignalError::Closed)
    ));
}

#[tokio::test]
async fn channel_byte_capacity_includes_outstanding_and_reserved_payloads() {
    fn large(sequence: u64) -> SignalObservation {
        let mut row = row("source", sequence);
        row.payload
            .reserve_exact(MAX_SIGNAL_OBSERVATION_BYTES - row.payload.len());
        row
    }
    let (sender, mut feed) = signal_channel();
    for sequence in 2..=4 {
        sender.try_send(large(sequence)).unwrap();
    }
    let refused = sender.try_send(large(5)).unwrap_err();
    assert!(matches!(&refused, SignalSendError::Full(_)));
    assert_eq!(feed.0.lock().ordinary_rows, 3);
    let delivered = feed.next_observation().await.unwrap();
    assert!(matches!(
        sender.try_send(refused.into_inner()),
        Err(SignalSendError::Full(_))
    ));
    feed.defer_last(delivered).unwrap();
    feed.set_gap_requests(&[request("source", 1)], &[]).unwrap();
    sender.try_send(large(1)).unwrap();
    assert_eq!(feed.next_observation().await.unwrap().sequence, 1);
    assert!(feed.0.lock().ordinary_bytes <= SIGNAL_CHANNEL_BYTES);
    assert!(feed.0.lock().recovery_used);
    feed.acknowledge_last().unwrap();
    let mut oversized = row("source", 1);
    oversized
        .payload
        .reserve_exact(MAX_SIGNAL_RETAINED_BYTES + 1);
    assert!(matches!(
        sender.try_send(oversized),
        Err(SignalSendError::Full(_))
    ));
}

#[tokio::test]
async fn a_full_new_generation_queue_cannot_block_old_generation_catchup() {
    let (sender, mut feed) = signal_channel();
    for sequence in 1..=SIGNAL_CHANNEL_CAPACITY as u64 {
        sender.try_send(row("new", sequence)).unwrap();
    }
    for sequence in 1..=2 {
        feed.set_gap_requests(&[request("old", sequence)], &[StrategyId(0)])
            .unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(10), feed.next_observation())
                .await
                .is_err()
        );
        sender.try_send(row("old", sequence)).unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), row("old", sequence));
        feed.acknowledge_last().unwrap();
        assert_eq!(feed.0.lock().ordinary_rows, SIGNAL_CHANNEL_CAPACITY);
        assert!(!feed.0.lock().recovery_used);
    }
    feed.set_gap_requests(&[], &[]).unwrap();
    for sequence in 1..=SIGNAL_CHANNEL_CAPACITY as u64 {
        assert_eq!(feed.next_observation().await.unwrap(), row("new", sequence));
        feed.acknowledge_last().unwrap();
    }
    assert_eq!(feed.0.lock().ordinary_bytes, 0);
}

#[tokio::test]
async fn cancelled_channel_poll_and_sender_close_do_not_discard_pending_rows() {
    let (sender, mut feed) = signal_channel();
    feed.set_gap_requests(&[request("source", 1)], &[]).unwrap();
    sender.try_send(row("source", 2)).unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(10), feed.next_observation())
            .await
            .is_err()
    );
    drop(sender);
    feed.set_gap_requests(&[request("source", 2)], &[]).unwrap();
    assert_eq!(feed.next_observation().await.unwrap(), row("source", 2));
    feed.acknowledge_last().unwrap();
    assert!(matches!(
        feed.next_observation().await,
        Err(SignalError::Closed)
    ));
}

#[test]
fn cancelled_spool_scan_reselects_when_the_gap_policy_changes() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(1)
        .build()
        .unwrap();
    runtime.block_on(async {
        let directory = crate::testpath::temp_path("signal-cancel-policy");
        std::fs::create_dir(directory.path()).unwrap();
        let mut feed = SpoolSignalFeed::new(directory.path());
        let future = row("future", 2);
        let future_path = write(&feed, &future);
        let (release, held) = std::sync::mpsc::channel::<()>();
        let hold = tokio::task::spawn_blocking(move || held.recv().unwrap());
        assert!(
            tokio::time::timeout(Duration::from_millis(10), feed.next_observation())
                .await
                .is_err()
        );
        assert!(feed.selection.is_some());
        feed.set_gap_requests(&[request("future", 1)], &[]).unwrap();
        let other = row("other", 3);
        write(&feed, &other);
        release.send(()).unwrap();
        hold.await.unwrap();
        assert_eq!(
            tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
                .await
                .unwrap()
                .unwrap(),
            other
        );
        assert!(future_path.exists());
        feed.acknowledge_last().unwrap();
        let missing = row("future", 1);
        write(&feed, &missing);
        assert_eq!(feed.next_observation().await.unwrap(), missing);
    });
}

#[test]
fn cancelled_retirement_only_removes_the_acknowledged_row() {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .max_blocking_threads(1)
        .build()
        .unwrap();
    runtime.block_on(async {
        let directory = crate::testpath::temp_path("signal-cancel-retirement");
        std::fs::create_dir(directory.path()).unwrap();
        let mut feed = SpoolSignalFeed::new(directory.path());
        let first_path = write(&feed, &row("source", 1));
        let second = row("source", 2);
        let second_path = write(&feed, &second);
        assert_eq!(feed.next_observation().await.unwrap().sequence, 1);
        feed.acknowledge_last().unwrap();
        let (release, held) = std::sync::mpsc::channel::<()>();
        let hold = tokio::task::spawn_blocking(move || held.recv().unwrap());
        assert!(
            tokio::time::timeout(Duration::from_millis(10), feed.next_observation())
                .await
                .is_err()
        );
        assert!(feed.retirement.is_some());
        assert!(first_path.exists());
        release.send(()).unwrap();
        hold.await.unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), second);
        assert!(!first_path.exists());
        assert!(second_path.exists());
    });
}

#[test]
fn physical_file_limit_is_checked_before_allocating_the_envelope() {
    let directory = crate::testpath::temp_path("signal-oversize-file");
    std::fs::create_dir(directory.path()).unwrap();
    let feed = SpoolSignalFeed::new(directory.path());
    let path = feed.path_for(&row("source", 1));
    std::fs::File::create(&path)
        .unwrap()
        .set_len(MAX_SIGNAL_FILE_BYTES + 1)
        .unwrap();
    assert!(SpoolSignalFeed::read_one(&path)
        .unwrap_err()
        .to_string()
        .contains("exceeds"));
    assert!(path.exists());
}

#[tokio::test]
async fn a_doorbell_during_an_empty_scan_cannot_be_overwritten_by_its_result() {
    let directory = crate::testpath::temp_path("signal-scan-doorbell");
    std::fs::create_dir(directory.path()).unwrap();
    let mut feed =
        SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_secs(30));
    let scanner = feed.scanner.take().unwrap();
    feed.selection = Some(tokio::task::spawn_blocking(move || {
        (scanner, Vec::new(), Vec::new(), Ok(None))
    }));
    let expected = row("source", 1);
    write(&feed, &expected);
    feed.wake();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
            .await
            .unwrap()
            .unwrap(),
        expected
    );
}

#[tokio::test]
async fn a_socket_frame_is_only_a_prompt_to_read_the_durable_spool() {
    use tokio::io::AsyncWriteExt;
    let directory = PathBuf::from(format!("/tmp/lm-doorbell-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&directory);
    std::fs::create_dir(&directory).unwrap();
    let mut feed = HybridSignalFeed::new(&directory)
        .unwrap()
        .with_poll_interval(Duration::from_secs(30));
    assert!(
        tokio::time::timeout(Duration::from_millis(20), feed.next_observation())
            .await
            .is_err()
    );
    let expected = row("source", 1);
    let body = serde_json::to_vec(&expected).unwrap();
    let mut socket = tokio::net::UnixStream::connect(directory.join("stream.sock"))
        .await
        .unwrap();
    socket
        .write_all(&(body.len() as u32).to_le_bytes())
        .await
        .unwrap();
    socket.write_all(&body).await.unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(20), feed.next_observation())
            .await
            .is_err()
    );
    let path = write(&feed.spool, &expected);
    // The file is authoritative even if the producer dies partway through
    // its next socket frame.
    socket.write_all(&[1]).await.unwrap();
    let start = std::time::Instant::now();
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), feed.next_observation())
            .await
            .unwrap()
            .unwrap(),
        expected
    );
    eprintln!(
        "durable socket wake delivery: {:?} (30s spool interval)",
        start.elapsed()
    );
    assert!(path.exists());
    drop(feed);
    drop(socket);
    std::fs::remove_dir_all(directory).unwrap();
}
