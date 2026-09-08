use super::super::*;

pub(super) fn render(heartbeat: &Heartbeat, facts: &Facts, wall_ts_ms: i64) -> String {
    let taken = facts.account_age_ns.is_some();
    let mut fields: Vec<(&str, String)> = vec![
        (
            "account_available_usdt",
            or_null(taken.then(|| amount(facts.available_usdt))),
        ),
        (
            "account_equity_usdt",
            or_null(taken.then(|| amount(facts.equity_usdt))),
        ),
        (
            // When the venue reading was taken, on the same clock as
            // `wall_ts_ms` beside it. Deliberately not the engine's own
            // monotonic stamp: see `Facts::account_age_ns`.
            "account_observed_wall_ts_ms",
            or_null(
                facts
                    .account_age_ns
                    .map(|age_ns| (wall_ts_ms - (age_ns / 1_000_000) as i64).to_string()),
            ),
        ),
        (
            "account_user_id",
            or_null(heartbeat.account.as_ref().map(|a| quoted(&a.user_id))),
        ),
        (
            "decide_p50_ns",
            figure(facts.decide.count, facts.decide.p50_ns),
        ),
        (
            "decide_p99_ns",
            figure(facts.decide.count, facts.decide.p99_ns),
        ),
        (
            "decide_p999_ns",
            figure(facts.decide.count, facts.decide.p999_ns),
        ),
        (
            "durable_p50_ns",
            figure(facts.durable.count, facts.durable.p50_ns),
        ),
        (
            "durable_p99_ns",
            figure(facts.durable.count, facts.durable.p99_ns),
        ),
        (
            "durable_p999_ns",
            figure(facts.durable.count, facts.durable.p999_ns),
        ),
        ("entry_blockers", blockers(facts.entry_blockers)),
        ("strategy_errors", strategy_errors(facts.strategy_errors)),
        (
            "strategy_entries_enabled",
            strategy_permissions(facts.strategy_entries_enabled),
        ),
        (
            "pending_flatten_requests",
            pending_flatten_requests(facts.pending_flatten_requests),
        ),
        ("engine_commit", quoted(ENGINE_COMMIT)),
        ("engine_version", quoted(ENGINE_VERSION)),
        ("fills", facts.costs.fills.to_string()),
        (
            // Positive is adverse, throughout. The exception is the
            // markout below, and its key says so.
            "fill_all_in_arrival_bps",
            or_null(facts.costs.all_in_arrival_bps().map(bps)),
        ),
        (
            "fill_arrival_shortfall_bps",
            or_null(facts.costs.arrival_shortfall.mean().map(bps)),
        ),
        (
            "fill_fee_coverage",
            or_null(
                facts
                    .costs
                    .fee_coverage()
                    .map(|share| format!("{share:.4}")),
            ),
        ),
        (
            // The other way round: positive means the price moved our way
            // after the fill, so a persistently negative one is a book
            // being picked off. Named for its horizon, because a markout
            // without one is meaningless.
            "fill_markout_1m_our_way_bps",
            or_null(facts.costs.markout[2].mean().map(bps)),
        ),
        (
            "fills_maker_share",
            or_null(facts.costs.maker_share().map(|share| format!("{share:.4}"))),
        ),
        (
            "lease_path",
            or_null(
                heartbeat
                    .lease_path
                    .as_ref()
                    .map(|p| quoted(&p.display().to_string())),
            ),
        ),
        ("market_events", facts.market_events.to_string()),
        ("may_open", facts.may_open.to_string()),
        ("mode", quoted("live")),
        ("orders_sent", facts.orders_sent.to_string()),
        ("pid", std::process::id().to_string()),
        (
            "realm",
            or_null(heartbeat.account.as_ref().map(|a| quoted(&a.realm))),
        ),
        // `scripts/runtime/check_fleet_liveness.py` reads these names to
        // page the owner. `rolling_loss_tripped` is the only one that is
        // never null: it answers whether entries are held back, and that
        // has no unknown state.
        (
            "rolling_loss_limit_usdt",
            or_null(facts.rolling_loss.map(|window| amount(window.limit_usdt))),
        ),
        (
            "rolling_loss_net_usdt",
            or_null(
                facts
                    .rolling_loss
                    .filter(|window| window.trades > 0 || window.net_usdt != 0.0)
                    .map(|window| amount(window.net_usdt)),
            ),
        ),
        (
            "rolling_loss_trades",
            or_null(facts.rolling_loss.map(|window| window.trades.to_string())),
        ),
        (
            "rolling_loss_tripped",
            facts
                .rolling_loss
                .is_some_and(|window| window.tripped)
                .to_string(),
        ),
        (
            "rolling_loss_window_ms",
            or_null(
                facts
                    .rolling_loss
                    .map(|window| window.window_ms.to_string()),
            ),
        ),
        (
            "venue",
            or_null(heartbeat.account.as_ref().map(|a| quoted(&a.venue))),
        ),
        ("positions", positions(facts.holdings)),
        ("strategies", list(facts.strategies)),
        ("wall_ts_ms", wall_ts_ms.to_string()),
        ("wire_p50_ns", figure(facts.wire.count, facts.wire.p50_ns)),
        ("wire_p99_ns", figure(facts.wire.count, facts.wire.p99_ns)),
        ("wire_p999_ns", figure(facts.wire.count, facts.wire.p999_ns)),
        ("working_entries", working_entries(facts.working_entries)),
        ("ack_p50_ns", figure(facts.ack.count, facts.ack.p50_ns)),
        ("ack_p99_ns", figure(facts.ack.count, facts.ack.p99_ns)),
        ("ack_p999_ns", figure(facts.ack.count, facts.ack.p999_ns)),
        (
            "dispatch_queue_p50_ns",
            figure(facts.dispatch_queue.count, facts.dispatch_queue.p50_ns),
        ),
        (
            "dispatch_queue_p99_ns",
            figure(facts.dispatch_queue.count, facts.dispatch_queue.p99_ns),
        ),
        (
            "dispatch_queue_p999_ns",
            figure(facts.dispatch_queue.count, facts.dispatch_queue.p999_ns),
        ),
        (
            "venue_task_p50_ns",
            figure(facts.venue_task.count, facts.venue_task.p50_ns),
        ),
        (
            "venue_task_p99_ns",
            figure(facts.venue_task.count, facts.venue_task.p99_ns),
        ),
        (
            "venue_task_p999_ns",
            figure(facts.venue_task.count, facts.venue_task.p999_ns),
        ),
        (
            "core_resume_p50_ns",
            figure(facts.core_resume.count, facts.core_resume.p50_ns),
        ),
        (
            "core_resume_p99_ns",
            figure(facts.core_resume.count, facts.core_resume.p99_ns),
        ),
        (
            "core_resume_p999_ns",
            figure(facts.core_resume.count, facts.core_resume.p999_ns),
        ),
        (
            "end_to_end_p50_ns",
            figure(facts.end_to_end.count, facts.end_to_end.p50_ns),
        ),
        (
            "end_to_end_p99_ns",
            figure(facts.end_to_end.count, facts.end_to_end.p99_ns),
        ),
        (
            "end_to_end_p999_ns",
            figure(facts.end_to_end.count, facts.end_to_end.p999_ns),
        ),
        (
            "barrier_wait_p99_ns",
            figure(facts.barrier_wait.count, facts.barrier_wait.p99_ns),
        ),
        (
            "barrier_wait_p999_ns",
            figure(facts.barrier_wait.count, facts.barrier_wait.p999_ns),
        ),
        (
            "quota_hold_p99_ns",
            figure(facts.quota_hold.count, facts.quota_hold.p99_ns),
        ),
        (
            "quota_hold_p999_ns",
            figure(facts.quota_hold.count, facts.quota_hold.p999_ns),
        ),
        ("amends_confirmed", facts.amends_confirmed.to_string()),
        (
            "amends_pulled_unconfirmed",
            facts.amends_pulled_unconfirmed.to_string(),
        ),
        ("stream_resets", facts.stream_resets.to_string()),
        ("uptime_s", facts.uptime_s.to_string()),
        (
            "venue_clock_offset_ms",
            or_null(facts.venue_clock_offset_ms.map(|ms| ms.to_string())),
        ),
    ];
    fields.sort_by_key(|(key, _)| *key);
    let body: Vec<String> = fields
        .iter()
        .map(|(key, value)| format!("{}: {value}", quoted(key)))
        .collect();
    format!("{{{}}}\n", body.join(", "))
}

/// One JSON string. Every value in the file is a string, a number, a boolean,
/// null, or a list of strings, so this is most of the encoder.
fn quoted(text: &str) -> String {
    serde_json::to_string(text).expect("a string is always encodable as JSON")
}

/// One JSON number from a venue amount. A reading that is not finite is
/// written as null. NaN dressed as account evidence is unsafe for every
/// observer; null states "no reading".
fn amount(value: f64) -> String {
    if value.is_finite() {
        format!("{value}")
    } else {
        "null".to_string()
    }
}

fn or_null(value: Option<String>) -> String {
    value.unwrap_or_else(|| "null".to_string())
}

/// A basis-point figure, to two places. An unreal one is null rather than a
/// number JSON cannot hold.
fn bps(value: f64) -> String {
    if value.is_finite() {
        format!("{value:.2}")
    } else {
        "null".to_string()
    }
}

/// A latency figure, or null when nothing has been measured in this window.
/// Zero would read as "instant" rather than "no orders yet".
fn figure(count: u64, ns: u64) -> String {
    if count == 0 {
        "null".to_string()
    } else {
        ns.to_string()
    }
}

/// What is held, as an array of objects. Empty is a real answer and means
/// the account holds nothing -- not the same as the key being absent, which
/// would mean an engine too old to say.
fn positions(held: &[(String, Side, f64, f64, Option<String>)]) -> String {
    let rows: Vec<String> = held
        .iter()
        .map(|(symbol, side, qty, entry_px, strategy)| {
            format!(
                "{{{}: {}, {}: {}, {}: {}, {}: {}, {}: {}}}",
                quoted("symbol"),
                quoted(symbol),
                quoted("side"),
                quoted(match side {
                    Side::Buy => "long",
                    Side::Sell => "short",
                }),
                quoted("qty"),
                amount(*qty),
                quoted("entry_px"),
                amount(*entry_px),
                quoted("strategy"),
                or_null(strategy.as_ref().map(|name| quoted(name))),
            )
        })
        .collect();
    format!("[{}]", rows.join(", "))
}

fn list(names: &[String]) -> String {
    let names: Vec<String> = names.iter().map(|name| quoted(name.as_str())).collect();
    format!("[{}]", names.join(", "))
}

/// Why the asked-for names are not being opened, as an array of objects.
/// Empty is a real answer and means nothing is blocked -- not the same as the
/// key being absent, which would mean an engine too old to say.
fn blockers(rows: &[(String, String, String)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, symbol, reason)| {
            format!(
                "{{{}: {}, {}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("symbol"),
                quoted(symbol),
                quoted("reason"),
                quoted(reason)
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

fn strategy_permissions(rows: &[(String, bool)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, entries_enabled)| {
            format!(
                "{{{}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("entries_enabled"),
                entries_enabled
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

fn strategy_errors(rows: &[(String, String)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, error)| {
            format!(
                "{{{}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("error"),
                quoted(error),
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

fn pending_flatten_requests(rows: &[(String, String)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, request_id)| {
            format!(
                "{{{}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("request_id"),
                quoted(request_id),
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}

fn working_entries(rows: &[(String, String)]) -> String {
    let items: Vec<String> = rows
        .iter()
        .map(|(strategy, symbol)| {
            format!(
                "{{{}: {}, {}: {}}}",
                quoted("strategy"),
                quoted(strategy),
                quoted("symbol"),
                quoted(symbol),
            )
        })
        .collect();
    format!("[{}]", items.join(", "))
}
