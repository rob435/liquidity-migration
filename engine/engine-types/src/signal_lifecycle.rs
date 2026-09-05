use crate::{SignalSourceFrontier, StrategyId, Subscription};
use serde::{Deserialize, Serialize};

pub const SIGNAL_LIFECYCLE_SCHEMA_VERSION: u16 = 2;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalGenerationState {
    pub epoch: u64,
    pub generation: String,
    pub sources: Vec<SignalSourceFrontier>,
    pub sealed: bool,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalLegacyGeneration {
    pub source: String,
    pub destination: StrategyId,
    pub published_through: Option<u64>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalProducerLifecycle {
    pub producer: String,
    pub retired_through: u64,
    pub active: Option<SignalGenerationState>,
    pub legacy: Vec<SignalLegacyGeneration>,
    pub routes: Vec<SignalProducerRoute>,
    pub unresolved_tail: bool,
    pub previous_seal: Vec<SignalSourceFrontier>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalProducerRoute {
    pub destination: StrategyId,
    pub subscriptions: Vec<Subscription>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalLifecycleRequest {
    pub schema_version: u16,
    pub boot_nonce: String,
    pub producers: Vec<SignalProducerLifecycle>,
    pub legacy_sources: Vec<SignalSourceFrontier>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalProducerReport {
    pub producer: String,
    pub epoch: Option<u64>,
    pub generation: String,
    pub sealed: bool,
    pub sources: Vec<SignalSourceFrontier>,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalLifecycleResponse {
    pub schema_version: u16,
    pub boot_nonce: String,
    pub producer: SignalProducerReport,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SignalLane {
    Long,
    Carry,
}

impl SignalLane {
    pub fn name(self) -> &'static str {
        match self {
            Self::Long => "long",
            Self::Carry => "carry",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        match value {
            "long" => Some(Self::Long),
            "carry" => Some(Self::Carry),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ManagedSignalSource<'a> {
    pub producer: &'a str,
    pub epoch: u64,
    pub generation: &'a str,
    pub lane: SignalLane,
}

impl<'a> ManagedSignalSource<'a> {
    pub fn parse(source: &'a str) -> Option<Self> {
        let (identity, lane) = source.rsplit_once('.')?;
        let lane = SignalLane::parse(lane)?;
        let (identity, generation) = identity.rsplit_once(".g")?;
        let (producer, epoch) = identity.rsplit_once(".e")?;
        if producer.is_empty()
            || source.len() > 256
            || epoch.len() != 20
            || !epoch.bytes().all(|byte| byte.is_ascii_digit())
            || !valid_signal_generation(generation)
        {
            return None;
        }
        let epoch = epoch.parse().ok()?;
        (epoch != 0).then_some(Self {
            producer,
            epoch,
            generation,
            lane,
        })
    }

    pub fn encode(self) -> Result<String, &'static str> {
        let source = format!(
            "{}.e{:020}.g{}.{}",
            self.producer,
            self.epoch,
            self.generation,
            self.lane.name()
        );
        (ManagedSignalSource::parse(&source).is_some())
            .then_some(source)
            .ok_or("invalid managed signal source identity")
    }
}

pub fn valid_signal_generation(generation: &str) -> bool {
    generation.len() == 32
        && generation
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

pub fn legacy_signal_lane(producer: &str, source: &str) -> Option<SignalLane> {
    let suffix = source.strip_prefix(producer)?.strip_prefix('.')?;
    if let Some((generation, lane)) = suffix.rsplit_once('.') {
        let generation = generation.strip_prefix('g')?;
        valid_signal_generation(generation)
            .then(|| SignalLane::parse(lane))
            .flatten()
    } else {
        SignalLane::parse(suffix)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn managed_source_epoch_is_canonical_and_separate_from_legacy_identity() {
        let generation = "a".repeat(32);
        let source = ManagedSignalSource {
            producer: "native.feed",
            epoch: 37,
            generation: &generation,
            lane: SignalLane::Carry,
        }
        .encode()
        .unwrap();
        let parsed = ManagedSignalSource::parse(&source).unwrap();
        assert_eq!(parsed.epoch, 37);
        assert_eq!(parsed.producer, "native.feed");
        assert!(legacy_signal_lane("native.feed", &source).is_none());
        assert!(
            ManagedSignalSource::parse(&source.replace("00000000000000000037", "37")).is_none()
        );
        assert!(ManagedSignalSource::parse(
            &source.replace("00000000000000000037", "00000000000000000000")
        )
        .is_none());
        assert_eq!(
            legacy_signal_lane("native.feed", "native.feed.long"),
            Some(SignalLane::Long)
        );
        assert_eq!(
            legacy_signal_lane("native.feed", &format!("native.feed.g{generation}.carry")),
            Some(SignalLane::Carry)
        );
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SignalAdmissionSuspensionReason {
    SubscriptionBudget { subscriptions: Vec<Subscription> },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SignalAdmissionSuspension {
    pub destination: StrategyId,
    pub reason: SignalAdmissionSuspensionReason,
}
