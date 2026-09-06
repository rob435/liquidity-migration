//! The rolling window of this engine's own closed round trips: what each one
//! made or lost net of venue fees, and what they add up to inside
//! [`ROLLING_LOSS_WINDOW_MS`].
//!
//! Times here are the venue's wall clock in milliseconds, not the engine's
//! monotonic clock, because the window has to survive a restart.

use engine_types::numeric::Exact;
use engine_types::risk::ClosedTradeRow;

use crate::ROLLING_LOSS_WINDOW_MS;

#[derive(Debug)]
struct LossRow {
    trade: ClosedTradeRow,
    net: Option<Exact>,
    expires: bool,
}

#[derive(Debug, Default)]
pub(crate) struct LossWindow {
    rows: Vec<LossRow>,
    net: Exact,
    invalid: usize,
    oldest_closed_ms: Option<i64>,
    /// Only ever moves forward. A reading from behind the latest cannot
    /// re-open a window that has already drained.
    latest_wall_ms: Option<i64>,
}

impl LossWindow {
    pub(crate) fn observe_clock(&mut self, wall_ms: i64) {
        if wall_ms <= 0 {
            return;
        }
        self.advance(wall_ms);
        self.prune();
    }

    pub(crate) fn observe(&mut self, row: ClosedTradeRow) {
        self.insert(row);
        self.prune();
    }

    pub(crate) fn restore(&mut self, rows: &[ClosedTradeRow]) {
        self.rows.clear();
        self.net = Exact::zero();
        self.invalid = 0;
        self.oldest_closed_ms = None;
        for row in rows {
            self.insert(row.clone());
        }
        self.prune();
    }

    fn insert(&mut self, trade: ClosedTradeRow) {
        let valuation = if trade.closed_ms > 0 {
            trade.net()
        } else {
            Err(engine_types::numeric::ExactError::InvalidProjection)
        };
        let expires = valuation.is_ok();
        let net = valuation.ok().flatten();
        if let Some(net) = &net {
            self.net += net;
        } else if trade.net_usdt_exact.is_some() || trade.unpriced.is_some() {
            self.invalid += 1;
        } else {
            return;
        }
        if expires {
            self.advance(trade.closed_ms);
            self.oldest_closed_ms = Some(
                self.oldest_closed_ms
                    .map_or(trade.closed_ms, |oldest| oldest.min(trade.closed_ms)),
            );
        }
        self.rows.push(LossRow {
            trade,
            net,
            expires,
        });
    }

    pub(crate) fn net_usdt(&self) -> Option<&Exact> {
        (!self.rows.is_empty() && self.valid()).then_some(&self.net)
    }

    pub(crate) fn valid(&self) -> bool {
        self.invalid == 0
    }

    pub(crate) fn rows(&self) -> Vec<ClosedTradeRow> {
        self.rows.iter().map(|row| row.trade.clone()).collect()
    }

    pub(crate) fn trades(&self) -> usize {
        self.rows.len()
    }

    fn advance(&mut self, wall_ms: i64) {
        if self.latest_wall_ms.is_none_or(|latest| wall_ms > latest) {
            self.latest_wall_ms = Some(wall_ms);
        }
    }

    fn prune(&mut self) {
        let Some(latest) = self.latest_wall_ms else {
            return;
        };
        let edge = latest - ROLLING_LOSS_WINDOW_MS;
        if self.oldest_closed_ms.is_none_or(|oldest| oldest > edge) {
            return;
        }
        let mut oldest = None;
        self.rows.retain(|row| {
            if row.expires && row.trade.closed_ms <= edge {
                if let Some(net) = &row.net {
                    self.net -= net;
                } else {
                    self.invalid -= 1;
                }
                return false;
            }
            if row.expires {
                oldest = Some(oldest.map_or(row.trade.closed_ms, |value: i64| {
                    value.min(row.trade.closed_ms)
                }));
            }
            true
        });
        self.oldest_closed_ms = oldest;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn trade(closed_ms: i64, net: Exact) -> ClosedTradeRow {
        ClosedTradeRow {
            unpriced: None,
            net_usdt: net.to_f64().unwrap(),
            net_usdt_exact: Some(net),
            closed_ms,
        }
    }
    fn direct(window: &LossWindow) -> Option<Exact> {
        let rows = window.rows();
        (!rows.is_empty()).then(|| rows.iter().map(|row| row.net().unwrap().unwrap()).sum())
    }

    #[test]
    fn variable_fraction_losses_remain_exact_through_expiry_and_restatement() {
        let mut window = LossWindow::default();
        for n in 0..256 {
            let value = Exact::from_ratio(
                &(if n % 2 == 0 { n + 1 } else { -n - 1 }).to_string(),
                &(101 + n).to_string(),
            )
            .unwrap();
            window.observe(trade(ROLLING_LOSS_WINDOW_MS + n * 675_000, value));
            if n % 13 == 0 {
                assert_eq!(window.net_usdt(), direct(&window).as_ref());
            }
            if n % 47 == 0 {
                let rows = serde_json::from_slice::<Vec<ClosedTradeRow>>(
                    &serde_json::to_vec(&window.rows()).unwrap(),
                )
                .unwrap();
                let mut restored = LossWindow::default();
                restored.restore(&rows);
                assert_eq!(restored.net_usdt(), window.net_usdt());
                restored.restore(&rows);
                assert_eq!(restored.net_usdt(), window.net_usdt());
                window = restored;
            }
        }
        assert_eq!(window.net_usdt(), direct(&window).as_ref());
        window.observe_clock(5 * ROLLING_LOSS_WINDOW_MS);
        assert_eq!(window.net_usdt(), None);
        assert!(window.rows().is_empty());
    }

    #[test]
    fn repeated_risk_reads_do_not_walk_closed_trade_history() {
        let mut window = LossWindow::default();
        let value = Exact::from_ratio(&"7".repeat(200), &"9".repeat(201)).unwrap();
        for n in 0..1024 {
            let value = if n % 2 == 0 { value.clone() } else { -&value };
            window.observe(trade(n + 1, value));
        }
        let started = std::time::Instant::now();
        for n in 0..4096 {
            window.observe_clock(1024 + n);
            assert!(window.valid());
            assert_eq!(window.net_usdt(), Some(&Exact::zero()));
            assert!(
                started.elapsed() < std::time::Duration::from_secs(3),
                "risk decisions repeatedly revalidated or summed closed-trade history"
            );
        }
    }
}
