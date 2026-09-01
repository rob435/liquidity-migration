//! Lossless signal delivery into the single-threaded core.
//!
//! Production uses [`SpoolSignalFeed`]: signal workers atomically rename immutable
//! JSON envelopes into a directory. A returned envelope remains until the
//! reader is polled again, after the engine's WAL barrier; that next poll
//! retires it. Filenames carry the contiguous source sequence and exact
//! content hash, and the WAL cursor rejects a duplicate left by a crash.
//! The bounded channel is for an in-process credential-free worker or tests;
//! its sender is non-blocking and says `Full` instead of waiting on the core.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::time::Duration;

use engine_types::{
    SignalError, SignalFeed, SignalObservation, SignalSubscriptionState, Subscription, WalRecord,
    MAX_SIGNAL_OBSERVATION_BYTES, MAX_SIGNAL_SUBSCRIPTIONS, SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use sha2::{Digest, Sha256};

pub const SIGNAL_CHANNEL_CAPACITY: usize = 256;
const FIELD_BYTES_MAX: usize = 256;
const SYMBOL_BYTES_MAX: usize = 128;

pub fn content_sha256(observation: &SignalObservation) -> String {
    hex::encode(Sha256::digest(observation.canonical_envelope_bytes()))
}

pub fn validate(observation: &SignalObservation) -> Result<(), String> {
    fn field(name: &str, value: &str) -> Result<(), String> {
        if value.is_empty() || value.len() > FIELD_BYTES_MAX {
            return Err(format!("{name} must contain 1..={FIELD_BYTES_MAX} bytes"));
        }
        Ok(())
    }

    if observation.schema_version != SIGNAL_OBSERVATION_SCHEMA_VERSION {
        return Err(format!(
            "signal schema {} is not supported; expected {}",
            observation.schema_version, SIGNAL_OBSERVATION_SCHEMA_VERSION
        ));
    }
    field("decision_fingerprint", &observation.decision_fingerprint)?;
    field("source", &observation.source)?;
    field("observation_id", &observation.observation_id)?;
    field("kind", &observation.kind)?;
    if observation.sequence == 0 {
        return Err("signal sequence must start at 1".to_string());
    }
    if observation.observed_wall_ts_ms <= 0
        || observation.available_wall_ts_ms < observation.observed_wall_ts_ms
    {
        return Err(
            "signal availability must be at or after a positive observation time".to_string(),
        );
    }
    if observation.payload.len() > MAX_SIGNAL_OBSERVATION_BYTES {
        return Err(format!(
            "signal payload is {} bytes; maximum is {}",
            observation.payload.len(),
            MAX_SIGNAL_OBSERVATION_BYTES
        ));
    }
    if observation.subscriptions.len() > MAX_SIGNAL_SUBSCRIPTIONS {
        return Err(format!(
            "signal requests {} subscriptions; maximum is {}",
            observation.subscriptions.len(),
            MAX_SIGNAL_SUBSCRIPTIONS
        ));
    }
    let mut subscriptions = BTreeSet::new();
    for subscription in &observation.subscriptions {
        if subscription.symbol.is_empty() || subscription.symbol.len() > SYMBOL_BYTES_MAX {
            return Err(format!(
                "signal subscription symbol must contain 1..={SYMBOL_BYTES_MAX} bytes"
            ));
        }
        let key = (
            subscription.symbol.as_str(),
            format!("{:?}", subscription.feed),
        );
        if !subscriptions.insert(key) {
            return Err(format!(
                "signal repeats the {:?} subscription for {}",
                subscription.feed, subscription.symbol
            ));
        }
    }
    if observation.content_sha256.len() != 64
        || !observation
            .content_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("signal content_sha256 must be 64 lowercase hex bytes".to_string());
    }
    let calculated = content_sha256(observation);
    if calculated != observation.content_sha256 {
        return Err(format!(
            "signal content hash is {}, calculated {}",
            observation.content_sha256, calculated
        ));
    }
    Ok(())
}

/// The monotonic subscription union for each source/destination after replay.
/// Runner adds it to its boot feed before core restore.
pub fn active_subscriptions(replayed: &[WalRecord]) -> Vec<Subscription> {
    let mut active: std::collections::BTreeMap<(String, u16), SignalSubscriptionState> =
        std::collections::BTreeMap::new();
    for record in replayed {
        match record {
            WalRecord::SignalObservation { observation, .. } => {
                let row = active
                    .entry((observation.source.clone(), observation.destination.0))
                    .or_insert_with(|| SignalSubscriptionState {
                        source: observation.source.clone(),
                        destination: observation.destination,
                        subscriptions: Vec::new(),
                    });
                for subscription in &observation.subscriptions {
                    if !row.subscriptions.contains(subscription) {
                        row.subscriptions.push(subscription.clone());
                    }
                }
            }
            WalRecord::SegmentBase {
                signal_subscriptions,
                ..
            } => {
                active = signal_subscriptions
                    .iter()
                    .map(|row| ((row.source.clone(), row.destination.0), row.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    let mut subscriptions = Vec::new();
    for row in active.values() {
        for subscription in &row.subscriptions {
            if !subscriptions.contains(subscription) {
                subscriptions.push(subscription.clone());
            }
        }
    }
    subscriptions
}

#[derive(Debug, PartialEq, Eq)]
pub enum SignalSendError {
    Full,
    Closed,
}

impl std::fmt::Display for SignalSendError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            SignalSendError::Full => "signal queue is full",
            SignalSendError::Closed => "signal receiver is closed",
        })
    }
}

impl std::error::Error for SignalSendError {}

#[derive(Clone)]
pub struct SignalSender(tokio::sync::mpsc::Sender<SignalObservation>);

pub struct SignalReceiver(tokio::sync::mpsc::Receiver<SignalObservation>);

pub fn signal_channel() -> (SignalSender, SignalReceiver) {
    let (sender, receiver) = tokio::sync::mpsc::channel(SIGNAL_CHANNEL_CAPACITY);
    (SignalSender(sender), SignalReceiver(receiver))
}

impl SignalSender {
    pub fn try_send(&self, observation: SignalObservation) -> Result<(), SignalSendError> {
        self.0.try_send(observation).map_err(|error| match error {
            tokio::sync::mpsc::error::TrySendError::Full(_) => SignalSendError::Full,
            tokio::sync::mpsc::error::TrySendError::Closed(_) => SignalSendError::Closed,
        })
    }
}

impl SignalFeed for SignalReceiver {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        self.0.recv().await.ok_or(SignalError::Closed)
    }
}

/// A source that never produces. Keeps the ordinary `Engine::run` API while
/// `run_with_signals` owns the real injection seam.
pub struct NoSignals;

impl SignalFeed for NoSignals {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        std::future::pending().await
    }
}

/// Read immutable, ordered JSON envelopes from one spool directory.
///
/// A complete filename is `<sequence:020>-<content_sha256>.json`. The signal
/// worker writes elsewhere and renames into this name only after closing it.
/// The reader performs filesystem work on Tokio's blocking pool, so a slow
/// disk cannot stall private-order or market processing on the core thread.
pub struct SpoolSignalFeed {
    directory: PathBuf,
    returned_path: Option<PathBuf>,
    known_paths: BTreeSet<PathBuf>,
    poll: Duration,
}

impl SpoolSignalFeed {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            returned_path: None,
            known_paths: BTreeSet::new(),
            poll: Duration::from_millis(100),
        }
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.poll = poll.max(Duration::from_millis(1));
        self
    }

    fn read_one(path: &Path) -> Result<Option<SignalObservation>, SignalError> {
        let file_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| SignalError::Source("signal filename is not UTF-8".to_string()))?;
        let stem = file_name
            .strip_suffix(".json")
            .ok_or_else(|| SignalError::Source("signal file must end in .json".to_string()))?;
        let (sequence, hash) = stem.split_once('-').ok_or_else(|| {
            SignalError::Source(format!(
                "signal file {file_name} must be <sequence>-<sha256>.json"
            ))
        })?;
        if sequence.len() != 20 || !sequence.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(SignalError::Source(format!(
                "signal file {file_name} sequence must be 20 decimal digits"
            )));
        }
        let named_sequence = sequence.parse::<u64>().map_err(|error| {
            SignalError::Source(format!("signal file {file_name} has bad sequence: {error}"))
        })?;
        let raw = match std::fs::read(path) {
            Ok(raw) => raw,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(SignalError::Source(format!(
                    "cannot read signal file {}: {error}",
                    path.display()
                )));
            }
        };
        let observation: SignalObservation = serde_json::from_slice(&raw).map_err(|error| {
            SignalError::Source(format!(
                "signal file {} is not an observation: {error}",
                path.display()
            ))
        })?;
        validate(&observation).map_err(|error| {
            SignalError::Source(format!("signal file {}: {error}", path.display()))
        })?;
        if observation.sequence != named_sequence || observation.content_sha256 != hash {
            return Err(SignalError::Source(format!(
                "signal file {} name does not match its sequence/hash envelope",
                path.display()
            )));
        }
        Ok(Some(observation))
    }
}

impl SignalFeed for SpoolSignalFeed {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        if let Some(path) = self.returned_path.take() {
            tokio::task::spawn_blocking(move || match std::fs::remove_file(&path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(SignalError::Source(format!(
                    "cannot retire durable signal file {}: {error}",
                    path.display()
                ))),
            })
            .await
            .map_err(|error| {
                SignalError::Source(format!("signal retire task failed: {error}"))
            })??;
        }
        loop {
            let directory = self.directory.clone();
            let discovered = tokio::task::spawn_blocking(move || {
                let entries = std::fs::read_dir(&directory).map_err(|error| {
                    SignalError::Source(format!(
                        "cannot scan signal spool {}: {error}",
                        directory.display()
                    ))
                })?;
                let mut paths = BTreeSet::new();
                for entry in entries {
                    let path = entry
                        .map_err(|error| SignalError::Source(error.to_string()))?
                        .path();
                    if path
                        .extension()
                        .is_some_and(|extension| extension == "json")
                    {
                        paths.insert(path);
                    }
                }
                Ok::<_, SignalError>(paths)
            })
            .await
            .map_err(|error| SignalError::Source(format!("signal spool task failed: {error}")))??;
            self.known_paths.extend(discovered);

            if let Some(path) = self.known_paths.pop_first() {
                let read_path = path.clone();
                let observation = tokio::task::spawn_blocking(move || Self::read_one(&read_path))
                    .await
                    .map_err(|error| {
                        SignalError::Source(format!("signal read task failed: {error}"))
                    })??;
                let Some(observation) = observation else {
                    continue;
                };
                self.returned_path = Some(path);
                return Ok(observation);
            }
            tokio::time::sleep(self.poll).await;
        }
    }
}

#[cfg(test)]
mod tests {
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
        let mut changed = observation.clone();
        changed.payload.push(b' ');
        assert!(validate(&changed).unwrap_err().contains("content hash"));
    }

    #[tokio::test]
    async fn bounded_sender_never_waits() {
        let (sender, _receiver) = signal_channel();
        for _ in 0..SIGNAL_CHANNEL_CAPACITY {
            sender.try_send(observation()).unwrap();
        }
        assert_eq!(sender.try_send(observation()), Err(SignalSendError::Full));
    }

    #[tokio::test]
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
        std::fs::write(&first_path, serde_json::to_vec(&first).unwrap()).unwrap();
        std::fs::write(&second_path, serde_json::to_vec(&second).unwrap()).unwrap();

        let mut feed =
            SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
        assert_eq!(feed.next_observation().await.unwrap(), first);
        assert!(
            first_path.exists(),
            "not retired before core can barrier it"
        );
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

    #[tokio::test]
    async fn invalid_spool_row_is_never_retired() {
        let directory = crate::testpath::temp_path("bad-signal-spool");
        std::fs::create_dir(directory.path()).unwrap();
        let mut bad = observation();
        bad.content_sha256 = "0".repeat(64);
        let path = spool_path(directory.path(), &bad);
        std::fs::write(&path, serde_json::to_vec(&bad).unwrap()).unwrap();
        let mut feed = SpoolSignalFeed::new(directory.path());
        assert!(feed.next_observation().await.is_err());
        assert!(
            path.exists(),
            "a failed admission source stays for inspection"
        );
        std::fs::remove_file(path).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    }

    #[tokio::test]
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
        std::fs::write(&missing_path, serde_json::to_vec(&missing).unwrap()).unwrap();
        std::fs::write(&live_path, serde_json::to_vec(&live).unwrap()).unwrap();

        let mut feed =
            SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
        feed.known_paths.insert(missing_path.clone());
        feed.known_paths.insert(live_path.clone());
        std::fs::remove_file(missing_path).unwrap();
        assert_eq!(feed.next_observation().await.unwrap(), live);

        std::fs::remove_file(live_path).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    }

    #[tokio::test]
    async fn each_pop_merges_new_lower_sequences_from_an_independent_lane() {
        let directory = crate::testpath::temp_path("signal-spool-independent-lanes");
        std::fs::create_dir(directory.path()).unwrap();
        let mut high = observation();
        high.sequence = 100;
        high.observation_id = "long-100".into();
        high.content_sha256 = content_sha256(&high);
        let high_path = spool_path(directory.path(), &high);
        std::fs::write(&high_path, serde_json::to_vec(&high).unwrap()).unwrap();

        let mut feed =
            SpoolSignalFeed::new(directory.path()).with_poll_interval(Duration::from_millis(1));
        assert_eq!(feed.next_observation().await.unwrap(), high);

        let mut low = observation();
        low.destination = StrategyId(3);
        low.sequence = 1;
        low.observation_id = "carry-1".into();
        low.content_sha256 = content_sha256(&low);
        let low_path = spool_path(directory.path(), &low);
        std::fs::write(&low_path, serde_json::to_vec(&low).unwrap()).unwrap();

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
        };
        assert_eq!(active_subscriptions(&[rotated]), expected);
    }
}
