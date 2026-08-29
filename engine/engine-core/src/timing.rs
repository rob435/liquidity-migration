//! What each step of the order path actually took, read back from the log.
//!
//! The live ledger keeps a 60-second rolling histogram and writes p50 and p99
//! of it. That is the glance. This is the reconstruction: every
//! [`WalRecord::VenueTiming`] the run wrote, split into the steps between its
//! stamps, grouped by what the command was, and reported at the tail as well
//! as the middle. A p99 over a minute of a quiet market says nothing about
//! the stall that cost a fill.
//!
//! The split that matters most is the third one. Time inside the venue task
//! is not all the venue's: the adapter holds a command back to stay inside
//! the request quota, and that wait is charged separately from the round
//! trip. They ask for opposite fixes — a slow round trip is the network or
//! the matching engine, a long hold is a quota to raise or a strategy asking
//! for more requests than it has — and a single "venue task" number cannot
//! tell them apart.

use std::collections::BTreeMap;

use engine_types::WalRecord;

use crate::ledger::pretty;

/// One measurable step between two stamps on the same command.
#[derive(Copy, Clone, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum Step {
    Queue,
    Paced,
    Encode,
    Venue,
    Reply,
    Resume,
    Total,
}

impl Step {
    pub const ALL: [Step; 7] = [
        Step::Queue,
        Step::Paced,
        Step::Encode,
        Step::Venue,
        Step::Reply,
        Step::Resume,
        Step::Total,
    ];

    pub fn plain_name(self) -> &'static str {
        match self {
            Step::Queue => "wait for the venue task",
            Step::Paced => "held back for quota",
            Step::Encode => "sign and write",
            Step::Venue => "venue round trip",
            Step::Reply => "read the reply",
            Step::Resume => "back into the engine",
            Step::Total => "all of it",
        }
    }
}

/// The quantiles this report answers at, and the tail it exists for.
const QUANTILES: [f64; 4] = [0.50, 0.90, 0.99, 0.999];

/// Nearest-rank on the sorted samples. Offline and exact: there is no reason
/// to carry a histogram's approximation error into a report that already has
/// every sample in memory.
fn quantile(sorted: &[u64], q: f64) -> u64 {
    if sorted.is_empty() {
        return 0;
    }
    let rank = (q * sorted.len() as f64).ceil().max(1.0) as usize;
    sorted[rank.min(sorted.len()) - 1]
}

/// Every sample of one step of one operation.
#[derive(Default, Clone)]
pub struct Samples {
    values: Vec<u64>,
}

impl Samples {
    fn push(&mut self, ns: u64) {
        self.values.push(ns);
    }

    pub fn count(&self) -> usize {
        self.values.len()
    }

    /// count, the four quantiles, and the worst one seen.
    pub fn summary(&self) -> (usize, [u64; 4], u64) {
        let mut sorted = self.values.clone();
        sorted.sort_unstable();
        let mut marks = [0u64; 4];
        for (slot, q) in marks.iter_mut().zip(QUANTILES) {
            *slot = quantile(&sorted, q);
        }
        (sorted.len(), marks, sorted.last().copied().unwrap_or(0))
    }
}

/// One log's timing records, split by operation and step.
#[derive(Default)]
pub struct Timings {
    by_operation: BTreeMap<String, BTreeMap<Step, Samples>>,
    /// Records whose adapter exposed no transport stamps, so the venue round
    /// trip could not be separated from the rest of the task.
    unstamped: BTreeMap<String, usize>,
}

impl Timings {
    pub fn from_records(records: &[WalRecord]) -> Self {
        let mut out = Self::default();
        for record in records {
            let WalRecord::VenueTiming {
                operation,
                queued_ns,
                task_started_ns,
                socket_write_ns,
                ack_ns,
                rate_wait_ns,
                task_completed_ns,
                core_handled_ns,
                ..
            } = record
            else {
                continue;
            };
            let steps = out.by_operation.entry(operation.clone()).or_default();
            let mut add = |step: Step, ns: u64| steps.entry(step).or_default().push(ns);

            add(Step::Queue, task_started_ns.saturating_sub(*queued_ns));
            add(Step::Total, core_handled_ns.saturating_sub(*queued_ns));
            add(
                Step::Resume,
                core_handled_ns.saturating_sub(*task_completed_ns),
            );
            if let Some(paced) = rate_wait_ns {
                add(Step::Paced, *paced);
            }
            match (socket_write_ns, ack_ns) {
                (Some(written), Some(acked)) => {
                    // The hold happens before the bytes are signed, so it
                    // comes out of this leg rather than sitting beside it.
                    add(
                        Step::Encode,
                        written
                            .saturating_sub(*task_started_ns)
                            .saturating_sub(rate_wait_ns.unwrap_or(0)),
                    );
                    add(Step::Venue, acked.saturating_sub(*written));
                    add(Step::Reply, task_completed_ns.saturating_sub(*acked));
                }
                _ => {
                    *out.unstamped.entry(operation.clone()).or_default() += 1;
                }
            }
        }
        out
    }

    pub fn is_empty(&self) -> bool {
        self.by_operation.is_empty()
    }

    pub fn operations(&self) -> impl Iterator<Item = (&str, &BTreeMap<Step, Samples>)> {
        self.by_operation
            .iter()
            .map(|(name, steps)| (name.as_str(), steps))
    }

    pub fn unstamped(&self, operation: &str) -> usize {
        self.unstamped.get(operation).copied().unwrap_or(0)
    }
}

const STEP_WIDTH: usize = 24;
const CELL_WIDTH: usize = 11;

/// Read a log and say how long each step of the order path took.
pub fn of_log(records: &[WalRecord]) -> String {
    report(&Timings::from_records(records))
}

pub fn report(timings: &Timings) -> String {
    let mut out = String::from("how long each step of the order path took\n");
    if timings.is_empty() {
        out.push_str(
            "\n  no timing records in this log. They are written per order, cancel and amend,\n  \
             so a log with no order commands in it has none.\n",
        );
        return out;
    }

    for (operation, steps) in timings.operations() {
        let sent = steps
            .get(&Step::Total)
            .map(Samples::count)
            .unwrap_or_default();
        out.push_str(&format!("\n  {operation} — {sent} command(s)\n\n    "));
        out.push_str(&format!("{:<STEP_WIDTH$}", "step"));
        out.push_str(&format!("{:>8}", "count"));
        for head in ["p50", "p90", "p99", "p99.9", "worst"] {
            out.push_str(&format!("{head:>CELL_WIDTH$}"));
        }
        out.push('\n');

        for step in Step::ALL {
            let Some(samples) = steps.get(&step) else {
                continue;
            };
            let (count, marks, worst) = samples.summary();
            out.push_str(&format!("    {:<STEP_WIDTH$}", step.plain_name()));
            out.push_str(&format!("{count:>8}"));
            for mark in marks {
                out.push_str(&format!("{:>CELL_WIDTH$}", pretty(mark)));
            }
            out.push_str(&format!("{:>CELL_WIDTH$}\n", pretty(worst)));
        }

        let unstamped = timings.unstamped(operation);
        if unstamped > 0 {
            out.push_str(&format!(
                "\n    {unstamped} of these carry no transport stamps, so their venue round trip\n    \
                 is inside `all of it` and not on its own line.\n"
            ));
        }
        let thin = steps
            .get(&Step::Total)
            .map(Samples::count)
            .unwrap_or_default();
        if thin < 1_000 {
            out.push_str(&format!(
                "\n    p99.9 of {thin} samples is the worst one or two. Read it as the tail's\n    \
                 shape, not as a number.\n"
            ));
        }
    }
    out.push_str(
        "\n  `held back for quota` is delay this engine chose, to stay inside the venue's\n  \
         request limit. Everything else is work or waiting it did not choose.\n",
    );
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn timing(operation: &str, marks: [u64; 6], rate_wait_ns: Option<u64>) -> WalRecord {
        WalRecord::VenueTiming {
            command_id: 1,
            operation: operation.to_string(),
            client_order_id: "eng-1".to_string(),
            queued_ns: marks[0],
            task_started_ns: marks[1],
            socket_write_ns: Some(marks[2]),
            ack_ns: Some(marks[3]),
            rate_wait_ns,
            task_completed_ns: marks[4],
            core_handled_ns: marks[5],
            core_handled_wall_ns: 1_700_000_000_000_000_000,
        }
    }

    #[test]
    fn nearest_rank_picks_a_sample_that_was_actually_measured() {
        let sorted: Vec<u64> = (1..=1000).collect();
        assert_eq!(quantile(&sorted, 0.50), 500);
        assert_eq!(quantile(&sorted, 0.90), 900);
        assert_eq!(quantile(&sorted, 0.99), 990);
        assert_eq!(quantile(&sorted, 0.999), 999);
        // The tail quantile of a short series is the worst sample, not an
        // interpolation past the end of it.
        assert_eq!(quantile(&[7, 9], 0.999), 9);
        assert_eq!(quantile(&[], 0.5), 0);
    }

    #[test]
    fn the_quota_hold_comes_out_of_the_signing_leg_not_beside_it() {
        // Queued at 0, task at 100, held 500 for quota, bytes out at 700,
        // acked at 900, task done at 950, engine has it at 1000.
        let records = vec![timing("place", [0, 100, 700, 900, 950, 1_000], Some(500))];
        let timings = Timings::from_records(&records);
        let (_, steps) = timings.operations().next().unwrap();
        let one = |step: Step| steps.get(&step).unwrap().summary().1[0];
        assert_eq!(one(Step::Queue), 100);
        assert_eq!(one(Step::Paced), 500);
        // 700 - 100 - 500: what was left after the hold.
        assert_eq!(one(Step::Encode), 100);
        assert_eq!(one(Step::Venue), 200);
        assert_eq!(one(Step::Reply), 50);
        assert_eq!(one(Step::Resume), 50);
        assert_eq!(one(Step::Total), 1_000);
        // The parts add up to the whole, which is what makes the split
        // readable as an account of where the time went.
        let parts: u64 = [
            Step::Queue,
            Step::Paced,
            Step::Encode,
            Step::Venue,
            Step::Reply,
            Step::Resume,
        ]
        .into_iter()
        .map(one)
        .sum();
        assert_eq!(parts, one(Step::Total));
    }

    #[test]
    fn a_record_with_no_transport_stamps_is_counted_and_not_guessed() {
        let mut record = timing("cancel", [0, 10, 20, 30, 40, 50], None);
        if let WalRecord::VenueTiming {
            socket_write_ns,
            ack_ns,
            ..
        } = &mut record
        {
            *socket_write_ns = None;
            *ack_ns = None;
        }
        let timings = Timings::from_records(&[record]);
        let (name, steps) = timings.operations().next().unwrap();
        assert_eq!(name, "cancel");
        assert_eq!(timings.unstamped("cancel"), 1);
        assert!(steps.get(&Step::Venue).is_none(), "invented a round trip");
        assert!(steps.get(&Step::Paced).is_none(), "invented a quota hold");
        assert_eq!(steps.get(&Step::Total).unwrap().count(), 1);
    }

    #[test]
    fn operations_are_reported_apart() {
        let records = vec![
            timing("place", [0, 1, 2, 3, 4, 5], Some(0)),
            timing("cancel", [0, 1, 2, 3, 4, 900], Some(0)),
            timing("cancel", [0, 1, 2, 3, 4, 800], Some(0)),
        ];
        let timings = Timings::from_records(&records);
        let names: Vec<_> = timings.operations().map(|(name, _)| name).collect();
        assert_eq!(names, vec!["cancel", "place"]);
        let text = report(&timings);
        assert!(text.contains("cancel — 2 command(s)"), "{text}");
        assert!(text.contains("place — 1 command(s)"), "{text}");
    }

    #[test]
    fn a_log_with_no_order_commands_says_so_instead_of_printing_zeroes() {
        let text = of_log(&[WalRecord::Note {
            source: "engine".into(),
            text: "nothing to do".into(),
        }]);
        assert!(text.contains("no timing records"), "{text}");
        assert!(!text.contains("p99.9"), "{text}");
    }
}
