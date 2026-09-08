use super::tests::some_costs;
use super::*;

fn beat_with(costs: &Costs) -> serde_json::Value {
    let facts = Facts {
        may_open: true,
        market_events: 1,
        orders_sent: 1,
        strategies: &[],
        strategy_entries_enabled: &[],
        pending_flatten_requests: &[],
        decide: Quantiles::default(),
        durable: Quantiles::default(),
        wire: Quantiles::default(),
        ack: Quantiles::default(),
        dispatch_queue: Quantiles::default(),
        venue_task: Quantiles::default(),
        core_resume: Quantiles::default(),
        end_to_end: Quantiles::default(),
        barrier_wait: Quantiles::default(),
        quota_hold: Quantiles::default(),
        amends_confirmed: 0,
        amends_pulled_unconfirmed: 0,
        stream_resets: 0,
        uptime_s: 0,
        venue_clock_offset_ms: None,
        equity_usdt: 100.0,
        available_usdt: 100.0,
        account_age_ns: Some(1),
        account_metrics: None,
        holdings: &[],
        entry_blockers: &[],
        strategy_errors: &[],
        working_entries: &[],
        costs,
        rolling_loss: None,
    };
    let beat = Heartbeat::new("unused".into(), None, None);
    serde_json::from_str(&beat.render(&facts, 1_700_000_000_000)).expect("valid json")
}

#[test]
fn the_beat_says_what_the_fills_cost() {
    // The engine can be fast and still be trading badly. The latency pair
    // beside these cannot tell anybody which.
    let fields = beat_with(&some_costs());
    assert_eq!(fields["fills"], 1);
    assert_eq!(fields["fills_maker_share"], 1.0, "the one fill rested");
    assert_eq!(fields["fill_fee_coverage"], 1.0);
    assert_eq!(fields["fill_arrival_shortfall_bps"], 100.0);
    assert_eq!(
        fields["fill_all_in_arrival_bps"], 105.5,
        "plus 5.5 bp of fee"
    );
}

#[test]
fn nothing_filled_yet_reads_as_null_and_never_as_zero() {
    // A zero here would say "we checked, and it cost nothing", which is
    // the opposite of the truth about an engine that has not traded.
    let fields = beat_with(&Costs::default());
    assert_eq!(fields["fills"], 0, "the count is a real zero");
    for key in [
        "fills_maker_share",
        "fill_arrival_shortfall_bps",
        "fill_all_in_arrival_bps",
        "fill_fee_coverage",
        "fill_markout_1m_our_way_bps",
    ] {
        assert!(
            fields[key].is_null(),
            "{key} should be null: {}",
            fields[key]
        );
    }
}

#[test]
fn a_markout_that_has_not_come_round_is_null_beside_a_priced_fill() {
    // The arrival numbers land the moment a fill does; the markout waits
    // for its horizon. One being absent must not make the other absent.
    let fields = beat_with(&some_costs());
    assert!(fields["fill_markout_1m_our_way_bps"].is_null());
    assert!(!fields["fill_arrival_shortfall_bps"].is_null());
}
