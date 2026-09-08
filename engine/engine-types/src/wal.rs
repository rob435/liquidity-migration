use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, SymbolId};
use crate::orders::{
    AmendSpec, ForcedClose, Intent, OrderRequest, OrderUpdate, QuoteFillFeatures, Side,
};
use crate::risk::{ClosedTradeRow, RiskVerdict};
use crate::strategy::{
    CheckpointProvenance, RuntimeControlRequest, SignalObservation, StrategyCheckpoint,
    StrategyEvent,
};

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveredCallbacks {
    pub owners: Vec<StrategyId>,
    pub recv_ns: u64,
}

/// One record in the append-only log. Serialized as tagged JSON inside a
/// checksummed binary frame (framing is the WAL crate's concern). Kept
/// human-readable on purpose: the log is the engine's audit trail.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)]
pub enum WalRecord {
    OrderIdEpoch {
        epoch_ms: i64,
    },
    ExecutionPrecisionV1,
    PortfolioExitChanged {
        state: crate::portfolio_control::PortfolioExit,
    },
    PortfolioExitCompleted {
        id: u64,
        strategy: StrategyId,
        symbol: SymbolId,
    },
    PortfolioEmergencyChanged {
        state: crate::portfolio_control::PortfolioEmergency,
    },
    PortfolioEmergencyCompleted {
        id: u64,
        symbol: SymbolId,
    },
    PortfolioOffsetSettled {
        settlement: crate::portfolio_control::PortfolioOffsetSettlement,
    },
    SleeveStopSet {
        strategy: StrategyId,
        symbol: SymbolId,
        side: Side,
        trigger_price: crate::numeric::Exact,
        wall_ts_ms: i64,
    },

    /// Engine start: code identity and config identity, so every later
    /// record is attributable. `commit` is the git commit the binary was built
    /// from; logs written before builds were stamped read back with it empty.
    Boot {
        version: String,
        config_sha256: String,
        wall_ts_ms: i64,
        #[serde(default)]
        commit: String,
    },
    OrderDispatchQueued {
        order: crate::order_dispatch::OrderDispatchState,
    },
    OrderDispatchAttempted {
        client_order_id: String,
    },
    OrderDispatchCompleted {
        client_order_id: String,
    },
    StrategyTransitionQueued {
        transition: StrategyTransitionState,
    },
    StrategyCallbackSource {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        placement: Option<String>,
        strategy: StrategyId,
        event: crate::strategy_process::CallbackEvent,
    },

    StrategyEffectCompleted {
        transition_id: u64,
        effect_index: usize,
    },
    Intent {
        intent: Intent,
    },
    Verdict {
        client_order_id: Option<String>,
        verdict: RiskVerdict,
    },
    /// Written and made durable BEFORE the order bytes leave the socket. A
    /// crash between this record and the ack can never forget an in-flight
    /// order.
    OrderLineageRestored {
        order: OpenOrderState,
    },
    #[serde(rename = "order_sent_v2", alias = "order_sent")]
    OrderSent {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        dispatch: Option<Box<crate::order_dispatch::QueuedOrderDispatch>>,
        request: OrderRequest,
        wire_ns: u64,
        /// `M0`: the midpoint of the book at the moment this order left, which
        /// is what every arrival number is measured against
        /// (`docs/architecture.md` §Trade diagnostics). Zero means the book
        /// could not be read, and a zero anchor yields no measurement rather
        /// than a flattering one.
        ///
        /// It is written here, on the send, because this is the only moment
        /// it exists: a worked entry can rest for a minute before it fills,
        /// and by then the price it was decided against is gone. Defaulted on
        /// the way in so a log written before the field existed still replays
        /// — as unmeasurable, which is the truth about it.
        #[serde(default)]
        arrival_mid: f64,
    },
    #[serde(
        rename = "order_update_v3",
        alias = "order_update_v2",
        alias = "order_update"
    )]
    OrderUpdate {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callbacks: Option<Vec<StrategyId>>,
        update: OrderUpdate,
    },
    /// A cancel on its way out. Not barriered before the wire: a cancel adds
    /// no exposure, and an order the log still shows working is recovered at
    /// boot whether or not the cancel survived the crash.
    CancelSent {
        symbol: SymbolId,
        client_order_id: String,
        wire_ns: u64,
    },
    /// A venue-native stop moved on a position, with no order involved.
    /// Written before the call so a crash cannot forget where the stop was
    /// meant to be: `intended_stops` folds this the same way it folds an
    /// opening order's own stop, and boot's repair uses the result.
    StopSet {
        symbol: SymbolId,
        trigger_px: f64,
        wall_ts_ms: i64,
    },
    /// An in-place reprice or resize on its way out.
    #[serde(rename = "amend_sent_v2", alias = "amend_sent")]
    AmendSent {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        wire_ns: u64,
    },
    /// A definitive venue answer to an amend. Until this arrives, replay
    /// keeps the full old/requested price range reserved: its high end prices
    /// notional and both ends price stop loss. An accepted/rejected answer
    /// narrows that conservative ambiguity to the price actually working.
    #[serde(rename = "amend_resolved_v2", alias = "amend_resolved")]
    AmendResolved {
        client_order_id: String,
        effective_px: f64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        exact_effective_px: Option<crate::numeric::ExactNumber>,
    },

    /// Where the market went after one of our fills.
    ///
    /// The one execution-quality number that is an observation rather than
    /// arithmetic: what a fill cost against the book when the order left can
    /// always be recomputed from `OrderSent` and the fill, but a price five
    /// minutes later exists only if somebody wrote it down at the time. So
    /// this is written when the horizon comes due.
    ///
    /// Names and signs are `docs/architecture.md` §Trade diagnostics.
    Markout {
        client_order_id: String,
        strategy: StrategyId,
        symbol: SymbolId,
        /// Which fill, by the venue's own stamp — an order can fill more than
        /// once.
        fill_ts_ms: i64,
        horizon_ms: u64,
        /// `Mh`. Absent when no readable book turned up inside the lateness
        /// bound, which is a horizon terminally missing rather than a zero.
        mid: Option<f64>,
        /// **Positive means the price moved our way.** The opposite sign
        /// convention from everything else, and it is the doc's.
        signed_markout_bps: Option<f64>,
        /// What the horizon actually came to. The engine looks on its
        /// group-flush tick, so a mark is always a little late.
        actual_horizon_ms: u64,
        /// What this mark speaks for, so a rollup can weight it.
        notional_usdt: f64,
    },
    /// The public-flow and book state surrounding one quoter fill. Fee and
    /// markout stay in their existing records and join through the ids here.
    QuoteFill {
        features: QuoteFillFeatures,
    },
    /// Periodic latency ledger line: histogram quantiles in nanoseconds.
    /// Missing p99.9 means an older writer or an empty segment, never zero latency.
    LatencyLedger {
        window_s: u32,
        events: u64,
        decide_p50_ns: u64,
        decide_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        decide_p999_ns: Option<u64>,
        #[serde(default)]
        durable_p50_ns: u64,
        #[serde(default)]
        durable_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        durable_p999_ns: Option<u64>,
        /// Time waiting for disk durability before venue dispatch.
        #[serde(default)]
        barrier_wait_p50_ns: u64,
        #[serde(default)]
        barrier_wait_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        barrier_wait_p999_ns: Option<u64>,
        wire_p50_ns: u64,
        wire_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        wire_p999_ns: Option<u64>,
        #[serde(default)]
        ack_p50_ns: u64,
        #[serde(default)]
        ack_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        ack_p999_ns: Option<u64>,
        #[serde(default)]
        dispatch_queue_p50_ns: u64,
        #[serde(default)]
        dispatch_queue_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        dispatch_queue_p999_ns: Option<u64>,
        #[serde(default)]
        venue_task_p50_ns: u64,
        #[serde(default)]
        venue_task_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        venue_task_p999_ns: Option<u64>,
        #[serde(default)]
        core_resume_p50_ns: u64,
        #[serde(default)]
        core_resume_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        core_resume_p999_ns: Option<u64>,
        #[serde(default)]
        end_to_end_p50_ns: u64,
        #[serde(default)]
        end_to_end_p99_ns: u64,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        end_to_end_p999_ns: Option<u64>,
    },
    /// Exact monotonic timing marks for one venue mutation item. These are
    /// the reconstructable measurements; histogram rows above are only the
    /// quick roll-up.
    VenueTiming {
        command_id: u64,
        operation: String,
        client_order_id: String,
        queued_ns: u64,
        task_started_ns: u64,
        /// Exact transport hand-off when the adapter exposes it.
        socket_write_ns: Option<u64>,
        /// Parsed venue acknowledgement for placements.
        ack_ns: Option<u64>,
        /// Of the task's own span, how much was the adapter holding this
        /// command back to stay inside the venue's request quota. Delay we
        /// chose; the rest of the span is the venue's.
        #[serde(default)]
        rate_wait_ns: Option<u64>,
        task_completed_ns: u64,
        core_handled_ns: u64,
        /// Unix time beside `core_handled_ns`, so the record can be aligned
        /// with forward market capture without using wall time for duration.
        #[serde(default)]
        core_handled_wall_ns: u64,
    },

    /// Free-form strategy note, tagged and rare.
    Note {
        source: String,
        text: String,
    },

    /// What boot found when it compared this log against the venue, and
    /// whether the engine may open new exposure afterwards.
    ///
    /// `may_open` false is a latch, and it is written here rather than held in
    /// memory: a restart that cleared it would turn "stop and tell somebody"
    /// into "stop until the next crash". Boot reads the newest one back before
    /// it reads anything from the venue.
    Reconciled {
        wall_ts_ms: i64,
        findings: Vec<String>,
        may_open: bool,
    },
    /// A fill this engine's own stream never delivered, recovered from the
    /// venue's execution history: it happened while the engine was down, or
    /// inside a private-stream gap. Counted into the per-symbol exposure sum
    /// exactly like a delivered fill, so the log stays an account of what the
    /// position actually is rather than only of what this process witnessed.
    #[serde(
        rename = "recovered_fill_v3",
        alias = "recovered_fill_v2",
        alias = "recovered_fill"
    )]
    RecoveredFill {
        #[serde(default, skip_serializing_if = "Option::is_none")]
        callbacks: Option<RecoveredCallbacks>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        allocation: Option<Box<crate::execution_allocation::ExecutionAllocation>>,
        /// The venue's own execution id — the dedup key against fetching the
        /// same history twice.
        exec_id: String,
        /// Empty when the venue reports none: a venue-attached stop firing,
        /// or a hand trade. A blank id with a `forced_close` below is charged
        /// to the sleeve the close reduces.
        client_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        /// What the venue charged in account currency. A numeric field in an
        /// older WAL becomes `Some`; missing or explicit null stays unknown.
        #[serde(default)]
        fee: Option<f64>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        amounts: Option<crate::numeric::ExecutionAmounts>,
        is_maker: bool,
        /// The venue's own reason for closing the position, when it says one.
        /// Missing in an older WAL, which reads as no reason recorded.
        #[serde(default)]
        forced_close: Option<ForcedClose>,
        /// When it happened, by the venue's clock.
        venue_ts_ms: i64,
        /// When this engine learned of it.
        recovered_wall_ts_ms: i64,
    },
    /// A complete venue execution-history read reached this wall time and all
    /// rows it returned are durable before this record. Empty successful reads
    /// write the same checkpoint; a process stopping does not.
    ExecutionHistoryCheckpoint {
        through_wall_ts_ms: i64,
    },
    /// An operator looked at the log (`engine reconcile-clear --execute`):
    /// the per-symbol exposure sum is restated to the venue's own positions,
    /// with the standing findings kept here as the receipt, and the may-open
    /// latch resets. This is the deliberate act [`WalRecord::Reconciled`]'s
    /// latch waits for — the next boot still runs its own comparison, so a
    /// difference that appears again latches again.
    LatchCleared {
        wall_ts_ms: i64,
        /// Why, in the operator's words.
        note: String,
        /// The exposure ledger as restated — the venue's signed positions at
        /// the moment of clearing. A reader treats this as "set", like
        /// [`WalRecord::SegmentBase`]'s copy.
        restated_exposure: Vec<SymbolTotal>,
        /// What stood unexplained before the clear, so the log keeps saying
        /// what was absorbed.
        findings: Vec<String>,
    },

    /// Strategy-owned state made durable before the venue action it guards.
    StrategyCheckpoint {
        wall_ts_ms: i64,
        strategy: StrategyId,
        symbol: SymbolId,
        checkpoint: StrategyCheckpoint,
    },
    /// Whole-sleeve state made durable before the later effect it guards.
    StrategyGlobalCheckpoint {
        wall_ts_ms: i64,
        strategy: StrategyId,
        checkpoint: StrategyCheckpoint,
        /// Present for a stopped-runtime takeover import; absent for live
        /// reducer checkpoints.
        #[serde(default, skip_serializing_if = "Option::is_none")]
        provenance: Option<CheckpointProvenance>,
    },
    /// One immutable cross-sleeve event, durable before either strategy can
    /// act on it.
    StrategyEventPublished {
        wall_ts_ms: i64,
        event: StrategyEvent,
    },
    /// The addressed strategy durably consumed one cross-sleeve event.
    StrategyEventConsumed {
        wall_ts_ms: i64,
        source: StrategyId,
        destination: StrategyId,
        event_id: String,
    },
    /// One normalized external observation, durable before reducer delivery.
    SignalObservation {
        wall_ts_ms: i64,
        observation: SignalObservation,
    },
    /// The addressed strategy durably consumed one external observation.
    SignalObservationConsumed {
        wall_ts_ms: i64,
        strategy: StrategyId,
        source: String,
        sequence: u64,
        observation_id: String,
    },
    /// A consumer explicitly rejected this input; no successful work is implied.
    SignalObservationRejected {
        wall_ts_ms: i64,
        strategy: StrategyId,
        source: String,
        sequence: u64,
        observation_id: String,
        reason: String,
    },
    /// A missing source prefix, durable before deferring its later spool row.
    SignalGapRecorded {
        wall_ts_ms: i64,
        gap: SignalGap,
    },
    SignalAdmissionChanged {
        destination: StrategyId,
        suspension: Option<crate::SignalAdmissionSuspensionReason>,
    },
    InstrumentCatalogCheckpoint {
        wall_ts_ms: i64,
        checkpoint: Box<crate::orders::InstrumentCatalogCheckpoint>,
    },
    IdentityState {
        wall_ts_ms: i64,
        state: crate::identity::IdentityState,
    },
    SignalProducerLifecycle {
        wall_ts_ms: i64,
        state: crate::SignalProducerLifecycle,
    },
    #[serde(rename = "legacy_quantity_grid_adopted_v2")]
    LegacyQuantityGridAdopted {
        version: u8,
        wall_ts_ms: i64,
        sleeves: Vec<LegacySleeveQuantityCorrection>,
        physical: Vec<LegacyPhysicalQuantityCorrection>,
    },
    LegacySignalSourceRetired {
        wall_ts_ms: i64,
        retirement: LegacySignalSourceRetirement,
    },
    /// One operator request, durable before its gate changes in memory.
    RuntimeControlAccepted {
        wall_ts_ms: i64,
        request: RuntimeControlRequest,
    },
    /// A strategy finished applying a replayable runtime command.
    RuntimeControlConsumed {
        wall_ts_ms: i64,
        strategy: StrategyId,
        request_id: String,
    },
    /// The first record of every log segment after the first: everything boot
    /// replay needs from the segments before this one, restated, so replaying
    /// this one segment recovers the same engine as replaying them all.
    ///
    /// One record on purpose. The frame checksum makes it all-or-nothing, so
    /// "this segment is complete enough to trust" is a single mechanical
    /// check: its first record reads back as one of these. A restatement
    /// spread over several records would need its own end-marker protocol,
    /// and a crash between two of them would leave a segment that replays
    /// half a state without saying so.
    ///
    /// Why not restate with the existing kinds: three of the things boot
    /// needs are sums over the whole history — whose fills built each
    /// position (`attribution`), what every fill in the log adds up to per
    /// symbol (`logged_exposure`), and the newest intended stop per symbol —
    /// and no copy of "the still-open orders' records" carries them. Copying
    /// every contributing fill forward would grow without bound, and writing
    /// invented fills that sum right would make the log lie.
    ///
    /// Every field is a **restatement of state at the moment of rotation**,
    /// so a reader of the whole segment chain treats it as "set", not "add":
    /// at that point in the stream it is exactly what the records before it
    /// already produced, which is what makes chain reads and single-segment
    /// reads agree.
    #[serde(rename = "segment_base_v7", alias = "segment_base")]
    SegmentBase {
        #[serde(default)]
        order_id_epoch_ms: Option<i64>,
        #[serde(default)]
        open_trade_lots: Option<Vec<crate::trade::OpenTradeLot>>,
        #[serde(default)]
        portfolio_control: crate::portfolio_control::PortfolioControlState,
        #[serde(default)]
        identities: Option<crate::identity::IdentityState>,
        #[serde(default)]
        instrument_catalog: Option<Box<crate::orders::InstrumentCatalogCheckpoint>>,
        #[serde(default)]
        pending_order_dispatches: Vec<crate::order_dispatch::OrderDispatchState>,
        #[serde(default)]
        strategy_processes: Vec<crate::strategy_process::StrategyProcessState>,
        #[serde(default)]
        strategy_callbacks: Vec<crate::strategy_process::StrategyCallbackInput>,
        #[serde(default)]
        strategy_callback_queues: Vec<crate::strategy_process::CallbackQueueSlot>,
        #[serde(default)]
        strategy_callback_sources: Vec<crate::strategy_process::CallbackSourceFrontier>,
        #[serde(default)]
        signal_callback_deliveries: Vec<crate::strategy_process::SignalCallbackDelivery>,
        #[serde(default)]
        portfolio: Option<crate::portfolio::PortfolioState>,
        wall_ts_ms: i64,
        /// The id tables, same meaning as [`RetainedWalRecord::Names`].
        strategies: Vec<String>,
        symbols: Vec<String>,
        /// The reconciliation latch, same meaning as
        /// [`WalRecord::Reconciled`]'s `may_open`.
        may_open: bool,
        /// Historical control anchors carried by older rotations. Retained in
        /// the schema for replay compatibility; new rotations leave it empty.
        control_anchors: Vec<AnchorState>,
        /// Signed filled quantity per (strategy, symbol): whose fills built
        /// each position. Flat rows are absent.
        attribution: Vec<FilledTotal>,
        /// Signed quantity per symbol summed over every fill this engine can
        /// account for — one joined to an order the log sent, or a
        /// venue-initiated close of a position one sleeve is holding. A
        /// stranger's fill stays out of it. This is what reconcile compares
        /// the venue's positions against.
        logged_exposure: Vec<SymbolTotal>,
        /// The stop each symbol's newest opening order asked for, so a stop
        /// the venue drops can still be put back after a rotation.
        intended_stops: Vec<IntendedStop>,
        /// Venue execution ids still inside the recovery horizon. Bounded in
        /// memory and restated so rotating cannot make an old fill new again.
        #[serde(default)]
        recent_execution_ids: Vec<RecentExecutionId>,
        /// Newest successful execution-history boundary carried across a log
        /// rotation. Older segment bases have no such proof.
        #[serde(default)]
        execution_history_through_ms: Option<i64>,
        /// Retired target-book latches kept in the segment schema so older
        /// restatements remain readable. Current rotations leave this empty.
        #[serde(default)]
        target_book_latches: Vec<StrategySymbol>,
        /// Newest strategy-owned state per strategy and symbol.
        #[serde(default)]
        strategy_checkpoints: Vec<StrategyCheckpointState>,
        /// Newest whole-sleeve state per strategy.
        #[serde(default)]
        strategy_global_checkpoints: Vec<StrategyGlobalCheckpointState>,
        /// Cross-sleeve events still waiting for their destination.
        #[serde(default)]
        strategy_events: Vec<StrategyEvent>,
        /// External observations still waiting for their strategy.
        #[serde(default)]
        signal_observations: Vec<SignalObservation>,
        /// Highest contiguous external sequence durably accepted per source.
        #[serde(default)]
        signal_cursors: Vec<SignalCursor>,
        /// Monotonic requested feed union per external source and destination.
        /// Consumption and later universe changes never remove held names.
        #[serde(default)]
        signal_subscriptions: Vec<SignalSubscriptionState>,
        /// Source prefixes that must recover before their destination opens.
        #[serde(default)]
        signal_gaps: Vec<SignalGap>,
        #[serde(default)]
        signal_producers: Vec<crate::SignalProducerLifecycle>,
        #[serde(default)]
        legacy_signal_source_retirements: Vec<LegacySignalSourceRetirement>,
        #[serde(default)]
        signal_suspensions: Vec<crate::SignalAdmissionSuspension>,
        #[serde(default)]
        strategy_effects: StrategyEffectsState,
        /// Accepted runtime entry requests in append order. The whole history
        /// is retained so a retried old request id stays a no-op after
        /// rotation instead of changing the current gate again.
        #[serde(default)]
        runtime_control_requests: Vec<RuntimeControlRequest>,
        /// Durable acknowledgements for replayable runtime commands.
        #[serde(default)]
        runtime_control_consumed: Vec<(StrategyId, String)>,
        /// Every order still in flight, with the fields its own records
        /// carried.
        open_orders: Vec<OpenOrderState>,
        /// The closed round trips the risk kernel's rolling loss window still
        /// holds, so a boot from this segment keeps the window instead of
        /// starting it empty. Older bases read back empty.
        #[serde(default)]
        rolling_loss_rows: Vec<ClosedTradeRow>,
    },
    #[serde(untagged)]
    Retained(RetainedWalRecord),
}

/// Retained wire kinds with no current append path.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[allow(clippy::large_enum_variant)]
pub enum RetainedWalRecord {
    /// Historical durable control state. The daily-loss feature that wrote
    /// these records is retired, but the shape remains so existing logs stay
    /// readable. New engine runs do not emit or restore it.
    ControlAnchor { source: String, state: String },
    /// Retired target-book follower state. Read for WAL compatibility and
    /// ignored by current runtimes.
    TargetBookLatch {
        wall_ts_ms: i64,
        strategy: StrategyId,
        symbol: SymbolId,
        latched: bool,
    },
    /// Historical claim removals still affect replayed sleeve quantities.
    ClaimsDropped {
        wall_ts_ms: i64,
        /// Exactly the rows as they stood when dropped, as the receipt.
        rows: Vec<FilledTotal>,
    },
    StrategyCallbackQueued {
        input: crate::strategy_process::StrategyCallbackInput,
    },
    StrategyCallbackPrepared {
        input: crate::strategy_process::StrategyCallbackInput,
    },
    StrategyProcessTransitionQueued {
        input_id: u64,
        transition: Option<StrategyTransitionState>,
        process: crate::strategy_process::StrategyProcessState,
    },
    /// Dense id tables from families predating IdentityState.
    Names {
        /// `strategies[i]` is the name of `StrategyId(i)`.
        strategies: Vec<String>,
        /// `symbols[i]` is the name of `SymbolId(i)`.
        symbols: Vec<String>,
    },
    /// Early fill signal from Bybit's fee-less fast stream. The ordinary
    /// authoritative fill follows separately and owns position accounting.
    FastExecution {
        exec_id: String,
        client_order_id: String,
        venue_order_id: String,
        symbol: SymbolId,
        side: Side,
        qty: f64,
        px: f64,
        is_maker: bool,
        venue_ts_ms: i64,
        recv_ns: u64,
    },
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegacySleeveQuantityCorrection {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub before: crate::numeric::Exact,
    pub after: crate::numeric::Exact,
    pub step: crate::numeric::Exact,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegacyPhysicalQuantityCorrection {
    pub symbol: SymbolId,
    pub before: crate::numeric::Exact,
    pub after: crate::numeric::Exact,
    pub step: crate::numeric::Exact,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LegacySignalSourceRetirement {
    pub source: String,
    pub destination: StrategyId,
    pub accepted_through: u64,
    pub published_through: u64,
    pub reason: String,
}

#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct StrategyEffectsState {
    pub next_transition_id: u64,
    pub transitions: Vec<StrategyTransitionState>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "owner", rename_all = "snake_case")]
pub enum StrategyTransitionOrigin {
    #[default]
    Embedded,
    Process {
        callback_id: u64,
    },
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct StrategyTransitionState {
    #[serde(default)]
    pub origin: StrategyTransitionOrigin,
    pub id: u64,
    pub strategy: StrategyId,
    pub effects: Vec<crate::Action>,
    pub order_ids: Vec<Option<String>>,
    pub completed: Vec<usize>,
}

/// One control-state line inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AnchorState {
    pub source: String,
    pub state: String,
}

/// One attribution row inside [`WalRecord::SegmentBase`]. Positive is long.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct FilledTotal {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub signed_qty: f64,
}

/// One per-symbol fill total inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct SymbolTotal {
    pub symbol: SymbolId,
    pub signed_qty: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_signed_qty: Option<crate::numeric::ExactNumber>,
}

impl SymbolTotal {
    pub fn exact_quantity(&self) -> Result<crate::numeric::Exact, crate::numeric::ExactError> {
        match &self.exact_signed_qty {
            Some(quantity) => {
                quantity.validate_provenance()?;
                if quantity.value.to_f64()? != self.signed_qty {
                    return Err(crate::numeric::ExactError::InvalidProjection);
                }
                Ok(quantity.value.clone())
            }
            None => crate::numeric::Exact::from_legacy_f64(self.signed_qty),
        }
    }
}

impl<'de> Deserialize<'de> for SymbolTotal {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        struct Row {
            symbol: SymbolId,
            signed_qty: f64,
            #[serde(default)]
            exact_signed_qty: Option<crate::numeric::ExactNumber>,
        }
        let row = Row::deserialize(deserializer)?;
        let total = Self {
            symbol: row.symbol,
            signed_qty: row.signed_qty,
            exact_signed_qty: row.exact_signed_qty,
        };
        total.exact_quantity().map_err(serde::de::Error::custom)?;
        Ok(total)
    }
}

/// One strategy/symbol state key inside [`WalRecord::SegmentBase`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategySymbol {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
}

/// One strategy-owned checkpoint inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategyCheckpointState {
    pub strategy: StrategyId,
    pub symbol: SymbolId,
    pub checkpoint: StrategyCheckpoint,
}

/// One whole-sleeve checkpoint inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct StrategyGlobalCheckpointState {
    pub strategy: StrategyId,
    pub checkpoint: StrategyCheckpoint,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provenance: Option<CheckpointProvenance>,
}

/// Highest contiguous external observation accepted from one source.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignalCursor {
    pub source: String,
    pub sequence: u64,
    pub content_sha256: String,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignalGap {
    pub source: String,
    pub destination: StrategyId,
    pub next_sequence: u64,
    pub observed_sequence: u64,
}

/// Durable subscription union from one external source to one strategy.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SignalSubscriptionState {
    pub source: String,
    pub destination: StrategyId,
    pub subscriptions: Vec<crate::market::Subscription>,
}

/// One intended-stop row inside [`WalRecord::SegmentBase`].
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub struct IntendedStop {
    pub symbol: SymbolId,
    /// Direction of the position this stop protects. Older segment bases did
    /// not carry it; those rows deserialize as unknown and are deliberately
    /// not trusted for automatic repair.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub side: Option<Side>,
    pub trigger_px: f64,
}

/// One execution-id dedup entry inside [`WalRecord::SegmentBase`].
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct RecentExecutionId {
    pub exec_id: String,
    /// Wall-clock milliseconds when the engine first learned this id.
    pub seen_ms: i64,
}

/// One still-open order inside [`WalRecord::SegmentBase`]: what its own
/// `OrderSent` record and the updates so far said about it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum OrderFillQuantity {
    Exact { quantity: crate::numeric::Exact },
    LegacyBinary64 { quantity: f64 },
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum OrderEnding {
    Rejected { code: i64, reason: String },
    Cancelled,
    Filled,
    NeverSent,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct TerminalOrderState {
    pub ending: OrderEnding,
    pub retained_since_ms: i64,
}

#[derive(Clone, Debug, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct ExactPriceRange {
    pub low: crate::numeric::Exact,
    pub high: crate::numeric::Exact,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct OpenOrderState {
    pub request: OrderRequest,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entry_work: Option<crate::WorkPolicy>,
    pub wire_ns: u64,
    #[serde(default)]
    pub arrival_mid: f64,
    pub acked: bool,
    pub filled_qty: f64,
    #[serde(default)]
    pub fill_quantity: Option<OrderFillQuantity>,
    /// Plausible working-price bounds after an amend whose answer was lost.
    /// Zero in older segments means "derive the exact price from request".
    #[serde(default)]
    pub reservation_low_px: f64,
    #[serde(default)]
    pub reservation_high_px: f64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exact_price_range: Option<ExactPriceRange>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub terminal: Option<TerminalOrderState>,
}

#[derive(Debug, thiserror::Error)]
pub enum WalError {
    #[error("wal io: {0}")]
    Io(#[from] std::io::Error),
    #[error("wal frame corrupt at offset {offset}: {detail}")]
    Corrupt { offset: u64, detail: String },
}

/// A durability barrier that has been started and not yet confirmed.
///
/// The bytes are already with the operating system when this is handed back;
/// what is outstanding is the disk saying so. Waiting is what turns it back
/// into the guarantee — see [`Wal::barrier_begin`].
pub struct PendingBarrier {
    /// `None` for a barrier that was already complete when it was made: a log
    /// with no disk behind it, or an implementation that stayed synchronous.
    done: Option<std::sync::mpsc::Receiver<Result<(), WalError>>>,
}

impl PendingBarrier {
    /// Nothing to wait for. Waiting on this succeeds immediately.
    pub fn settled() -> Self {
        PendingBarrier { done: None }
    }

    /// A barrier running elsewhere, which will send its result down this
    /// channel exactly once.
    pub fn running(done: std::sync::mpsc::Receiver<Result<(), WalError>>) -> Self {
        PendingBarrier { done: Some(done) }
    }

    /// True when this barrier still owes an answer. Only for a caller
    /// deciding whether waiting is worth reporting; waiting itself is safe
    /// either way.
    pub fn outstanding(&self) -> bool {
        self.done.is_some()
    }

    /// Wait for the disk to confirm. A sender that vanished without answering
    /// is a failed barrier, not a passed one: the thread that owed the answer
    /// is gone, so nothing can say the bytes are down.
    pub fn wait(self) -> Result<(), WalError> {
        match self.done {
            None => Ok(()),
            Some(done) => done.recv().unwrap_or_else(|_| {
                Err(WalError::Io(std::io::Error::other(
                    "the log's durability thread stopped without answering",
                )))
            }),
        }
    }
}

impl std::fmt::Debug for PendingBarrier {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PendingBarrier")
            .field("outstanding", &self.outstanding())
            .finish()
    }
}

/// The append-only log. One writer (the engine loop). Appends are buffered;
/// `barrier` is the durability point used before order sends; `flush` is the
/// cheap group commit for everything else.
pub trait OrderLineageReader: Send {
    fn set_cancel(&mut self, _cancel: std::sync::Arc<std::sync::atomic::AtomicBool>) {}
    fn next(&mut self) -> Result<Option<WalRecord>, WalError>;
}
pub trait OrderEpochReader: Send {
    fn set_cancel(&mut self, _cancel: std::sync::Arc<std::sync::atomic::AtomicBool>) {}
    fn max_order_epoch_ms(&mut self) -> Result<Option<i64>, WalError>;
}

pub trait Wal {
    /// Buffered append. Returns the record's sequence number.
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError>;
    /// Make everything appended so far durable now (fdatasync).
    fn barrier(&mut self) -> Result<(), WalError>;
    /// Start that barrier and return without waiting for the disk.
    ///
    /// The bytes reach the operating system before this returns, so the order
    /// of writes is fixed here; only the disk's confirmation is outstanding.
    /// The caller holds the handle and waits on it before doing anything that
    /// the confirmation is a precondition for.
    ///
    /// The default is the synchronous barrier with a handle that is already
    /// settled, which is the honest answer for a log with no disk behind it.
    fn barrier_begin(&mut self) -> Result<PendingBarrier, WalError> {
        self.barrier()?;
        Ok(PendingBarrier::settled())
    }
    /// Push buffered bytes to the OS without forcing disk durability.
    fn flush(&mut self) -> Result<(), WalError>;
    /// A bounded cursor reader of parent order updates in the current segment.
    fn callback_reader(
        &mut self,
    ) -> Result<Option<Box<dyn crate::strategy_process::CallbackWalReader>>, WalError> {
        Ok(None)
    }
    /// Matching order records from the retained WAL family, through this read's frontier.
    fn order_lineage_reader(
        &mut self,
        _client_order_id: &str,
    ) -> Result<Option<Box<dyn OrderLineageReader>>, WalError> {
        Ok(None)
    }
    fn supports_order_lineage_archive(&self) -> bool {
        false
    }
    fn order_epoch_reader(&mut self) -> Result<Option<Box<dyn OrderEpochReader>>, WalError> {
        Ok(None)
    }
    /// Bytes in the current segment, buffered ones included. Zero for a log
    /// that does not live in a file, which also means it is never rotated.
    fn segment_size(&self) -> u64 {
        0
    }
    /// Start a fresh segment whose first record is `base` (a
    /// [`WalRecord::SegmentBase`]), archiving the current one in place.
    /// Returns false for a log that does not rotate — the in-memory test
    /// doubles — which is not an error, just a log with nothing to archive.
    fn rotate(&mut self, base: &WalRecord) -> Result<bool, WalError> {
        let _ = base;
        Ok(false)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn legacy_latency_rows_keep_p999_unmeasured_and_preserve_known_zero() {
        let old = serde_json::json!({
            "kind": "latency_ledger", "window_s": 60, "events": 42,
            "decide_p50_ns": 100, "decide_p99_ns": 200,
            "wire_p50_ns": 300, "wire_p99_ns": 400,
        });
        let record: WalRecord = serde_json::from_value(old.clone()).unwrap();
        let rendered = serde_json::to_value(&record).unwrap();
        assert!(!rendered
            .as_object()
            .unwrap()
            .keys()
            .any(|key| key.ends_with("p999_ns")));
        for (key, value) in old.as_object().unwrap() {
            assert_eq!(&rendered[key], value);
        }
        for stage in [
            "decide",
            "durable",
            "barrier_wait",
            "wire",
            "ack",
            "dispatch_queue",
            "venue_task",
            "core_resume",
            "end_to_end",
        ] {
            let key = format!("{stage}_p999_ns");
            for value in [0, 900] {
                let mut with_p999 = old.clone();
                with_p999[&key] = value.into();
                let decoded: WalRecord = serde_json::from_value(with_p999).unwrap();
                let roundtrip = serde_json::to_value(decoded).unwrap();
                assert_eq!(roundtrip[&key], value);
                assert_eq!(roundtrip["decide_p99_ns"], 200);
            }
            let mut explicit_null = old.clone();
            explicit_null[&key] = serde_json::Value::Null;
            assert_eq!(
                serde_json::from_value::<WalRecord>(explicit_null).unwrap(),
                record
            );
        }
    }

    /// The exact bytes a live log already holds. A refusal is written whole,
    /// so every reason the kernel has ever produced is frozen into the format
    /// and the reader has to keep understanding it long after the rule that
    /// produced it is gone.
    #[test]
    fn a_refusal_the_kernel_no_longer_produces_still_reads_back() {
        let frame = r#"{"kind":"verdict","client_order_id":null,"verdict":{"Deny":{"reason":{"SymbolNotionalBreached":{"symbol":11,"notional_usdt":156255.2326,"cap_usdt":125000.0}}}}}"#;

        let record: WalRecord =
            serde_json::from_str(frame).expect("an old refusal must still parse");

        let WalRecord::Verdict { verdict, .. } = record else {
            panic!("expected a verdict record");
        };
        let RiskVerdict::Deny { reason } = verdict else {
            panic!("expected a denial");
        };
        let rendered = format!("{reason:?}");
        assert!(rendered.contains("SymbolNotionalBreached"), "{rendered}");
        assert!(rendered.contains("156255.2326"), "{rendered}");
        assert!(rendered.contains("125000.0"), "{rendered}");
    }

    #[test]
    fn a_retired_loss_guard_verdict_and_anchor_still_read_back() {
        let verdict = r#"{"kind":"verdict","client_order_id":null,"verdict":{"Deny":{"reason":{"LossGuardTripped":{"equity_usdt":89.5,"floor_usdt":90.0}}}}}"#;
        let anchor = r#"{"kind":"control_anchor","source":"risk","state":"{\"day\":20693,\"tripped\":true}"}"#;

        let verdict: WalRecord =
            serde_json::from_str(verdict).expect("a retired loss verdict must still parse");
        let anchor: WalRecord =
            serde_json::from_str(anchor).expect("a retired control anchor must still parse");

        assert!(matches!(
            verdict,
            WalRecord::Verdict {
                verdict: RiskVerdict::Deny {
                    reason: crate::risk::DenyReason::LossGuardTripped {
                        equity_usdt: 89.5,
                        floor_usdt: 90.0,
                    },
                },
                ..
            }
        ));
        assert!(matches!(
            anchor,
            WalRecord::Retained(crate::wal::RetainedWalRecord::ControlAnchor { source, state })
                if source == "risk" && state.contains("tripped")
        ));
    }

    #[test]
    fn a_legacy_intended_stop_reads_as_direction_unknown() {
        let row: IntendedStop = serde_json::from_str(r#"{"symbol":3,"trigger_px":90.0}"#)
            .expect("legacy segment rows must still parse");

        assert_eq!(row.symbol, SymbolId(3));
        assert_eq!(row.side, None);
        assert_eq!(row.trigger_px, 90.0);
    }

    #[test]
    fn old_numeric_fees_stay_known_and_absent_fees_stay_unknown() {
        let recovered = WalRecord::RecoveredFill {
            callbacks: None,
            allocation: None,
            amounts: None,
            exec_id: "exec-1".into(),
            client_order_id: "eng-1".into(),
            symbol: SymbolId(2),
            side: Side::Buy,
            qty: 3.0,
            px: 4.0,
            fee: Some(0.25),
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 10,
            recovered_wall_ts_ms: 20,
        };
        let mut encoded = serde_json::to_value(&recovered).expect("serialize recovered fill");
        assert_eq!(encoded["fee"], 0.25, "old numeric wire shape is retained");
        assert!(matches!(
            serde_json::from_value::<WalRecord>(encoded.clone()).expect("numeric fee reads"),
            WalRecord::RecoveredFill {
                fee: Some(0.25),
                ..
            }
        ));
        encoded
            .as_object_mut()
            .expect("tagged record is an object")
            .remove("fee");
        assert!(matches!(
            serde_json::from_value::<WalRecord>(encoded).expect("missing fee reads"),
            WalRecord::RecoveredFill { fee: None, .. }
        ));

        let delivered = WalRecord::OrderUpdate {
            callbacks: None,
            update: OrderUpdate::Fill {
                allocation: None,
                amounts: None,
                exec_id: "exec-2".into(),
                client_order_id: "eng-2".into(),
                symbol: SymbolId(1),
                side: Side::Sell,
                qty: 1.0,
                px: 100.0,
                fee: Some(0.0),
                is_maker: true,
                forced_close: None,
                venue_ts_ms: 30,
                recv_ns: 40,
            },
        };
        let mut encoded = serde_json::to_value(&delivered).expect("serialize stream fill");
        assert_eq!(encoded["update"]["Fill"]["fee"], 0.0);
        encoded["update"]["Fill"]
            .as_object_mut()
            .expect("fill payload is an object")
            .remove("fee");
        assert!(matches!(
            serde_json::from_value::<WalRecord>(encoded).expect("legacy stream fill reads"),
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill { fee: None, .. },
                ..
            }
        ));
    }

    #[test]
    fn a_recovered_fill_written_before_the_venue_reason_still_replays() {
        let recovered = WalRecord::RecoveredFill {
            callbacks: None,
            allocation: None,
            amounts: None,
            exec_id: "exec-1".into(),
            client_order_id: String::new(),
            symbol: SymbolId(2),
            side: Side::Sell,
            qty: 10.0,
            px: 4.0,
            fee: Some(0.25),
            is_maker: false,
            forced_close: Some(ForcedClose::Liquidation),
            venue_ts_ms: 10,
            recovered_wall_ts_ms: 20,
        };
        let mut encoded = serde_json::to_value(&recovered).expect("serialize recovered fill");
        assert_eq!(encoded["forced_close"], "Liquidation", "it round trips");
        encoded
            .as_object_mut()
            .expect("tagged record is an object")
            .remove("forced_close");
        assert!(matches!(
            serde_json::from_value::<WalRecord>(encoded).expect("an older recovered fill reads"),
            WalRecord::RecoveredFill {
                forced_close: None,
                ..
            }
        ));
    }

    #[test]
    fn old_segment_base_without_new_defaulted_fields_still_reads() {
        let base = WalRecord::SegmentBase {
            order_id_epoch_ms: None,
            open_trade_lots: Some(Vec::new()),
            legacy_signal_source_retirements: Vec::new(),
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
            wall_ts_ms: 1,
            strategies: Vec::new(),
            symbols: Vec::new(),
            may_open: true,
            control_anchors: Vec::new(),
            attribution: Vec::new(),
            logged_exposure: Vec::new(),
            intended_stops: Vec::new(),
            recent_execution_ids: Vec::new(),
            execution_history_through_ms: Some(123),
            target_book_latches: Vec::new(),
            strategy_checkpoints: Vec::new(),
            strategy_global_checkpoints: Vec::new(),
            strategy_events: Vec::new(),
            signal_observations: Vec::new(),
            signal_cursors: Vec::new(),
            signal_subscriptions: Vec::new(),
            signal_gaps: Vec::new(),
            strategy_effects: Default::default(),
            runtime_control_requests: Vec::new(),
            runtime_control_consumed: Vec::new(),
            open_orders: Vec::new(),
            rolling_loss_rows: vec![ClosedTradeRow {
                unpriced: None,
                net_usdt_exact: None,
                closed_ms: 1,
                net_usdt: -4.0,
            }],
        };
        let mut encoded = serde_json::to_value(&base).expect("serialize segment base");
        assert_eq!(encoded["kind"], "segment_base_v7");
        encoded["kind"] = serde_json::Value::String("segment_base".into());
        encoded
            .as_object_mut()
            .expect("tagged record is an object")
            .remove("execution_history_through_ms");
        encoded
            .as_object_mut()
            .expect("tagged record is an object")
            .remove("strategy_checkpoints");
        for field in [
            "strategy_global_checkpoints",
            "strategy_events",
            "signal_observations",
            "signal_cursors",
            "signal_subscriptions",
            "signal_gaps",
            "strategy_effects",
            "portfolio",
            "signal_producers",
            "strategy_processes",
            "strategy_callbacks",
            "pending_order_dispatches",
            "runtime_control_requests",
            "runtime_control_consumed",
            "rolling_loss_rows",
        ] {
            encoded
                .as_object_mut()
                .expect("tagged record is an object")
                .remove(field);
        }
        assert!(matches!(
            serde_json::from_value::<WalRecord>(encoded).expect("legacy segment base reads"),
            WalRecord::SegmentBase {
                execution_history_through_ms: None,
                strategy_checkpoints,
                rolling_loss_rows,
                ..
            } if strategy_checkpoints.is_empty() && rolling_loss_rows.is_empty()
        ));
    }
}

#[cfg(test)]
mod boot_shape_tests {
    use super::WalRecord;

    #[test]
    fn a_boot_written_before_commit_stamping_reads_with_an_empty_commit() {
        let old =
            r#"{"kind":"boot","version":"engine-core 0.1.0","config_sha256":"abc","wall_ts_ms":7}"#;
        let record: WalRecord = serde_json::from_str(old).expect("old boot shape decodes");
        assert_eq!(
            record,
            WalRecord::Boot {
                version: "engine-core 0.1.0".into(),
                config_sha256: "abc".into(),
                wall_ts_ms: 7,
                commit: String::new(),
            }
        );
        let stamped = serde_json::to_string(&WalRecord::Boot {
            version: "v".into(),
            config_sha256: "c".into(),
            wall_ts_ms: 1,
            commit: "0123abcd".into(),
        })
        .unwrap();
        assert!(stamped.contains(r#""commit":"0123abcd""#));
    }
}

impl WalRecord {
    pub fn recovered_callback(&self) -> Option<(Vec<StrategyId>, OrderUpdate)> {
        let Self::RecoveredFill {
            callbacks: Some(callbacks),
            allocation,
            amounts,
            exec_id,
            client_order_id,
            symbol,
            side,
            qty,
            px,
            fee,
            is_maker,
            forced_close,
            venue_ts_ms,
            ..
        } = self
        else {
            return None;
        };
        Some((
            callbacks.owners.clone(),
            OrderUpdate::Fill {
                allocation: allocation.clone(),
                amounts: amounts.clone().map(Box::new),
                exec_id: exec_id.clone(),
                client_order_id: client_order_id.clone(),
                symbol: *symbol,
                side: *side,
                qty: *qty,
                px: *px,
                fee: *fee,
                is_maker: *is_maker,
                forced_close: *forced_close,
                venue_ts_ms: *venue_ts_ms,
                recv_ns: callbacks.recv_ns,
            },
        ))
    }
}
