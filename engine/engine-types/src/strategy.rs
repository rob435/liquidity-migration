use crate::ids::{StrategyId, SymbolId, TimerId};
use crate::market::{Depth, MarketEvent, Quote, Subscription, Ticker, TradeFlow};
use crate::orders::{
    Action, AmendSpec, InstrumentRule, Intent, OrderFacts, OrderUpdate, RestingOrder,
};
use crate::risk::PositionView;
use serde::{Deserialize, Serialize};

pub const SIGNAL_OBSERVATION_SCHEMA_VERSION: u16 = 1;
pub const STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION: u16 = 1;
pub const MAX_STRATEGY_STATE_BYTES: usize = 4 * 1024 * 1024;
pub const MAX_STRATEGY_EVENT_BYTES: usize = 1024 * 1024;
pub const MAX_SIGNAL_OBSERVATION_BYTES: usize = 16 * 1024 * 1024;
pub const MAX_SIGNAL_SUBSCRIPTIONS: usize = 512;
pub const MAX_DURABLE_SIGNAL_SUBSCRIPTIONS: usize = 4_096;

/// Durable state owned by one strategy decision.
///
/// The engine stores the bytes and knows nothing about their meaning. The
/// strategy checks both the schema and the decision fingerprint before using
/// them, so a new config cannot inherit a one-shot decision made by an older
/// one. `payload` is deterministic strategy-owned encoding.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategyCheckpoint {
    pub schema_version: u16,
    pub decision_fingerprint: String,
    pub payload: Vec<u8>,
}

/// The exact durable state contract accepted by one strategy build.
///
/// The stopped-runtime importer uses this before it appends translated bytes,
/// and the reducer checks the same pair again when it restores them.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyCheckpointIdentity {
    pub schema_version: u16,
    pub decision_fingerprint: String,
}

/// Audit identity for a checkpoint translated from a retired runtime.
///
/// This says which source bytes were translated; it does not change the
/// reducer's checkpoint identity. Live checkpoints have no provenance.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct CheckpointProvenance {
    pub source_format: String,
    pub source_sha256: String,
    /// Hash of the translated checkpoint plus every pending event in this
    /// import. Empty only on records written before bundle imports existed.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub bundle_sha256: String,
    /// The final marker written only after every bundled event is durable.
    #[serde(default)]
    pub import_complete: bool,
}

/// One pending durable handoff recovered from a retired runtime.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TranslatedStrategyEvent {
    pub source_strategy: String,
    pub destination_strategy: String,
    pub kind: String,
    pub event_id: String,
    pub payload: Vec<u8>,
}

/// Canonical state and pending handoffs produced by a strategy-owned legacy
/// decoder. The importer resolves names to the WAL's exact ids.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct TranslatedStrategyState {
    pub checkpoint_payload: Vec<u8>,
    pub pending_events: Vec<TranslatedStrategyEvent>,
}

/// One named, exact legacy file supplied to a stopped-runtime import.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyImportSource {
    pub name: String,
    pub bytes: Vec<u8>,
}

/// Authenticated account identity available only during a stopped import.
///
/// The values come from the venue after the importer owns both the WAL and
/// account leases. They let a legacy codec bind retired state to the exact
/// account without putting an account id in committed strategy config.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StrategyImportContext {
    pub venue: String,
    pub realm: String,
    pub account_user_id: String,
}

/// One immutable message from one strategy reducer to another.
///
/// The payload is canonical bytes owned by the source strategy. The engine
/// owns routing, durability, deduplication, and visibility; it never decodes
/// the payload.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategyEvent {
    pub source: StrategyId,
    pub destination: StrategyId,
    pub kind: String,
    pub event_id: String,
    pub payload: Vec<u8>,
}

/// A normalized, credential-free signal delivered to one native strategy.
///
/// `sequence` is contiguous within `source`. The spool may replay old files;
/// the engine's durable per-source cursor removes duplicates. `content_sha256`
/// covers [`SignalObservation::canonical_envelope_bytes`] exactly, including
/// the requested subscriptions and raw payload but excluding the hash itself.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignalObservation {
    pub schema_version: u16,
    pub decision_fingerprint: String,
    pub destination: StrategyId,
    pub source: String,
    pub sequence: u64,
    pub observation_id: String,
    pub kind: String,
    pub observed_wall_ts_ms: i64,
    pub available_wall_ts_ms: i64,
    pub subscriptions: Vec<Subscription>,
    #[serde(with = "payload_wire")]
    pub payload: Vec<u8>,
    pub content_sha256: String,
}

/// The payload on the wire: a JSON string when the bytes are UTF-8 (the
/// worker's payloads are JSON text), else an array of byte values. Readers
/// take both; rows and WAL records written before 2026-09-03 carry the
/// array. `content_sha256` covers the bytes, not their encoding.
mod payload_wire {
    use serde::de::{Error, SeqAccess, Visitor};
    use serde::{Deserializer, Serialize, Serializer};
    use std::fmt;

    pub fn serialize<S: Serializer>(bytes: &[u8], serializer: S) -> Result<S::Ok, S::Error> {
        match std::str::from_utf8(bytes) {
            Ok(text) => serializer.serialize_str(text),
            Err(_) => bytes.serialize(serializer),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Vec<u8>, D::Error> {
        struct PayloadVisitor;

        impl<'de> Visitor<'de> for PayloadVisitor {
            type Value = Vec<u8>;

            fn expecting(&self, formatter: &mut fmt::Formatter) -> fmt::Result {
                formatter.write_str("a payload string or an array of byte values")
            }

            fn visit_str<E: Error>(self, value: &str) -> Result<Vec<u8>, E> {
                Ok(value.as_bytes().to_vec())
            }

            fn visit_string<E: Error>(self, value: String) -> Result<Vec<u8>, E> {
                Ok(value.into_bytes())
            }

            fn visit_bytes<E: Error>(self, value: &[u8]) -> Result<Vec<u8>, E> {
                Ok(value.to_vec())
            }

            fn visit_byte_buf<E: Error>(self, value: Vec<u8>) -> Result<Vec<u8>, E> {
                Ok(value)
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Vec<u8>, A::Error> {
                let mut bytes = Vec::with_capacity(seq.size_hint().unwrap_or(0));
                while let Some(byte) = seq.next_element::<u8>()? {
                    bytes.push(byte);
                }
                Ok(bytes)
            }
        }

        deserializer.deserialize_any(PayloadVisitor)
    }
}

impl SignalObservation {
    /// Stable bytes hashed by the signal worker and verified by the engine.
    ///
    /// Strings and byte arrays are length-prefixed little-endian; enum values
    /// have fixed numeric tags. This is deliberately independent of JSON map
    /// ordering and serializer versions.
    pub fn canonical_envelope_bytes(&self) -> Vec<u8> {
        fn bytes(out: &mut Vec<u8>, value: &[u8]) {
            out.extend_from_slice(&(value.len() as u64).to_le_bytes());
            out.extend_from_slice(value);
        }
        fn text(out: &mut Vec<u8>, value: &str) {
            bytes(out, value.as_bytes());
        }
        fn feed_tag(feed: crate::market::Feed) -> u8 {
            match feed {
                crate::market::Feed::Quote => 1,
                crate::market::Feed::Depth => 2,
                crate::market::Feed::Trades => 3,
                crate::market::Feed::Ticker => 4,
            }
        }

        let mut out = Vec::with_capacity(self.payload.len().saturating_add(256));
        out.extend_from_slice(b"engine.signal-observation.v1\0");
        out.extend_from_slice(&self.schema_version.to_le_bytes());
        text(&mut out, &self.decision_fingerprint);
        out.extend_from_slice(&self.destination.0.to_le_bytes());
        text(&mut out, &self.source);
        out.extend_from_slice(&self.sequence.to_le_bytes());
        text(&mut out, &self.observation_id);
        text(&mut out, &self.kind);
        out.extend_from_slice(&self.observed_wall_ts_ms.to_le_bytes());
        out.extend_from_slice(&self.available_wall_ts_ms.to_le_bytes());
        out.extend_from_slice(&(self.subscriptions.len() as u64).to_le_bytes());
        for subscription in &self.subscriptions {
            text(&mut out, &subscription.symbol);
            out.push(feed_tag(subscription.feed));
        }
        bytes(&mut out, &self.payload);
        out
    }
}

#[derive(Debug, thiserror::Error)]
pub enum SignalError {
    #[error("signal source closed")]
    Closed,
    #[error("signal source: {0}")]
    Source(String),
}

/// A lossless source of normalized observations. A source blocks its own task;
/// the core polls this future beside market, private-order, timer, and venue
/// work and never waits synchronously for the signal worker.
#[allow(async_fn_in_trait)]
pub trait SignalFeed {
    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError>;
}

/// One typed command carried by the live runtime control spool.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum RuntimeControlCommand {
    SetEntriesEnabled {
        entries_enabled: bool,
    },
    /// Ask the addressed reducer to drive all of its attributed exposure to
    /// zero. The command remains pending across restart until that strategy
    /// durably acknowledges it.
    FlattenDirectional,
}

/// One idempotent operator command addressed to a configured sleeve.
///
/// This is engine control state, not reducer config or checkpoint state. The
/// engine owns durability and replay. The hash covers
/// [`RuntimeControlRequest::canonical_envelope_bytes`] exactly.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct RuntimeControlRequest {
    pub schema_version: u16,
    pub strategy: StrategyId,
    pub strategy_name: String,
    pub request_id: String,
    pub command: RuntimeControlCommand,
    pub content_sha256: String,
}

impl RuntimeControlRequest {
    pub fn canonical_envelope_bytes(&self) -> Vec<u8> {
        fn text(out: &mut Vec<u8>, value: &str) {
            out.extend_from_slice(&(value.len() as u64).to_le_bytes());
            out.extend_from_slice(value.as_bytes());
        }

        let mut out = b"engine.runtime-control.v1\0".to_vec();
        out.extend_from_slice(&self.schema_version.to_le_bytes());
        out.extend_from_slice(&self.strategy.0.to_le_bytes());
        text(&mut out, &self.strategy_name);
        text(&mut out, &self.request_id);
        match self.command {
            RuntimeControlCommand::SetEntriesEnabled { entries_enabled } => {
                out.push(1);
                out.push(u8::from(entries_enabled));
            }
            RuntimeControlCommand::FlattenDirectional => out.push(2),
        }
        out
    }
}

#[derive(Debug, thiserror::Error)]
pub enum RuntimeControlError {
    #[error("runtime control source closed")]
    Closed,
    #[error("runtime control source: {0}")]
    Source(String),
}

/// A lossless source of runtime control requests. The core appends and
/// barriers each accepted request before polling this source again.
#[allow(async_fn_in_trait)]
pub trait RuntimeControlFeed {
    async fn next_request(&mut self) -> Result<RuntimeControlRequest, RuntimeControlError>;

    /// Retire the last returned request as refused instead of consumed. The
    /// source must never return that request again; a durable source keeps
    /// the refused bytes inspectable rather than deleting them.
    async fn reject_last(&mut self) -> Result<(), RuntimeControlError> {
        Ok(())
    }
}

/// Account-wide capital facts exposed read-only to one strategy callback.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct StrategyAccountSummary {
    pub equity_usdt: f64,
    pub available_margin_usdt: f64,
    pub observed_ns: u64,
}

/// One strategy-attributed position joined to the venue's latest row and the
/// engine's send-ahead cover. The signed attributed quantity moves on fills;
/// the optional venue row remains the account fact.
#[derive(Clone, Debug, PartialEq)]
pub struct StrategyPositionFacts {
    pub symbol: SymbolId,
    pub attributed_signed_qty: f64,
    pub venue: Option<PositionView>,
    pub in_flight_signed_qty: f64,
}

/// Everything a strategy can be woken by.
// The inline L50 market variant keeps strategy wakes allocation-free.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq)]
pub enum EngineEvent {
    /// Core restore is complete: checkpoints, attribution, account state,
    /// pending messages, and runtime controls are all visible.
    Boot,
    Market(MarketEvent),
    Timer {
        id: TimerId,
        now_ns: u64,
    },
    Order(OrderUpdate),
    /// One normalized observation from the credential-free signal spool.
    /// It is already in the WAL and all requested symbols are admitted before
    /// this wake. It is replayed after restart until the strategy consumes it.
    Signal(SignalObservation),
    /// One durable cross-sleeve event, delivered only to its destination.
    /// It is replayed after restart until that destination consumes it.
    StrategyEvent(StrategyEvent),
    /// An intent this strategy placed died inside the engine before it
    /// became an order: the risk kernel refused it, it could not be
    /// quantized, its leverage could not be set, or the engine is latched
    /// against opening. No order exists and no fill will ever come of it.
    /// The engine's own in-flight accounting (read back through
    /// [`StrategyCtx::in_flight`]) is already settled before this arrives:
    /// a refused entry was never booked, and a refused exit has dropped
    /// every cover on the symbol. (An order the VENUE ends arrives as
    /// [`EngineEvent::Order`] news instead, with its id.)
    IntentRefused {
        symbol: SymbolId,
        reduce_only: bool,
        reason: String,
    },
    /// The engine durably changed this sleeve's runtime entry permission.
    /// Reducers use the wake to re-run their ordinary plan; exits and signal
    /// acknowledgement continue regardless of the value.
    EntryPermission {
        request_id: String,
        entries_enabled: bool,
    },
    /// A durable operator command to reduce this sleeve's attributed
    /// exposure to zero. It is replayed until the strategy acknowledges it.
    FlattenDirectional {
        request_id: String,
    },
}

/// The strategy's window into the engine. Read market state, the account
/// reading, and your own resting orders; emit actions, arm timers. No venue
/// access, no log access, no clock other than these.
pub trait StrategyCtx {
    fn quote(&self, symbol: SymbolId) -> &Quote;
    fn depth(&self, symbol: SymbolId) -> &Depth;
    fn trade_flow(&self, symbol: SymbolId) -> &TradeFlow;
    fn ticker(&self, symbol: SymbolId) -> &Ticker;
    fn symbol_id(&self, name: &str) -> Option<SymbolId>;
    fn symbol_name(&self, symbol: SymbolId) -> Option<&str> {
        let _ = symbol;
        None
    }
    fn now_ns(&self) -> u64;
    /// Config may only narrow this gate. A durable runtime disable wins over
    /// a config default of true; a runtime enable never overrides false in
    /// the strategy's committed config.
    fn entries_enabled(&self, config_default: bool) -> bool {
        config_default
    }
    /// Account equity and available margin from the exact view the risk kernel
    /// judges. The strategy cannot mutate or replace the view.
    fn account_summary(&self) -> StrategyAccountSummary;
    /// What the engine's latest account reading says is held in this symbol,
    /// or `None` when it is flat or the reading does not mention it. This is
    /// the venue's own picture, refreshed on the engine's schedule — not a
    /// running total of the strategy's fills.
    fn position(&self, symbol: SymbolId) -> Option<PositionView>;
    /// Whether a strategy other than this one is holding this symbol.
    ///
    /// [`StrategyCtx::position`] above is the account's holding and says
    /// nothing about whose it is, so on an account running two strategies it
    /// is not enough to act on: the second one would size against exposure it
    /// never opened, and exit it. This answers the question that matters
    /// before touching a name at all.
    ///
    /// It is asked per symbol rather than per quantity because the venue's
    /// stop is attached to the *position*, and there is one position per
    /// symbol. Two strategies holding one symbol would have one stop between
    /// them, set by whichever placed the last opening order — so sharing is
    /// not something to be sized around. Whoever got there first keeps the
    /// name until it is flat.
    ///
    /// Exposure the engine's own log has no fills for belongs to nobody here:
    /// a hand trade reads as `false`. Boot's reconciliation is what notices
    /// that and stops the engine opening on top of it.
    fn foreign_position(&self, symbol: SymbolId) -> bool;
    /// This strategy's own signed position in a symbol, summed from the fills
    /// of the orders it placed. Positive is long.
    ///
    /// [`StrategyCtx::position`] above is the venue's account reading, and it
    /// is the wrong number for a strategy to hold inventory against, twice
    /// over. It is refreshed on the engine's own schedule — seconds — so a
    /// strategy that has just filled goes on acting as though it had not,
    /// which for a maker means quoting the same side again and again. And on
    /// an account running two sleeves it is the sum of both, so one sleeve
    /// would count the other's position as its own.
    ///
    /// This is the strategy's own trading and nobody else's. It moves the
    /// instant a fill arrives, before the strategy is woken. What it is *not*
    /// is a second opinion about what the account holds: hand-placed exposure
    /// belongs to nobody here and reads as zero, and where this and the
    /// account reading disagree the account reading is the fact.
    fn my_position(&self, symbol: SymbolId) -> f64;
    /// Names this strategy's fills still claim as open, appended to `out`.
    ///
    /// This is the restart-safe complement to [`StrategyCtx::my_position`]:
    /// a strategy can ask about one known name above, while this lets it find
    /// names recovered from the log that are absent from its current decision
    /// and boot subscription seed. The venue reading remains the fact about
    /// whether any returned name is actually held.
    fn my_position_names<'a>(&'a self, out: &mut Vec<&'a str>) {
        let _ = out;
    }
    /// Signed quantity this strategy has sent that the engine's account
    /// reading has not yet absorbed. Positive is long.
    ///
    /// The gap it bridges: a fully filled order leaves
    /// [`StrategyCtx::resting`] the instant the fill lands, while the account
    /// reading refreshes on the engine's own schedule — seconds. In that
    /// window the position exists at the venue and shows nowhere else a
    /// strategy can look, and a strategy deciding from "target minus
    /// position" sends the same target twice. The reading plus this closes
    /// the window.
    ///
    /// The engine books the cover itself when it hands an order to the
    /// venue, at the quantized size that actually went, and releases it as
    /// the reading absorbs it (or as a reject or cancel ends the unfilled
    /// part). It is not a second position record: where this and the reading
    /// disagree about what is held, the reading is the fact, and a refused
    /// exit clears every cover on its symbol for exactly that reason. Zero
    /// for a context double that keeps no cover book.
    fn in_flight(&self, symbol: SymbolId) -> f64 {
        let _ = symbol;
        0.0
    }
    /// This strategy's position, venue row, and send-ahead cover joined in one
    /// typed fact. `None` means it has no attributed quantity or cover there.
    fn my_position_facts(&self, symbol: SymbolId) -> Option<StrategyPositionFacts> {
        let _ = symbol;
        None
    }
    /// Every non-flat attributed position or cover owned by this strategy.
    fn my_positions(&self, out: &mut Vec<StrategyPositionFacts>) {
        let _ = out;
    }
    /// Tick, step and minimums, as the venue stated them at boot. `None`
    /// means nothing can be quantized for that symbol, so nothing can be
    /// sent for it either.
    fn instrument(&self, symbol: SymbolId) -> Option<InstrumentRule>;
    /// Wall-clock milliseconds since the unix epoch, comparable with venue
    /// timestamps. Every other clock in here is monotonic on purpose; this
    /// one exists because durable signals and native decisions carry unix
    /// validity windows. Never measure latency with it — it can be stepped,
    /// and two readings can come back in the wrong order.
    fn wall_ms(&self) -> i64;
    /// Hand an action to the engine. The risk kernel still gates every send.
    fn emit(&mut self, action: Action);
    /// One-shot timer; fires as [`EngineEvent::Timer`] after `after_ns`.
    fn arm_timer(&mut self, id: TimerId, after_ns: u64);
    /// This strategy's own orders that the log says are still working,
    /// appended to `out`. The engine mints order ids, so this is how a
    /// quoting strategy finds the order it wants to pull or move. Pass a
    /// buffer you keep between wakes and the read allocates nothing.
    fn resting<'a>(&'a self, out: &mut Vec<RestingOrder<'a>>);

    /// What the log says about one of this strategy's own orders: its
    /// symbol, its size, and how much of it filled. `None` for an id this
    /// engine never minted, an order another strategy placed, or a context
    /// double that keeps no ledger. The terminal order news (`Reject`,
    /// `Cancelled`) carries only the id, and a strategy keeping any record
    /// of its own by symbol needs the way back.
    fn order_facts(&self, client_order_id: &str) -> Option<OrderFacts> {
        let _ = client_order_id;
        None
    }

    /// The newest durable state this strategy wrote for one symbol.
    fn strategy_checkpoint(&self, symbol: SymbolId) -> Option<&StrategyCheckpoint> {
        let _ = symbol;
        None
    }

    /// The newest durable state this strategy wrote for its whole sleeve.
    fn strategy_global_checkpoint(&self) -> Option<&StrategyCheckpoint> {
        None
    }

    /// Resolve a configured sleeve name to its stable WAL id.
    fn strategy_id(&self, name: &str) -> Option<StrategyId> {
        let _ = name;
        None
    }

    /// Durable, not-yet-consumed cross-sleeve events visible to this strategy.
    /// Only events it sourced or events addressed to it are appended.
    fn strategy_events(&self, out: &mut Vec<StrategyEvent>) {
        let _ = out;
    }

    fn place(&mut self, intent: Intent) {
        self.emit(Action::Place(intent));
    }

    fn cancel(&mut self, symbol: SymbolId, client_order_id: &str) {
        self.emit(Action::Cancel {
            symbol,
            client_order_id: client_order_id.to_string(),
        });
    }

    fn amend(&mut self, symbol: SymbolId, client_order_id: &str, spec: AmendSpec) {
        self.emit(Action::Amend {
            symbol,
            client_order_id: client_order_id.to_string(),
            spec,
        });
    }
}

/// A pluggable strategy. Built by the registry from a name and a TOML
/// config block emitted by the research system. Strategies are synchronous
/// and single-threaded by construction: `on_event` runs on the engine loop
/// and must return quickly.
///
/// A strategy overrides only the per-event hooks it acts on — `on_market`,
/// `on_timer`, `on_order`, `on_intent_refused` — and the ones
/// it ignores do nothing by default. The one exhaustive match over
/// [`EngineEvent`] lives in the provided `on_event` below and nowhere else,
/// so adding a new kind of event means: add the variant, add a hook with a
/// do-nothing default body, route it in that one match. No strategy needs an
/// edit.
pub trait Strategy {
    fn name(&self) -> &str;
    /// Market data wanted, collected once at boot.
    fn subscriptions(&self) -> Vec<Subscription>;

    /// The whole-sleeve checkpoint contract this exact strategy build accepts.
    /// Strategies without durable whole-sleeve state leave this absent.
    fn checkpoint_identity(&self) -> Option<StrategyCheckpointIdentity> {
        None
    }

    /// Canonical empty whole-sleeve state for a genuinely new WAL. The core
    /// writes and barriers this before it reads the venue. A strategy without
    /// durable whole-sleeve state leaves it absent.
    fn initial_checkpoint(&self) -> Option<StrategyCheckpoint> {
        None
    }

    /// Validate durable bytes against this exact reducer and config. Boot and
    /// stopped import call this before any venue mutation can depend on them.
    fn validate_checkpoint(&self, checkpoint: &StrategyCheckpoint) -> Result<(), String> {
        let _ = checkpoint;
        Ok(())
    }

    /// Decode one retired runtime's state and return this reducer's canonical
    /// payload bytes. The importer never accepts caller-supplied canonical
    /// bytes: the selected strategy owns the legacy format and validation.
    fn translate_checkpoint(
        &self,
        context: &StrategyImportContext,
        source_format: &str,
        sources: &[StrategyImportSource],
    ) -> Result<TranslatedStrategyState, String> {
        let _ = (context, source_format, sources);
        Err("this strategy has no legacy state translator".to_string())
    }

    /// Whether this strategy needs the ordered external signal spool to make
    /// decisions. A required source omitted from config is a boot error.
    fn requires_signal_feed(&self) -> bool {
        false
    }

    /// The committed config's entry toggle, for status reporting. Runtime
    /// control may narrow but never widen this value.
    fn configured_entries_enabled(&self) -> bool {
        true
    }

    /// Every wake, routed. The engine calls this and only this; strategies
    /// override the hooks below instead, and only under a reason as good as
    /// this trait doc would they override the routing itself.
    fn on_event(&mut self, event: &EngineEvent, ctx: &mut dyn StrategyCtx) {
        match event {
            EngineEvent::Boot => self.on_boot(ctx),
            EngineEvent::Market(market) => self.on_market(market, ctx),
            EngineEvent::Timer { id, now_ns } => self.on_timer(*id, *now_ns, ctx),
            EngineEvent::Order(update) => self.on_order(update, ctx),
            EngineEvent::Signal(observation) => self.on_signal(observation, ctx),
            EngineEvent::StrategyEvent(event) => self.on_strategy_event(event, ctx),
            EngineEvent::IntentRefused {
                symbol,
                reduce_only,
                reason,
            } => self.on_intent_refused(*symbol, *reduce_only, reason, ctx),
            EngineEvent::EntryPermission {
                request_id,
                entries_enabled,
            } => self.on_entry_permission(request_id, *entries_enabled, ctx),
            EngineEvent::FlattenDirectional { request_id } => {
                self.on_flatten_directional(request_id, ctx)
            }
        }
    }

    /// The single post-restore wake. Stateful strategies restore checkpoints,
    /// arm deadlines, and re-derive outstanding work here.
    fn on_boot(&mut self, ctx: &mut dyn StrategyCtx) {
        let _ = ctx;
    }

    /// A market message: a quote, a ticker, or a feed reset.
    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let _ = (event, ctx);
    }

    /// A timer this strategy armed has come due.
    fn on_timer(&mut self, id: TimerId, now_ns: u64, ctx: &mut dyn StrategyCtx) {
        let _ = (id, now_ns, ctx);
    }

    /// News about one of this strategy's own orders.
    fn on_order(&mut self, update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        let _ = (update, ctx);
    }

    /// A durable normalized observation from the credential-free worker.
    fn on_signal(&mut self, observation: &SignalObservation, ctx: &mut dyn StrategyCtx) {
        let _ = (observation, ctx);
    }

    /// A durable event emitted by another strategy in this engine.
    fn on_strategy_event(&mut self, event: &StrategyEvent, ctx: &mut dyn StrategyCtx) {
        let _ = (event, ctx);
    }

    /// An intent this strategy placed died inside the engine before it
    /// became an order, with the reason the engine logged. See
    /// [`EngineEvent::IntentRefused`].
    fn on_intent_refused(
        &mut self,
        symbol: SymbolId,
        reduce_only: bool,
        reason: &str,
        ctx: &mut dyn StrategyCtx,
    ) {
        let _ = (symbol, reduce_only, reason, ctx);
    }

    /// A durable runtime entry gate changed. Strategies that can derive no
    /// action from this wake leave it alone; native directional reducers use
    /// it to recompute immediately from the same account and decision state.
    fn on_entry_permission(
        &mut self,
        request_id: &str,
        entries_enabled: bool,
        ctx: &mut dyn StrategyCtx,
    ) {
        let _ = (request_id, entries_enabled, ctx);
    }

    fn on_flatten_directional(&mut self, request_id: &str, ctx: &mut dyn StrategyCtx) {
        let _ = (request_id, ctx);
    }

    /// Why this strategy is not opening each name it is asking for right
    /// now, as (symbol name, reason) pairs, for the heartbeat.
    ///
    /// Without this an entry the kernel refused, or a size below the entry
    /// floor, is invisible to operators. Refusals the kernel delivered and
    /// skips the planner took both belong here. Empty by default.
    fn entry_blockers(&self) -> Vec<(String, String)> {
        Vec::new()
    }

    /// The current strategy-level fault, when this sleeve cannot reduce its
    /// inputs or publish the resulting work. This is separate from ordinary
    /// per-symbol entry blockers: a skipped name is expected trading state;
    /// a broken reducer is strategy health.
    fn health_error(&self) -> Option<&str> {
        None
    }
}

#[cfg(test)]
mod payload_wire_tests {
    use super::*;

    fn observation(payload: &[u8]) -> SignalObservation {
        SignalObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "carry-v1".into(),
            destination: StrategyId(1),
            source: "worker".into(),
            sequence: 1,
            observation_id: "row-1".into(),
            kind: "funding_update".into(),
            observed_wall_ts_ms: 10,
            available_wall_ts_ms: 11,
            subscriptions: Vec::new(),
            payload: payload.to_vec(),
            content_sha256: "hash".into(),
        }
    }

    /// Both wire shapes decode to the same bytes and the same hash; text
    /// payloads are written as a string, anything else as the array.
    #[test]
    fn a_payload_reads_as_a_string_or_as_an_array_of_bytes() {
        let expected = observation(br#"{"rate":"0.0001"}"#);
        let as_string = serde_json::to_string(&expected).unwrap();
        assert!(
            as_string.contains(r#""payload":"{\"rate\":\"0.0001\"}""#),
            "{as_string}"
        );
        let as_array = as_string.replace(
            r#""payload":"{\"rate\":\"0.0001\"}""#,
            "\"payload\":[123,34,114,97,116,101,34,58,34,48,46,48,48,48,49,34,125]",
        );
        assert_ne!(as_array, as_string, "the replacement found the string");
        let binary = observation(&[0xff, 0x00, 0x7b]);
        let binary_json = serde_json::to_string(&binary).unwrap();
        assert!(
            binary_json.contains("\"payload\":[255,0,123]"),
            "{binary_json}"
        );
        assert_eq!(
            serde_json::from_str::<SignalObservation>(&binary_json).unwrap(),
            binary
        );
        let from_array: SignalObservation = serde_json::from_str(&as_array).unwrap();
        let from_string: SignalObservation = serde_json::from_str(&as_string).unwrap();
        assert_eq!(from_array, expected);
        assert_eq!(from_string, expected);
        assert_eq!(
            from_string.canonical_envelope_bytes(),
            expected.canonical_envelope_bytes()
        );
    }
}
