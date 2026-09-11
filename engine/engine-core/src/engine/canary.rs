//! The funded canary's operating policy: what a `live-canary` realm may open
//! while it is still gathering its own receipts.
//!
//! Every `[risk]` cap is a ratio of verified equity, so a deposit enlarges all
//! of them at once. The ceilings here are absolute money and do not move. They
//! bound openings only — a reduction, a cancel, a stop and a flatten command
//! never reach this file.

use std::collections::BTreeSet;

use engine_types::numeric::Exact;
use engine_types::risk::ClosedTradeRow;
use engine_venue::{VenueName, VenueReadiness};

use super::intent_admission::OpeningRefusal;
use super::*;
use crate::config::Config;

/// The compiled `[canary]` section.
#[derive(Clone, Debug)]
pub struct CanaryPolicy {
    /// Venue wall-clock milliseconds; the start of the loss window.
    started_ms: i64,
    expires_ms: i64,
    /// Sleeve labels, resolved from the configured `[[strategy]]` block names
    /// the section lists: the engine's log, heartbeat and refusal all speak
    /// sleeves, and the config speaks blocks.
    sleeves: BTreeSet<String>,
    symbols: Option<BTreeSet<String>>,
    max_positions: usize,
    max_open_orders: usize,
    max_gross_notional_usdt: f64,
    max_position_notional_usdt: f64,
    max_loss_usdt: f64,
    /// Money lost on round trips closed at or after `started_ms`, as a
    /// positive number: `-` their net. Derived state, rebuilt by the boot
    /// replay, so no WAL record carries it across a rotation.
    realised_loss_usdt: Exact,
    /// Closed trips the log could not value. Reported, never counted as a
    /// trip that lost nothing.
    unvalued_trips: usize,
}

/// One opening, in the terms the policy judges.
pub(crate) struct CanaryIntent<'a> {
    pub sleeve: &'a str,
    pub symbol: &'a str,
    pub notional_usdt: f64,
}

/// What the account holds now, valued at the marks the engine already holds.
#[derive(Clone, Debug, Default)]
pub(crate) struct CanaryBook {
    /// One entry per held symbol: its name and its gross notional.
    pub positions: Vec<(String, f64)>,
    /// Live orders that add exposure. Reductions are not counted.
    pub open_orders: usize,
    /// Realised loss since `started_at` plus unrealised loss now, as a
    /// positive number. Zero when the window is in profit.
    pub loss_usdt: f64,
}

impl CanaryBook {
    fn gross_usdt(&self) -> f64 {
        self.positions.iter().map(|(_, notional)| notional).sum()
    }

    fn notional_of(&self, symbol: &str) -> Option<f64> {
        self.positions
            .iter()
            .find(|(name, _)| name == symbol)
            .map(|(_, notional)| *notional)
    }
}

/// What the policy is doing right now, for the heartbeat.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct CanaryStatus {
    pub expires_in_s: i64,
    pub gross_notional_usdt: f64,
    pub positions: usize,
    pub open_orders: usize,
    pub loss_usdt: f64,
    /// Closed trips since `started_at` the log could not value: money the
    /// loss ceiling is not counting.
    pub unvalued_trips: usize,
    /// The word every opening would be refused with, whatever it asked for.
    pub blocked: Option<&'static str>,
}

impl CanaryPolicy {
    /// Read `[canary]` against the realm it is for.
    ///
    /// Called from `runner::run` before the log claim, any credential and any
    /// socket, beside `require_engine_run_ready`.
    pub fn compile(config: &Config, venue: VenueName) -> Result<Option<Self>, String> {
        let readiness = venue.readiness();
        let canary = readiness == VenueReadiness::LiveCanary;
        match (&config.canary, canary) {
            (None, false) => return Ok(None),
            (None, true) => {
                return Err(format!(
                    "{venue} readiness is {}; engine.toml must carry a [canary] operating policy",
                    readiness.as_str()
                ))
            }
            (Some(_), false) => {
                return Err(format!(
                    "engine.toml carries [canary] but {venue} readiness is {}; a canary policy is only for a live-canary realm",
                    readiness.as_str()
                ))
            }
            (Some(_), true) => {}
        }
        let section = config.canary.as_ref().expect("matched above");

        let accepted: BTreeSet<&str> = section
            .accepted_unproven
            .iter()
            .map(String::as_str)
            .collect();
        let unproven: BTreeSet<&str> = venue
            .unproven_capabilities()
            .into_iter()
            .map(|capability| capability.as_str())
            .collect();
        if accepted != unproven {
            return Err(format!(
                "canary.accepted_unproven is [{}] and {venue} holds no current receipt for [{}]; the policy must accept exactly what the realm owes",
                joined(&accepted),
                joined(&unproven)
            ));
        }

        let started_ms = unix_ms(&section.started_at)
            .map_err(|why| format!("canary.started_at {:?}: {why}", section.started_at))?;
        let expires_ms = unix_ms(&section.expires_at)
            .map_err(|why| format!("canary.expires_at {:?}: {why}", section.expires_at))?;
        if expires_ms <= started_ms {
            return Err("canary.expires_at must be after canary.started_at".into());
        }

        for (key, value) in [
            ("max_gross_notional_usdt", section.max_gross_notional_usdt),
            (
                "max_position_notional_usdt",
                section.max_position_notional_usdt,
            ),
            ("max_loss_usdt", section.max_loss_usdt),
        ] {
            if !value.is_finite() || value <= 0.0 {
                return Err(format!(
                    "canary.{key} must be a positive number, not {value}"
                ));
            }
        }
        for (key, value) in [
            ("max_positions", section.max_positions),
            ("max_open_orders", section.max_open_orders),
        ] {
            if value == 0 {
                return Err(format!("canary.{key} must be positive"));
            }
        }

        if section.strategies.is_empty() {
            return Err("canary.strategies must name at least one [[strategy]] block".into());
        }
        let mut sleeves = BTreeSet::new();
        for name in &section.strategies {
            let block = config
                .strategies
                .iter()
                .find(|block| &block.name == name)
                .ok_or_else(|| {
                    format!(
                        "canary.strategies names {name:?}, which is not a configured [[strategy]] block ({})",
                        config
                            .strategies
                            .iter()
                            .map(|block| block.name.as_str())
                            .collect::<Vec<_>>()
                            .join(", ")
                    )
                })?;
            sleeves.insert(block.sleeve_name().to_string());
        }

        let symbols = match &section.symbols {
            None => None,
            Some(listed) if listed.is_empty() => return Err(
                "canary.symbols is present and empty; leave the key out to admit every instrument"
                    .into(),
            ),
            Some(listed) => Some(listed.iter().cloned().collect()),
        };

        Ok(Some(Self {
            started_ms,
            expires_ms,
            sleeves,
            symbols,
            max_positions: section.max_positions,
            max_open_orders: section.max_open_orders,
            max_gross_notional_usdt: section.max_gross_notional_usdt,
            max_position_notional_usdt: section.max_position_notional_usdt,
            max_loss_usdt: section.max_loss_usdt,
            realised_loss_usdt: Exact::zero(),
            unvalued_trips: 0,
        }))
    }

    /// Fold one closed round trip into the window's realised loss.
    ///
    /// Called once per trip: at boot for every trip the replay rebuilt, and
    /// from `record_trades` for every trip that closes while the run is up.
    /// The risk kernel's own window is a day wide and this one is the whole
    /// experiment, so the two do not share an accumulator.
    pub(crate) fn observe_closed_trip(&mut self, row: &ClosedTradeRow) {
        if row.closed_ms < self.started_ms {
            return;
        }
        match row.net() {
            Ok(Some(net)) => self.realised_loss_usdt -= net,
            Ok(None) | Err(_) => self.unvalued_trips += 1,
        }
    }

    /// Why this opening may not go. `None` admits it.
    pub(crate) fn refusal(
        &self,
        intent: &CanaryIntent<'_>,
        book: &CanaryBook,
        now_wall_ms: i64,
    ) -> Option<OpeningRefusal> {
        if now_wall_ms >= self.expires_ms {
            return Some(OpeningRefusal::CanaryExpired);
        }
        if !self.sleeves.contains(intent.sleeve) {
            return Some(OpeningRefusal::CanaryStrategyNotAllowed);
        }
        if self
            .symbols
            .as_ref()
            .is_some_and(|allowed| !allowed.contains(intent.symbol))
        {
            return Some(OpeningRefusal::CanarySymbolNotAllowed);
        }
        let held = book.notional_of(intent.symbol);
        if held.is_none() && book.positions.len() >= self.max_positions {
            return Some(OpeningRefusal::CanaryPositionsAtCap);
        }
        if book.open_orders >= self.max_open_orders {
            return Some(OpeningRefusal::CanaryOpenOrdersAtCap);
        }
        if book.gross_usdt() + intent.notional_usdt > self.max_gross_notional_usdt {
            return Some(OpeningRefusal::CanaryGrossNotionalAtCap);
        }
        if held.unwrap_or(0.0) + intent.notional_usdt > self.max_position_notional_usdt {
            return Some(OpeningRefusal::CanaryPositionNotionalAtCap);
        }
        if book.loss_usdt >= self.max_loss_usdt {
            return Some(OpeningRefusal::CanaryLossCeiling);
        }
        None
    }

    fn status(&self, book: &CanaryBook, now_wall_ms: i64) -> CanaryStatus {
        let gross = book.gross_usdt();
        let blocked = if now_wall_ms >= self.expires_ms {
            Some(OpeningRefusal::CanaryExpired)
        } else if book.loss_usdt >= self.max_loss_usdt {
            Some(OpeningRefusal::CanaryLossCeiling)
        } else if gross >= self.max_gross_notional_usdt {
            Some(OpeningRefusal::CanaryGrossNotionalAtCap)
        } else if book.positions.len() >= self.max_positions {
            Some(OpeningRefusal::CanaryPositionsAtCap)
        } else if book.open_orders >= self.max_open_orders {
            Some(OpeningRefusal::CanaryOpenOrdersAtCap)
        } else {
            None
        };
        CanaryStatus {
            expires_in_s: (self.expires_ms - now_wall_ms) / 1_000,
            gross_notional_usdt: gross,
            positions: book.positions.len(),
            open_orders: book.open_orders,
            loss_usdt: book.loss_usdt,
            unvalued_trips: self.unvalued_trips,
            blocked: blocked.map(OpeningRefusal::as_str),
        }
    }

    /// One line for the boot log, so an operator reads the policy the run is
    /// actually under rather than the file they think was installed.
    pub fn summary(&self) -> String {
        format!(
            "expires_at_ms={} strategies=[{}] symbols={} max_positions={} max_open_orders={} max_gross_notional_usdt={} max_position_notional_usdt={} max_loss_usdt={}",
            self.expires_ms,
            self.sleeves
                .iter()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(", "),
            self.symbols.as_ref().map_or_else(
                || "any".to_string(),
                |listed| listed
                    .iter()
                    .map(String::as_str)
                    .collect::<Vec<_>>()
                    .join(", ")
            ),
            self.max_positions,
            self.max_open_orders,
            self.max_gross_notional_usdt,
            self.max_position_notional_usdt,
            self.max_loss_usdt,
        )
    }
}

fn joined(names: &BTreeSet<&str>) -> String {
    names.iter().copied().collect::<Vec<_>>().join(", ")
}

/// `YYYY-MM-DDTHH:MM:SSZ` to Unix milliseconds.
///
/// UTC only. The window is judged against the venue's own wall clock, and an
/// offset spelled anything but `Z` would put the boundary somewhere else.
fn unix_ms(text: &str) -> Result<i64, String> {
    let bytes = text.as_bytes();
    if bytes.len() != 20 || bytes[4] != b'-' || bytes[7] != b'-' || bytes[10] != b'T' {
        return Err("must be RFC3339 UTC, YYYY-MM-DDTHH:MM:SSZ".into());
    }
    if bytes[13] != b':' || bytes[16] != b':' || bytes[19] != b'Z' {
        return Err("must be RFC3339 UTC, YYYY-MM-DDTHH:MM:SSZ".into());
    }
    let field = |from: usize, to: usize| -> Result<i64, String> {
        // Digits only: `i64` would otherwise read a signed field as a number.
        let part = &text[from..to];
        part.bytes()
            .all(|byte| byte.is_ascii_digit())
            .then(|| part.parse::<i64>().ok())
            .flatten()
            .ok_or_else(|| "must be RFC3339 UTC, YYYY-MM-DDTHH:MM:SSZ".to_string())
    };
    let (year, month, day) = (field(0, 4)?, field(5, 7)?, field(8, 10)?);
    let (hour, minute, second) = (field(11, 13)?, field(14, 16)?, field(17, 19)?);
    if !(1..=12).contains(&month) {
        return Err(format!("month {month} does not exist"));
    }
    let leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    let last = [
        31,
        if leap { 29 } else { 28 },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ][(month - 1) as usize];
    if !(1..=last).contains(&day) {
        return Err(format!("day {day} does not exist in month {month}"));
    }
    if hour > 23 || minute > 59 || second > 59 {
        return Err(format!("{hour:02}:{minute:02}:{second:02} is not a time"));
    }
    let days = days_from_civil(year, month, day);
    Ok((days * 86_400 + hour * 3_600 + minute * 60 + second) * 1_000)
}

/// Days since 1970-01-01 from a proleptic Gregorian date.
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = if month <= 2 { year - 1 } else { year };
    let era = if year >= 0 { year } else { year - 399 } / 400;
    let year_of_era = year - era * 400;
    let shifted = (month + 9) % 12;
    let day_of_year = (153 * shifted + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    era * 146_097 + day_of_era - 719_468
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    /// Put this run under a canary policy. `runner::run` calls it once, after
    /// boot and before the loop.
    ///
    /// The trips the boot replay rebuilt are folded in here: the policy's
    /// loss window is the whole experiment, and the kernel's is a day.
    pub fn enforce_canary(&mut self, mut policy: CanaryPolicy) {
        for row in std::mem::take(&mut self.canary_seed) {
            policy.observe_closed_trip(&row);
        }
        self.canary = Some(policy);
    }

    /// What the policy is doing now, for whoever writes the heartbeat. `None`
    /// when this run has no policy.
    pub fn canary_status(&self) -> Option<CanaryStatus> {
        let policy = self.canary.as_ref()?;
        Some(policy.status(&self.canary_book(policy), clock::wall_ms()))
    }

    /// Why the policy refuses this opening. Judged after every other opening
    /// refusal and before the risk reservation.
    pub(super) fn canary_refusal(&self, intent: &Intent) -> Option<OpeningRefusal> {
        let policy = self.canary.as_ref()?;
        let table = &self.books.market.table;
        let symbol = if intent.symbol.idx() < table.len() {
            table.name(intent.symbol)
        } else {
            ""
        };
        let facts = CanaryIntent {
            sleeve: self
                .host
                .names
                .get(intent.strategy.idx())
                .map_or("", String::as_str),
            symbol,
            notional_usdt: intent.qty.abs() * self.canary_mark(intent.symbol, intent.kind),
        };
        policy.refusal(&facts, &self.canary_book(policy), clock::wall_ms())
    }

    fn canary_book(&self, policy: &CanaryPolicy) -> CanaryBook {
        CanaryBook {
            positions: self.canary_positions(),
            open_orders: self
                .books
                .orders
                .iter_in_flight()
                .filter(|order| !order.request.reduce_only)
                .count(),
            loss_usdt: self.canary_loss_usdt(policy),
        }
    }

    fn canary_positions(&self) -> Vec<(String, f64)> {
        let table = &self.books.market.table;
        self.books
            .account
            .positions
            .iter()
            .filter(|row| row.qty != 0.0 && row.symbol.idx() < table.len())
            .map(|row| {
                (
                    table.name(row.symbol).to_string(),
                    row.qty.abs() * self.canary_mark(row.symbol, OrderKind::Market),
                )
            })
            .collect()
    }

    /// What the engine already prices this symbol at, through the same
    /// `reference_px` an order's arrival is measured against: a limit order's
    /// own price, else the book's mid, else the ticker's last or mark. Zero
    /// when it has never seen one, which values the row at nothing rather than
    /// at a guess.
    fn canary_mark(&self, symbol: SymbolId, kind: OrderKind) -> f64 {
        if symbol.idx() >= self.books.market.table.len() {
            return 0.0;
        }
        self.reference_px(symbol, &kind)
            .filter(|px| px.is_finite() && *px > 0.0)
            .unwrap_or(0.0)
    }

    /// Realised loss since the policy started, plus unrealised loss now.
    ///
    /// The realised half is the policy's own accumulator: every round trip
    /// closed since `started_at`, whatever the risk kernel's day-wide window
    /// has dropped since. The unrealised half is this engine's marks against
    /// the account's entry prices, counted only while it is negative.
    fn canary_loss_usdt(&self, policy: &CanaryPolicy) -> f64 {
        let open_loss = (-self.canary_open_pnl_usdt()).max(0.0);
        (policy.realised_loss_usdt.reporting_f64() + open_loss).max(0.0)
    }

    fn canary_open_pnl_usdt(&self) -> f64 {
        self.books
            .account
            .positions
            .iter()
            .filter(|row| row.qty != 0.0)
            .map(|row| {
                let mark = self.canary_mark(row.symbol, OrderKind::Market);
                if mark <= 0.0 || !row.entry_px.is_finite() || row.entry_px <= 0.0 {
                    return 0.0;
                }
                let direction = if row.side == Side::Buy { 1.0 } else { -1.0 };
                (mark - row.entry_px) * row.qty.abs() * direction
            })
            .sum()
    }
}

#[cfg(test)]
mod tests;
