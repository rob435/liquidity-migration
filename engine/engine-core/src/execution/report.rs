//! The fills, in a table.
//!
//! One row per sleeve and symbol, a total, and a footer that says which way
//! round each sign runs — because the markout's convention is the opposite of
//! everything beside it, and a bare column of numbers invites exactly the
//! wrong reading.

use std::collections::BTreeMap;

use engine_types::WalRecord;

use super::{Costs, Fills, HORIZONS_MS};
use crate::replay::LogNames;

/// Read a log and say what its trading cost, and what its positions made.
pub fn of_log(records: &[WalRecord]) -> String {
    let fills = Fills::from_records(records);
    format!(
        "{}\n{}\n{}",
        table(&fills),
        trips(&fills),
        quote_features(records)
    )
}

#[derive(Default)]
struct Mean {
    sum: f64,
    n: u64,
}

impl Mean {
    fn add(&mut self, value: Option<f64>) {
        if let Some(value) = value.filter(|value| value.is_finite()) {
            self.sum += value;
            self.n += 1;
        }
    }

    fn text(&self, width: usize) -> String {
        if self.n == 0 {
            return format!("{NOTHING:>width$}");
        }
        format!("{:>width$.2}", self.sum / self.n as f64)
    }
}

#[derive(Default)]
struct FeatureRow {
    fills: u64,
    makers: u64,
    flow: Mean,
    ratio: Mean,
    depth: Mean,
    spread: Mean,
    volatility: Mean,
    queue: Mean,
}

fn quote_features(records: &[WalRecord]) -> String {
    let mut names = LogNames::default();
    let mut rows: BTreeMap<(String, String), FeatureRow> = BTreeMap::new();
    for record in records {
        names.learn(record);
        let WalRecord::QuoteFill { features } = record else {
            continue;
        };
        let row = rows
            .entry((
                names.strategy(features.strategy),
                names.symbol(features.symbol),
            ))
            .or_default();
        row.fills += 1;
        row.makers += u64::from(features.is_maker);
        row.flow.add(features.flow_score);
        row.ratio.add(features.last_depth_ratio);
        row.depth.add(features.same_side_depth_usdt);
        row.spread.add(features.spread_bps);
        row.volatility.add(features.volatility_bps);
        row.queue.add(features.queue_ahead_usdt);
    }

    let mut out = String::from("what surrounded the quoter fills\n\n");
    out.push_str(
        "  sleeve        symbol         fills maker   flow   ratio  depth USDT spread bp  vol bp queue USDT\n",
    );
    if rows.is_empty() {
        out.push_str("\n  no quoter feature receipts yet.\n");
        return out;
    }
    for ((sleeve, symbol), row) in rows {
        out.push_str(&format!(
            "  {:<14}{:<14}{:>6}{:>6}{:>7}{:>8}{:>12}{:>10}{:>8}{:>11}\n",
            clipped(&sleeve, 13),
            clipped(&symbol, 13),
            row.fills,
            format!("{:.0}%", 100.0 * row.makers as f64 / row.fills as f64),
            row.flow.text(7),
            row.ratio.text(8),
            row.depth.text(12),
            row.spread.text(10),
            row.volatility.text(8),
            row.queue.text(11),
        ));
    }
    out.push_str(
        "\n  flow is positive for buyer aggression and negative for seller aggression.\n  depth and queue are the same-side public book visible when the fill arrived.\n",
    );
    out
}

/// The em dash a number that was never measured is printed as. Not a zero:
/// zero is a measurement, and this is the absence of one.
const NOTHING: &str = "—";

struct Column {
    head: &'static str,
    width: usize,
}

const COLUMNS: &[Column] = &[
    Column {
        head: "sleeve",
        width: 14,
    },
    Column {
        head: "symbol",
        width: 14,
    },
    Column {
        head: "fills",
        width: 6,
    },
    Column {
        head: "maker",
        width: 6,
    },
    Column {
        head: "traded USDT",
        width: 13,
    },
    Column {
        head: "fee bp",
        width: 8,
    },
    Column {
        head: "arrival bp",
        width: 11,
    },
    Column {
        head: "all-in bp",
        width: 10,
    },
    Column {
        head: "+1s",
        width: 8,
    },
    Column {
        head: "+15s",
        width: 8,
    },
    Column {
        head: "+1m",
        width: 8,
    },
    Column {
        head: "+5m",
        width: 8,
    },
];

pub fn table(fills: &Fills) -> String {
    let mut out = String::from("what the fills cost\n\n");
    out.push_str("  ");
    for (index, column) in COLUMNS.iter().enumerate() {
        // The two name columns read left, every number reads right.
        if index < 2 {
            out.push_str(&format!("{:<width$}", column.head, width = column.width));
        } else {
            out.push_str(&format!("{:>width$}", column.head, width = column.width));
        }
    }
    out.push('\n');

    let mut rows = 0usize;
    for (sleeve, symbol, costs) in fills.rows() {
        rows += 1;
        out.push_str(&row(sleeve, symbol, costs));
    }
    if rows == 0 {
        out.push_str("\n  nothing has filled yet.\n");
        return out;
    }

    let total = fills.total();
    out.push_str(&format!("  {}\n", "-".repeat(width_of_all() - 2)));
    out.push_str(&row("everything", "", &total));
    out.push_str(&footer(&total, fills));
    out
}

fn width_of_all() -> usize {
    COLUMNS.iter().map(|c| c.width).sum::<usize>() + 2
}

fn row(sleeve: &str, symbol: &str, costs: &Costs) -> String {
    let mut cells = vec![
        format!("{:<width$}", clipped(sleeve, 13), width = COLUMNS[0].width),
        format!("{:<width$}", clipped(symbol, 13), width = COLUMNS[1].width),
        format!("{:>width$}", costs.fills, width = COLUMNS[2].width),
        format!(
            "{:>width$}",
            costs
                .maker_share()
                .map(|share| format!("{:.0}%", share * 100.0))
                .unwrap_or_else(|| NOTHING.to_string()),
            width = COLUMNS[3].width
        ),
        format!(
            "{:>width$.2}",
            costs.notional_usdt,
            width = COLUMNS[4].width
        ),
        bps(costs.fee.mean(), COLUMNS[5].width),
        bps(costs.arrival_shortfall.mean(), COLUMNS[6].width),
        bps(costs.all_in_arrival_bps(), COLUMNS[7].width),
    ];
    for (index, _) in HORIZONS_MS.iter().enumerate() {
        cells.push(bps(costs.markout[index].mean(), COLUMNS[8 + index].width));
    }
    format!("  {}\n", cells.join(""))
}

fn bps(value: Option<f64>, width: usize) -> String {
    match value {
        Some(v) => format!("{v:>width$.2}"),
        None => format!("{NOTHING:>width$}"),
    }
}

fn clipped(text: &str, width: usize) -> String {
    if text.chars().count() <= width {
        return text.to_string();
    }
    text.chars().take(width).collect()
}

/// What the closed positions made, per sleeve.
///
/// The other half of the question the table above answers. Costs say what the
/// trading paid away; this says whether the trading was worth doing.
pub fn trips(fills: &Fills) -> String {
    let mut out = String::from("what the positions made\n\n");
    out.push_str("  sleeve            trips     won      net USDT          best         worst\n");
    let mut sleeves: Vec<&str> = fills.closed().iter().map(|t| t.sleeve.as_str()).collect();
    sleeves.sort_unstable();
    sleeves.dedup();
    if sleeves.is_empty() {
        out.push_str("\n  nothing has closed yet.\n");
        return out;
    }
    for sleeve in sleeves {
        out.push_str(&trip_row(sleeve, fills, |trade| trade.sleeve == sleeve));
    }
    out.push_str(&format!("  {}\n", "-".repeat(74)));
    out.push_str(&trip_row("everything", fills, |_| true));
    out.push_str(&trip_list(fills));
    out.push_str(
        "\n  the crowd fee (funding) is NOT in these numbers. The venue settles it\n  into the wallet on its own clock and tells the engine nothing about it, so\n  a net that carried it would be an estimate in a receipt's clothes.\n",
    );
    // A trip whose opening fills are in an older segment has no money on it.
    // Counting it as a zero would drag every average toward nothing.
    let unpriced = fills
        .closed()
        .iter()
        .filter(|trade| trade.round_trip.is_none())
        .count();
    if unpriced > 0 {
        out.push_str(&format!(
            "  {unpriced} close(s) are left out: this log does not hold what opened them.\n"
        ));
    }
    out
}

/// The newest trips one to a line, so a number in the table above can be
/// taken apart.
fn trip_list(fills: &Fills) -> String {
    const SHOWN: usize = 30;
    let closed = fills.closed();
    let skipped = closed.len().saturating_sub(SHOWN);
    let mut out = String::from(
        "\n  sleeve        symbol          side      qty         in        out       held   net USDT\n",
    );
    for trade in closed.iter().skip(skipped) {
        let rt = trade.round_trip.as_ref();
        out.push_str(&format!(
            "  {:<14}{:<15}{:<7}{:>9}{:>11}{:>11}{:>11}{:>11}\n",
            clipped(&trade.sleeve, 13),
            clipped(&trade.symbol, 14),
            trade.side,
            figure(trade.qty),
            rt.map(|rt| figure(rt.entry_px))
                .unwrap_or_else(|| NOTHING.into()),
            figure(trade.exit_px),
            rt.map(|rt| held(rt.held_ms))
                .unwrap_or_else(|| NOTHING.into()),
            rt.map(|rt| format!("{:+.2}", rt.net_usdt))
                .unwrap_or_else(|| NOTHING.into()),
        ));
    }
    if skipped > 0 {
        out.push_str(&format!(
            "  ...and {skipped} older trip(s), counted above but not listed.\n"
        ));
    }
    out
}

/// A price or a quantity at the precision it needs, since this fleet trades
/// both 100,000 of a coin worth 0.0037 and 0.05 of one worth 800.
fn figure(value: f64) -> String {
    let text = match value.abs() {
        v if v >= 1_000.0 => format!("{value:.0}"),
        v if v >= 1.0 => format!("{value:.4}"),
        v if v > 0.0 => format!("{value:.8}"),
        _ => format!("{value}"),
    };
    // A trailing run of zeros is precision this number does not have.
    if text.contains('.') {
        return text.trim_end_matches('0').trim_end_matches('.').to_string();
    }
    text
}

fn held(ms: i64) -> String {
    let seconds = ms.max(0) / 1_000;
    let (days, hours, minutes) = (
        seconds / 86_400,
        (seconds % 86_400) / 3_600,
        (seconds % 3_600) / 60,
    );
    if days > 0 {
        return format!("{days}d{hours}h");
    }
    if hours > 0 {
        return format!("{hours}h{minutes}m");
    }
    format!("{minutes}m")
}

fn trip_row(
    label: &str,
    fills: &Fills,
    keep: impl Fn(&super::roundtrip::ClosedTrade) -> bool,
) -> String {
    let nets: Vec<f64> = fills
        .closed()
        .iter()
        .filter(|trade| keep(trade))
        .filter_map(|trade| trade.round_trip.as_ref().map(|rt| rt.net_usdt))
        .collect();
    if nets.is_empty() {
        return format!(
            "  {:<18}{:>5}{:>8}{:>14}{:>14}{:>14}\n",
            clipped(label, 17),
            0,
            NOTHING,
            NOTHING,
            NOTHING,
            NOTHING
        );
    }
    let won = nets.iter().filter(|net| **net > 0.0).count();
    let total: f64 = nets.iter().sum();
    let best = nets.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let worst = nets.iter().cloned().fold(f64::INFINITY, f64::min);
    format!(
        "  {:<18}{:>5}{:>7.0}%{:>14.2}{:>14.2}{:>14.2}\n",
        clipped(label, 17),
        nets.len(),
        100.0 * won as f64 / nets.len() as f64,
        total,
        best,
        worst
    )
}

fn footer(total: &Costs, fills: &Fills) -> String {
    let mut out = String::from(
        "\n  fee, arrival and all-in are positive when they cost us.\n  \
         the markout columns are the other way round: positive means the price\n  \
         moved our way after the fill, and persistently negative is being picked off.\n",
    );
    match total.arrival_coverage() {
        // Said only when it is not the whole thing. A line claiming full
        // coverage on every report would stop being read.
        Some(share) if share < 0.999 => out.push_str(&format!(
            "  the arrival columns cover {:.0}% of what traded; the rest filled against a\n  \
             book the engine could not read when the order left.\n",
            share * 100.0
        )),
        _ => {}
    }
    if total.marks_unmeasurable > 0 {
        out.push_str(&format!(
            "  {} markout(s) had no readable book inside the lateness bound and were\n  \
             never measured.\n",
            total.marks_unmeasurable
        ));
    }
    if total.marks_late > 0 {
        out.push_str(&format!(
            "  {} markout(s) were read too long after their horizon to be that horizon,\n  \
             and were thrown away rather than averaged in.\n",
            total.marks_late
        ));
    }
    // Deliberately not reported off a log: a replay owes nothing a future
    // mark and drops nothing, so both counters are always zero there. They
    // belong to a running engine, and this footer is written for both.
    if fills.dropped > 0 {
        out.push_str(&format!(
            "  {} fill(s) were dropped from the markout queue: too many were waiting at\n  \
             once.\n",
            fills.dropped
        ));
    }
    if fills.pending() > 0 {
        out.push_str(&format!(
            "  {} fill(s) are still waiting for a horizon to come round.\n",
            fills.pending()
        ));
    }
    if fills.stream_gaps > 0 {
        out.push_str(&format!(
            "  the private stream reconnected {} time(s), and delivered nothing while it was\n  \
             down.\n",
            fills.stream_gaps
        ));
    }
    if fills.recovered > 0 {
        out.push_str(&format!(
            "  {} fill(s) reached this account only through the venue's own execution\n  \
             history, and are counted here. One of them has a markout only where its\n  \
             horizon had not already passed by the time it was found.\n",
            fills.recovered
        ));
    } else if fills.stream_gaps > 0 {
        out.push_str(
            "  nothing was read back off the venue, so a fill inside one of those gaps is\n  \
             in none of these numbers.\n",
        );
    }
    // The later columns are measured over fewer fills than the earlier ones,
    // for two quite different reasons, and the difference matters: one is a
    // run that simply has not aged yet, the other is measurement that was lost
    // and will not come back. Naming only the alarming one would have this
    // line cry wolf on every short run, so it names both and leaves the
    // reader to tell which they are looking at.
    if total.markout[0].weight > total.markout[HORIZONS_MS.len() - 1].weight {
        out.push_str(
            "  the later horizons cover less of the trading than the earlier ones, because\n  \
             a fill is owed its five-minute mark five minutes later. The run is younger\n  \
             than that, or it restarted -- a restart ends every horizon a fill was still\n  \
             owed, and restarts cluster on deploys -- or this is one segment of a log\n  \
             whose next segment holds the marks that are missing.\n",
        );
    }
    out
}

#[cfg(test)]
mod tests {
    use engine_types::{
        OrderKind, OrderRequest, OrderUpdate, QuoteFillFeatures, Side, StrategyId, SymbolId,
    };

    use super::*;

    fn log() -> Vec<WalRecord> {
        vec![
            WalRecord::Names {
                strategies: vec!["carry".into(), "long".into()],
                symbols: vec!["BTCUSDT".into(), "ETHUSDT".into()],
            },
            WalRecord::OrderSent {
                request: OrderRequest {
                    client_order_id: "eng-1".into(),
                    strategy: StrategyId(0),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: false,
                    close_position: false,
                },
                wire_ns: 1,
                arrival_mid: 100.0,
            },
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill {
                    exec_id: String::new(),
                    client_order_id: "eng-1".into(),
                    symbol: SymbolId(0),
                    side: Side::Buy,
                    qty: 1.0,
                    px: 101.0,
                    fee: 0.0555,
                    is_maker: true,
                    venue_ts_ms: 1,
                    recv_ns: 1,
                },
            },
        ]
    }

    #[test]
    fn the_table_names_the_sleeve_and_the_coin_not_their_numbers() {
        // The whole reason the log records its own id tables. "strategy 0
        // traded symbol 0" is not a report anybody can act on.
        let text = of_log(&log());
        assert!(text.contains("carry"), "{text}");
        assert!(text.contains("BTCUSDT"), "{text}");
        assert!(!text.contains("strategy 0"), "{text}");
        assert!(!text.contains("symbol 0"), "{text}");
    }

    #[test]
    fn a_log_that_never_said_what_its_ids_meant_still_reads() {
        let mut anonymous = log();
        anonymous.remove(0);
        let text = of_log(&anonymous);
        assert!(
            text.contains("strategy 0"),
            "falls back to the number: {text}"
        );
        assert!(text.contains("symbol 0"), "{text}");
    }

    #[test]
    fn the_numbers_are_there_and_the_signs_are_explained() {
        let text = of_log(&log());
        // 100 bp adverse on the arrival, 5.5 bp of fee, so 105.5 all in.
        assert!(text.contains("100.00"), "{text}");
        assert!(text.contains("5.50"), "{text}");
        assert!(text.contains("105.50"), "{text}");
        assert!(text.contains("100%"), "the fill rested: {text}");
        assert!(text.contains("positive when they cost us"), "{text}");
        assert!(text.contains("moved our way"), "{text}");
    }

    #[test]
    fn a_horizon_that_was_never_marked_is_a_dash_and_not_a_zero() {
        let text = of_log(&log());
        assert!(text.contains(NOTHING), "an unmarked horizon: {text}");
    }

    #[test]
    fn an_empty_log_says_so_instead_of_printing_an_empty_table() {
        let text = of_log(&[]);
        assert!(text.contains("nothing has filled yet"), "{text}");
    }

    #[test]
    fn partial_coverage_is_confessed_in_the_footer() {
        // Half the trading had no book to be measured against. A report that
        // did not say so would read as a complete picture.
        let mut records = log();
        records.push(WalRecord::OrderSent {
            request: OrderRequest {
                client_order_id: "eng-2".into(),
                strategy: StrategyId(1),
                symbol: SymbolId(1),
                side: Side::Buy,
                qty: 1.0,
                kind: OrderKind::Market,
                stop: None,
                reduce_only: false,
                close_position: false,
            },
            wire_ns: 2,
            arrival_mid: 0.0,
        });
        records.push(WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
                exec_id: String::new(),
                client_order_id: "eng-2".into(),
                symbol: SymbolId(1),
                side: Side::Buy,
                qty: 1.0,
                px: 101.0,
                fee: 0.0,
                is_maker: false,
                venue_ts_ms: 2,
                recv_ns: 2,
            },
        });
        let text = of_log(&records);
        assert!(text.contains("cover 50%"), "{text}");
        assert!(
            text.contains("long"),
            "the second sleeve is named too: {text}"
        );
    }

    #[test]
    fn every_row_is_the_same_width_as_the_header() {
        // The cost table alone: the trips table below it has its own header
        // and its own widths.
        let text = table(&Fills::from_records(&log()));
        let lines: Vec<&str> = text
            .lines()
            .filter(|l| l.starts_with("  ") && (l.contains("sleeve") || l.contains("BTCUSDT")))
            .collect();
        assert_eq!(lines.len(), 2, "a header and one row: {lines:?}");
        assert_eq!(
            lines[0].chars().count(),
            lines[1].chars().count(),
            "columns line up:\n{}\n{}",
            lines[0],
            lines[1]
        );
    }

    #[test]
    fn quoter_fill_features_are_named_and_missing_values_stay_missing() {
        let mut records = log();
        records.push(WalRecord::QuoteFill {
            features: QuoteFillFeatures {
                strategy: StrategyId(0),
                symbol: SymbolId(0),
                exec_id: "exec-1".into(),
                client_order_id: "eng-1".into(),
                side: Side::Buy,
                is_maker: true,
                recv_ns: 10,
                flow_fast: Some(-0.4),
                flow_slow: Some(-0.2),
                flow_score: Some(-0.33),
                last_depth_ratio: Some(-0.5),
                same_side_depth_usdt: Some(123.0),
                spread_bps: Some(6.0),
                volatility_bps: None,
                queue_ahead_usdt: Some(80.0),
            },
        });
        let text = quote_features(&records);
        assert!(text.contains("carry"), "{text}");
        assert!(text.contains("BTCUSDT"), "{text}");
        assert!(text.contains("-0.33"), "{text}");
        assert!(
            text.contains(NOTHING),
            "missing volatility is not zero: {text}"
        );
    }
}
