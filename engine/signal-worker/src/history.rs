//! What history the worker holds per public source, and how much of it is proven.
//!
//! Bybit klines, Bybit funding and Binance whale ratios share one shape on the
//! checkpoint: rows keyed by timestamp per symbol, beside a coverage record of
//! the hour ranges that were fetched completely. The coverage record is two
//! representations of one fact — canonical per-symbol intervals, plus the
//! legacy single-boundary pair kept equal to the sole interval of any symbol
//! that has exactly one — so the checkpoint stays readable by the release
//! before intervals existed.

use std::collections::{BTreeMap, BTreeSet};

use crate::model::{BinanceWhaleObservation, CoverageInterval, HourlyKline, SettledFunding};
use crate::worker::WorkerError;
use crate::HOUR_MS;

/// One timestamped row of a source's history.
pub(crate) trait HistoryRow {
    /// The source's name in error text: `kline`, `funding`, `whale`.
    const LABEL: &'static str;
    /// The timestamp the row is keyed on.
    fn key(&self) -> i64;
    fn available_at_ms(&self) -> i64;
    fn set_available_at_ms(&mut self, at_ms: i64);
    /// Every field except `available_at_ms`. A source may restate a row; it
    /// may never change one.
    fn same_value(&self, other: &Self) -> bool;
}

/// Insert a row, or confirm an existing one says the same thing, keeping the
/// earliest time it was available. Returns whether the row was new.
pub(crate) fn merge_row<R: HistoryRow>(
    rows: &mut BTreeMap<i64, R>,
    row: R,
) -> Result<bool, WorkerError> {
    let key = row.key();
    if let Some(existing) = rows.get_mut(&key) {
        if !existing.same_value(&row) {
            return Err(WorkerError::input(format!(
                "{} history rewrote timestamp {key}",
                R::LABEL
            )));
        }
        existing.set_available_at_ms(existing.available_at_ms().min(row.available_at_ms()));
        return Ok(false);
    }
    rows.insert(key, row);
    Ok(true)
}

impl HistoryRow for HourlyKline {
    const LABEL: &'static str = "kline";

    fn key(&self) -> i64 {
        self.open_ts_ms
    }

    fn available_at_ms(&self) -> i64 {
        self.available_at_ms
    }

    fn set_available_at_ms(&mut self, at_ms: i64) {
        self.available_at_ms = at_ms;
    }

    fn same_value(&self, other: &Self) -> bool {
        self.symbol == other.symbol
            && self.open_ts_ms == other.open_ts_ms
            && self.open == other.open
            && self.high == other.high
            && self.low == other.low
            && self.close == other.close
            && self.volume_base == other.volume_base
            && self.turnover_quote == other.turnover_quote
    }
}

impl HistoryRow for SettledFunding {
    const LABEL: &'static str = "funding";

    fn key(&self) -> i64 {
        self.settlement_ts_ms
    }

    fn available_at_ms(&self) -> i64 {
        self.available_at_ms
    }

    fn set_available_at_ms(&mut self, at_ms: i64) {
        self.available_at_ms = at_ms;
    }

    fn same_value(&self, other: &Self) -> bool {
        self.symbol == other.symbol
            && self.settlement_ts_ms == other.settlement_ts_ms
            && self.rate == other.rate
            && self.funding_interval_min == other.funding_interval_min
    }
}

impl HistoryRow for BinanceWhaleObservation {
    const LABEL: &'static str = "whale";

    fn key(&self) -> i64 {
        self.day_end_ms
    }

    fn available_at_ms(&self) -> i64 {
        self.available_at_ms
    }

    fn set_available_at_ms(&mut self, at_ms: i64) {
        self.available_at_ms = at_ms;
    }

    fn same_value(&self, other: &Self) -> bool {
        self.symbol == other.symbol
            && self.day_end_ms == other.day_end_ms
            && self.long_short_ratio == other.long_short_ratio
    }
}

/// A read view of one source's coverage.
#[derive(Clone, Copy)]
pub(crate) struct CoverageRef<'a> {
    checked_from: &'a BTreeMap<String, i64>,
    checked_through: &'a BTreeMap<String, i64>,
    intervals: &'a BTreeMap<String, Vec<CoverageInterval>>,
}

impl<'a> CoverageRef<'a> {
    pub(crate) fn new(
        checked_from: &'a BTreeMap<String, i64>,
        checked_through: &'a BTreeMap<String, i64>,
        intervals: &'a BTreeMap<String, Vec<CoverageInterval>>,
    ) -> Self {
        Self {
            checked_from,
            checked_through,
            intervals,
        }
    }

    /// Whether one proven interval covers `[required_from_ms, required_through_ms]`.
    pub(crate) fn contains(
        &self,
        symbol: &str,
        required_from_ms: i64,
        required_through_ms: i64,
    ) -> bool {
        self.intervals.get(symbol).is_some_and(|intervals| {
            intervals.iter().any(|interval| {
                interval.checked_from_ms <= required_from_ms
                    && interval.checked_through_ms >= required_through_ms
            })
        }) || legacy_contains(
            self.checked_from,
            self.checked_through,
            symbol,
            required_from_ms,
            required_through_ms,
        )
    }

    /// Where a fetch for `[required_start_ms, required_through_ms]` must begin:
    /// the end of the proven interval that already holds the start, else the
    /// start itself.
    pub(crate) fn repair_start(
        &self,
        symbol: &str,
        required_start_ms: i64,
        required_through_ms: i64,
    ) -> i64 {
        if let Some(interval) = self.intervals.get(symbol).and_then(|intervals| {
            intervals.iter().find(|interval| {
                interval.checked_from_ms <= required_start_ms
                    && interval.checked_through_ms > required_start_ms
            })
        }) {
            return interval.checked_through_ms.min(required_through_ms);
        }
        coverage_repair_start(
            required_start_ms,
            required_through_ms,
            self.checked_from.get(symbol).copied(),
            self.checked_through.get(symbol).copied(),
        )
    }
}

/// A write view of one source's coverage. Every mutation keeps the legacy
/// pair equal to the sole interval of a symbol that has exactly one.
pub(crate) struct CoverageMut<'a> {
    checked_from: &'a mut BTreeMap<String, i64>,
    checked_through: &'a mut BTreeMap<String, i64>,
    intervals: &'a mut BTreeMap<String, Vec<CoverageInterval>>,
    label: &'static str,
}

impl<'a> CoverageMut<'a> {
    pub(crate) fn new(
        checked_from: &'a mut BTreeMap<String, i64>,
        checked_through: &'a mut BTreeMap<String, i64>,
        intervals: &'a mut BTreeMap<String, Vec<CoverageInterval>>,
        label: &'static str,
    ) -> Self {
        Self {
            checked_from,
            checked_through,
            intervals,
            label,
        }
    }

    /// Record a fetched frontier. Both boundaries or neither; hour-aligned;
    /// not past the time the rows became available.
    pub(crate) fn merge(
        &mut self,
        symbol: &str,
        new_from_ms: Option<i64>,
        new_through_ms: Option<i64>,
        available_at_ms: i64,
        replace: bool,
    ) -> Result<(), WorkerError> {
        if new_from_ms.is_some() != new_through_ms.is_some() {
            return Err(WorkerError::input(format!(
                "{} coverage frontier has only one boundary",
                self.label
            )));
        }
        let (Some(new_from_ms), Some(new_through_ms)) = (new_from_ms, new_through_ms) else {
            return Ok(());
        };
        if new_from_ms <= 0
            || new_from_ms % HOUR_MS != 0
            || new_through_ms % HOUR_MS != 0
            || new_from_ms >= new_through_ms
            || new_through_ms > available_at_ms
        {
            return Err(WorkerError::input(format!(
                "{} coverage frontier has an invalid clock",
                self.label
            )));
        }
        if replace {
            self.intervals.remove(symbol);
        }
        merge_coverage_interval(
            self.intervals.entry(symbol.to_owned()).or_default(),
            CoverageInterval {
                checked_from_ms: new_from_ms,
                checked_through_ms: new_through_ms,
            },
        );
        self.sync_legacy(symbol);
        Ok(())
    }

    /// Validate a restored checkpoint's coverage and rebuild the legacy pair
    /// from the intervals. A checkpoint from before intervals existed carries
    /// only the pair; it becomes one interval per symbol.
    pub(crate) fn restore(&mut self, allowed: &BTreeSet<String>) -> Result<(), WorkerError> {
        let legacy_symbols = self
            .checked_from
            .keys()
            .chain(self.checked_through.keys())
            .cloned()
            .collect::<BTreeSet<_>>();
        if legacy_symbols.iter().any(|symbol| {
            self.checked_from.contains_key(symbol) != self.checked_through.contains_key(symbol)
        }) {
            return Err(WorkerError::state(format!(
                "checkpoint {} coverage has only one boundary",
                self.label
            )));
        }
        if self.intervals.is_empty() {
            for symbol in legacy_symbols {
                self.intervals.insert(
                    symbol.clone(),
                    vec![CoverageInterval {
                        checked_from_ms: self.checked_from[&symbol],
                        checked_through_ms: self.checked_through[&symbol],
                    }],
                );
            }
        }
        for (symbol, intervals) in self.intervals.iter() {
            if !allowed.contains(symbol) || intervals.is_empty() {
                return Err(WorkerError::state(format!(
                    "checkpoint {} coverage cardinality is invalid",
                    self.label
                )));
            }
            let mut prior_through = None;
            for interval in intervals {
                if interval.checked_from_ms <= 0
                    || interval.checked_from_ms % HOUR_MS != 0
                    || interval.checked_through_ms % HOUR_MS != 0
                    || interval.checked_from_ms >= interval.checked_through_ms
                    || prior_through.is_some_and(|through| through >= interval.checked_from_ms)
                {
                    return Err(WorkerError::state(format!(
                        "checkpoint {} coverage intervals are not canonical",
                        self.label
                    )));
                }
                prior_through = Some(interval.checked_through_ms);
            }
        }
        self.checked_from.clear();
        self.checked_through.clear();
        for (symbol, intervals) in self.intervals.iter() {
            if let [interval] = intervals.as_slice() {
                self.checked_from
                    .insert(symbol.clone(), interval.checked_from_ms);
                self.checked_through
                    .insert(symbol.clone(), interval.checked_through_ms);
            }
        }
        Ok(())
    }

    /// Keep only the symbols `keep` admits.
    pub(crate) fn retain_symbols(&mut self, keep: impl Fn(&str) -> bool) {
        self.checked_from.retain(|symbol, _| keep(symbol));
        self.checked_through.retain(|symbol, _| keep(symbol));
        self.intervals.retain(|symbol, _| keep(symbol));
    }

    /// Clip one symbol's coverage to `windows`, dropping what falls outside.
    pub(crate) fn retain_windows(&mut self, symbol: &str, windows: &[(i64, i64)]) {
        if let Some(intervals) = self.intervals.get_mut(symbol) {
            let mut retained = Vec::new();
            for interval in intervals.iter() {
                for (from, through) in windows {
                    let checked_from_ms = interval.checked_from_ms.max(*from);
                    let checked_through_ms = interval.checked_through_ms.min(*through);
                    if checked_from_ms < checked_through_ms {
                        merge_coverage_interval(
                            &mut retained,
                            CoverageInterval {
                                checked_from_ms,
                                checked_through_ms,
                            },
                        );
                    }
                }
            }
            *intervals = retained;
        }
        self.sync_legacy(symbol);
    }

    /// Forget symbols with no coverage left.
    pub(crate) fn drop_empty(&mut self) {
        self.intervals.retain(|_, intervals| !intervals.is_empty());
    }

    fn sync_legacy(&mut self, symbol: &str) {
        match self.intervals.get(symbol).map(Vec::as_slice) {
            Some([interval]) => {
                self.checked_from
                    .insert(symbol.to_owned(), interval.checked_from_ms);
                self.checked_through
                    .insert(symbol.to_owned(), interval.checked_through_ms);
            }
            _ => {
                self.checked_from.remove(symbol);
                self.checked_through.remove(symbol);
            }
        }
    }
}

/// Whether the legacy pair alone covers the range.
fn legacy_contains(
    checked_from: &BTreeMap<String, i64>,
    checked_through: &BTreeMap<String, i64>,
    symbol: &str,
    required_from_ms: i64,
    required_through_ms: i64,
) -> bool {
    matches!(
        (
            checked_from.get(symbol).copied(),
            checked_through.get(symbol).copied(),
        ),
        (Some(from), Some(through))
            if from <= required_from_ms && through >= required_through_ms
    )
}

/// Where a fetch must begin given only the legacy pair: the proven end when
/// it already holds the start, bounded to `[required_start, end_ms]`.
pub(crate) fn coverage_repair_start(
    required_start: i64,
    end_ms: i64,
    checked_from: Option<i64>,
    checked_through: Option<i64>,
) -> i64 {
    match (checked_from, checked_through) {
        (Some(from), Some(through)) if from <= required_start => {
            through.max(required_start).min(end_ms)
        }
        _ => required_start,
    }
}

/// Add an interval and re-canonicalise: sorted, non-overlapping, touching
/// intervals joined.
fn merge_coverage_interval(intervals: &mut Vec<CoverageInterval>, incoming: CoverageInterval) {
    intervals.push(incoming);
    intervals.sort_by_key(|interval| interval.checked_from_ms);
    let mut merged = Vec::<CoverageInterval>::with_capacity(intervals.len());
    for interval in intervals.drain(..) {
        if let Some(last) = merged.last_mut() {
            if interval.checked_from_ms <= last.checked_through_ms {
                last.checked_through_ms = last.checked_through_ms.max(interval.checked_through_ms);
                continue;
            }
        }
        merged.push(interval);
    }
    *intervals = merged;
}
