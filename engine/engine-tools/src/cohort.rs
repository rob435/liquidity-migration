//! Every opportunity the log holds, and where each one stopped.
//!
//! `fills` reads the orders that filled. This reads the population they came
//! out of: the rows the signal source published, the decisions the reducers
//! made from them, and everything that reached no order at all. A hit rate
//! computed over filled orders alone cannot see the row that arrived after
//! its own validity, the order the risk kernel refused, or the command whose
//! authority lapsed in the venue queue.
//!
//! Two lanes, printed side by side, because the log does not join them: a
//! [`WalRecord::SignalObservation`] carries no order identity and a
//! [`WalRecord::Intent`] carries no observation identity. Charging an order
//! to the source row that happens to precede it would be a guess about which
//! callback produced it, so the source lane ends at the reducer's
//! acknowledgement, the order lane starts at the reducer's decision, and the
//! interval between them is named and reported as unmeasurable.
//!
//! Two refusals reach the log only as a [`WalRecord::Note`], so they are read
//! by the text the engine writes. A `Note` suppressed as a repeat is not in
//! the log at all, and the order it would have named reads as unresolved.

use std::collections::BTreeMap;
use std::fmt::Write as _;

use engine_types::orders::OrderUpdate;
use engine_types::risk::{DenyReason, RiskVerdict};
use engine_types::wal::RetainedWalRecord;
use engine_types::WalRecord;

/// Observation kinds a sleeve turns into entry, resize or exit decisions.
/// Everything else the source publishes is settlement accounting, marks or a
/// producer verdict, and can produce no order by itself.
const ORDER_BEARING: [&str; 4] = [
    "carry_feature_batch",
    "carry_scorer_catchup",
    "llm_gate_candidates",
    "long_feature_batch",
];

/// The only order-bearing kind whose payload states when it stops being
/// actionable. The rest are retired by reducer config, which is not logged.
const GATE_KIND: &str = "llm_gate_candidates";

const QUANTILES: [f64; 4] = [0.50, 0.90, 0.99, 0.999];

/// Nearest-rank on the sorted samples: exact, because every sample is here.
fn quantile(sorted: &[i64], q: f64) -> i64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = (q * sorted.len() as f64).ceil().max(1.0) as usize;
    sorted[rank.min(sorted.len()) - 1]
}

/// One lane's four fates. Every unit of the lane's population is in exactly
/// one of them.
#[derive(Clone, Debug, Default, PartialEq, Eq, serde::Serialize)]
pub struct Lane {
    pub admitted: u64,
    pub rejected: u64,
    pub expired: u64,
    pub unresolved: u64,
    /// Why it was refused, by name.
    pub rejected_by_reason: BTreeMap<String, u64>,
    /// Why its validity had run out, by name.
    pub expired_by_reason: BTreeMap<String, u64>,
}

impl Lane {
    pub fn total(&self) -> u64 {
        self.admitted + self.rejected + self.expired + self.unresolved
    }

    fn reject(&mut self, reason: String) {
        self.rejected += 1;
        *self.rejected_by_reason.entry(reason).or_default() += 1;
    }

    fn expire(&mut self, reason: String) {
        self.expired += 1;
        *self.expired_by_reason.entry(reason).or_default() += 1;
    }
}

/// One age, in milliseconds, and the stamps it is made of.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Interval {
    pub name: &'static str,
    pub from_stamp: &'static str,
    pub to_stamp: &'static str,
    /// Which clock or clocks the two ends were read from.
    pub clocks: &'static str,
    /// Present when the log carries no pair of stamps that measures this.
    pub unmeasurable: Option<&'static str>,
    pub count: usize,
    pub p50_ms: Option<f64>,
    pub p90_ms: Option<f64>,
    pub p99_ms: Option<f64>,
    pub p999_ms: Option<f64>,
    pub max_ms: Option<f64>,
    /// Pairs whose second stamp is before the first. Reported, never clamped
    /// to zero, and left out of the quantiles.
    pub stamped_backwards: u64,
    /// Units this interval's stamp is absent from.
    pub without_a_stamp: u64,
}

#[derive(Default)]
struct Samples {
    /// Nanoseconds.
    values: Vec<i64>,
    backwards: u64,
    missing: u64,
}

impl Samples {
    fn push_ns(&mut self, ns: i64) {
        if ns < 0 {
            self.backwards += 1;
        } else {
            self.values.push(ns);
        }
    }

    fn push_ms(&mut self, ms: i64) {
        self.push_ns(ms.saturating_mul(1_000_000));
    }

    fn interval(
        mut self,
        name: &'static str,
        from_stamp: &'static str,
        to_stamp: &'static str,
        clocks: &'static str,
    ) -> Interval {
        self.values.sort_unstable();
        let ms = |ns: i64| ns as f64 / 1_000_000.0;
        let mark = |q| (!self.values.is_empty()).then(|| ms(quantile(&self.values, q)));
        Interval {
            name,
            from_stamp,
            to_stamp,
            clocks,
            unmeasurable: None,
            count: self.values.len(),
            p50_ms: mark(QUANTILES[0]),
            p90_ms: mark(QUANTILES[1]),
            p99_ms: mark(QUANTILES[2]),
            p999_ms: mark(QUANTILES[3]),
            max_ms: self.values.last().copied().map(ms),
            stamped_backwards: self.backwards,
            without_a_stamp: self.missing,
        }
    }
}

/// The source lane's age, whole and by observation kind: a catch-up row is
/// old by construction and a live batch is not, and one quantile over both
/// says neither.
#[derive(Default)]
struct SourceAges {
    all: Samples,
    by_kind: BTreeMap<String, Samples>,
}

impl SourceAges {
    fn push(&mut self, kind: &str, ms: i64) {
        self.all.push_ms(ms);
        self.by_kind
            .entry(kind.to_string())
            .or_default()
            .push_ms(ms);
    }
}

const SOURCE_TO_CONSUME: (&str, &str, &str, &str) = (
    "source stamp to engine consume",
    "signal_observation.observation.observed_wall_ts_ms",
    "signal_observation_consumed|rejected.wall_ts_ms",
    "two processes' realtime clocks: the worker stamps one end, the engine the other",
);

/// The whole census of one log family.
#[derive(Clone, Debug, PartialEq, serde::Serialize)]
pub struct Cohort {
    pub records: u64,
    /// `signal_observation` records of an order-bearing kind.
    pub source_row_records: u64,
    /// Distinct source rows in the lane below. Higher than
    /// `source_row_records` when a segment restatement names rows whose own
    /// record is below the range read.
    pub source_rows: u64,
    /// Rows named only by a segment restatement's `signal_observations`.
    pub restated_only_rows: u64,
    pub source_lane: Lane,
    /// `intent` records.
    pub intent_records: u64,
    pub order_lane: Lane,
    /// Every other record, by its wire kind; a source row of a kind that can
    /// produce no order is counted as `signal:<kind>`.
    pub not_an_opportunity: BTreeMap<String, u64>,
    /// Verdicts naming no order this walk held an intent for. Zero unless the
    /// log holds a decision path this reader does not model.
    pub unattached_verdicts: u64,
    pub ages: Vec<Interval>,
    /// `source stamp to engine consume` again, one interval per observation
    /// kind; the counts add up to the first entry of `ages`.
    pub source_ages_by_kind: BTreeMap<String, Interval>,
}

impl Cohort {
    /// Records the two lanes and the census between them account for. Must
    /// equal `records`.
    pub fn accounted(&self) -> u64 {
        self.source_row_records
            + self.intent_records
            + self.not_an_opportunity.values().sum::<u64>()
    }

    pub fn balanced(&self) -> bool {
        self.accounted() == self.records
    }
}

/// One source row's identity and the stamps it carries.
struct Row {
    kind: String,
    observed_wall_ts_ms: i64,
    /// From the row's own payload, when the payload states one.
    valid_until_ms: Option<i64>,
    settled: bool,
}

/// One allowed intent waiting for the wire.
#[derive(Default)]
struct Allowed {
    on_the_wire: bool,
    never_sent: Option<String>,
    refused: Option<String>,
}

/// Allowed orders in the order the log allowed them. Order ids are unique
/// within a boot, not across a log family, so a reused id is a second order
/// here and not a second look at the first one.
#[derive(Default)]
struct AllowedOrders {
    rows: Vec<Allowed>,
    newest: BTreeMap<String, usize>,
}

impl AllowedOrders {
    fn allow(&mut self, client_order_id: &str) {
        self.newest
            .insert(client_order_id.to_string(), self.rows.len());
        self.rows.push(Allowed::default());
    }

    fn newest_mut(&mut self, client_order_id: &str) -> Option<&mut Allowed> {
        let at = *self.newest.get(client_order_id)?;
        self.rows.get_mut(at)
    }
}

type RowKey = (u16, String, u64, String);

/// The `DenyReason` variant, which is what a refusal is grouped by. The
/// numbers inside a reason belong to the one order, not to the population.
fn deny_name(reason: &DenyReason) -> &'static str {
    match reason {
        DenyReason::LossGuardTripped { .. } => "LossGuardTripped",
        DenyReason::EnvelopeBreached { .. } => "EnvelopeBreached",
        DenyReason::ComponentGrossBreached { .. } => "ComponentGrossBreached",
        DenyReason::InitialMarginBreached { .. } => "InitialMarginBreached",
        DenyReason::AvailableMarginExhausted { .. } => "AvailableMarginExhausted",
        DenyReason::RollingLossTripped { .. } => "RollingLossTripped",
        DenyReason::PartitionExhausted { .. } => "PartitionExhausted",
        DenyReason::MissingStop => "MissingStop",
        DenyReason::StaleAccountView { .. } => "StaleAccountView",
        DenyReason::StaleQuote { .. } => "StaleQuote",
        DenyReason::UnknownState { .. } => "UnknownState",
        DenyReason::SymbolNotionalBreached { .. } => "SymbolNotionalBreached",
    }
}

/// A refusal's own words, with the one number in an authority refusal taken
/// out so the whole population groups on one line.
fn refusal_name(reason: &str) -> String {
    if reason.starts_with("authority: expired") {
        return "authority: expired in the venue queue (dispatch TTL)".to_string();
    }
    if reason.starts_with("authority: epoch") {
        return "authority: epoch superseded".to_string();
    }
    reason.to_string()
}

/// When the row stops being actionable, as its own payload states it.
fn payload_valid_until_ms(kind: &str, payload: &[u8]) -> Option<i64> {
    if kind != GATE_KIND {
        return None;
    }
    let envelope: serde_json::Value = serde_json::from_slice(payload).ok()?;
    envelope
        .get("payload")?
        .get("valid_until_ms")?
        .as_i64()
        .filter(|value| *value > 0)
}

/// `intent <tag> refused: <why>` — the engine refused an intent it had
/// already journaled, and wrote no verdict for it.
fn intent_refusal(text: &str) -> Option<&str> {
    text.strip_prefix("intent ")?
        .split_once(" refused: ")
        .map(|(_, why)| why)
}

/// `<client_order_id> not sent (<tag>): <why>` — the engine refused an order
/// the kernel had already allowed, after the verdict and before the wire.
fn order_refusal(text: &str) -> Option<(&str, String)> {
    let (id, rest) = text.split_once(" not sent (")?;
    let (_, why) = rest.split_once("): ")?;
    let why = why.split(" (and ").next().unwrap_or(why);
    Some((id, why.to_string()))
}

fn count(census: &mut BTreeMap<String, u64>, kind: &str) {
    *census.entry(kind.to_string()).or_default() += 1;
}

pub fn of_log(records: &[WalRecord]) -> Cohort {
    let mut census = BTreeMap::<String, u64>::new();
    let mut rows = BTreeMap::<RowKey, Row>::new();
    let mut source_lane = Lane::default();
    let mut order_lane = Lane::default();
    let mut allowed = AllowedOrders::default();
    let mut pending_intent = false;
    let mut source_row_records = 0u64;
    let mut restated_only_rows = 0u64;
    let mut intent_records = 0u64;
    let mut unattached_verdicts = 0u64;
    let mut newest_wall_ms = 0i64;
    let mut source_to_consume = SourceAges::default();
    let mut decision_to_wire = Samples::default();

    for record in records {
        if let Some(wall_ms) = wall_stamp(record) {
            newest_wall_ms = newest_wall_ms.max(wall_ms);
        }
        match record {
            WalRecord::SignalObservation { observation, .. } => {
                if !ORDER_BEARING.contains(&observation.kind.as_str()) {
                    count(&mut census, &format!("signal:{}", observation.kind));
                    continue;
                }
                source_row_records += 1;
                rows.entry((
                    observation.destination.0,
                    observation.source.clone(),
                    observation.sequence,
                    observation.observation_id.clone(),
                ))
                .or_insert_with(|| Row {
                    kind: observation.kind.clone(),
                    observed_wall_ts_ms: observation.observed_wall_ts_ms,
                    valid_until_ms: payload_valid_until_ms(&observation.kind, &observation.payload),
                    settled: false,
                });
            }
            WalRecord::SignalObservationConsumed {
                wall_ts_ms,
                strategy,
                source,
                sequence,
                observation_id,
            } => {
                count(&mut census, kind_of(record));
                settle_row(
                    &mut rows,
                    &mut source_lane,
                    &mut source_to_consume,
                    (
                        strategy.0,
                        source.clone(),
                        *sequence,
                        observation_id.clone(),
                    ),
                    *wall_ts_ms,
                    None,
                );
            }
            WalRecord::SignalObservationRejected {
                wall_ts_ms,
                strategy,
                source,
                sequence,
                observation_id,
                reason,
            } => {
                count(&mut census, kind_of(record));
                settle_row(
                    &mut rows,
                    &mut source_lane,
                    &mut source_to_consume,
                    (
                        strategy.0,
                        source.clone(),
                        *sequence,
                        observation_id.clone(),
                    ),
                    *wall_ts_ms,
                    Some(reason.clone()),
                );
            }
            WalRecord::Intent { .. } => {
                intent_records += 1;
                if pending_intent {
                    order_lane.unresolved += 1;
                }
                pending_intent = true;
            }
            WalRecord::Verdict {
                client_order_id,
                verdict,
            } => {
                count(&mut census, kind_of(record));
                match (verdict, client_order_id) {
                    (RiskVerdict::Deny { reason }, _) if pending_intent => {
                        pending_intent = false;
                        order_lane.reject(deny_name(reason).to_string());
                    }
                    (RiskVerdict::Allow { .. }, Some(id)) if pending_intent => {
                        pending_intent = false;
                        allowed.allow(id);
                    }
                    // No intent is waiting, so this is an in-place amend
                    // re-judged under the id its order already carries.
                    (_, Some(_)) => {}
                    _ => unattached_verdicts += 1,
                }
            }
            WalRecord::OrderSent {
                dispatch,
                request,
                wire_ns,
                ..
            } => {
                count(&mut census, kind_of(record));
                match dispatch {
                    Some(dispatch) => decision_to_wire
                        .push_ns(*wire_ns as i64 - dispatch.intent.decided_ns as i64),
                    None => decision_to_wire.missing += 1,
                }
                if let Some(order) = allowed.newest_mut(&request.client_order_id) {
                    order.on_the_wire = true;
                }
            }
            WalRecord::OrderUpdate { update, .. } => {
                count(&mut census, kind_of(record));
                if let OrderUpdate::Reject {
                    client_order_id,
                    reason,
                    ..
                } = update
                {
                    if let Some(tail) = reason.strip_prefix("never sent: ") {
                        if let Some(order) = allowed.newest_mut(client_order_id) {
                            order.never_sent = Some(refusal_name(tail));
                        }
                    }
                }
            }
            WalRecord::Note { text, .. } => {
                count(&mut census, kind_of(record));
                if let (true, Some(why)) = (pending_intent, intent_refusal(text)) {
                    pending_intent = false;
                    order_lane.reject(why.to_string());
                } else if let Some((id, why)) = order_refusal(text) {
                    if let Some(order) = allowed.newest_mut(id) {
                        order.refused = Some(why);
                    }
                }
            }
            WalRecord::SegmentBase {
                signal_observations,
                ..
            } => {
                count(&mut census, kind_of(record));
                for observation in signal_observations {
                    if !ORDER_BEARING.contains(&observation.kind.as_str()) {
                        continue;
                    }
                    let key = (
                        observation.destination.0,
                        observation.source.clone(),
                        observation.sequence,
                        observation.observation_id.clone(),
                    );
                    if rows.contains_key(&key) {
                        continue;
                    }
                    restated_only_rows += 1;
                    rows.insert(
                        key,
                        Row {
                            kind: observation.kind.clone(),
                            observed_wall_ts_ms: observation.observed_wall_ts_ms,
                            valid_until_ms: payload_valid_until_ms(
                                &observation.kind,
                                &observation.payload,
                            ),
                            settled: false,
                        },
                    );
                }
            }
            other => count(&mut census, kind_of(other)),
        }
    }

    if pending_intent {
        order_lane.unresolved += 1;
    }
    for order in &allowed.rows {
        match (&order.never_sent, &order.refused, order.on_the_wire) {
            (Some(reason), _, _) => order_lane.expire(reason.clone()),
            (None, Some(why), _) => order_lane.reject(why.clone()),
            (None, None, true) => order_lane.admitted += 1,
            (None, None, false) => order_lane.unresolved += 1,
        }
    }
    for row in rows.values().filter(|row| !row.settled) {
        match row.valid_until_ms {
            Some(until) if until <= newest_wall_ms => source_lane.expire(format!(
                "{}: validity passed with no decision in this log",
                row.kind
            )),
            _ => source_lane.unresolved += 1,
        }
    }

    let source_rows = rows.len() as u64;
    Cohort {
        records: records.len() as u64,
        source_row_records,
        source_rows,
        restated_only_rows,
        source_lane,
        intent_records,
        order_lane,
        not_an_opportunity: census,
        unattached_verdicts,
        source_ages_by_kind: source_to_consume
            .by_kind
            .into_iter()
            .map(|(kind, samples)| {
                (
                    kind,
                    samples.interval(
                        SOURCE_TO_CONSUME.0,
                        SOURCE_TO_CONSUME.1,
                        SOURCE_TO_CONSUME.2,
                        SOURCE_TO_CONSUME.3,
                    ),
                )
            })
            .collect(),
        ages: vec![
            source_to_consume.all.interval(
                SOURCE_TO_CONSUME.0,
                SOURCE_TO_CONSUME.1,
                SOURCE_TO_CONSUME.2,
                SOURCE_TO_CONSUME.3,
            ),
            Interval {
                unmeasurable: Some(
                    "no id joins a source row to an intent, and the two ends are in \
                     different clocks: the acknowledgement carries engine realtime \
                     milliseconds, Intent.decided_ns carries engine monotonic nanoseconds, \
                     and no record ties them to a shared origin",
                ),
                ..Samples::default().interval(
                    "engine consume to decision",
                    "signal_observation_consumed|rejected.wall_ts_ms",
                    "intent.decided_ns",
                    "engine realtime milliseconds against engine monotonic nanoseconds",
                )
            },
            decision_to_wire.interval(
                "decision to wire",
                "order_sent_v2.dispatch.intent.decided_ns",
                "order_sent_v2.wire_ns",
                "one process's monotonic clock",
            ),
        ],
    }
}

fn settle_row(
    rows: &mut BTreeMap<RowKey, Row>,
    lane: &mut Lane,
    ages: &mut SourceAges,
    key: RowKey,
    wall_ts_ms: i64,
    rejected: Option<String>,
) {
    let Some(row) = rows.get_mut(&key) else {
        return;
    };
    if row.settled {
        return;
    }
    row.settled = true;
    ages.push(&row.kind, wall_ts_ms - row.observed_wall_ts_ms);
    match (row.valid_until_ms, rejected) {
        (Some(until), _) if until <= wall_ts_ms => lane.expire(format!(
            "{}: validity passed before the engine consumed it",
            row.kind
        )),
        (_, Some(reason)) => lane.reject(reason),
        (_, None) => lane.admitted += 1,
    }
}

/// The newest realtime millisecond the record carries, for judging a row
/// whose validity ran out with no decision. Monotonic stamps are not wall
/// time and are never read as it.
fn wall_stamp(record: &WalRecord) -> Option<i64> {
    match record {
        WalRecord::Boot { wall_ts_ms, .. }
        | WalRecord::SleeveStopSet { wall_ts_ms, .. }
        | WalRecord::StopSet { wall_ts_ms, .. }
        | WalRecord::Reconciled { wall_ts_ms, .. }
        | WalRecord::LatchCleared { wall_ts_ms, .. }
        | WalRecord::StrategyCheckpoint { wall_ts_ms, .. }
        | WalRecord::StrategyGlobalCheckpoint { wall_ts_ms, .. }
        | WalRecord::StrategyEventPublished { wall_ts_ms, .. }
        | WalRecord::StrategyEventConsumed { wall_ts_ms, .. }
        | WalRecord::SignalObservation { wall_ts_ms, .. }
        | WalRecord::SignalObservationConsumed { wall_ts_ms, .. }
        | WalRecord::SignalObservationRejected { wall_ts_ms, .. }
        | WalRecord::SignalGapRecorded { wall_ts_ms, .. }
        | WalRecord::InstrumentCatalogCheckpoint { wall_ts_ms, .. }
        | WalRecord::IdentityState { wall_ts_ms, .. }
        | WalRecord::SignalProducerLifecycle { wall_ts_ms, .. }
        | WalRecord::LegacyQuantityGridAdopted { wall_ts_ms, .. }
        | WalRecord::LegacySignalSourceRetired { wall_ts_ms, .. }
        | WalRecord::RuntimeControlAccepted { wall_ts_ms, .. }
        | WalRecord::RuntimeControlConsumed { wall_ts_ms, .. }
        | WalRecord::SegmentBase { wall_ts_ms, .. } => Some(*wall_ts_ms),
        WalRecord::ExecutionHistoryCheckpoint { through_wall_ts_ms } => Some(*through_wall_ts_ms),
        WalRecord::RecoveredFill {
            recovered_wall_ts_ms,
            ..
        } => Some(*recovered_wall_ts_ms),
        _ => None,
    }
}

/// The wire kind of a record, so the census names every row it counts.
/// Exhaustive on purpose: a new record kind is a compile error here, not an
/// unnamed line in an audit.
fn kind_of(record: &WalRecord) -> &'static str {
    match record {
        WalRecord::StrategyRuntimeReconfigured { .. } => "strategy_runtime_reconfigured",
        WalRecord::OrderIdEpoch { .. } => "order_id_epoch",
        WalRecord::ExecutionPrecisionV1 => "execution_precision_v1",
        WalRecord::PortfolioExitChanged { .. } => "portfolio_exit_changed",
        WalRecord::PortfolioExitCompleted { .. } => "portfolio_exit_completed",
        WalRecord::PortfolioEmergencyChanged { .. } => "portfolio_emergency_changed",
        WalRecord::PortfolioEmergencyCompleted { .. } => "portfolio_emergency_completed",
        WalRecord::PortfolioOffsetSettled { .. } => "portfolio_offset_settled",
        WalRecord::SleeveStopSet { .. } => "sleeve_stop_set",
        WalRecord::Boot { .. } => "boot",
        WalRecord::OrderDispatchQueued { .. } => "order_dispatch_queued",
        WalRecord::OrderDispatchAttempted { .. } => "order_dispatch_attempted",
        WalRecord::OrderDispatchCompleted { .. } => "order_dispatch_completed",
        WalRecord::StrategyTransitionQueued { .. } => "strategy_transition_queued",
        WalRecord::StrategyCallbackSource { .. } => "strategy_callback_source",
        WalRecord::StrategyEffectCompleted { .. } => "strategy_effect_completed",
        WalRecord::Intent { .. } => "intent",
        WalRecord::Verdict { .. } => "verdict",
        WalRecord::OrderLineageRestored { .. } => "order_lineage_restored",
        WalRecord::OrderSent { .. } => "order_sent_v2",
        WalRecord::OrderUpdate { .. } => "order_update_v3",
        WalRecord::CancelSent { .. } => "cancel_sent",
        WalRecord::StopSet { .. } => "stop_set",
        WalRecord::AmendSent { .. } => "amend_sent_v2",
        WalRecord::AmendResolved { .. } => "amend_resolved_v2",
        WalRecord::Markout { .. } => "markout",
        WalRecord::QuoteFill { .. } => "quote_fill",
        WalRecord::LatencyLedger { .. } => "latency_ledger",
        WalRecord::VenueTiming { .. } => "venue_timing",
        WalRecord::Note { .. } => "note",
        WalRecord::Reconciled { .. } => "reconciled",
        WalRecord::RecoveredFill { .. } => "recovered_fill_v3",
        WalRecord::ExecutionHistoryCheckpoint { .. } => "execution_history_checkpoint",
        WalRecord::LatchCleared { .. } => "latch_cleared",
        WalRecord::StrategyCheckpoint { .. } => "strategy_checkpoint",
        WalRecord::StrategyGlobalCheckpoint { .. } => "strategy_global_checkpoint",
        WalRecord::StrategyEventPublished { .. } => "strategy_event_published",
        WalRecord::StrategyEventConsumed { .. } => "strategy_event_consumed",
        WalRecord::SignalObservation { .. } => "signal_observation",
        WalRecord::SignalObservationConsumed { .. } => "signal_observation_consumed",
        WalRecord::SignalObservationRejected { .. } => "signal_observation_rejected",
        WalRecord::SignalGapRecorded { .. } => "signal_gap_recorded",
        WalRecord::SignalAdmissionChanged { .. } => "signal_admission_changed",
        WalRecord::InstrumentCatalogCheckpoint { .. } => "instrument_catalog_checkpoint",
        WalRecord::IdentityState { .. } => "identity_state",
        WalRecord::SignalProducerLifecycle { .. } => "signal_producer_lifecycle",
        WalRecord::LegacyQuantityGridAdopted { .. } => "legacy_quantity_grid_adopted_v2",
        WalRecord::LegacySignalSourceRetired { .. } => "legacy_signal_source_retired",
        WalRecord::RuntimeControlAccepted { .. } => "runtime_control_accepted",
        WalRecord::RuntimeControlConsumed { .. } => "runtime_control_consumed",
        WalRecord::SegmentBase { .. } => "segment_base_v7",
        WalRecord::Retained(retained) => match retained {
            RetainedWalRecord::ControlAnchor { .. } => "control_anchor",
            RetainedWalRecord::TargetBookLatch { .. } => "target_book_latch",
            RetainedWalRecord::ClaimsDropped { .. } => "claims_dropped",
            RetainedWalRecord::StrategyCallbackQueued { .. } => "strategy_callback_queued",
            RetainedWalRecord::StrategyCallbackPrepared { .. } => "strategy_callback_prepared",
            RetainedWalRecord::StrategyProcessTransitionQueued { .. } => {
                "strategy_process_transition_queued"
            }
            RetainedWalRecord::Names { .. } => "names",
            RetainedWalRecord::FastExecution { .. } => "fast_execution",
        },
    }
}

const NAME_WIDTH: usize = 40;
const CELL_WIDTH: usize = 13;

/// Sub-second ages keep their microseconds; anything longer is whole
/// milliseconds, so a queue age of minutes stays readable in its column.
fn cell(value: Option<f64>) -> String {
    match value {
        Some(ms) if ms < 1_000.0 => format!("{ms:.3}"),
        Some(ms) => format!("{ms:.0}"),
        None => "-".to_string(),
    }
}

impl Cohort {
    pub fn table(&self) -> String {
        let mut out = String::from("where every opportunity in this log stopped\n\n");
        let _ = writeln!(
            out,
            "  {:<NAME_WIDTH$}{:>12}{:>12}",
            "bucket", "source rows", "orders"
        );
        for (name, source, order) in [
            (
                "admitted",
                self.source_lane.admitted,
                self.order_lane.admitted,
            ),
            (
                "rejected",
                self.source_lane.rejected,
                self.order_lane.rejected,
            ),
            ("expired", self.source_lane.expired, self.order_lane.expired),
            (
                "unresolved",
                self.source_lane.unresolved,
                self.order_lane.unresolved,
            ),
        ] {
            let _ = writeln!(out, "  {name:<NAME_WIDTH$}{source:>12}{order:>12}");
        }
        let _ = writeln!(
            out,
            "  {:<NAME_WIDTH$}{:>12}{:>12}",
            "in the lane",
            self.source_lane.total(),
            self.order_lane.total()
        );
        out.push_str(
            "\n  A source row is admitted when its destination reducer consumed it, expired \
             when\n  its own payload's validity had passed, rejected when the reducer rejected \
             it, and\n  unresolved when the log ends with it still held. An order is admitted \
             when its\n  bytes were journaled to the wire and no refusal followed, rejected on \
             a Deny\n  verdict or a pre-wire refusal, expired when the venue task answered \
             `never sent`,\n  and unresolved when no decision or wire record closed it. The \
             two lanes are not\n  joined: no id in this log links a source row to an order.\n",
        );

        for (lane, title, counts) in [
            (
                "source rows",
                "rejected by the consumer",
                &self.source_lane.rejected_by_reason,
            ),
            (
                "source rows",
                "expired",
                &self.source_lane.expired_by_reason,
            ),
            (
                "orders",
                "refused before the wire",
                &self.order_lane.rejected_by_reason,
            ),
            (
                "orders",
                "never sent after the wire journal",
                &self.order_lane.expired_by_reason,
            ),
        ] {
            if counts.is_empty() {
                continue;
            }
            let _ = write!(out, "\n  {lane} — {title}\n");
            for (reason, count) in counts {
                let _ = writeln!(out, "    {count:>8}  {reason}");
            }
        }

        let _ = write!(out, "\n  ages, in milliseconds\n\n    ");
        let _ = write!(out, "{:<32}{:>8}", "interval", "count");
        for head in ["p50", "p90", "p99", "p99.9", "max"] {
            let _ = write!(out, "{head:>CELL_WIDTH$}");
        }
        out.push('\n');
        for age in &self.ages {
            let _ = write!(out, "    {:<32}{:>8}", age.name, age.count);
            for value in [age.p50_ms, age.p90_ms, age.p99_ms, age.p999_ms, age.max_ms] {
                let _ = write!(out, "{:>CELL_WIDTH$}", cell(value));
            }
            out.push('\n');
            if age.name == SOURCE_TO_CONSUME.0 {
                for (kind, by_kind) in &self.source_ages_by_kind {
                    let _ = write!(out, "      {:<30}{:>8}", kind, by_kind.count);
                    for value in [
                        by_kind.p50_ms,
                        by_kind.p90_ms,
                        by_kind.p99_ms,
                        by_kind.p999_ms,
                        by_kind.max_ms,
                    ] {
                        let _ = write!(out, "{:>CELL_WIDTH$}", cell(value));
                    }
                    out.push('\n');
                }
            }
        }
        for age in &self.ages {
            let _ = writeln!(out, "\n    {}", age.name);
            let _ = writeln!(out, "      from  {}", age.from_stamp);
            let _ = writeln!(out, "      to    {}", age.to_stamp);
            let _ = writeln!(out, "      clock {}", age.clocks);
            if let Some(why) = age.unmeasurable {
                let _ = writeln!(out, "      UNMEASURABLE: {why}");
            }
            if age.stamped_backwards > 0 {
                let _ = writeln!(
                    out,
                    "      {} pair(s) stamped backwards, left out rather than read as zero",
                    age.stamped_backwards
                );
            }
            if age.without_a_stamp > 0 {
                let _ = writeln!(
                    out,
                    "      {} unit(s) carry no such stamp at all",
                    age.without_a_stamp
                );
            }
        }
        out.push_str(
            "\n  `source stamp to engine consume` is stamped by two processes. On one host it \
             is\n  one realtime clock read twice, so only a clock step between the reads is in \
             the\n  number; a worker on another host puts its offset in there too.\n",
        );

        let _ = write!(out, "\n  not an order opportunity\n");
        for (kind, count) in &self.not_an_opportunity {
            let _ = writeln!(out, "    {count:>8}  {kind}");
        }

        let _ = write!(out, "\n  the count\n");
        for (name, value) in [
            (
                "source rows of an order-bearing kind",
                self.source_row_records,
            ),
            ("order decisions (intent)", self.intent_records),
            (
                "not an order opportunity",
                self.not_an_opportunity.values().sum::<u64>(),
            ),
        ] {
            let _ = writeln!(out, "    {value:>8}  {name}");
        }
        let _ = writeln!(out, "    {:>8}  accounted for", self.accounted());
        let _ = writeln!(out, "    {:>8}  records read", self.records);
        out.push_str(if self.balanced() {
            "\n  every record in this log is on exactly one line above.\n"
        } else {
            "\n  THESE DO NOT ADD UP. The difference is records this reader did not \
             classify.\n"
        });
        if self.restated_only_rows > 0 {
            let _ = writeln!(
                out,
                "\n  {} source row(s) are named only by a segment restatement, so they are in \
                 the\n  source lane and not in the record count.",
                self.restated_only_rows
            );
        }
        if self.unattached_verdicts > 0 {
            let _ = writeln!(
                out,
                "\n  {} verdict(s) named no order this reader held an intent for. Read the log \
                 with\n  `engine replay` before trusting the order lane.",
                self.unattached_verdicts
            );
        }
        out.push_str(
            "\n  What this log cannot hold. The signal worker replaces a still-unread \
             replaceable\n  output in its spool before the engine ever sees it, counted as \
             `replaceable_outputs_coalesced`\n  in the worker's `heartbeat.json`. Names the \
             venue does not list are dropped by\n  `universe.listed_on` \
             (engine/signal-worker/src/universe.rs) before an observation is\n  built at all: \
             no counter holds those drops, and the domain the filter kept is\n  \
             `universe_symbols`, `universe_long_symbols` and `universe_carry_symbols` in the \
             same\n  heartbeat.\n",
        );
        out.push_str(
            "\n  A long_feature_batch, carry_feature_batch or carry_scorer_catchup row states \
             no\n  validity in its payload: the cutoffs that retire one \
             (signal_freshness_ms,\n  book_validity_ms, engine_entry_cutoff_ms) are reducer \
             config, not log. Only a\n  llm_gate_candidates row, which carries \
             `valid_until_ms`, can reach `expired` in the\n  source lane.\n",
        );
        out
    }
}

#[cfg(test)]
#[path = "cohort/tests.rs"]
mod tests;
