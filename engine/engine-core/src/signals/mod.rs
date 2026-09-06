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

#[cfg(test)]
use engine_types::SignalSubscriptionState;
use engine_types::{
    SignalError, SignalFeed, SignalGapRequest, SignalObservation, StrategyId, Subscription,
    WalRecord, MAX_SIGNAL_OBSERVATION_BYTES, MAX_SIGNAL_SUBSCRIPTIONS,
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
pub(crate) const SYMBOL_BYTES_MAX: usize = 128;

pub fn ordered_gap_requests(
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

pub fn ordered_blocked_destinations(
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

pub fn signal_eligible(
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

pub fn signal_requested(gaps: &[SignalGapRequest], observation: &SignalObservation) -> bool {
    requested_sequence(gaps, &observation.source) == Some(observation.sequence)
}

pub(crate) fn signal_available(observation: &SignalObservation, wall_ms: i64) -> bool {
    observation.available_wall_ts_ms <= wall_ms
}

fn availability_wait(available_ms: i64, wall_ms: i64) -> Duration {
    // Monotonic timers cannot observe wall-clock corrections. Recheck long
    // waits without changing the timestamp required for delivery.
    Duration::from_millis(available_ms.saturating_sub(wall_ms).max(0) as u64)
        .min(Duration::from_secs(1))
}

pub(crate) fn retained_bytes(observation: &SignalObservation) -> usize {
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
    available_wall_ts_ms: i64,
}

impl DeliveryIdentity {
    fn of(observation: &SignalObservation) -> Self {
        Self {
            destination: observation.destination,
            source: observation.source.clone(),
            sequence: observation.sequence,
            content_sha256: observation.content_sha256.clone(),
            available_wall_ts_ms: observation.available_wall_ts_ms,
        }
    }

    fn matches(&self, observation: &SignalObservation) -> bool {
        self.destination == observation.destination
            && self.source == observation.source
            && self.sequence == observation.sequence
            && self.content_sha256 == observation.content_sha256
            && self.available_wall_ts_ms == observation.available_wall_ts_ms
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
    let mut sources = BTreeMap::<String, Vec<Subscription>>::new();
    let mut producers = BTreeMap::<String, engine_types::SignalProducerLifecycle>::new();
    for record in replayed {
        match record {
            WalRecord::SignalObservation { observation, .. } => {
                let rows = sources.entry(observation.source.clone()).or_default();
                for subscription in &observation.subscriptions {
                    if !rows.contains(subscription) {
                        rows.push(subscription.clone());
                    }
                }
            }
            WalRecord::SignalProducerLifecycle { state, .. } => {
                sources.retain(|source, _| {
                    engine_types::ManagedSignalSource::parse(source)
                        .is_none_or(|identity| identity.producer != state.producer)
                        && engine_types::legacy_signal_lane(&state.producer, source).is_none()
                });
                producers.insert(state.producer.clone(), state.clone());
            }
            WalRecord::SegmentBase {
                signal_subscriptions,
                signal_producers,
                ..
            } => {
                sources = signal_subscriptions
                    .iter()
                    .map(|row| (row.source.clone(), row.subscriptions.clone()))
                    .collect();
                producers = signal_producers
                    .iter()
                    .map(|state| (state.producer.clone(), state.clone()))
                    .collect();
            }
            _ => {}
        }
    }
    let mut subscriptions = Vec::new();
    for rows in sources.values().chain(
        producers
            .values()
            .flat_map(|state| state.routes.iter().map(|route| &route.subscriptions)),
    ) {
        for row in rows {
            if !subscriptions.contains(row) {
                subscriptions.push(row.clone());
            }
        }
    }
    subscriptions
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

mod channel;
mod readiness;
mod spool;
mod unix;

pub use channel::{signal_channel, SignalReceiver, SignalSendError, SignalSender};
#[cfg(test)]
use spool::SpoolScanner;
pub use spool::SpoolSignalFeed;
pub use unix::{HybridSignalFeed, UnixSignalFeed};

#[cfg(test)]
fn publish_test_row(path: &Path, raw: &[u8]) {
    let staged = path.with_extension("publishing");
    std::fs::write(&staged, raw).unwrap();
    std::fs::rename(staged, path).unwrap();
}

#[cfg(test)]
mod delivery_tests;
#[cfg(test)]
mod tests;
