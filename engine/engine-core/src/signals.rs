//! Lossless signal delivery into the single-threaded core.
//!
//! Production uses [`HybridSignalFeed`]. The signal worker renames every
//! observation into the spool directory as an immutable JSON envelope named
//! `<sequence:020>-<content_sha256>.json`, and only then sends the same bytes
//! down `stream.sock` so the engine need not wait for its next spool poll. The
//! spool row is the delivery; the frame is the doorbell. A returned envelope
//! remains until the reader is polled again, after the engine's WAL barrier;
//! that next poll retires it, whichever path it arrived by. The WAL cursor
//! rejects a duplicate left by a crash.
//!
//! `next_observation` is one branch of the core's `select!`, which drops the
//! future whenever another branch wins. Every read here is therefore
//! resumable: a frame that has yielded its length prefix and none of its body
//! is finished on the next poll, never re-read from its first four bytes.
//!
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

    pub async fn retire_last(&mut self) -> Result<(), SignalError> {
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
        Ok(())
    }

    /// The spool row an observation is delivered as, whichever path it took.
    pub fn path_for(&self, observation: &SignalObservation) -> PathBuf {
        self.directory.join(format!(
            "{:020}-{}.json",
            observation.sequence, observation.content_sha256
        ))
    }

    /// `(sequence, content_sha256)` from a spool filename.
    fn parse_name(path: &Path) -> Result<(u64, &str), SignalError> {
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
        Ok((named_sequence, hash))
    }

    /// Every `.json` row in the directory, on the blocking pool.
    async fn scan(&self) -> Result<BTreeSet<PathBuf>, SignalError> {
        let directory = self.directory.clone();
        tokio::task::spawn_blocking(move || {
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
        .map_err(|error| SignalError::Source(format!("signal spool task failed: {error}")))?
    }

    async fn read_known(
        &mut self,
        path: PathBuf,
    ) -> Result<Option<SignalObservation>, SignalError> {
        let read_path = path.clone();
        let observation = tokio::task::spawn_blocking(move || Self::read_one(&read_path))
            .await
            .map_err(|error| SignalError::Source(format!("signal read task failed: {error}")))??;
        if observation.is_some() {
            self.returned_path = Some(path);
        }
        Ok(observation)
    }

    fn read_one(path: &Path) -> Result<Option<SignalObservation>, SignalError> {
        let (named_sequence, hash) = Self::parse_name(path)?;
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
        self.retire_last().await?;
        loop {
            if let Some(path) = self.known_paths.pop_first() {
                if let Some(observation) = self.read_known(path).await? {
                    return Ok(observation);
                }
                continue;
            }
            let discovered = self.scan().await?;
            self.known_paths.extend(discovered);
            if let Some(path) = self.known_paths.pop_first() {
                if let Some(observation) = self.read_known(path).await? {
                    return Ok(observation);
                }
                continue;
            }
            tokio::time::sleep(self.poll).await;
        }
    }
}

/// Streaming signal feed over an AF_UNIX domain socket.
///
/// Observations are framed as `[u32 length_le][raw JSON bytes of SignalObservation]`.
/// The frame being read lives in `frame`, not on the future's stack, because
/// the core drops this future every time another `select!` branch wins.
pub struct UnixSignalFeed {
    socket_path: PathBuf,
    listener: tokio::net::UnixListener,
    active_stream: Option<tokio::net::UnixStream>,
    frame: Frame,
}

/// One frame in progress. `body` is sized once the four length bytes are in.
#[derive(Default)]
struct Frame {
    len_buf: [u8; 4],
    len_filled: usize,
    body: Vec<u8>,
    body_filled: usize,
}

impl Frame {
    fn started(&self) -> bool {
        self.len_filled > 0
    }
}

impl UnixSignalFeed {
    pub fn bind(socket_path: impl Into<PathBuf>) -> std::io::Result<Self> {
        let socket_path = socket_path.into();
        let _ = std::fs::remove_file(&socket_path);
        let listener = tokio::net::UnixListener::bind(&socket_path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o770));
        }
        Ok(Self {
            socket_path,
            listener,
            active_stream: None,
            frame: Frame::default(),
        })
    }

    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    fn drop_stream(&mut self) {
        self.active_stream = None;
        self.frame = Frame::default();
    }
}

impl SignalFeed for UnixSignalFeed {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        use tokio::io::AsyncReadExt;
        loop {
            let Some(stream) = self.active_stream.as_mut() else {
                match self.listener.accept().await {
                    Ok((stream, _)) => self.active_stream = Some(stream),
                    Err(err) => {
                        return Err(SignalError::Source(format!(
                            "cannot accept Unix signal socket connection on {}: {err}",
                            self.socket_path.display()
                        )));
                    }
                }
                continue;
            };
            let frame = &mut self.frame;
            // `read` is cancel-safe: a poll that returns Pending has taken
            // nothing off the socket, so a dropped future loses no bytes.
            let read = if frame.len_filled < 4 {
                stream.read(&mut frame.len_buf[frame.len_filled..]).await
            } else {
                stream.read(&mut frame.body[frame.body_filled..]).await
            };
            match read {
                Ok(0) => {
                    if frame.started() {
                        tracing::warn!(
                            length_bytes = frame.len_filled,
                            body_bytes = frame.body_filled,
                            "signal client disconnected during frame read"
                        );
                    } else {
                        tracing::debug!("signal client disconnected; waiting for next connection");
                    }
                    self.drop_stream();
                }
                Ok(read) if frame.len_filled < 4 => {
                    frame.len_filled += read;
                    if frame.len_filled == 4 {
                        let len = u32::from_le_bytes(frame.len_buf) as usize;
                        if len == 0 || len > MAX_SIGNAL_OBSERVATION_BYTES {
                            self.drop_stream();
                            return Err(SignalError::Source(format!(
                                "invalid signal frame size: {len} bytes (max: {MAX_SIGNAL_OBSERVATION_BYTES})"
                            )));
                        }
                        frame.body = vec![0u8; len];
                        frame.body_filled = 0;
                    }
                }
                Ok(read) => {
                    frame.body_filled += read;
                    if frame.body_filled == frame.body.len() {
                        let body = std::mem::take(&mut frame.body);
                        *frame = Frame::default();
                        let observation: SignalObservation = serde_json::from_slice(&body)
                            .map_err(|err| {
                                SignalError::Source(format!("malformed signal frame JSON: {err}"))
                            })?;
                        validate(&observation).map_err(SignalError::Source)?;
                        return Ok(observation);
                    }
                }
                Err(err) => {
                    tracing::debug!(error = %err, "signal client read failed; waiting for next connection");
                    self.drop_stream();
                }
            }
        }
    }
}

impl Drop for UnixSignalFeed {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.socket_path);
    }
}

/// The production feed: spool rows, with the socket as a wake-up.
///
/// The worker writes an observation's spool row before it sends the frame, so
/// any row still on disk with a lower sequence than a frame was written before
/// it. Those rows go first; the frame waits in `parked`. Sequences are per
/// source, but a row from another source going first costs nothing.
pub struct HybridSignalFeed {
    unix: UnixSignalFeed,
    spool: SpoolSignalFeed,
    parked: Option<SignalObservation>,
}

impl HybridSignalFeed {
    pub fn new(directory: impl Into<PathBuf>) -> std::io::Result<Self> {
        let dir = directory.into();
        let sock_path = dir.join("stream.sock");
        let unix = UnixSignalFeed::bind(sock_path)?;
        let spool = SpoolSignalFeed::new(dir);
        Ok(Self {
            unix,
            spool,
            parked: None,
        })
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.spool = self.spool.with_poll_interval(poll);
        self
    }
}

impl SignalFeed for HybridSignalFeed {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        loop {
            self.spool.retire_last().await?;
            if let Some(path) = self.spool.known_paths.pop_first() {
                if let Some(observation) = self.spool.read_known(path).await? {
                    return Ok(observation);
                }
                continue;
            }
            if let Some(observation) = self.parked.take() {
                self.spool.returned_path = Some(self.spool.path_for(&observation));
                return Ok(observation);
            }
            let observation = tokio::select! {
                biased;
                observation = self.unix.next_observation() => observation?,
                observation = self.spool.next_observation() => return observation,
            };
            let below = self
                .spool
                .scan()
                .await?
                .into_iter()
                .filter(|path| {
                    SpoolSignalFeed::parse_name(path)
                        .is_ok_and(|(sequence, _)| sequence < observation.sequence)
                })
                .collect::<BTreeSet<_>>();
            if below.is_empty() {
                self.spool.returned_path = Some(self.spool.path_for(&observation));
                return Ok(observation);
            }
            // Rows a dropped spool scan left behind are rediscovered later;
            // only the ones written before this frame may go ahead of it.
            self.spool.known_paths = below;
            self.parked = Some(observation);
        }
    }
}

/// Unified signal feed selection for production runners.
pub enum EngineSignalFeed {
    Hybrid(Box<HybridSignalFeed>),
    Spool(SpoolSignalFeed),
}

impl EngineSignalFeed {
    pub fn for_directory(directory: impl Into<PathBuf>) -> Self {
        let dir = directory.into();
        match HybridSignalFeed::new(&dir) {
            Ok(hybrid) => Self::Hybrid(Box::new(hybrid)),
            Err(err) => {
                tracing::warn!(error = %err, path = %dir.display(), "falling back to pure file spool signal feed");
                Self::Spool(SpoolSignalFeed::new(dir))
            }
        }
    }
}

impl SignalFeed for EngineSignalFeed {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        match self {
            Self::Hybrid(feed) => feed.next_observation().await,
            Self::Spool(feed) => feed.next_observation().await,
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

    #[tokio::test]
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

    #[tokio::test]
    async fn hybrid_signal_feed_drains_spool_then_receives_socket() {
        use std::io::Write;
        use std::os::unix::net::UnixStream;

        let directory = short_test_dir("hy-sig");

        let mut first = observation();
        first.sequence = 1;
        first.content_sha256 = content_sha256(&first);
        let first_path = spool_path(&directory, &first);
        std::fs::write(&first_path, serde_json::to_vec(&first).unwrap()).unwrap();

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

        let sock_path = directory.join("stream.sock");
        let mut client = UnixStream::connect(&sock_path).unwrap();
        client.write_all(&len).unwrap();
        client.write_all(&body).unwrap();
        client.flush().unwrap();

        let obs2 = feed.next_observation().await.unwrap();
        assert_eq!(obs2, second);

        assert!(!first_path.exists());

        drop(client);
        let _ = std::fs::remove_dir_all(&directory);
    }

    /// The core's `select!` drops the feed future whenever another branch
    /// wins. A frame whose length prefix was read before that and whose body
    /// arrives after it is one frame, not a length followed by `{"sc`.
    #[tokio::test]
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

        let dropped =
            tokio::time::timeout(Duration::from_millis(50), feed.next_observation()).await;
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
    #[tokio::test]
    async fn a_frame_waits_for_the_row_written_before_it_and_retires_its_own() {
        use std::io::Write;
        use std::os::unix::net::UnixStream;

        let directory = short_test_dir("hy-order");

        let mut first = observation();
        first.sequence = 1;
        first.content_sha256 = content_sha256(&first);
        let first_path = spool_path(&directory, &first);
        std::fs::write(&first_path, serde_json::to_vec(&first).unwrap()).unwrap();

        let mut second = observation();
        second.sequence = 2;
        second.content_sha256 = content_sha256(&second);
        let second_path = spool_path(&directory, &second);
        let second_body = serde_json::to_vec(&second).unwrap();
        std::fs::write(&second_path, &second_body).unwrap();

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
        assert_eq!(feed.next_observation().await.unwrap(), second);
        assert!(!first_path.exists(), "the row before the frame is retired");
        assert!(
            second_path.exists(),
            "the frame's row waits for the barrier"
        );

        let quiet = tokio::time::timeout(Duration::from_millis(200), feed.next_observation()).await;
        assert!(quiet.is_err(), "nothing else was written");
        assert!(
            !second_path.exists(),
            "the next poll retires the frame's own row"
        );

        drop(client);
        let _ = std::fs::remove_dir_all(&directory);
    }
}
