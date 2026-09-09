use super::LaneContext;
use super::{
    bounded_instrument_source_ranges, carry_required_lanes_pending, closed_kline_end,
    complete_funding_coverage, complete_whale_coverage, heartbeat_status, runtime_status,
    send_repair_chunk_and_wait, send_whale_chunk_and_wait, source_grid_slots,
    startup_runtime_status, stream_transport_healthy, trading_intervals_contain,
    transient_recovery_acceptable, validate_funding_source_against_state,
    validate_instrument_source_against_state, validate_source_grid_timestamp,
    validate_source_page_rows, whale_fetch_bounds, CycleFreshness, FetchedFunding,
    FetchedFundingBatch, FetchedInstruments, FetchedKlineBatch, FetchedKlineJobs, FetchedTickers,
    FetchedUniverseInputs, FetchedWhales, LaneCompletion, LaneState, LiveRunOptions, LiveRunner,
    RecoveryState, StreamEvent, StreamHealth, TickerSample, BOOT_REPAIR_MAX_MS,
    FUNDING_PUBLICATION_LAG_MS, LANE_COMPLETION_QUEUE_CAPACITY, STARTUP_MAX_MS,
    TRANSIENT_RECOVERY_MAX_MS,
};
use crate::config::SignalWorkerConfig;
use crate::history::{coverage_repair_start, CoverageRef};
use crate::model::{
    BinanceWhaleWire, BootstrapCoverage, BybitFundingWire, BybitInstrumentWire, BybitTickerWire,
    CoverageInterval, HourlyKline, InstrumentTradingInterval, ObservationPayload, SettledFunding,
    SignalPayloadEnvelope, UniverseIdentity, UniverseMode, WireEvent,
};
use crate::store::AtomicJsonStore;
use crate::venue::bybit::BybitPublicStream;
use crate::venue::{PublicStream, StreamContinuity};
use crate::worker::{SignalWorker, WorkerError};
use crate::SCHEMA_VERSION;
use crate::{DAY_MS, HOUR_MS};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

#[test]
fn production_frontier_plans_only_unchecked_or_rebased_ranges() {
    let start = 100 * HOUR_MS;
    let end = start + 5 * HOUR_MS;
    assert_eq!(coverage_repair_start(start, end, None, None), start);
    assert_eq!(
        coverage_repair_start(start, end, Some(start), Some(end)),
        end
    );
    assert_eq!(
        coverage_repair_start(start, end, Some(start), Some(end - HOUR_MS)),
        end - HOUR_MS
    );
    assert_eq!(
        coverage_repair_start(start, end, Some(start + HOUR_MS), Some(end)),
        start,
        "a wider requirement rebases instead of pretending old coverage still exists"
    );
    assert_eq!(
        coverage_repair_start(
            start,
            end,
            Some(start - 10 * HOUR_MS),
            Some(start - HOUR_MS)
        ),
        start,
        "a wholly retired frontier is replaced after long downtime"
    );
}

#[test]
fn publication_horizon_does_not_claim_the_just_closed_hour() {
    let boundary = 1_000 * HOUR_MS;
    assert_eq!(closed_kline_end(boundary + 59_999), boundary - HOUR_MS);
    assert_eq!(closed_kline_end(boundary + 60_000), boundary);
}

#[test]
fn runtime_stays_starting_only_for_the_bounded_cycle_warmup() {
    assert_eq!(
        startup_runtime_status(
            "degraded",
            [
                CycleFreshness::on_cadence(None, 60_000),
                CycleFreshness::on_cadence(None, 60_000)
            ],
            true,
            1_000,
            STARTUP_MAX_MS,
        ),
        "starting"
    );
    assert_eq!(
        startup_runtime_status(
            "ready",
            [
                CycleFreshness::on_cadence(Some(2_000), 60_000),
                CycleFreshness::on_cadence(Some(3_000), 60_000)
            ],
            true,
            1_000,
            3_000,
        ),
        "ready"
    );
    assert_eq!(
        startup_runtime_status(
            "degraded",
            [
                CycleFreshness::on_cadence(Some(2_000), 60_000),
                CycleFreshness::on_cadence(None, 60_000)
            ],
            true,
            1_000,
            STARTUP_MAX_MS + 1_000,
        ),
        "degraded"
    );
    assert_eq!(
        startup_runtime_status(
            "degraded",
            [
                CycleFreshness::on_cadence(None, 60_000),
                CycleFreshness::on_cadence(None, 60_000)
            ],
            false,
            1_000,
            60_000,
        ),
        "degraded",
        "a disconnected or incomplete stream is not healthy warmup"
    );
    assert_eq!(
        startup_runtime_status(
            "ready",
            [
                CycleFreshness::on_cadence(Some(1_000), 60_000),
                CycleFreshness::on_cadence(Some(200_000), 60_000)
            ],
            true,
            1_000,
            200_001,
        ),
        "degraded",
        "a completed LONG lane cannot remain ready after it stalls"
    );
    assert_eq!(
        startup_runtime_status(
            "ready",
            [
                CycleFreshness::on_cadence(Some(200_000), 60_000),
                CycleFreshness::on_cadence(Some(200_002), 60_000)
            ],
            true,
            1_000,
            200_001,
        ),
        "degraded",
        "a future cycle timestamp is not fresh"
    );
}

/// The daily decision roll: the carry lane cannot score the new boundary until
/// its funding print is publishable, and the decision is not due until the
/// kline lag, so a completion from before the boundary is not a stall until the
/// window past that instant.
#[test]
fn a_carry_lane_waiting_for_a_boundarys_funding_print_is_not_stale() {
    let boundary_ms = 1_788_912_000_000;
    let last_completed_ms = boundary_ms - 6_000;
    let not_before_ms = boundary_ms + 1_200_000;
    let cycles = |now_ms: i64| {
        [
            CycleFreshness::on_cadence(Some(now_ms - 1_000), 60_000),
            CycleFreshness::due_after(Some(last_completed_ms), 60_000, Some(not_before_ms)),
        ]
    };
    for (stalled_ms, note) in [
        (
            boundary_ms + 290_000,
            "the funded fleet paged at 00:05 UTC on a carry cycle that could not have completed",
        ),
        (
            boundary_ms + 535_000,
            "the longest observed roll sweep cleared at 00:08:55 UTC",
        ),
        (
            not_before_ms + 180_000,
            "the window still runs from the moment the lane is due",
        ),
    ] {
        assert_eq!(
            startup_runtime_status(
                "ready",
                cycles(stalled_ms),
                true,
                boundary_ms - STARTUP_MAX_MS,
                stalled_ms,
            ),
            "ready",
            "{note}"
        );
    }
    let overdue_ms = not_before_ms + 180_001;
    assert_eq!(
        startup_runtime_status(
            "ready",
            cycles(overdue_ms),
            true,
            boundary_ms - STARTUP_MAX_MS,
            overdue_ms,
        ),
        "degraded",
        "a carry lane still silent past the window is a fault"
    );
}

#[test]
fn a_cold_start_still_filling_ticker_coverage_is_starting_not_degraded() {
    let started_at_ms = 1_000_000;
    let now_ms = started_at_ms + 274_000;
    // What a worker 274 s into its cold start has: socket up, every topic
    // accepted, frames arriving, the boot repair gap still open and the
    // ticker cache still filling.
    let booting = StreamHealth {
        connected: true,
        epoch: 1,
        gap_open: true,
        gap_open_since_ms: Some(started_at_ms + 13_000),
        last_frame_ts_ms: Some(now_ms - 1),
        ticker_capacity: 2,
        ticker_coverage_complete: false,
        ticker_topics_accepted: 2,
        kline_topics_accepted: 2,
        ..StreamHealth::default()
    };
    let cycles = [
        CycleFreshness::on_cadence(None, 60_000),
        CycleFreshness::on_cadence(None, 60_000),
    ];
    let mut old_recovery = RecoveryState {
        transient_started_at_ms: Some(started_at_ms + 1),
        reached_ready: false,
    };
    assert_eq!(runtime_status(&booting, true, now_ms, 30_000), "degraded");
    assert_eq!(
        heartbeat_status(
            &booting,
            true,
            cycles,
            started_at_ms,
            now_ms,
            30_000,
            &mut old_recovery,
        ),
        "starting",
        "a sound stream that has not finished filling coverage is warmup, not a fault"
    );

    let disconnected = StreamHealth {
        connected: false,
        ..booting.clone()
    };
    let mut disconnected_recovery = RecoveryState::default();
    assert_eq!(
        heartbeat_status(
            &disconnected,
            true,
            cycles,
            started_at_ms,
            now_ms,
            30_000,
            &mut disconnected_recovery,
        ),
        "degraded",
        "a disconnected stream is a fault from the first heartbeat"
    );

    let refused = StreamHealth {
        ticker_topics_quarantined: 1,
        ..booting.clone()
    };
    let mut refused_recovery = RecoveryState::default();
    assert_eq!(
        heartbeat_status(
            &refused,
            true,
            cycles,
            started_at_ms,
            now_ms,
            30_000,
            &mut refused_recovery,
        ),
        "degraded",
        "a refused topic never fills, so it is not warmup"
    );

    let mut expired_startup_recovery = RecoveryState {
        transient_started_at_ms: Some(started_at_ms + 1),
        reached_ready: false,
    };
    assert_eq!(
        heartbeat_status(
            &booting,
            true,
            cycles,
            started_at_ms,
            started_at_ms + STARTUP_MAX_MS,
            30_000,
            &mut expired_startup_recovery,
        ),
        "degraded",
        "past the cold-start bound an unfinished backfill is a fault"
    );
    let mut completed_cycle_recovery = RecoveryState {
        transient_started_at_ms: Some(started_at_ms + 1),
        reached_ready: false,
    };
    assert_eq!(
        heartbeat_status(
            &booting,
            false,
            [
                CycleFreshness::on_cadence(Some(now_ms - 1_000), 60_000),
                CycleFreshness::on_cadence(Some(now_ms - 1_000), 60_000)
            ],
            started_at_ms,
            now_ms,
            30_000,
            &mut completed_cycle_recovery,
        ),
        "degraded",
        "once both cycles have run, incomplete coverage is the live verdict again"
    );
}

#[test]
fn a_cold_start_boot_repair_gap_is_recovering_until_its_own_bound() {
    // Incident mainnet-014ec4a90a2fde5f: 126 s after the 15:53:32 handover boot
    // the mainnet worker had a sound transport, complete coverage, both 60 s
    // cycles already run off warm durable state, and the boot repair gap still
    // open. The gap closed at ~190 s, but the 2-minute transient window had
    // already made the verdict `degraded` and paged the funded realm.
    let started_at_ms = 1_000_000;
    let now_ms = started_at_ms + 126_000;
    let boot_repairing = StreamHealth {
        connected: true,
        epoch: 1,
        gap_open: true,
        gap_open_since_ms: Some(started_at_ms + 2_000),
        last_frame_ts_ms: Some(now_ms - 1),
        ticker_capacity: 2,
        ticker_coverage_complete: true,
        ticker_topics_accepted: 2,
        kline_topics_accepted: 2,
        ..StreamHealth::default()
    };
    let cycles = [
        CycleFreshness::on_cadence(Some(started_at_ms + 66_000), 60_000),
        CycleFreshness::on_cadence(Some(started_at_ms + 66_500), 60_000),
    ];
    let mut recovery = RecoveryState {
        transient_started_at_ms: Some(started_at_ms + 4_000),
        reached_ready: false,
    };
    assert_eq!(
        heartbeat_status(
            &boot_repairing,
            true,
            cycles,
            started_at_ms,
            now_ms,
            30_000,
            &mut recovery,
        ),
        "recovering",
        "the first repair after boot is cold fill, not a fault"
    );

    // Its own bound, well short of the 120-minute cold-start bound.
    let still_repairing = StreamHealth {
        last_frame_ts_ms: Some(started_at_ms + BOOT_REPAIR_MAX_MS - 1),
        ..boot_repairing.clone()
    };
    assert_eq!(
        heartbeat_status(
            &still_repairing,
            true,
            [
                CycleFreshness::on_cadence(
                    Some(started_at_ms + BOOT_REPAIR_MAX_MS - 60_000),
                    60_000
                ),
                CycleFreshness::on_cadence(
                    Some(started_at_ms + BOOT_REPAIR_MAX_MS - 59_000),
                    60_000
                ),
            ],
            started_at_ms,
            started_at_ms + BOOT_REPAIR_MAX_MS,
            30_000,
            &mut recovery,
        ),
        "degraded",
        "a boot repair that will not close is a fault"
    );

    // Once the repair closes the gap the worker is ready, and the next gap is a
    // mid-life reconnect on the 2-minute window again.
    let repaired = StreamHealth {
        gap_open: false,
        gap_open_since_ms: None,
        last_frame_ts_ms: Some(started_at_ms + 190_000),
        ..boot_repairing.clone()
    };
    assert_eq!(
        heartbeat_status(
            &repaired,
            false,
            cycles,
            started_at_ms,
            started_at_ms + 190_000,
            30_000,
            &mut recovery,
        ),
        "ready"
    );
    assert!(recovery.reached_ready);
    let reconnected = StreamHealth {
        epoch: 2,
        gap_open_since_ms: Some(started_at_ms + 200_000),
        last_frame_ts_ms: Some(started_at_ms + 330_000),
        ..boot_repairing.clone()
    };
    let late_cycles = [
        CycleFreshness::on_cadence(Some(started_at_ms + 300_000), 60_000),
        CycleFreshness::on_cadence(Some(started_at_ms + 300_500), 60_000),
    ];
    assert_eq!(
        heartbeat_status(
            &reconnected,
            true,
            late_cycles,
            started_at_ms,
            started_at_ms + 330_000,
            30_000,
            &mut recovery,
        ),
        "recovering"
    );
    let reconnect_expired = StreamHealth {
        last_frame_ts_ms: Some(started_at_ms + 330_000 + TRANSIENT_RECOVERY_MAX_MS - 1),
        ..reconnected.clone()
    };
    assert_eq!(
        heartbeat_status(
            &reconnect_expired,
            true,
            [
                CycleFreshness::on_cadence(
                    Some(started_at_ms + 330_000 + TRANSIENT_RECOVERY_MAX_MS - 60_000),
                    60_000
                ),
                CycleFreshness::on_cadence(
                    Some(started_at_ms + 330_000 + TRANSIENT_RECOVERY_MAX_MS - 59_000),
                    60_000
                ),
            ],
            started_at_ms,
            started_at_ms + 330_000 + TRANSIENT_RECOVERY_MAX_MS,
            30_000,
            &mut recovery,
        ),
        "degraded",
        "a reconnect gap on a worker that has been ready keeps the 2-minute window"
    );
}

#[test]
fn a_coverage_dip_when_the_boot_repair_hands_over_keeps_its_own_window() {
    // Incident demo-0922e9f30da3bf98: the demo worker restarted at 08:05:15,
    // opened its boot repair gap at 08:05:23, and at 08:08:27 -- 192 s in, on a
    // sound transport with both cycles run and the gap already closed --
    // reported `degraded` on `ticker coverage incomplete (166/166 rows, 166/166
    // topics accepted)`. That refused the funded handover of the 07:53 deploy.
    // The dip had cleared by 08:09:59, well inside the 2-minute window, but the
    // boot repair had held that window open from the first heartbeat and spent
    // it, so the live verdict took over with no grace left.
    let started_at_ms = 1_000_000;
    let boot_gap_at_ms = started_at_ms + 8_000;
    let health = |now_ms: i64, coverage_complete: bool, gap_open: bool| StreamHealth {
        connected: true,
        epoch: 2,
        gap_open,
        gap_open_since_ms: gap_open.then_some(boot_gap_at_ms),
        last_frame_ts_ms: Some(now_ms - 1),
        ticker_capacity: 2,
        ticker_coverage_complete: coverage_complete,
        ticker_topics_accepted: 2,
        kline_topics_accepted: 2,
        ..StreamHealth::default()
    };
    let cycles = |now_ms: i64| {
        [
            CycleFreshness::on_cadence(Some(now_ms - 1_000), 60_000),
            CycleFreshness::on_cadence(Some(now_ms - 500), 60_000),
        ]
    };
    // 187 s in: the boot repair still holds the gap open and coverage is full.
    let repairing_ms = started_at_ms + 187_000;
    // 192 s in: the repair closed the gap, and the same heartbeat finds one
    // symbol's mark past mark_max_age_ms, so the REST ticker lane refills it.
    let dip_ms = started_at_ms + 192_000;
    let mut recovery = RecoveryState {
        transient_started_at_ms: Some(boot_gap_at_ms),
        reached_ready: false,
    };
    assert_eq!(
        heartbeat_status(
            &health(repairing_ms, true, true),
            true,
            cycles(repairing_ms),
            started_at_ms,
            repairing_ms,
            30_000,
            &mut recovery,
        ),
        "recovering",
        "the boot repair carries the verdict inside its own bound"
    );
    assert_eq!(
        heartbeat_status(
            &health(dip_ms, false, false),
            false,
            cycles(dip_ms),
            started_at_ms,
            dip_ms,
            30_000,
            &mut recovery,
        ),
        "recovering",
        "a coverage dip as the boot repair hands over gets the 2-minute window, \
         not the remains of the boot repair's"
    );
    // The bound still holds: a dip that the REST lane cannot close is a fault.
    let expired_ms = dip_ms + TRANSIENT_RECOVERY_MAX_MS;
    assert_eq!(
        heartbeat_status(
            &health(expired_ms, false, false),
            false,
            cycles(expired_ms),
            started_at_ms,
            expired_ms,
            30_000,
            &mut recovery,
        ),
        "degraded",
        "coverage still short two minutes after the dip is a fault"
    );

    let mut refilled = RecoveryState {
        transient_started_at_ms: Some(boot_gap_at_ms),
        reached_ready: false,
    };
    for (now_ms, coverage_complete, gap_open, repair_running) in [
        (repairing_ms, true, true, true),
        (dip_ms, false, false, false),
    ] {
        heartbeat_status(
            &health(now_ms, coverage_complete, gap_open),
            repair_running,
            cycles(now_ms),
            started_at_ms,
            now_ms,
            30_000,
            &mut refilled,
        );
    }
    let refilled_ms = dip_ms + 5_000;
    assert_eq!(
        heartbeat_status(
            &health(refilled_ms, true, false),
            false,
            cycles(refilled_ms),
            started_at_ms,
            refilled_ms,
            30_000,
            &mut refilled,
        ),
        "ready",
        "the refilled mark is ready, and nothing paged for the dip"
    );
    assert!(refilled.reached_ready);
}

#[tokio::test(start_paused = true)]
async fn a_live_epoch_adopts_the_repair_already_started_at_boot() {
    let root = temporary_root("repair-adopts-epoch");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState {
        repair: true,
        ..LaneState::default()
    };

    runner
        .start_kline_repair(&lane_tx, &mut lanes, Some(7))
        .unwrap();

    assert!(lanes.repair);
    assert_eq!(lanes.repair_epoch, Some(7));
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_repair_restarted_without_an_epoch_keeps_the_live_one() {
    // The carry catch-up and the instrument lane restart the repair lane
    // with no epoch, and only `mark_gap_repaired(epoch)` closes the
    // WebSocket gap. Dropping the epoch there left the gap open for the
    // life of the process, however complete the coverage became.
    let root = temporary_root("repair-restart-keeps-epoch");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let mut stream: Box<dyn PublicStream> = Box::new(
        BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap(),
    );
    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState {
        repair: true,
        repair_epoch: Some(4),
        ..LaneState::default()
    };

    runner
        .handle_lane_completion(
            LaneCompletion::RepairFinished {
                end_ms: 100 * DAY_MS,
                epoch: None,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .unwrap();
    assert_eq!(
        lanes.repair_epoch,
        Some(4),
        "a finished repair leaves the live epoch for the next pass"
    );

    lanes.repair = false;
    runner
        .start_kline_repair(&lane_tx, &mut lanes, None)
        .unwrap();
    assert!(lanes.repair);
    assert_eq!(
        lanes.repair_epoch,
        Some(4),
        "an epoch-less restart keeps the epoch that closes the gap"
    );

    drop(stream);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn the_heartbeat_publishes_the_frame_age_limit_it_judges_itself_by() {
    // `stream_transport_healthy` decides `degraded` on the frame age and
    // the kline topic count. A reader that cannot see the limit cannot
    // name either clause, which is how an incident page ends up listing
    // only the consequences.
    let root = temporary_root("heartbeat-frame-limit");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let config = checked_demo_config();
    let runner = LiveRunner::new_with_universe(config.clone(), test_universe(), options).unwrap();
    let health = StreamHealth {
        connected: true,
        epoch: 1,
        last_frame_ts_ms: Some(100 * DAY_MS),
        ticker_capacity: 517,
        ticker_topics_accepted: 517,
        kline_topics_accepted: 516,
        ..StreamHealth::default()
    };

    runner.write_heartbeat("degraded", Some(health)).unwrap();

    let payload: Value =
        serde_json::from_slice(&std::fs::read(root.join("heartbeat.json")).unwrap()).unwrap();
    assert_eq!(
        payload["bybit_ws_max_frame_age_ms"],
        Value::from(config.sources.mark_max_age_ms)
    );
    assert_eq!(
        payload["bybit_ws_last_frame_ts_ms"],
        Value::from(100 * DAY_MS)
    );
    assert_eq!(payload["bybit_ws_kline_topics_accepted"], Value::from(516));
    assert_eq!(payload["bybit_ws_ticker_capacity"], Value::from(517));
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn the_heartbeat_publishes_when_the_carry_cycle_is_first_due() {
    // A reader that cannot see this instant reads a carry cycle frozen since
    // the decision boundary as a stall, and pages every realm once a day.
    let root = temporary_root("heartbeat-carry-due");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let config = checked_demo_config();
    let runner = LiveRunner::new_with_universe(config.clone(), test_universe(), options).unwrap();
    let boundary_ms = 1_788_912_000_000 + config.carry.decision_phase_ms;
    let expected_ms = boundary_ms
        + config.carry.decision_kline_lag_ms.max(
            FUNDING_PUBLICATION_LAG_MS + i64::try_from(config.live.funding_cadence_ms).unwrap(),
        );
    assert_eq!(
        runner.carry_cycle_not_before(boundary_ms + 1_000),
        Some(expected_ms),
        "the first carry cycle for a boundary waits for its funding print and its decision"
    );
    assert_eq!(
        runner.carry_cycle_not_before(boundary_ms - 1_000),
        Some(expected_ms - DAY_MS),
        "before the boundary the standing one is the previous day's"
    );

    runner.write_heartbeat("ready", None).unwrap();
    let payload: Value =
        serde_json::from_slice(&std::fs::read(root.join("heartbeat.json")).unwrap()).unwrap();
    assert!(
        payload["carry_cycle_not_before_wall_ts_ms"].is_i64(),
        "the heartbeat publishes the instant it judges the carry cycle by"
    );
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn runtime_ready_requires_exact_live_ticker_and_kline_topics() {
    let now_ms = 1_000_000;
    let mut health = StreamHealth {
        connected: true,
        gap_open: false,
        last_frame_ts_ms: Some(now_ms - 1),
        ticker_capacity: 2,
        ticker_coverage_complete: true,
        ticker_topics_accepted: 2,
        kline_topics_accepted: 2,
        ..StreamHealth::default()
    };
    assert_eq!(runtime_status(&health, false, now_ms, 30_000), "ready");

    health.kline_topics_accepted = 1;
    assert_eq!(runtime_status(&health, false, now_ms, 30_000), "degraded");
    health.kline_topics_accepted = 2;
    health.ticker_topics_quarantined = 1;
    assert_eq!(runtime_status(&health, false, now_ms, 30_000), "degraded");
    health.ticker_topics_quarantined = 0;
    health.ticker_coverage_complete = false;
    assert_eq!(runtime_status(&health, false, now_ms, 30_000), "degraded");
}

#[test]
fn transient_recovery_is_bounded_and_transport_failures_are_immediate() {
    let started_at_ms = 1_000_000;
    let mut health = StreamHealth {
        connected: true,
        gap_open: true,
        last_frame_ts_ms: Some(started_at_ms + 59_999),
        ticker_capacity: 2,
        ticker_coverage_complete: false,
        ticker_topics_accepted: 2,
        kline_topics_accepted: 2,
        ..StreamHealth::default()
    };
    let mut recovery_started_at_ms = None;

    assert!(transient_recovery_acceptable(
        &health,
        true,
        true,
        &mut recovery_started_at_ms,
        started_at_ms + 60_000,
    ));
    assert_eq!(recovery_started_at_ms, Some(started_at_ms + 60_000));
    assert!(!transient_recovery_acceptable(
        &health,
        true,
        true,
        &mut recovery_started_at_ms,
        started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
    ));

    health.ticker_coverage_complete = true;
    health.gap_open = false;
    assert!(transient_recovery_acceptable(
        &health,
        false,
        true,
        &mut recovery_started_at_ms,
        started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
    ));
    assert_eq!(recovery_started_at_ms, None);

    health.ticker_coverage_complete = false;
    assert!(!transient_recovery_acceptable(
        &health,
        false,
        false,
        &mut recovery_started_at_ms,
        started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
    ));
    assert_eq!(recovery_started_at_ms, None);
    assert!(stream_transport_healthy(
        &health,
        started_at_ms + 60_000,
        180_000,
    ));

    health.gap_open = true;
    health.last_frame_ts_ms = Some(started_at_ms + 59_999);
    let cycles = [
        CycleFreshness::on_cadence(Some(started_at_ms + 59_000), 60_000),
        CycleFreshness::on_cadence(Some(started_at_ms + 59_000), 60_000),
    ];
    let mut heartbeat_recovery = RecoveryState {
        transient_started_at_ms: None,
        reached_ready: true,
    };
    assert_eq!(
        heartbeat_status(
            &health,
            true,
            cycles,
            started_at_ms - STARTUP_MAX_MS,
            started_at_ms + 60_000,
            180_000,
            &mut heartbeat_recovery,
        ),
        "recovering"
    );
    health.last_frame_ts_ms = Some(started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS - 1);
    assert_eq!(
        heartbeat_status(
            &health,
            true,
            cycles,
            started_at_ms - STARTUP_MAX_MS,
            started_at_ms + 60_000 + TRANSIENT_RECOVERY_MAX_MS,
            180_000,
            &mut heartbeat_recovery,
        ),
        "degraded"
    );
}

#[test]
fn optional_whale_lane_never_blocks_a_carry_cycle() {
    let lanes = LaneState {
        instruments_ready: true,
        funding_ready: true,
        whales: true,
        ..LaneState::default()
    };
    assert!(!carry_required_lanes_pending(&lanes));
}

#[test]
fn source_pagination_grids_have_exact_per_job_row_ceilings() {
    let start = 100 * DAY_MS;
    let carry_end = start + crate::config::MAX_CARRY_SOURCE_HISTORY_HOURS * HOUR_MS;
    let widest_merged_kline_end =
        start + 3 * crate::config::MAX_CARRY_SOURCE_HISTORY_HOURS * HOUR_MS;
    assert_eq!(
        source_grid_slots(start, widest_merged_kline_end, HOUR_MS, false).unwrap(),
        13_104
    );
    assert_eq!(
        source_grid_slots(start, carry_end, HOUR_MS, true).unwrap(),
        4_369
    );
    let whale_end = start + crate::config::MAX_WHALE_FEED_DAYS as i64 * DAY_MS;
    assert_eq!(
        source_grid_slots(start, whale_end, super::FIVE_MIN_MS, true).unwrap(),
        8_641
    );
    validate_source_grid_timestamp(start, start, carry_end, HOUR_MS, false, "test kline").unwrap();
    assert!(validate_source_grid_timestamp(
        start + 1,
        start,
        carry_end,
        HOUR_MS,
        false,
        "test kline",
    )
    .is_err());
    assert!(validate_source_grid_timestamp(
        carry_end,
        start,
        carry_end,
        HOUR_MS,
        false,
        "test kline",
    )
    .is_err());
    validate_source_page_rows(200, 200, "test funding").unwrap();
    assert!(validate_source_page_rows(201, 200, "test funding").is_err());
}

#[test]
fn unaligned_cold_bootstrap_whale_bounds_keep_the_floor_point_fetchable() {
    let now_ms = 200 * DAY_MS + 12_345;
    let start_ms = now_ms - crate::config::MAX_WHALE_FEED_DAYS as i64 * DAY_MS;
    let (query_start_ms, query_end_ms, retained_row_cap) =
        whale_fetch_bounds(start_ms, now_ms).unwrap();

    assert!(query_start_ms < start_ms);
    assert_eq!(query_start_ms.rem_euclid(super::FIVE_MIN_MS), 0);
    assert_eq!(query_end_ms.rem_euclid(super::FIVE_MIN_MS), 0);
    assert_eq!(retained_row_cap, 8_640);
    validate_source_grid_timestamp(
        query_start_ms,
        query_start_ms,
        query_end_ms,
        super::FIVE_MIN_MS,
        true,
        "test whale",
    )
    .unwrap();
}

#[tokio::test(start_paused = true)]
async fn repair_fetch_waits_for_commit_ack_before_retaining_the_next_result() {
    assert_eq!(LANE_COMPLETION_QUEUE_CAPACITY, 1);

    let (lane_tx, mut lane_rx) = tokio::sync::mpsc::channel(1);
    let producer = tokio::spawn(async move {
        for _ in 0..2 {
            if !send_repair_chunk_and_wait(
                &lane_tx,
                Ok(FetchedKlineJobs {
                    batches: Vec::new(),
                    failures: Vec::new(),
                }),
            )
            .await
            {
                return false;
            }
        }
        true
    });

    let first = lane_rx.recv().await.expect("first repair result");
    assert!(!producer.is_finished());
    assert!(matches!(
        lane_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty)
    ));
    let LaneCompletion::RepairChunk { resume, .. } = first else {
        panic!("repair producer sent the wrong completion")
    };
    resume.send(true).expect("acknowledge first repair commit");

    let second = lane_rx.recv().await.expect("second repair result");
    assert!(!producer.is_finished());
    assert!(matches!(
        lane_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty)
    ));
    let LaneCompletion::RepairChunk { resume, .. } = second else {
        panic!("repair producer sent the wrong completion")
    };
    resume.send(false).expect("refuse second repair commit");
    assert!(!producer.await.expect("repair producer joins"));
}

#[tokio::test(start_paused = true)]
async fn whale_fetch_waits_for_commit_ack_before_retaining_the_next_result() {
    assert_eq!(LANE_COMPLETION_QUEUE_CAPACITY, 1);

    let (lane_tx, mut lane_rx) = tokio::sync::mpsc::channel(1);
    let producer = tokio::spawn(async move {
        for _ in 0..2 {
            if !send_whale_chunk_and_wait(
                &lane_tx,
                Ok(FetchedWhales {
                    available_at_ms: DAY_MS,
                    rows: Vec::new(),
                    coverage: Vec::new(),
                }),
            )
            .await
            {
                return false;
            }
        }
        true
    });

    let first = lane_rx.recv().await.expect("first whale result");
    assert!(!producer.is_finished());
    assert!(matches!(
        lane_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty)
    ));
    let LaneCompletion::WhaleChunk { resume, .. } = first else {
        panic!("whale producer sent the wrong completion")
    };
    resume.send(true).expect("acknowledge first whale commit");

    let second = lane_rx.recv().await.expect("second whale result");
    assert!(!producer.is_finished());
    assert!(matches!(
        lane_rx.try_recv(),
        Err(tokio::sync::mpsc::error::TryRecvError::Empty)
    ));
    let LaneCompletion::WhaleChunk { resume, .. } = second else {
        panic!("whale producer sent the wrong completion")
    };
    resume.send(false).expect("refuse second whale commit");
    assert!(!producer.await.expect("whale producer joins"));
}

#[tokio::test(start_paused = true)]
async fn a_realm_that_names_a_listing_venue_holds_until_the_listing_arrives() {
    let root = temporary_root("listing-filter");
    let _ = std::fs::remove_dir_all(&root);
    // The listing filter is a universe rule, not a realm: no checked-in realm
    // reads one venue's instruments while its engine trades another, so the
    // rule is set here on a realm whose rows the domain takes.
    let mut config = checked_demo_config();
    config.universe.listed_on = Some("hyperliquid".to_owned());
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let universe = crate::universe::unresolved_universe(
        &config.live.environment,
        crate::worker::realm_endpoint(&config),
    );
    let mut runner = LiveRunner::new_with_universe(config, universe, options).unwrap();
    assert_eq!(
        runner.listing_source.as_ref().map(|source| source.venue()),
        Some(super::ListingVenue::Hyperliquid)
    );
    let mut stream: Box<dyn PublicStream> =
        Box::new(BybitPublicStream::inert_for_test(vec!["BTCUSDT".into()]).unwrap());
    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let available_at_ms = 100 * DAY_MS;
    let fetched = |listing| {
        FetchedUniverseInputs {
            instruments: FetchedInstruments {
                observed_ts_ms: available_at_ms,
                available_at_ms,
                rows: vec![
                    instrument_wire("BTCUSDT", "Trading", DAY_MS, Some(0)),
                    instrument_wire("NOTONHLUSDT", "Trading", DAY_MS, Some(0)),
                ],
            },
            tickers: FetchedTickers {
                request_started_at_ms: available_at_ms,
                observed_ts_ms: available_at_ms,
                available_at_ms,
                // NOTONHL outranks BTC on turnover, so a rank taken over the
                // whole Bybit domain would put it in both sleeves.
                rows: vec![
                    turnover_ticker("BTCUSDT", "9000000"),
                    turnover_ticker("NOTONHLUSDT", "90000000"),
                ],
            },
            listing,
        }
    };
    let mut deliver =
        |runner: &mut LiveRunner,
         lanes: &mut LaneState,
         listing: Option<Result<BTreeSet<String>, WorkerError>>| {
            runner.handle_lane_completion(
                LaneCompletion::Instruments(Ok(fetched(listing))),
                LaneContext {
                    stream: &mut stream,
                    pending: &mut pending,
                    lane_tx: &lane_tx,
                    lanes,
                },
            )
        };
    let mut lanes = LaneState {
        instruments: true,
        funding: true,
        repair: true,
        ..LaneState::default()
    };

    deliver(
        &mut runner,
        &mut lanes,
        Some(Err(WorkerError::network("hyperliquid is unreachable"))),
    )
    .expect("a listing the venue would not give is retried, not fatal");
    assert!(!lanes.instruments, "the instrument cadence can retry");
    assert!(runner.listing_missing_reported);
    assert!(!crate::universe::universe_is_resolved(
        &runner.durable.worker().state().universe
    ));

    deliver(
        &mut runner,
        &mut lanes,
        Some(Ok(BTreeSet::from(["BTCUSDT".to_owned()]))),
    )
    .expect("the listing resolves the universe");
    assert!(!runner.listing_missing_reported);
    let resolved = &runner.durable.worker().state().universe;
    assert_eq!(resolved.symbols, ["BTCUSDT"]);
    assert_eq!(resolved.long_symbols, ["BTCUSDT"]);

    // The venue goes away again: the last good listing stands and the
    // membership does not move.
    deliver(
        &mut runner,
        &mut lanes,
        Some(Err(WorkerError::network("hyperliquid is unreachable"))),
    )
    .expect("a lost listing keeps the last one");
    assert!(runner.listing_missing_reported);
    assert_eq!(
        runner.durable.worker().state().universe.symbols,
        ["BTCUSDT"]
    );
    let _ = std::fs::remove_dir_all(&root);
}

#[tokio::test(start_paused = true)]
async fn malformed_source_lanes_retry_without_stopping_long() {
    let root = temporary_root("lane-source-errors");
    let _ = std::fs::remove_dir_all(&root);
    let universe = test_universe();
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), universe, options).unwrap();
    let mut stream: Box<dyn PublicStream> = Box::new(
        BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap(),
    );
    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState {
        instruments: true,
        tickers: true,
        funding: true,
        whales: true,
        repair: true,
        ..LaneState::default()
    };
    let available_at_ms = 100 * DAY_MS;

    runner
        .handle_lane_completion(
            LaneCompletion::Instruments(Ok(FetchedUniverseInputs {
                instruments: FetchedInstruments {
                    observed_ts_ms: available_at_ms,
                    available_at_ms,
                    rows: vec![instrument_wire(
                        "BTCUSDT",
                        "Trading",
                        DAY_MS,
                        Some(50 * DAY_MS),
                    )],
                },
                tickers: FetchedTickers {
                    request_started_at_ms: available_at_ms,
                    observed_ts_ms: available_at_ms,
                    available_at_ms,
                    rows: Vec::new(),
                },
                listing: None,
            })),
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("malformed instrument input stays lane-local");
    assert!(!lanes.instruments, "instrument cadence can retry the lane");

    runner
        .handle_lane_completion(
            LaneCompletion::Tickers(Ok(FetchedTickers {
                request_started_at_ms: available_at_ms,
                observed_ts_ms: available_at_ms,
                available_at_ms,
                rows: vec![ticker_wire_with_mark("not-a-number")],
            })),
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("malformed ticker input stays lane-local");
    assert!(!lanes.tickers, "ticker fallback can retry the lane");
    assert_eq!(runner.rest_ticker_failure_count, 1);

    let (funding_resume, funding_ack) = tokio::sync::oneshot::channel();
    runner
        .handle_lane_completion(
            LaneCompletion::FundingChunk {
                result: Ok(FetchedFunding {
                    batches: vec![(
                        "BTCUSDT".into(),
                        FetchedFundingBatch {
                            rows: vec![BybitFundingWire {
                                funding_rate_timestamp: Value::from(available_at_ms),
                                funding_rate: Value::from("not-a-number"),
                                funding_interval_hour: Some(Value::from(1)),
                            }],
                            available_at_ms,
                            checked_from_ms: None,
                            checked_through_ms: None,
                            emit_lifecycle: false,
                        },
                    )],
                    failures: Vec::new(),
                }),
                resume: funding_resume,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("malformed funding input stays lane-local");
    assert!(!funding_ack.await.expect("funding producer receives ack"));
    runner
        .handle_lane_completion(
            LaneCompletion::FundingFinished { succeeded: false },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .unwrap();
    assert!(!lanes.funding);
    assert!(!lanes.funding_ready);

    let (whale_resume, whale_ack) = tokio::sync::oneshot::channel();
    runner
        .handle_lane_completion(
            LaneCompletion::WhaleChunk {
                result: Ok(FetchedWhales {
                    available_at_ms,
                    rows: vec![BinanceWhaleWire {
                        symbol: "BTCUSDT".into(),
                        day_end_ms: Value::from(available_at_ms),
                        long_short_ratio: Some(Value::from("not-a-number")),
                    }],
                    coverage: Vec::new(),
                }),
                resume: whale_resume,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("malformed optional whale input stays lane-local");
    assert!(whale_ack.await.expect("whale producer receives ack"));
    runner
        .handle_lane_completion(
            LaneCompletion::WhaleFinished,
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .unwrap();
    assert!(!lanes.whales);

    let (repair_resume, repair_ack) = tokio::sync::oneshot::channel();
    runner
        .handle_lane_completion(
            LaneCompletion::RepairChunk {
                result: Ok(FetchedKlineJobs {
                    batches: vec![(
                        "BTCUSDT".into(),
                        FetchedKlineBatch {
                            rows: vec![vec![
                                Value::from(available_at_ms - HOUR_MS),
                                Value::from("not-a-number"),
                                Value::from("101"),
                                Value::from("99"),
                                Value::from("100"),
                                Value::from("1"),
                                Value::from("100"),
                            ]],
                            available_at_ms,
                            checked_from_ms: None,
                            checked_through_ms: None,
                        },
                    )],
                    failures: Vec::new(),
                }),
                resume: repair_resume,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("malformed repair input stays lane-local");
    assert!(!repair_ack.await.expect("repair producer receives ack"));
    runner
        .handle_lane_completion(
            LaneCompletion::RepairFinished {
                end_ms: available_at_ms,
                epoch: None,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .unwrap();
    assert!(!lanes.repair, "kline cadence can retry the repair lane");

    assert_eq!(runner.durable.worker().state().last_input_sequence, 0);
    runner
        .long_watermark(available_at_ms, Vec::new())
        .expect("LONG remains runnable after optional source failures");
    assert_eq!(runner.durable.worker().state().last_input_sequence, 1);

    drop(stream);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn an_instrument_refresh_held_off_by_funding_starts_when_that_pass_ends() {
    let root = temporary_root("instrument-cadence-deferred");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let mut stream: Box<dyn PublicStream> = Box::new(
        BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap(),
    );
    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState {
        funding: true,
        instruments_ready: true,
        ..LaneState::default()
    };

    // The hourly instrument tick lands inside a funding pass. Funding runs
    // every 60 s and instruments every hour, so dropping this tick is the
    // table standing still until the next one, and that one lands inside a
    // funding pass too.
    lanes.instruments_due = true;
    runner.start_instrument_lane_if_due(&lane_tx, &mut lanes);
    assert!(!lanes.instruments, "the funding pass still holds the venue");
    assert!(lanes.instruments_due, "the refresh is owed, not dropped");

    runner
        .handle_lane_completion(
            LaneCompletion::FundingFinished { succeeded: true },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .unwrap();
    assert!(!lanes.funding);
    assert!(
        lanes.instruments,
        "the owed instrument refresh starts as the funding pass ends"
    );
    assert!(!lanes.instruments_due);

    drop(stream);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn malformed_websocket_rows_open_a_repairable_gap_without_stopping_long() {
    let root = temporary_root("websocket-source-errors");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let mut stream: Box<dyn PublicStream> = Box::new(
        BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap(),
    );
    let available_at_ms = 100 * DAY_MS;

    runner
        .commit_stream_ticker_sample(
            &mut stream,
            TickerSample {
                observed_ts_ms: available_at_ms,
                available_at_ms,
                rows: vec![ticker_wire_with_mark("not-a-number")],
            },
        )
        .expect("malformed WebSocket ticker stays source-local");
    assert!(stream.health().gap_open);
    assert_eq!(stream.health().fault_count, 1);
    assert_eq!(runner.durable.worker().state().last_input_sequence, 0);

    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState::default();
    runner
        .handle_stream_event(
            StreamEvent::KlineClosed(crate::venue::ConfirmedKline {
                symbol: "BTCUSDT".into(),
                available_at_ms,
                row: vec![
                    Value::from(available_at_ms - HOUR_MS),
                    Value::from("not-a-number"),
                    Value::from("101"),
                    Value::from("99"),
                    Value::from("100"),
                    Value::from("1"),
                    Value::from("100"),
                ],
            }),
            &mut stream,
            &mut pending,
            64,
            &lane_tx,
            &mut lanes,
        )
        .expect("malformed WebSocket kline stays source-local");
    assert!(stream.health().gap_open);
    assert_eq!(stream.health().fault_count, 2);
    assert!(lanes.repair, "the REST repair lane is scheduled");
    assert_eq!(runner.durable.worker().state().last_input_sequence, 0);

    runner
        .long_watermark(available_at_ms, Vec::new())
        .expect("LONG remains runnable after WebSocket source faults");
    assert_eq!(runner.durable.worker().state().last_input_sequence, 1);

    drop(stream);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn revised_source_history_is_rejected_before_durable_mutation() {
    let root = temporary_root("lane-source-rewrite");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let available_at_ms = 100 * DAY_MS;
    let open_ts_ms = available_at_ms - HOUR_MS;
    runner
        .commit(WireEvent::BybitKlineBatch {
            schema_version: SCHEMA_VERSION,
            sequence: runner.next_sequence().unwrap(),
            symbol: "BTCUSDT".into(),
            available_at_ms,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            rows: vec![kline_wire(open_ts_ms, "100")],
        })
        .unwrap();
    runner
        .commit(WireEvent::BybitFundingBatch {
            schema_version: SCHEMA_VERSION,
            sequence: runner.next_sequence().unwrap(),
            symbol: "BTCUSDT".into(),
            available_at_ms,
            checked_from_ms: None,
            checked_through_ms: None,
            replace_coverage: false,
            emit_lifecycle: false,
            rows: vec![BybitFundingWire {
                funding_rate_timestamp: Value::from(available_at_ms),
                funding_rate: Value::from("0.001"),
                funding_interval_hour: Some(Value::from(1)),
            }],
        })
        .unwrap();
    runner
        .commit(WireEvent::BinanceWhaleBatch {
            schema_version: SCHEMA_VERSION,
            sequence: runner.next_sequence().unwrap(),
            available_at_ms,
            coverage: Vec::new(),
            rows: vec![BinanceWhaleWire {
                symbol: "BTCUSDT".into(),
                day_end_ms: Value::from(available_at_ms),
                long_short_ratio: Some(Value::from("1.2")),
            }],
        })
        .unwrap();
    let state_before = serde_json::to_vec(runner.durable.worker().state()).unwrap();
    let mut stream: Box<dyn PublicStream> = Box::new(
        BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap(),
    );
    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState {
        funding: true,
        whales: true,
        repair: true,
        ..LaneState::default()
    };

    let (funding_resume, funding_ack) = tokio::sync::oneshot::channel();
    runner
        .handle_lane_completion(
            LaneCompletion::FundingChunk {
                result: Ok(FetchedFunding {
                    batches: vec![(
                        "BTCUSDT".into(),
                        FetchedFundingBatch {
                            rows: vec![BybitFundingWire {
                                funding_rate_timestamp: Value::from(available_at_ms),
                                funding_rate: Value::from("0.002"),
                                funding_interval_hour: Some(Value::from(1)),
                            }],
                            available_at_ms,
                            checked_from_ms: None,
                            checked_through_ms: None,
                            emit_lifecycle: false,
                        },
                    )],
                    failures: Vec::new(),
                }),
                resume: funding_resume,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("revised funding stays lane-local");
    assert!(!funding_ack.await.unwrap());
    assert_eq!(
        serde_json::to_vec(runner.durable.worker().state()).unwrap(),
        state_before
    );

    let (whale_resume, whale_ack) = tokio::sync::oneshot::channel();
    runner
        .handle_lane_completion(
            LaneCompletion::WhaleChunk {
                result: Ok(FetchedWhales {
                    available_at_ms,
                    rows: vec![BinanceWhaleWire {
                        symbol: "BTCUSDT".into(),
                        day_end_ms: Value::from(available_at_ms),
                        long_short_ratio: Some(Value::from("1.3")),
                    }],
                    coverage: Vec::new(),
                }),
                resume: whale_resume,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("revised optional whale history stays lane-local");
    assert!(whale_ack.await.unwrap());
    assert_eq!(
        serde_json::to_vec(runner.durable.worker().state()).unwrap(),
        state_before
    );

    let (repair_resume, repair_ack) = tokio::sync::oneshot::channel();
    runner
        .handle_lane_completion(
            LaneCompletion::RepairChunk {
                result: Ok(FetchedKlineJobs {
                    batches: vec![(
                        "BTCUSDT".into(),
                        FetchedKlineBatch {
                            rows: vec![kline_wire(open_ts_ms, "101")],
                            available_at_ms,
                            checked_from_ms: None,
                            checked_through_ms: None,
                        },
                    )],
                    failures: Vec::new(),
                }),
                resume: repair_resume,
            },
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect("revised repair history stays lane-local");
    assert!(!repair_ack.await.unwrap());
    assert_eq!(
        serde_json::to_vec(runner.durable.worker().state()).unwrap(),
        state_before
    );

    lanes.repair = false;
    pending.insert(
        ("BTCUSDT".into(), open_ts_ms),
        crate::venue::ConfirmedKline {
            symbol: "BTCUSDT".into(),
            available_at_ms,
            row: kline_wire(open_ts_ms, "101"),
        },
    );
    assert!(!runner
        .flush_pending_klines_or_recover(&mut stream, &mut pending, &lane_tx, &mut lanes)
        .expect("a durable-history WS rewrite stays source-local"));
    assert!(pending.is_empty());
    assert!(stream.health().gap_open);
    assert!(
        lanes.repair,
        "the durable WS conflict schedules REST repair"
    );
    assert_eq!(
        serde_json::to_vec(runner.durable.worker().state()).unwrap(),
        state_before
    );

    drop(stream);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn durable_lane_commit_error_still_terminates_the_shared_loop() {
    let root = temporary_root("lane-durable-error");
    let _ = std::fs::remove_dir_all(&root);
    let state_dir = root.join("state");
    let options = LiveRunOptions {
        state_dir: state_dir.clone(),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let mut stream: Box<dyn PublicStream> = Box::new(
        BybitPublicStream::inert_for_test(vec!["BTCUSDT".into(), "ETHUSDT".into()]).unwrap(),
    );
    let mut pending = BTreeMap::new();
    let (lane_tx, _lane_rx) = tokio::sync::mpsc::channel(1);
    let mut lanes = LaneState {
        tickers: true,
        ..LaneState::default()
    };
    std::fs::remove_dir_all(&state_dir).unwrap();
    std::fs::write(&state_dir, b"block journal directory recreation").unwrap();
    let observed_ts_ms = 100 * DAY_MS;

    let error = runner
        .handle_lane_completion(
            LaneCompletion::Tickers(Ok(FetchedTickers {
                request_started_at_ms: observed_ts_ms,
                observed_ts_ms,
                available_at_ms: observed_ts_ms,
                rows: vec![ticker_wire_with_mark("100")],
            })),
            LaneContext {
                stream: &mut stream,
                pending: &mut pending,
                lane_tx: &lane_tx,
                lanes: &mut lanes,
            },
        )
        .expect_err("durable journal errors remain process-fatal");
    assert!(error.to_string().starts_with("io:"), "{error}");

    drop(stream);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

#[test]
fn only_source_failure_categories_are_lane_local() {
    assert!(WorkerError::input("bad venue row").is_lane_local_source_failure());
    assert!(WorkerError::network("venue unavailable").is_lane_local_source_failure());
    assert!(!WorkerError::state("broken invariant").is_lane_local_source_failure());
    assert!(!WorkerError::config("bad config").is_lane_local_source_failure());
    assert!(
        !WorkerError::io("durable write", std::io::Error::other("disk failure"))
            .is_lane_local_source_failure()
    );
}

#[test]
fn delivery_bounds_kline_funding_and_whale_plans_end_exclusively() {
    let launch = 10 * HOUR_MS + HOUR_MS / 2;
    let delivery = 20 * HOUR_MS + HOUR_MS / 2;
    let intervals = [InstrumentTradingInterval {
        trading_from_ms: launch,
        trading_through_ms: Some(delivery),
    }];
    assert_eq!(
        bounded_instrument_source_ranges(
            Some(&intervals),
            None,
            5 * HOUR_MS,
            30 * HOUR_MS,
            HOUR_MS,
        ),
        vec![(11 * HOUR_MS, 20 * HOUR_MS)]
    );
    assert_eq!(
        bounded_instrument_source_ranges(Some(&intervals), None, 0, 2 * DAY_MS, DAY_MS,),
        Vec::<(i64, i64)>::new(),
        "no complete UTC whale day exists inside a same-day listing interval"
    );
    assert!(trading_intervals_contain(
        Some(&intervals),
        None,
        delivery - 1
    ));
    assert!(!trading_intervals_contain(Some(&intervals), None, delivery));
    assert_eq!(
        bounded_instrument_source_ranges(
            Some(&intervals),
            Some(18 * HOUR_MS + HOUR_MS / 2),
            5 * HOUR_MS,
            30 * HOUR_MS,
            HOUR_MS,
        ),
        vec![(11 * HOUR_MS, 18 * HOUR_MS)],
        "an unknown authoritative status fails closed without inventing a delivery clock"
    );
}

#[test]
fn incomplete_whale_day_stays_uncovered_until_a_complete_row_arrives() {
    let start = 100 * crate::DAY_MS;
    let end = start + crate::DAY_MS;
    assert!(complete_whale_coverage("BTCUSDT", start, end, &[])
        .unwrap()
        .is_empty());
    let complete = complete_whale_coverage(
        "BTCUSDT",
        start,
        end,
        &[BinanceWhaleWire {
            symbol: "BTCUSDT".into(),
            day_end_ms: Value::from(end),
            long_short_ratio: Some(Value::from("1.2")),
        }],
    )
    .unwrap();
    assert_eq!(complete.len(), 1);
    assert_eq!(complete[0].checked_from_ms, start);
    assert_eq!(complete[0].checked_through_ms, end);
}

#[test]
fn funding_coverage_keeps_empty_late_and_internal_holes_retryable() {
    let start = 100 * HOUR_MS;
    let end = start + 6 * HOUR_MS;
    let row = |timestamp| BybitFundingWire {
        funding_rate_timestamp: Value::from(timestamp),
        funding_rate: Value::from("-0.001"),
        funding_interval_hour: Some(Value::from(1)),
    };
    assert!(complete_funding_coverage(start, end, HOUR_MS, &[])
        .unwrap()
        .is_empty());
    let late = (1..6)
        .map(|offset| row(start + offset * HOUR_MS))
        .collect::<Vec<_>>();
    assert_eq!(
        complete_funding_coverage(start, end, HOUR_MS, &late).unwrap(),
        vec![(start, end - HOUR_MS)]
    );
    let complete = (1..=6)
        .map(|offset| row(start + offset * HOUR_MS))
        .collect::<Vec<_>>();
    assert_eq!(
        complete_funding_coverage(start, end, HOUR_MS, &complete).unwrap(),
        vec![(start, end)]
    );
    let with_hole = complete
        .into_iter()
        .filter(|item| item.funding_rate_timestamp != start + 3 * HOUR_MS)
        .collect::<Vec<_>>();
    let intervals = complete_funding_coverage(start, end, HOUR_MS, &with_hole).unwrap();
    assert_eq!(intervals.len(), 2);
    assert!(intervals[0].1 < intervals[1].0);
    assert!(intervals
        .iter()
        .all(|(from, through)| !(*from <= start + 3 * HOUR_MS && *through >= start + 3 * HOUR_MS)));
}

#[test]
fn retained_fragmented_coverage_converges_without_refetching_a_known_run() {
    let base = 100 * HOUR_MS;
    let intervals = (0..6)
        .map(|index| CoverageInterval {
            checked_from_ms: base + index * 2 * HOUR_MS,
            checked_through_ms: base + (index * 2 + 1) * HOUR_MS,
        })
        .collect::<Vec<_>>();
    let coverage = BTreeMap::from([("BTCUSDT".to_owned(), intervals.clone())]);
    let empty = BTreeMap::new();

    for interval in &intervals {
        assert!(CoverageRef::new(&empty, &empty, &coverage).contains(
            "BTCUSDT",
            interval.checked_from_ms,
            interval.checked_through_ms
        ));
    }
    assert!(!CoverageRef::new(&empty, &empty, &coverage).contains(
        "BTCUSDT",
        base + HOUR_MS,
        base + 2 * HOUR_MS
    ));
}

#[test]
fn durable_carry_catchup_crosses_delivery_without_post_delivery_refetch() {
    let config = checked_demo_config();
    let universe = UniverseIdentity {
        mode: UniverseMode::Pit,
        environment: "demo".into(),
        endpoint: "api-demo.bybit.com".into(),
        snapshot_ts_ms: 100 * DAY_MS,
        available_at_ms: 100 * DAY_MS + 1,
        artifact_sha256: "1".repeat(64),
        file_sha256: "2".repeat(64),
        symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        long_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        carry_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
    };
    let mut worker = SignalWorker::with_universe(config.clone(), universe.clone()).unwrap();
    worker
        .apply(WireEvent::BybitInstrumentSnapshot {
            schema_version: SCHEMA_VERSION,
            sequence: 1,
            observed_ts_ms: 212 * DAY_MS,
            available_at_ms: 212 * DAY_MS,
            rows: vec![
                instrument_wire("BTCUSDT", "Closed", 100 * DAY_MS, Some(210 * DAY_MS)),
                instrument_wire("ETHUSDT", "Trading", 100 * DAY_MS, None),
            ],
        })
        .unwrap();
    let mut state = worker.state().clone();
    for symbol in ["BTCUSDT", "ETHUSDT"] {
        let klines = state.klines.entry(symbol.into()).or_default();
        for open_ts_ms in (100 * DAY_MS..212 * DAY_MS).step_by(HOUR_MS as usize) {
            klines.insert(
                open_ts_ms,
                HourlyKline {
                    symbol: symbol.into(),
                    open_ts_ms,
                    available_at_ms: open_ts_ms + HOUR_MS,
                    open: 100.0,
                    high: 102.0,
                    low: 99.0,
                    close: 100.0 + open_ts_ms as f64 / DAY_MS as f64,
                    volume_base: 1.0,
                    turnover_quote: 100.0,
                },
            );
        }
        let funding = state.funding.entry(symbol.into()).or_default();
        for settlement_ts_ms in (100 * DAY_MS + HOUR_MS..=212 * DAY_MS).step_by(HOUR_MS as usize) {
            funding.insert(
                settlement_ts_ms,
                SettledFunding {
                    symbol: symbol.into(),
                    settlement_ts_ms,
                    available_at_ms: settlement_ts_ms,
                    rate: -0.001,
                    funding_interval_min: 60,
                },
            );
        }
        let through = if symbol == "BTCUSDT" {
            210 * DAY_MS
        } else {
            212 * DAY_MS
        };
        state.kline_coverage_intervals.insert(
            symbol.into(),
            vec![CoverageInterval {
                checked_from_ms: 100 * DAY_MS,
                checked_through_ms: through,
            }],
        );
        state.funding_coverage_intervals.insert(
            symbol.into(),
            vec![CoverageInterval {
                checked_from_ms: 100 * DAY_MS,
                checked_through_ms: through,
            }],
        );
    }
    state.last_carry_decision_ts_ms = Some(207 * DAY_MS);
    state.last_carry_scorer_ts_ms = Some(207 * DAY_MS);
    state.last_observed_ts_ms = 212 * DAY_MS;
    state.bootstrap_coverage = Some(BootstrapCoverage {
        completed_at_ms: 212 * DAY_MS,
        kline_end_ms: 212 * DAY_MS,
        funding_end_ms: 212 * DAY_MS,
        whale_end_ms: 212 * DAY_MS,
        source_contract_sha256: state.source_contract_sha256.clone(),
        long_feature_sha256: state.long_feature_sha256.clone(),
        carry_feature_sha256: state.carry_feature_sha256.clone(),
    });

    let root = temporary_root("delivery-catchup");
    let state_dir = root.join("state");
    let spool_dir = root.join("spool");
    std::fs::create_dir_all(&state_dir).unwrap();
    AtomicJsonStore::new(state_dir.join("checkpoint.json"))
        .save(&state)
        .unwrap();
    let options = super::LiveRunOptions {
        state_dir,
        spool_dir,
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        super::LiveRunner::new_with_universe(config.clone(), universe.clone(), options.clone())
            .unwrap();
    assert!(runner.kline_repair_jobs(212 * DAY_MS).is_empty());
    assert!(!runner.needs_cold_bootstrap());

    let mut seen = BTreeMap::<i64, (Vec<String>, Vec<String>)>::new();
    for day in 208..=210 {
        let observations = runner
            .durable
            .apply_and_commit(WireEvent::CarryScorerCatchupWatermark {
                schema_version: SCHEMA_VERSION,
                sequence: runner.durable.worker().next_input_sequence().unwrap(),
                observed_ts_ms: 212 * DAY_MS,
                decision_through_ms: day * DAY_MS,
                gap_symbols: Vec::new(),
            })
            .unwrap();
        assert_eq!(observations.len(), 1);
        let envelope: SignalPayloadEnvelope =
            serde_json::from_slice(&observations[0].payload).unwrap();
        let ObservationPayload::CarryScorerCatchup {
            decision_ts_ms,
            rows,
            rejections,
        } = envelope.payload
        else {
            panic!("expected scorer-only CARRY catch-up");
        };
        seen.insert(
            decision_ts_ms,
            (
                rows.into_iter().map(|row| row.symbol).collect(),
                rejections.into_iter().map(|row| row.symbol).collect(),
            ),
        );
    }
    assert!(seen[&(208 * DAY_MS)].0.contains(&"BTCUSDT".into()));
    assert!(seen[&(209 * DAY_MS)].0.contains(&"BTCUSDT".into()));
    assert!(!seen[&(210 * DAY_MS)].0.contains(&"BTCUSDT".into()));
    assert!(seen[&(210 * DAY_MS)].1.contains(&"BTCUSDT".into()));

    drop(runner);
    let reopened = super::LiveRunner::new_with_universe(config, universe, options).unwrap();
    assert_eq!(
        reopened.durable.worker().state().last_carry_scorer_ts_ms,
        Some(210 * DAY_MS)
    );
    assert!(!reopened.needs_cold_bootstrap());
    assert!(reopened
        .kline_repair_jobs(212 * DAY_MS)
        .iter()
        .all(|(symbol, _, through)| symbol != "BTCUSDT" || *through <= 210 * DAY_MS));
    std::fs::remove_dir_all(root).unwrap();
}

fn turnover_ticker(symbol: &str, turnover: &str) -> BybitTickerWire {
    BybitTickerWire {
        symbol: symbol.into(),
        turnover24h: Some(Value::from(turnover)),
        ..ticker_wire_with_mark("100")
    }
}

fn checked_demo_config() -> SignalWorkerConfig {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
    SignalWorkerConfig::load(
        root.join("configs/signal-worker.demo.json"),
        root.join("configs/long_native_v12.json"),
        root.join("configs/lane2_carry_hold_v7.json"),
        root.join("configs/operational.json"),
        root.join("deploy/engine.demo.toml.template"),
    )
    .unwrap()
}

fn test_universe() -> UniverseIdentity {
    UniverseIdentity {
        mode: UniverseMode::Pit,
        environment: "demo".into(),
        endpoint: "api-demo.bybit.com".into(),
        snapshot_ts_ms: 100 * DAY_MS,
        available_at_ms: 100 * DAY_MS + 1,
        artifact_sha256: "1".repeat(64),
        file_sha256: "2".repeat(64),
        symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        long_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
        carry_symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
    }
}

fn ticker_wire_with_mark(mark: &str) -> BybitTickerWire {
    BybitTickerWire {
        symbol: "BTCUSDT".into(),
        mark_observed_ts_ms: None,
        funding_observed_ts_ms: None,
        schedule_observed_ts_ms: None,
        last_price: None,
        mark_price: Some(Value::from(mark)),
        index_price: None,
        bid1_price: None,
        ask1_price: None,
        bid1_size: None,
        ask1_size: None,
        open_interest: None,
        open_interest_value: None,
        turnover24h: None,
        volume24h: None,
        funding_rate: None,
        next_funding_time: None,
    }
}

fn kline_wire(open_ts_ms: i64, close: &str) -> Vec<Value> {
    vec![
        Value::from(open_ts_ms),
        Value::from("100"),
        Value::from("110"),
        Value::from("90"),
        Value::from(close),
        Value::from("1"),
        Value::from("100"),
    ]
}

fn instrument_wire(
    symbol: &str,
    status: &str,
    launch_time_ms: i64,
    delivery_time_ms: Option<i64>,
) -> BybitInstrumentWire {
    BybitInstrumentWire {
        symbol: symbol.into(),
        contract_type: Some("LinearPerpetual".into()),
        symbol_type: None,
        status: Some(status.into()),
        base_coin: Some(symbol.trim_end_matches("USDT").into()),
        quote_coin: Some("USDT".into()),
        settle_coin: Some("USDT".into()),
        launch_time: Some(Value::from(launch_time_ms)),
        delivery_time: delivery_time_ms.map(Value::from),
        price_filter: BTreeMap::new(),
        lot_size_filter: BTreeMap::new(),
        funding_interval: Some(Value::from(60)),
        is_pre_listing: false,
    }
}

/// Bybit's real shape: every perpetual carries `deliveryTime: "0"`. The
/// instrument lane must accept the venue's whole list, or the worker runs
/// with no instrument table at all.
#[test]
fn a_snapshot_of_perpetuals_with_zero_delivery_clocks_passes_source_validation() {
    let worker = crate::worker::SignalWorker::new(checked_demo_config()).unwrap();
    let observed = 100 * DAY_MS;
    let fetched = FetchedInstruments {
        observed_ts_ms: observed,
        available_at_ms: observed + 5,
        rows: vec![
            instrument_wire("BTCUSDT", "Trading", DAY_MS, Some(0)),
            instrument_wire("ETHUSDT", "Trading", DAY_MS, Some(0)),
            instrument_wire("ASPUSDT", "Trading", DAY_MS, Some(observed + DAY_MS)),
            instrument_wire("OLDUSDT", "Closed", DAY_MS, Some(observed - DAY_MS)),
        ],
    };
    validate_instrument_source_against_state(worker.state(), &fetched)
        .expect("the venue's own list is valid input");
}

/// The venue's own lists, when `LM_BYBIT_INSTRUMENTS_JSON` names them
/// (colon-separated `instruments-info` responses), through the same wire
/// parse and normalisation the lane uses. Run by hand:
/// `LM_BYBIT_INSTRUMENTS_JSON=a.json:b.json cargo test -p signal-worker -- --ignored the_venues_real`.
#[test]
#[ignore = "reads the venue's instrument lists from LM_BYBIT_INSTRUMENTS_JSON"]
fn the_venues_real_instrument_lists_normalize() {
    let paths = std::env::var("LM_BYBIT_INSTRUMENTS_JSON").expect("LM_BYBIT_INSTRUMENTS_JSON");
    let mut all = Vec::new();
    for path in paths.split(':') {
        let payload: Value =
            serde_json::from_slice(&std::fs::read(path).expect("readable file")).unwrap();
        for value in payload["result"]["list"].as_array().expect("result.list") {
            all.push(crate::venue::bybit::instrument_wire(value).expect("wire row"));
        }
    }
    let observed = 1_788_436_000_000;
    let (rows, rejected) =
        crate::normalize::normalize_instruments_reporting(observed, observed + 1, &all)
            .expect("whole list");
    eprintln!(
        "rows {} rejected {} ({:?})",
        rows.len(),
        rejected.rows.len(),
        rejected.summary("instrument")
    );
    let bad_reasons = rejected
        .rows
        .iter()
        .filter(|(_, reason)| !reason.contains("invalid symbol"))
        .collect::<Vec<_>>();
    assert!(
        bad_reasons.is_empty(),
        "rows refused for a reason other than a dated name: {bad_reasons:?}"
    );
    assert!(rows.len() > 800, "{}", rows.len());

    if let Ok(path) = std::env::var("LM_BYBIT_TICKERS_JSON") {
        let payload: Value =
            serde_json::from_slice(&std::fs::read(path).expect("readable file")).unwrap();
        let rows = payload["result"]["list"]
            .as_array()
            .expect("result.list")
            .iter()
            .map(|value| crate::venue::bybit::ticker_wire(value).expect("ticker wire row"))
            .collect::<Vec<_>>();
        let (kept, rejected) =
            crate::normalize::normalize_tickers_reporting(observed, observed + 1, &rows)
                .expect("whole ticker page");
        eprintln!(
            "tickers {} rejected {} ({:?})",
            kept.len(),
            rejected.rows.len(),
            rejected.summary("ticker")
        );
        let bad_reasons = rejected
            .rows
            .iter()
            .filter(|(_, reason)| !reason.contains("invalid symbol"))
            .collect::<Vec<_>>();
        assert!(bad_reasons.is_empty(), "{bad_reasons:?}");
        assert!(kept.len() > 800, "{}", kept.len());
    }
}

fn temporary_root(label: &str) -> PathBuf {
    static SEQUENCE: AtomicU64 = AtomicU64::new(0);
    std::env::temp_dir().join(format!(
        "signal-worker-live-{label}-{}-{}",
        std::process::id(),
        SEQUENCE.fetch_add(1, Ordering::Relaxed),
    ))
}

#[test]
fn the_gate_file_is_read_whole_and_an_absent_one_is_nothing() {
    let root = temporary_root("gate-file");
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir_all(&root).unwrap();
    let path = root.join("llm-gate-candidates.json");
    assert!(super::read_gate_candidates(&path).unwrap().is_none());
    std::fs::write(
        &path,
        serde_json::json!({
            "decision_ts_ms": 1_787_000_000_000_i64,
            "valid_until_ms": 1_787_003_600_000_i64,
            "events": [{
                "symbol": "aaausdt",
                "score": 7,
                "band": "wide",
                "trigger_ts_ms": 1_786_999_400_000_i64,
                "trigger_price": 10.0,
                "atr_pct": 0.05,
                "sigma_daily_30d": 0.03,
                "turnover_rank": 14,
                "trigger_window_h": 4,
                "a_future_field": "is ignored"
            }]
        })
        .to_string(),
    )
    .unwrap();
    let fetched = super::read_gate_candidates(&path).unwrap().unwrap();
    assert_eq!(fetched.decision_ts_ms, 1_787_000_000_000);
    assert_eq!(fetched.valid_until_ms, 1_787_003_600_000);
    assert!(fetched.read_at_ms > fetched.decision_ts_ms);
    assert_eq!(fetched.rows.len(), 1);
    let row = &fetched.rows[0];
    assert_eq!(row.symbol, "AAAUSDT");
    assert_eq!(row.score, 7.0);
    assert_eq!(row.band, "wide");
    assert_eq!(row.trigger_ts_ms, 1_786_999_400_000);
    assert_eq!(row.sigma_daily_30d, Some(0.03));
    assert_eq!(row.turnover_rank, Some(14.0));
    assert_eq!(row.trigger_window_h, Some(4));
    std::fs::write(&path, b"{not json").unwrap();
    let error = super::read_gate_candidates(&path).unwrap_err();
    assert!(error.is_lane_local_source_failure());
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn live_startup_waits_for_named_destinations_and_the_durable_successor_grant() {
    use engine_types::{
        ManagedSignalSource, SignalGenerationState, SignalLifecycleRequest,
        SignalLifecycleResponse, SignalProducerLifecycle, SignalProducerRoute,
        SignalSourceFrontier,
    };
    let root = temporary_root("named-startup");
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let mut runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options.clone())
            .unwrap();
    let before = serde_json::to_vec(runner.durable.worker().state()).unwrap();
    assert!(tokio::time::timeout(
        std::time::Duration::from_millis(100),
        runner.resolve_named_destinations()
    )
    .await
    .is_err());
    assert!(!runner.durable.destinations_verified());
    assert_eq!(
        serde_json::to_vec(runner.durable.worker().state()).unwrap(),
        before
    );
    let keys = [
        &runner.config.routing.carry_sleeve,
        &runner.config.routing.long_sleeve,
    ]
    .into_iter()
    .map(|key| engine_types::identity::SleeveKey::new(key.clone()).unwrap())
    .collect::<Vec<_>>();
    let request_store = AtomicJsonStore::new(
        options
            .spool_dir
            .join(engine_types::SIGNAL_READINESS_REQUEST_FILE),
    );
    request_store
        .save(&SignalLifecycleRequest {
            schema_version: 2,
            boot_nonce: "discover".into(),
            sleeve_keys: keys.clone(),
            producers: Vec::new(),
            legacy_sources: Vec::new(),
        })
        .unwrap();
    assert!(
        tokio::time::timeout(
            std::time::Duration::from_millis(100),
            runner.resolve_named_destinations()
        )
        .await
        .is_err(),
        "naming alone cannot publish before the epoch grant"
    );
    assert!(runner.durable.destinations_verified());
    let response: SignalLifecycleResponse = AtomicJsonStore::new(
        options
            .spool_dir
            .join(engine_types::SIGNAL_READINESS_RESPONSE_FILE),
    )
    .load()
    .unwrap()
    .unwrap();
    assert!(response.producer.sealed);
    assert!(response
        .producer
        .sources
        .iter()
        .all(|source| source.published_through == 0));
    let report = response.producer;
    let grant = SignalProducerLifecycle {
        producer: report.producer.clone(),
        retired_through: 0,
        active: Some(SignalGenerationState {
            epoch: 1,
            generation: report.generation.clone(),
            sealed: false,
            sources: report
                .sources
                .iter()
                .map(|source| SignalSourceFrontier {
                    source: ManagedSignalSource {
                        producer: &report.producer,
                        epoch: 1,
                        generation: &report.generation,
                        lane: engine_types::legacy_signal_lane(&report.producer, &source.source)
                            .unwrap(),
                    }
                    .encode()
                    .unwrap(),
                    destination: source.destination,
                    published_through: 0,
                })
                .collect(),
        }),
        routes: report
            .sources
            .iter()
            .map(|source| SignalProducerRoute {
                destination: source.destination,
                subscriptions: Vec::new(),
            })
            .collect(),
        legacy: Vec::new(),
        unresolved_tail: false,
        previous_seal: report.sources,
    };
    request_store
        .save(&SignalLifecycleRequest {
            schema_version: 2,
            boot_nonce: "grant".into(),
            sleeve_keys: keys,
            producers: vec![grant],
            legacy_sources: Vec::new(),
        })
        .unwrap();
    tokio::time::timeout(
        std::time::Duration::from_secs(1),
        runner.resolve_named_destinations(),
    )
    .await
    .unwrap()
    .unwrap();
    let state = runner.durable.worker().state();
    assert_eq!(state.signal_lifecycle.as_ref().unwrap().epoch, Some(1));
    assert!(!state.signal_lifecycle.as_ref().unwrap().sealed);
    assert_eq!(
        (
            state.long_output_sequence,
            state.carry_output_sequence,
            state.last_input_sequence
        ),
        (0, 0, 0)
    );
    std::fs::remove_dir_all(root).unwrap();
}

#[tokio::test(start_paused = true)]
async fn a_universe_refresh_hands_the_replacement_stream_the_old_transport_history() {
    // The hourly instrument lane replaces the stream whenever membership
    // moves. The gap stamp and the fault clocks belong to the process, not
    // to the stream object, so a page read seconds after a refresh reported
    // a seconds-old gap over an outage that had been open for hours.
    let root = temporary_root("reconfigure-keeps-transport-history");
    let _ = std::fs::remove_dir_all(&root);
    let options = LiveRunOptions {
        state_dir: root.join("state"),
        spool_dir: root.join("spool"),
        heartbeat: root.join("heartbeat.json"),
    };
    let runner =
        LiveRunner::new_with_universe(checked_demo_config(), test_universe(), options).unwrap();
    let outgoing: Box<dyn PublicStream> =
        Box::new(BybitPublicStream::inert_for_test(vec!["BTCUSDT".into()]).unwrap());
    let gap_opened_at_ms = 100 * DAY_MS;
    outgoing.mark_source_fault(gap_opened_at_ms);

    let (symbols, continuity) = runner
        .stream_reconfiguration(&*outgoing)
        .expect("a moved symbol set rebuilds the stream");

    assert!(symbols.contains(&"ETHUSDT".to_owned()));
    assert_eq!(
        continuity,
        StreamContinuity::from(&outgoing.health()),
        "the replacement continues the outgoing stream, it does not start a new one"
    );
    assert_eq!(continuity.gap_open_since_ms, Some(gap_opened_at_ms));
    assert!(continuity.gap_open);
    assert_eq!(continuity.fault_count, 1);
    assert_ne!(
        continuity,
        StreamContinuity::default(),
        "a fresh history is what reset the on-call page's gap clock"
    );

    drop(outgoing);
    drop(runner);
    std::fs::remove_dir_all(root).unwrap();
}

/// Bybit moved a carry symbol's `fundingInterval`, so the next pass stamped
/// every settlement already held with the new one and this gate read it as
/// rewritten venue history. The lane aborted on its first chunk every
/// minute and the carry cycle never completed again.
#[test]
fn a_changed_funding_interval_is_not_a_rewritten_settlement() {
    let settlement = 100 * DAY_MS;
    let mut state = SignalWorker::with_universe(checked_demo_config(), test_universe())
        .unwrap()
        .state()
        .clone();
    state.funding.entry("BTCUSDT".into()).or_default().insert(
        settlement,
        SettledFunding {
            symbol: "BTCUSDT".into(),
            settlement_ts_ms: settlement,
            available_at_ms: settlement,
            rate: -0.001,
            funding_interval_min: 480,
        },
    );
    let refetched = |rate: &str, hours: i64| FetchedFunding {
        batches: vec![(
            "BTCUSDT".to_owned(),
            FetchedFundingBatch {
                rows: vec![BybitFundingWire {
                    funding_rate_timestamp: Value::from(settlement),
                    funding_rate: Value::from(rate),
                    funding_interval_hour: Some(Value::from(hours)),
                }],
                available_at_ms: settlement + 5,
                checked_from_ms: Some(settlement),
                checked_through_ms: Some(settlement + 5),
                emit_lifecycle: false,
            },
        )],
        failures: Vec::new(),
    };

    validate_funding_source_against_state(&state, &refetched("-0.001", 4))
        .expect("a new instrument interval does not rewrite a settled rate");
    let error = validate_funding_source_against_state(&state, &refetched("-0.002", 8))
        .expect_err("a settled rate that moved is still a rewrite");
    assert!(error.to_string().contains("BTCUSDT"), "{error}");
}
