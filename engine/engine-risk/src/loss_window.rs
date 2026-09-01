//! The rolling window of this engine's own closed round trips: what each one
//! made or lost net of venue fees, and what they add up to inside
//! [`ROLLING_LOSS_WINDOW_MS`].
//!
//! Times here are the venue's wall clock in milliseconds, not the engine's
//! monotonic clock, because the window has to survive a restart.

use engine_types::risk::ClosedTradeRow;

use crate::ROLLING_LOSS_WINDOW_MS;

#[derive(Debug, Default)]
pub(crate) struct LossWindow {
    rows: Vec<ClosedTradeRow>,
    /// Only ever moves forward. A reading from behind the latest cannot
    /// re-open a window that has already drained.
    latest_wall_ms: Option<i64>,
}

fn readable(row: &ClosedTradeRow) -> bool {
    row.net_usdt.is_finite() && row.closed_ms > 0
}

impl LossWindow {
    /// Age the window on against the venue's clock, with nothing closing.
    pub(crate) fn observe_clock(&mut self, wall_ms: i64) {
        if wall_ms <= 0 {
            return;
        }
        self.advance(wall_ms);
        self.prune();
    }

    pub(crate) fn observe(&mut self, row: ClosedTradeRow) {
        if !readable(&row) {
            return;
        }
        self.rows.push(row);
        self.advance(row.closed_ms);
        self.prune();
    }

    /// Set the window's trades to these, rather than add them: this is a
    /// restart reading back what it closed before it stopped.
    pub(crate) fn restore(&mut self, rows: &[ClosedTradeRow]) {
        self.rows = rows.iter().copied().filter(readable).collect();
        if let Some(newest) = self.rows.iter().map(|row| row.closed_ms).max() {
            self.advance(newest);
        }
        self.prune();
    }

    /// None when no trade sits inside the window at all.
    pub(crate) fn net_usdt(&self) -> Option<f64> {
        if self.rows.is_empty() {
            return None;
        }
        Some(self.rows.iter().map(|row| row.net_usdt).sum())
    }

    pub(crate) fn rows(&self) -> Vec<ClosedTradeRow> {
        self.rows.clone()
    }

    pub(crate) fn trades(&self) -> usize {
        self.rows.len()
    }

    fn advance(&mut self, wall_ms: i64) {
        if self.latest_wall_ms.is_none_or(|latest| wall_ms > latest) {
            self.latest_wall_ms = Some(wall_ms);
        }
    }

    /// A trade exactly one window old is out; one millisecond newer is in.
    fn prune(&mut self) {
        let Some(latest) = self.latest_wall_ms else {
            return;
        };
        let edge = latest - ROLLING_LOSS_WINDOW_MS;
        self.rows.retain(|row| row.closed_ms > edge);
    }
}
