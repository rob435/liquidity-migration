//! The fills, in a table.
//!
//! One row per sleeve and symbol, a total, and a footer that says which way
//! round each sign runs — because the markout's convention is the opposite of
//! everything beside it, and a bare column of numbers invites exactly the
//! wrong reading.

use engine_types::WalRecord;

use super::{Costs, Fills, HORIZONS_MS};

/// Read a log and say what its trading cost.
pub fn of_log(records: &[WalRecord]) -> String {
    table(&Fills::from_records(records))
}

/// The em dash a number that was never measured is printed as. Not a zero:
/// zero is a measurement, and this is the absence of one.
const NOTHING: &str = "—";

struct Column {
    head: &'static str,
    width: usize,
}

const COLUMNS: &[Column] = &[
    Column { head: "sleeve", width: 14 },
    Column { head: "symbol", width: 14 },
    Column { head: "fills", width: 6 },
    Column { head: "maker", width: 6 },
    Column { head: "traded USDT", width: 13 },
    Column { head: "fee bp", width: 8 },
    Column { head: "arrival bp", width: 11 },
    Column { head: "all-in bp", width: 10 },
    Column { head: "+1s", width: 8 },
    Column { head: "+15s", width: 8 },
    Column { head: "+1m", width: 8 },
    Column { head: "+5m", width: 8 },
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
        format!("{:>width$.2}", costs.notional_usdt, width = COLUMNS[4].width),
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
    use engine_types::{OrderKind, OrderRequest, OrderUpdate, Side, StrategyId, SymbolId};

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
                },
                wire_ns: 1,
                arrival_mid: 100.0,
            },
            WalRecord::OrderUpdate {
                update: OrderUpdate::Fill {
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
        assert!(text.contains("strategy 0"), "falls back to the number: {text}");
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
            },
            wire_ns: 2,
            arrival_mid: 0.0,
        });
        records.push(WalRecord::OrderUpdate {
            update: OrderUpdate::Fill {
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
        assert!(text.contains("long"), "the second sleeve is named too: {text}");
    }

    #[test]
    fn every_row_is_the_same_width_as_the_header() {
        let text = of_log(&log());
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
}
