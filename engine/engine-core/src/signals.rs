//! Lossless signal delivery into the single-threaded core.
//!
//! Production uses [`HybridSignalFeed`]. The signal worker renames every
//! observation into the spool directory as an immutable JSON envelope named
//! `<sequence:020>-<content_sha256>.json`, and only then sends the same bytes
//! down `stream.sock` so the engine need not wait for its next spool poll. The
//! spool row is the delivery; the frame is the doorbell. A returned envelope
//! remains until the engine explicitly acknowledges its WAL barrier. Deferred
//! rows stay on disk; requested missing prefixes take priority over other rows.
//! The WAL cursor rejects a duplicate left by a crash.
//!
//! `next_observation` is one branch of the core's `select!`, which drops the
//! future whenever another branch wins. Every read here is therefore
//! resumable: a frame that has yielded its length prefix and none of its body
//! is finished on the next poll, never re-read from its first four bytes.
//!
//! The bounded channel is for an in-process credential-free worker or tests;
//! its sender is non-blocking and says `Full` instead of waiting on the core.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use engine_types::{
    SignalError, SignalFeed, SignalGapRequest, SignalObservation, SignalSubscriptionState,
    StrategyId, Subscription, WalRecord, MAX_SIGNAL_OBSERVATION_BYTES, MAX_SIGNAL_SUBSCRIPTIONS,
    SIGNAL_OBSERVATION_SCHEMA_VERSION,
};
use sha2::{Digest, Sha256};

pub const SIGNAL_CHANNEL_CAPACITY: usize = 256;
pub const SIGNAL_CHANNEL_BYTES: usize = 64 * 1024 * 1024;
pub const MAX_SIGNAL_RETAINED_BYTES: usize = MAX_SIGNAL_OBSERVATION_BYTES + 1024 * 1024;
pub const MAX_SIGNAL_GAP_REQUESTS: usize = 4_096;
const SPOOL_SCAN_PAGE: usize = 64;
const SPOOL_METADATA_CAPACITY: usize = 4_096;
// The producer permits byte-array JSON encoding as well as UTF-8 strings.
const MAX_SIGNAL_FILE_BYTES: u64 = 80 * 1024 * 1024;
const FIELD_BYTES_MAX: usize = 256;
const SYMBOL_BYTES_MAX: usize = 128;

pub(crate) fn ordered_gap_requests(
    gaps: &[SignalGapRequest],
) -> Result<Vec<SignalGapRequest>, SignalError> {
    if gaps.len() > MAX_SIGNAL_GAP_REQUESTS {
        return Err(SignalError::Source("too many signal gap requests".into()));
    }
    let mut ordered = gaps.to_vec();
    ordered.sort_by(|left, right| left.source.cmp(&right.source));
    if ordered.iter().any(|gap| {
        gap.source.is_empty() || gap.source.len() > FIELD_BYTES_MAX || gap.next_sequence == 0
    }) || ordered
        .windows(2)
        .any(|pair| pair[0].source == pair[1].source)
    {
        return Err(SignalError::Source(
            "signal gap requests must name distinct sources and positive sequences".into(),
        ));
    }
    Ok(ordered)
}

fn requested_sequence(gaps: &[SignalGapRequest], source: &str) -> Option<u64> {
    gaps.binary_search_by(|gap| gap.source.as_str().cmp(source))
        .ok()
        .map(|index| gaps[index].next_sequence)
}

pub(crate) fn ordered_blocked_destinations(
    destinations: &[StrategyId],
) -> Result<Vec<StrategyId>, SignalError> {
    if destinations.len() > u16::MAX as usize + 1 {
        return Err(SignalError::Source(
            "too many blocked signal destinations".into(),
        ));
    }
    let mut ordered = destinations.to_vec();
    ordered.sort_unstable_by_key(|destination| destination.0);
    ordered.dedup();
    Ok(ordered)
}

fn identity_eligible(
    gaps: &[SignalGapRequest],
    blocked: &[StrategyId],
    source: &str,
    sequence: u64,
    destination: StrategyId,
) -> bool {
    match requested_sequence(gaps, source) {
        Some(next) => sequence <= next,
        None => blocked
            .binary_search_by_key(&destination.0, |known| known.0)
            .is_err(),
    }
}

pub(crate) fn signal_eligible(
    gaps: &[SignalGapRequest],
    blocked: &[StrategyId],
    observation: &SignalObservation,
) -> bool {
    identity_eligible(
        gaps,
        blocked,
        &observation.source,
        observation.sequence,
        observation.destination,
    )
}

pub(crate) fn signal_requested(gaps: &[SignalGapRequest], observation: &SignalObservation) -> bool {
    requested_sequence(gaps, &observation.source) == Some(observation.sequence)
}

fn retained_bytes(observation: &SignalObservation) -> usize {
    std::mem::size_of::<SignalObservation>()
        .saturating_add(observation.payload.capacity())
        .saturating_add(observation.source.capacity())
        .saturating_add(observation.decision_fingerprint.capacity())
        .saturating_add(observation.observation_id.capacity())
        .saturating_add(observation.kind.capacity())
        .saturating_add(observation.content_sha256.capacity())
        .saturating_add(
            observation
                .subscriptions
                .capacity()
                .saturating_mul(std::mem::size_of::<Subscription>()),
        )
        .saturating_add(
            observation
                .subscriptions
                .iter()
                .fold(0usize, |bytes, subscription| {
                    bytes.saturating_add(subscription.symbol.capacity())
                }),
        )
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct DeliveryIdentity {
    destination: StrategyId,
    source: String,
    sequence: u64,
    content_sha256: String,
}

impl DeliveryIdentity {
    fn of(observation: &SignalObservation) -> Self {
        Self {
            destination: observation.destination,
            source: observation.source.clone(),
            sequence: observation.sequence,
            content_sha256: observation.content_sha256.clone(),
        }
    }

    fn matches(&self, observation: &SignalObservation) -> bool {
        self.destination == observation.destination
            && self.source == observation.source
            && self.sequence == observation.sequence
            && self.content_sha256 == observation.content_sha256
    }
}

fn protocol_error(message: &str) -> SignalError {
    SignalError::Source(format!("signal delivery protocol: {message}"))
}

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
    Full(Box<SignalObservation>),
    Closed(Box<SignalObservation>),
}

impl SignalSendError {
    pub fn into_inner(self) -> SignalObservation {
        match self {
            Self::Full(observation) | Self::Closed(observation) => *observation,
        }
    }
}

impl std::fmt::Display for SignalSendError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            SignalSendError::Full(_) => "signal queue is full",
            SignalSendError::Closed(_) => "signal receiver is closed",
        })
    }
}

impl std::error::Error for SignalSendError {}

pub struct SignalSender(Arc<SignalChannel>);

pub struct SignalReceiver(Arc<SignalChannel>);

struct SignalChannel {
    state: Mutex<ChannelState>,
    changed: tokio::sync::Notify,
}

struct ChannelState {
    queued: VecDeque<(SignalObservation, bool)>,
    gaps: Vec<SignalGapRequest>,
    blocked_destinations: Vec<StrategyId>,
    outstanding: Option<(DeliveryIdentity, usize, bool)>,
    ordinary_rows: usize,
    ordinary_bytes: usize,
    recovery_used: bool,
    senders: usize,
    receiver_alive: bool,
}

impl SignalChannel {
    fn lock(&self) -> std::sync::MutexGuard<'_, ChannelState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

pub fn signal_channel() -> (SignalSender, SignalReceiver) {
    let shared = Arc::new(SignalChannel {
        state: Mutex::new(ChannelState {
            queued: VecDeque::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            outstanding: None,
            ordinary_rows: 0,
            ordinary_bytes: 0,
            recovery_used: false,
            senders: 1,
            receiver_alive: true,
        }),
        changed: tokio::sync::Notify::new(),
    });
    (SignalSender(shared.clone()), SignalReceiver(shared))
}

impl Clone for SignalSender {
    fn clone(&self) -> Self {
        self.0.lock().senders += 1;
        Self(self.0.clone())
    }
}

impl Drop for SignalSender {
    fn drop(&mut self) {
        self.0.lock().senders -= 1;
        self.0.changed.notify_one();
    }
}

impl Drop for SignalReceiver {
    fn drop(&mut self) {
        self.0.lock().receiver_alive = false;
    }
}

impl SignalSender {
    pub fn try_send(&self, observation: SignalObservation) -> Result<(), SignalSendError> {
        let bytes = retained_bytes(&observation);
        let mut state = self.0.lock();
        if !state.receiver_alive {
            return Err(SignalSendError::Closed(Box::new(observation)));
        }
        if bytes > MAX_SIGNAL_RETAINED_BYTES {
            return Err(SignalSendError::Full(Box::new(observation)));
        }
        let ordinary = state.ordinary_rows < SIGNAL_CHANNEL_CAPACITY
            && state.ordinary_bytes.saturating_add(bytes) <= SIGNAL_CHANNEL_BYTES;
        let recovery =
            !ordinary && !state.recovery_used && signal_requested(&state.gaps, &observation);
        if ordinary {
            state.ordinary_rows += 1;
            state.ordinary_bytes += bytes;
        } else if recovery {
            state.recovery_used = true;
        } else {
            return Err(SignalSendError::Full(Box::new(observation)));
        }
        state.queued.push_back((observation, recovery));
        drop(state);
        self.0.changed.notify_one();
        Ok(())
    }
}

impl SignalFeed for SignalReceiver {
    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        let gaps = ordered_gap_requests(gaps)?;
        let blocked_destinations = ordered_blocked_destinations(blocked_destinations)?;
        let mut state = self.0.lock();
        state.gaps = gaps;
        state.blocked_destinations = blocked_destinations;
        drop(state);
        self.0.changed.notify_one();
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        let mut state = self.0.lock();
        let (_, bytes, recovery) = state
            .outstanding
            .take()
            .ok_or_else(|| protocol_error("no row to acknowledge"))?;
        if recovery {
            state.recovery_used = false;
        } else {
            state.ordinary_rows -= 1;
            state.ordinary_bytes -= bytes;
        }
        Ok(())
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        let mut state = self.0.lock();
        let (identity, bytes, recovery) = state
            .outstanding
            .as_ref()
            .ok_or_else(|| protocol_error("no row to defer"))?;
        let returned_bytes = retained_bytes(&observation);
        if !identity.matches(&observation) || returned_bytes > *bytes {
            return Err(protocol_error(
                "deferred row differs from the outstanding delivery",
            ));
        }
        let recovery = *recovery;
        let bytes = *bytes;
        if !recovery {
            state.ordinary_bytes -= bytes - returned_bytes;
        }
        state.outstanding = None;
        state.queued.push_front((observation, recovery));
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        loop {
            let changed = self.0.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            {
                let mut state = self.0.lock();
                if state.outstanding.is_some() {
                    return Err(protocol_error(
                        "previous row was neither acknowledged nor deferred",
                    ));
                }
                let index = state
                    .queued
                    .iter()
                    .position(|(observation, _)| signal_requested(&state.gaps, observation))
                    .or_else(|| {
                        state.queued.iter().position(|(observation, _)| {
                            signal_eligible(&state.gaps, &state.blocked_destinations, observation)
                        })
                    });
                if let Some(index) = index {
                    let (observation, recovery) =
                        state.queued.remove(index).expect("queued index exists");
                    state.outstanding = Some((
                        DeliveryIdentity::of(&observation),
                        retained_bytes(&observation),
                        recovery,
                    ));
                    return Ok(observation);
                }
                if state.senders == 0 && state.queued.is_empty() {
                    return Err(SignalError::Closed);
                }
            }
            changed.await;
        }
    }
}

/// A source that never produces. Keeps the ordinary `Engine::run` API while
/// `run_with_signals` owns the real injection seam.
pub struct NoSignals;

impl SignalFeed for NoSignals {
    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        ordered_blocked_destinations(blocked_destinations)?;
        ordered_gap_requests(gaps).map(|_| ())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        Err(protocol_error("no row to acknowledge"))
    }

    fn defer_last(&mut self, _observation: SignalObservation) -> Result<(), SignalError> {
        Err(protocol_error("no row to defer"))
    }

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
    returned: Option<(PathBuf, DeliveryIdentity)>,
    acknowledged: Option<PathBuf>,
    retirement: Option<tokio::task::JoinHandle<Result<(), SignalError>>>,
    scanner: Option<SpoolScanner>,
    selection: Option<tokio::task::JoinHandle<ScanResult>>,
    gaps: Vec<SignalGapRequest>,
    blocked_destinations: Vec<StrategyId>,
    poll: Duration,
    next_scan: tokio::time::Instant,
    wake_generation: u64,
    scan_generation: u64,
}

type SelectedRow = Option<(PathBuf, SignalObservation)>;
type ScanResult = (
    SpoolScanner,
    Vec<SignalGapRequest>,
    Vec<StrategyId>,
    Result<SelectedRow, SignalError>,
);

#[derive(Default)]
struct SpoolScanner {
    deferred: BTreeMap<PathBuf, DeliveryIdentity>,
}

impl SpoolScanner {
    fn remember(&mut self, path: PathBuf, observation: &SignalObservation) {
        if !self.deferred.contains_key(&path) && self.deferred.len() == SPOOL_METADATA_CAPACITY {
            self.deferred.pop_first();
        }
        self.deferred
            .insert(path, DeliveryIdentity::of(observation));
    }

    fn page(
        directory: &Path,
        after: Option<&Path>,
        exact: Option<&BTreeSet<u64>>,
    ) -> Result<Vec<PathBuf>, SignalError> {
        let entries = std::fs::read_dir(directory).map_err(|error| {
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
            if path.extension().is_none_or(|extension| extension != "json")
                || after.is_some_and(|after| path.as_path() <= after)
            {
                continue;
            }
            if let Some(exact) = exact {
                let (sequence, _) = SpoolSignalFeed::parse_name(&path)?;
                if !exact.contains(&sequence) {
                    continue;
                }
            }
            paths.insert(path);
            if paths.len() > SPOOL_SCAN_PAGE {
                paths.pop_last();
            }
        }
        Ok(paths.into_iter().collect())
    }

    fn select_pass(
        &mut self,
        directory: &Path,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
        exact: bool,
    ) -> Result<SelectedRow, SignalError> {
        let sequences = exact.then(|| {
            gaps.iter()
                .map(|gap| gap.next_sequence)
                .collect::<BTreeSet<_>>()
        });
        let mut after = None;
        loop {
            let page = Self::page(directory, after.as_deref(), sequences.as_ref())?;
            if page.is_empty() {
                return Ok(None);
            }
            after = page.last().cloned();
            for path in page {
                if let Some(known) = self.deferred.get(&path) {
                    let next = requested_sequence(gaps, &known.source);
                    if (exact && next != Some(known.sequence))
                        || (!exact
                            && !identity_eligible(
                                gaps,
                                blocked_destinations,
                                &known.source,
                                known.sequence,
                                known.destination,
                            ))
                    {
                        continue;
                    }
                }
                let Some(observation) = SpoolSignalFeed::read_one(&path)? else {
                    self.deferred.remove(&path);
                    continue;
                };
                let eligible = if exact {
                    signal_requested(gaps, &observation)
                } else {
                    signal_eligible(gaps, blocked_destinations, &observation)
                };
                if eligible {
                    self.deferred.remove(&path);
                    return Ok(Some((path, observation)));
                }
                self.remember(path, &observation);
            }
        }
    }

    fn select(
        &mut self,
        directory: &Path,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<SelectedRow, SignalError> {
        if !gaps.is_empty() {
            if let Some(row) = self.select_pass(directory, gaps, blocked_destinations, true)? {
                return Ok(Some(row));
            }
        }
        self.select_pass(directory, gaps, blocked_destinations, false)
    }
}

impl SpoolSignalFeed {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            returned: None,
            acknowledged: None,
            retirement: None,
            scanner: Some(SpoolScanner::default()),
            selection: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            poll: Duration::from_millis(100),
            next_scan: tokio::time::Instant::now(),
            wake_generation: 0,
            scan_generation: 0,
        }
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.poll = poll.max(Duration::from_millis(1));
        self
    }

    fn wake(&mut self) {
        self.wake_generation = self.wake_generation.wrapping_add(1);
        self.next_scan = tokio::time::Instant::now();
    }

    async fn retire_acknowledged(&mut self) -> Result<(), SignalError> {
        let Some(path) = self.acknowledged.as_ref() else {
            return Ok(());
        };
        if self.retirement.is_none() {
            let path = path.clone();
            self.retirement = Some(tokio::task::spawn_blocking(
                move || match std::fs::remove_file(&path) {
                    Ok(()) => Ok(()),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(error) => Err(SignalError::Source(format!(
                        "cannot retire durable signal file {}: {error}",
                        path.display()
                    ))),
                },
            ));
        }
        let result = self.retirement.as_mut().expect("retirement exists").await;
        self.retirement = None;
        result.map_err(|error| {
            SignalError::Source(format!("signal retire task failed: {error}"))
        })??;
        self.acknowledged = None;
        Ok(())
    }

    /// The immutable spool filename includes the sequence and canonical hash.
    pub fn path_for(&self, observation: &SignalObservation) -> PathBuf {
        self.directory.join(format!(
            "{:020}-{}.json",
            observation.sequence, observation.content_sha256
        ))
    }

    fn parse_name(path: &Path) -> Result<(u64, &str), SignalError> {
        let file_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| SignalError::Source("signal filename is not UTF-8".into()))?;
        let stem = file_name
            .strip_suffix(".json")
            .ok_or_else(|| SignalError::Source("signal file must end in .json".into()))?;
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
        let sequence = sequence.parse::<u64>().map_err(|error| {
            SignalError::Source(format!("signal file {file_name} has bad sequence: {error}"))
        })?;
        Ok((sequence, hash))
    }

    pub fn read_one(path: &Path) -> Result<Option<SignalObservation>, SignalError> {
        let (named_sequence, hash) = Self::parse_name(path)?;
        let file = match std::fs::File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(SignalError::Source(format!(
                    "cannot read signal file {}: {error}",
                    path.display()
                )))
            }
        };
        let length = file
            .metadata()
            .map_err(|error| SignalError::Source(error.to_string()))?
            .len();
        if length > MAX_SIGNAL_FILE_BYTES {
            return Err(SignalError::Source(format!(
                "signal file {} exceeds {MAX_SIGNAL_FILE_BYTES} bytes",
                path.display()
            )));
        }
        let mut raw = Vec::with_capacity(length as usize);
        file.take(MAX_SIGNAL_FILE_BYTES + 1)
            .read_to_end(&mut raw)
            .map_err(|error| {
                SignalError::Source(format!(
                    "cannot read signal file {}: {error}",
                    path.display()
                ))
            })?;
        if raw.len() as u64 > MAX_SIGNAL_FILE_BYTES {
            return Err(SignalError::Source(format!(
                "signal file {} exceeds {MAX_SIGNAL_FILE_BYTES} bytes",
                path.display()
            )));
        }
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
    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        let gaps = ordered_gap_requests(gaps)?;
        let blocked_destinations = ordered_blocked_destinations(blocked_destinations)?;
        if self.gaps != gaps || self.blocked_destinations != blocked_destinations {
            self.gaps = gaps;
            self.blocked_destinations = blocked_destinations;
            self.wake();
        }
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        let (path, _) = self
            .returned
            .take()
            .ok_or_else(|| protocol_error("no row to acknowledge"))?;
        self.acknowledged = Some(path);
        self.wake();
        Ok(())
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        let (path, identity) = self
            .returned
            .as_ref()
            .ok_or_else(|| protocol_error("no row to defer"))?;
        if !identity.matches(&observation) {
            return Err(protocol_error(
                "deferred row differs from the outstanding delivery",
            ));
        }
        self.scanner
            .as_mut()
            .expect("delivery restored its scanner")
            .remember(path.clone(), &observation);
        self.returned = None;
        self.wake();
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        if self.returned.is_some() {
            return Err(protocol_error(
                "previous row was neither acknowledged nor deferred",
            ));
        }
        self.retire_acknowledged().await?;
        loop {
            if self.selection.is_none() {
                tokio::time::sleep_until(self.next_scan).await;
                let mut scanner = self
                    .scanner
                    .take()
                    .expect("one scanner is retained across polls");
                let directory = self.directory.clone();
                let gaps = self.gaps.clone();
                let blocked_destinations = self.blocked_destinations.clone();
                self.scan_generation = self.wake_generation;
                self.selection = Some(tokio::task::spawn_blocking(move || {
                    let selected = scanner.select(&directory, &gaps, &blocked_destinations);
                    (scanner, gaps, blocked_destinations, selected)
                }));
            }
            let joined = self.selection.as_mut().expect("selection exists").await;
            self.selection = None;
            let (scanner, selected_gaps, selected_blocked, selected) = match joined {
                Ok(result) => result,
                Err(error) => {
                    self.scanner = Some(SpoolScanner::default());
                    return Err(SignalError::Source(format!(
                        "signal scan task failed: {error}"
                    )));
                }
            };
            self.scanner = Some(scanner);
            let selected = selected?;
            if selected_gaps != self.gaps || selected_blocked != self.blocked_destinations {
                self.wake();
                continue;
            }
            if let Some((path, observation)) = selected {
                // A cancelled poll may have been followed by a new gap policy.
                if !signal_eligible(&self.gaps, &self.blocked_destinations, &observation) {
                    self.scanner
                        .as_mut()
                        .expect("scanner restored")
                        .remember(path, &observation);
                    self.wake();
                    continue;
                }
                self.returned = Some((path, DeliveryIdentity::of(&observation)));
                return Ok(observation);
            }
            self.next_scan = if self.scan_generation == self.wake_generation {
                tokio::time::Instant::now() + self.poll
            } else {
                tokio::time::Instant::now()
            };
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

    async fn next_doorbell(&mut self) -> Result<(), SignalError> {
        use tokio::io::AsyncReadExt;
        let mut chunk = [0u8; 8192];
        loop {
            let Some(stream) = self.active_stream.as_mut() else {
                let (stream, _) = self.listener.accept().await.map_err(|error| {
                    SignalError::Source(format!("cannot accept signal doorbell: {error}"))
                })?;
                self.active_stream = Some(stream);
                continue;
            };
            match stream.read(&mut chunk).await {
                Ok(0) | Err(_) => self.drop_stream(),
                Ok(_) => return Ok(()),
            }
        }
    }
}

impl UnixSignalFeed {
    pub async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
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
                            // The frame is only the doorbell: the row is on
                            // disk and the spool poll delivers it.
                            tracing::warn!(
                                length_bytes = len,
                                max_bytes = MAX_SIGNAL_OBSERVATION_BYTES,
                                "invalid signal frame size; dropping the stream"
                            );
                            self.drop_stream();
                            continue;
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

/// The socket wakes the spool reader; only immutable spool files deliver rows.
pub struct HybridSignalFeed {
    unix: UnixSignalFeed,
    spool: SpoolSignalFeed,
}

impl HybridSignalFeed {
    pub fn new(directory: impl Into<PathBuf>) -> std::io::Result<Self> {
        let directory = directory.into();
        let unix = UnixSignalFeed::bind(directory.join("stream.sock"))?;
        Ok(Self {
            unix,
            spool: SpoolSignalFeed::new(directory),
        })
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.spool = self.spool.with_poll_interval(poll);
        self
    }
}

impl SignalFeed for HybridSignalFeed {
    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        self.spool.set_gap_requests(gaps, blocked_destinations)
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        self.spool.acknowledge_last()
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        self.spool.defer_last(observation)
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        loop {
            tokio::select! {
                biased;
                observation = self.spool.next_observation() => return observation,
                frame = self.unix.next_doorbell() => {
                    if let Err(error) = frame {
                        tracing::warn!(%error, "signal doorbell rejected; durable spool remains authoritative");
                    }
                    self.spool.wake();
                }
            }
        }
    }
}

/// Unified signal feed selection for production runners.
pub enum EngineSignalFeed {
    Hybrid(Box<HybridSignalFeed>),
    Spool(Box<SpoolSignalFeed>),
}

impl EngineSignalFeed {
    pub fn for_directory(directory: impl Into<PathBuf>) -> Self {
        let dir = directory.into();
        match HybridSignalFeed::new(&dir) {
            Ok(hybrid) => Self::Hybrid(Box::new(hybrid)),
            Err(err) => {
                tracing::warn!(error = %err, path = %dir.display(), "falling back to pure file spool signal feed");
                Self::Spool(Box::new(SpoolSignalFeed::new(dir)))
            }
        }
    }
}

impl SignalFeed for EngineSignalFeed {
    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        match self {
            Self::Hybrid(feed) => feed.set_gap_requests(gaps, blocked_destinations),
            Self::Spool(feed) => feed.set_gap_requests(gaps, blocked_destinations),
        }
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        match self {
            Self::Hybrid(feed) => feed.acknowledge_last(),
            Self::Spool(feed) => feed.acknowledge_last(),
        }
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        match self {
            Self::Hybrid(feed) => feed.defer_last(observation),
            Self::Spool(feed) => feed.defer_last(observation),
        }
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        match self {
            Self::Hybrid(feed) => feed.next_observation().await,
            Self::Spool(feed) => feed.next_observation().await,
        }
    }
}

#[cfg(test)]
fn publish_test_row(path: &Path, raw: &[u8]) {
    let staged = path.with_extension("publishing");
    std::fs::write(&staged, raw).unwrap();
    std::fs::rename(staged, path).unwrap();
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
        assert!(matches!(
            sender.try_send(observation()),
            Err(SignalSendError::Full(_))
        ));
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

    #[tokio::test]
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

    #[tokio::test]
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
    #[tokio::test]
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
        let waiting =
            tokio::time::timeout(Duration::from_millis(100), feed.next_observation()).await;
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
}

#[cfg(test)]
mod delivery_tests {
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
}
