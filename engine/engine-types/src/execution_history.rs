//! Complete execution windows, sorted on disk with bounded resident runs.

use std::fs::File;
use std::io::{self, BufRead, BufReader, BufWriter, Read, Seek, Write};
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc,
};

use serde::{Deserialize, Serialize};

use crate::{VenueError, VenueExecution};

const CHUNK_BYTES: usize = 256 * 1024;
const MAX_ROW_BYTES: usize = 8 * 1024 * 1024;

struct RowSize(usize);
impl Write for RowSize {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.0 = self
            .0
            .checked_add(bytes.len())
            .filter(|size| *size < MAX_ROW_BYTES)
            .ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::InvalidData,
                    "execution row exceeds byte limit",
                )
            })?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

fn storage(error: impl std::fmt::Display) -> VenueError {
    VenueError::Transport(format!("execution history spool: {error}"))
}

#[derive(Serialize, Deserialize)]
struct Row {
    ordinal: u64,
    execution: VenueExecution,
}

impl Row {
    fn key(&self) -> (i64, u64) {
        (self.execution.venue_ts_ms, self.ordinal)
    }
}

struct Run {
    file: BufReader<File>,
    remaining: usize,
}

impl Run {
    fn next(&mut self) -> Result<Option<Row>, VenueError> {
        if self.remaining == 0 {
            return Ok(None);
        }
        let mut line = Vec::new();
        self.file
            .by_ref()
            .take((MAX_ROW_BYTES + 1) as u64)
            .read_until(b'\n', &mut line)
            .map_err(storage)?;
        if line.len() > MAX_ROW_BYTES || line.last() != Some(&b'\n') {
            return Err(storage("execution row exceeds byte limit or is truncated"));
        }
        let row = serde_json::from_slice(&line).map_err(storage)?;
        self.remaining -= 1;
        Ok(Some(row))
    }
}

fn write_row(file: &mut impl Write, row: &Row) -> Result<(), VenueError> {
    serde_json::to_writer(&mut *file, row).map_err(storage)?;
    file.write_all(b"\n").map_err(storage)
}

fn finish_run(mut file: BufWriter<File>, remaining: usize) -> Result<Run, VenueError> {
    file.flush().map_err(storage)?;
    let mut file = file.into_inner().map_err(storage)?;
    file.rewind().map_err(storage)?;
    Ok(Run {
        file: BufReader::new(file),
        remaining,
    })
}

fn merge(
    mut left: Run,
    mut right: Run,
    cancelled: &AtomicBool,
    progress: &AtomicU64,
) -> Result<Run, VenueError> {
    let count = left
        .remaining
        .checked_add(right.remaining)
        .ok_or_else(|| storage("row count overflow"))?;
    let mut file = BufWriter::new(tempfile::tempfile().map_err(storage)?);
    let mut a = left.next()?;
    let mut b = right.next()?;
    while a.is_some() || b.is_some() {
        if cancelled.load(Ordering::Relaxed) {
            return Err(storage("read cancelled"));
        }
        progress.fetch_add(1, Ordering::Relaxed);
        let take_left = match (&a, &b) {
            (Some(a), Some(b)) => a.key() <= b.key(),
            (Some(_), None) => true,
            _ => false,
        };
        if take_left {
            write_row(&mut file, &a.take().expect("left row"))?;
            a = left.next()?;
        } else {
            write_row(&mut file, &b.take().expect("right row"))?;
            b = right.next()?;
        }
    }
    finish_run(file, count)
}

#[derive(Default)]
pub struct ExecutionHistoryBuilder {
    rows: Vec<Row>,
    bytes: usize,
    next_ordinal: u64,
    // A binary merge owns at most one anonymous file per usize bit.
    runs: Vec<Option<Run>>,
    cancelled: Arc<AtomicBool>,
    progress: Arc<AtomicU64>,
    #[cfg(test)]
    peak_bytes: usize,
}

impl ExecutionHistoryBuilder {
    pub fn with_progress(progress: Arc<AtomicU64>) -> Self {
        Self {
            progress,
            ..Default::default()
        }
    }
    pub fn cancellation(&self) -> Arc<AtomicBool> {
        self.cancelled.clone()
    }

    pub fn push(&mut self, execution: VenueExecution) -> Result<(), VenueError> {
        if self.cancelled.load(Ordering::Relaxed) {
            return Err(storage("read cancelled"));
        }
        let row = Row {
            ordinal: self.next_ordinal,
            execution,
        };
        self.next_ordinal = self
            .next_ordinal
            .checked_add(1)
            .ok_or_else(|| storage("row identity overflow"))?;
        let mut size = RowSize(0);
        serde_json::to_writer(&mut size, &row).map_err(storage)?;
        let size = size.0;
        if self.bytes > 0 && self.bytes.saturating_add(size) > CHUNK_BYTES {
            self.flush_chunk()?;
        }
        self.bytes = self.bytes.saturating_add(size);
        #[cfg(test)]
        {
            self.peak_bytes = self.peak_bytes.max(self.bytes);
        }
        self.rows.push(row);
        self.progress.fetch_add(1, Ordering::Relaxed);
        if self.bytes >= CHUNK_BYTES {
            self.flush_chunk()?;
        }
        Ok(())
    }

    pub fn extend(
        &mut self,
        rows: impl IntoIterator<Item = VenueExecution>,
    ) -> Result<(), VenueError> {
        for row in rows {
            self.push(row)?;
        }
        Ok(())
    }

    fn flush_chunk(&mut self) -> Result<(), VenueError> {
        if self.rows.is_empty() {
            return Ok(());
        }
        self.rows.sort_by_key(Row::key);
        let count = self.rows.len();
        let mut file = BufWriter::new(tempfile::tempfile().map_err(storage)?);
        for row in self.rows.drain(..) {
            write_row(&mut file, &row)?;
        }
        self.bytes = 0;
        let mut run = finish_run(file, count)?;
        let mut level = 0;
        loop {
            if level == self.runs.len() {
                self.runs.push(None);
            }
            match self.runs[level].take() {
                Some(previous) => {
                    run = merge(previous, run, &self.cancelled, &self.progress)?;
                    level += 1;
                }
                None => {
                    self.runs[level] = Some(run);
                    break;
                }
            }
        }
        Ok(())
    }

    pub fn finish(mut self) -> Result<ExecutionHistory, VenueError> {
        if self.cancelled.load(Ordering::Relaxed) {
            return Err(storage("read cancelled"));
        }
        self.flush_chunk()?;
        let mut result = None;
        for run in self.runs.into_iter().flatten() {
            result = Some(match result {
                Some(previous) => merge(previous, run, &self.cancelled, &self.progress)?,
                None => run,
            });
        }
        Ok(ExecutionHistory { run: result })
    }
}

#[derive(Default)]
pub struct ExecutionHistory {
    run: Option<Run>,
}

impl std::fmt::Debug for ExecutionHistory {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ExecutionHistory")
            .field("remaining", &self.len())
            .finish()
    }
}

impl ExecutionHistory {
    pub fn from_rows(rows: impl IntoIterator<Item = VenueExecution>) -> Result<Self, VenueError> {
        let mut builder = ExecutionHistoryBuilder::default();
        builder.extend(rows)?;
        builder.finish()
    }
    pub fn len(&self) -> usize {
        self.run.as_ref().map_or(0, |run| run.remaining)
    }
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
    pub fn pop_front(&mut self) -> Result<Option<VenueExecution>, VenueError> {
        self.run.as_mut().map_or(Ok(None), |run| {
            run.next().map(|row| row.map(|row| row.execution))
        })
    }
}

impl Iterator for ExecutionHistory {
    type Item = Result<VenueExecution, VenueError>;
    fn next(&mut self) -> Option<Self::Item> {
        match self.pop_front() {
            Ok(Some(row)) => Some(Ok(row)),
            Ok(None) => None,
            Err(error) => {
                self.run = None;
                Some(Err(error))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(index: usize) -> VenueExecution {
        VenueExecution {
            exec_id: format!("execution-{index}"),
            client_order_id: "o".repeat(256),
            symbol: "BTCUSDT".into(),
            side: crate::Side::Buy,
            qty: 0.01,
            px: 100.0,
            fee: None,
            amounts: None,
            is_maker: false,
            forced_close: None,
            venue_ts_ms: (index % 97) as i64,
        }
    }

    #[test]
    fn an_arbitrary_history_merges_stably_with_constant_resident_bytes() {
        let mut builder = ExecutionHistoryBuilder::default();
        for index in 0..30_000 {
            builder.push(row(index)).unwrap();
        }
        assert!(
            builder.peak_bytes <= CHUNK_BYTES,
            "aggregate response remained resident: {}",
            builder.peak_bytes
        );
        assert!(builder.runs.len() <= usize::BITS as usize);
        let history = builder.finish().unwrap();
        assert_eq!(history.len(), 30_000);
        let mut previous = None;
        let mut count = 0;
        for execution in history {
            let execution = execution.unwrap();
            let ordinal = execution
                .exec_id
                .strip_prefix("execution-")
                .unwrap()
                .parse::<usize>()
                .unwrap();
            let key = (execution.venue_ts_ms, ordinal);
            assert!(previous.is_none_or(|before| before < key));
            previous = Some(key);
            count += 1;
        }
        assert_eq!(count, 30_000);
    }

    #[test]
    fn a_failed_spool_read_is_an_error_and_terminates_iteration() {
        let mut history = ExecutionHistory::from_rows([row(0)]).unwrap();
        history
            .run
            .as_mut()
            .unwrap()
            .file
            .get_mut()
            .set_len(0)
            .unwrap();
        assert!(history.next().unwrap().is_err());
        assert!(history.next().is_none());
    }

    #[test]
    fn cancellation_releases_all_anonymous_runs_instead_of_finishing_a_long_merge() {
        let mut builder = ExecutionHistoryBuilder::default();
        for index in 0..3_000 {
            builder.push(row(index)).unwrap();
        }
        assert!(builder.runs.iter().any(Option::is_some));
        builder.cancellation().store(true, Ordering::Relaxed);
        assert!(
            matches!(builder.finish(), Err(VenueError::Transport(reason)) if reason.contains("cancelled"))
        );
    }

    #[test]
    fn one_oversized_row_cannot_bypass_the_aggregate_bound() {
        let mut oversized = row(0);
        oversized.exec_id = "x".repeat(MAX_ROW_BYTES);
        let mut builder = ExecutionHistoryBuilder::default();
        assert!(
            matches!(builder.push(oversized), Err(VenueError::Transport(reason)) if reason.contains("byte limit"))
        );
        assert!(builder.rows.is_empty());
        assert!(builder.runs.is_empty());
    }
}
