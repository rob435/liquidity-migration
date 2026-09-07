//! Historical facts after venue-specific decoding; availability drives delivery.

use super::tape::{TapeError, TapeStats};
use engine_types::{Depth, Symbol};

#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct TradeRow {
    pub symbol: Symbol,
    pub recv_ns: u64,
    pub exchange_ts_ns: u64,
    pub price: f64,
    pub qty: f64,
    /// `Buy` on the tape: the buyer crossed the spread.
    pub buyer_aggressor: bool,
}

/// A ticker delta. Every field is what the venue pushed in that message;
/// absent means unchanged, never zero.
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize, serde::Deserialize)]
pub struct TickerRow {
    pub symbol: Symbol,
    pub recv_ns: u64,
    pub exchange_ts_ns: u64,
    pub last_price: Option<f64>,
    pub mark_price: Option<f64>,
    pub index_price: Option<f64>,
    pub funding_rate: Option<f64>,
    pub next_funding_time_ms: Option<i64>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum HistoricalEvent {
    Book {
        symbol: Symbol,
        depth: u32,
        levels: Option<Box<Depth>>,
    },
    Trade(TradeRow),
    Ticker(TickerRow),
    Bar(BarRow),
}

pub trait HistoricalSource: Send {
    fn next_event(&mut self) -> Result<Option<(u64, HistoricalEvent)>, TapeError>;
    fn stats(&self) -> &TapeStats;
}

impl<T: HistoricalSource + ?Sized> HistoricalSource for Box<T> {
    fn next_event(&mut self) -> Result<Option<(u64, HistoricalEvent)>, TapeError> {
        (**self).next_event()
    }
    fn stats(&self) -> &TapeStats {
        (**self).stats()
    }
}

#[derive(Clone, Debug, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BarRow {
    pub symbol: Symbol,
    pub recv_ns: u64,
    pub exchange_ts_ns: u64,
    pub start_ns: u64,
    pub end_ns: u64,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: f64,
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Membership {
    pub symbol: Symbol,
    pub start_ns: u64,
    pub end_ns: u64,
}

#[derive(Clone, Debug, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistoryHeader {
    pub schema: String,
    pub venue: String,
    pub delivery: String,
    pub channels: Vec<String>,
    pub membership: Vec<Membership>,
    pub instrument_assumption: String,
}

pub struct NormalizedReader {
    lines: std::io::BufReader<std::fs::File>,
    buffer: String,
    line: u64,
    stats: TapeStats,
    pub header: HistoryHeader,
}

impl NormalizedReader {
    pub fn open(path: &std::path::Path) -> Result<Self, TapeError> {
        use std::io::BufRead;
        let mut lines = std::io::BufReader::new(std::fs::File::open(path)?);
        let mut buffer = String::new();
        lines.read_line(&mut buffer)?;
        let header: HistoryHeader =
            serde_json::from_str(&buffer).map_err(|e| TapeError::Malformed {
                line: 1,
                detail: e.to_string(),
            })?;
        if header.schema != "historical_v1"
            || header.venue.is_empty()
            || header.delivery.trim().is_empty()
            || header.instrument_assumption.trim().is_empty()
            || header.membership.is_empty()
            || header
                .membership
                .iter()
                .any(|m| m.symbol.is_empty() || m.start_ns >= m.end_ns)
            || header.channels.is_empty()
            || header
                .channels
                .iter()
                .any(|c| !["book", "trade", "ticker", "bar"].contains(&c.as_str()))
        {
            return Err(TapeError::Malformed { line: 1, detail: "invalid historical_v1 header: declare venue, delivery, channels, membership intervals and instrument assumption".into() });
        }
        Ok(Self {
            lines,
            buffer,
            line: 1,
            stats: TapeStats::default(),
            header,
        })
    }

    fn malformed(&self, detail: impl ToString) -> TapeError {
        TapeError::Malformed {
            line: self.line,
            detail: detail.to_string(),
        }
    }
}

impl HistoricalSource for NormalizedReader {
    fn next_event(&mut self) -> Result<Option<(u64, HistoricalEvent)>, TapeError> {
        use std::io::BufRead;
        self.buffer.clear();
        if self.lines.read_line(&mut self.buffer)? == 0 {
            return Ok(None);
        }
        self.line += 1;
        let mut row: serde_json::Value =
            serde_json::from_str(&self.buffer).map_err(|e| self.malformed(e))?;
        let string = |key| {
            row.get(key)
                .and_then(serde_json::Value::as_str)
                .ok_or_else(|| self.malformed(format!("missing string {key}")))
        };
        let symbol = string("symbol")?.to_string();
        let kind = string("kind")?.to_string();
        if string("venue")? != self.header.venue || !self.header.channels.contains(&kind) {
            return Err(self.malformed("row venue/channel differs from the header"));
        }
        let integer = |key| {
            row.get(key)
                .and_then(serde_json::Value::as_u64)
                .ok_or_else(|| self.malformed(format!("missing integer {key}")))
        };
        let at = integer("recv_ns")?;
        let exchange = integer("exchange_ts_ns")?;
        if at < exchange || at / 1_000_000 > i64::MAX as u64 {
            return Err(
                self.malformed("availability must follow exchange time within the supported clock")
            );
        }
        if !self
            .header
            .membership
            .iter()
            .any(|m| m.symbol == symbol && m.start_ns <= exchange && exchange < m.end_ns)
        {
            return Err(self.malformed(format!(
                "{symbol} at {exchange} outside declared historical membership"
            )));
        }
        if let Some(previous) = self.stats.last_recv_ns {
            if at < previous {
                return Err(TapeError::OutOfOrder {
                    line: self.line,
                    previous,
                    this: at,
                });
            }
        }
        let positive = |v: f64| v.is_finite() && v > 0.0;
        let event = match kind.as_str() {
            "trade" => {
                let trade: TradeRow = serde_json::from_value(row).map_err(|e| self.malformed(e))?;
                if !positive(trade.price) || !positive(trade.qty) {
                    return Err(
                        self.malformed("trade price and quantity must be finite and positive")
                    );
                }
                self.stats.trades += 1;
                HistoricalEvent::Trade(trade)
            }
            "ticker" => {
                let ticker: TickerRow =
                    serde_json::from_value(row).map_err(|e| self.malformed(e))?;
                if [ticker.last_price, ticker.mark_price, ticker.index_price]
                    .into_iter()
                    .flatten()
                    .any(|p| !positive(p))
                    || ticker.funding_rate.is_some_and(|p| !p.is_finite())
                    || ticker.next_funding_time_ms.is_some_and(|p| p <= 0)
                {
                    return Err(self.malformed("invalid ticker price or funding"));
                }
                self.stats.tickers += 1;
                HistoricalEvent::Ticker(ticker)
            }
            "bar" => {
                row.as_object_mut().unwrap().remove("kind");
                row.as_object_mut().unwrap().remove("venue");
                let bar: BarRow = serde_json::from_value(row).map_err(|e| self.malformed(e))?;
                if bar.start_ns >= bar.end_ns
                    || bar.end_ns > at
                    || exchange != bar.end_ns
                    || [bar.open, bar.high, bar.low, bar.close]
                        .into_iter()
                        .any(|p| !positive(p))
                    || bar.low > bar.open.min(bar.close)
                    || bar.high < bar.open.max(bar.close)
                    || !bar.volume.is_finite()
                    || bar.volume < 0.0
                {
                    return Err(
                        self.malformed("invalid OHLCV bar or candle available before its close")
                    );
                }
                self.stats.bars += 1;
                HistoricalEvent::Bar(bar)
            }
            "book" => {
                let depth = u32::try_from(integer("depth")?).map_err(|e| self.malformed(e))?;
                let valid = row
                    .get("valid")
                    .and_then(serde_json::Value::as_bool)
                    .ok_or_else(|| self.malformed("book lacks valid"))?;
                let levels = if valid {
                    let side = |key| -> Result<Vec<engine_types::BookLevel>, TapeError> {
                        serde_json::from_value(
                            row.get(key)
                                .cloned()
                                .ok_or_else(|| self.malformed(format!("book lacks {key}")))?,
                        )
                        .map_err(|e| self.malformed(e))
                    };
                    let bids = side("bids")?;
                    let asks = side("asks")?;
                    if depth == 0
                        || bids.len() > engine_types::BOOK_DEPTH
                        || asks.len() > engine_types::BOOK_DEPTH
                        || bids
                            .iter()
                            .chain(&asks)
                            .any(|l| !positive(l.px) || !positive(l.qty))
                        || bids.windows(2).any(|w| w[0].px <= w[1].px)
                        || asks.windows(2).any(|w| w[0].px >= w[1].px)
                        || bids
                            .first()
                            .zip(asks.first())
                            .is_some_and(|(b, a)| b.px >= a.px)
                    {
                        return Err(self.malformed(
                            "invalid normalized book levels; supply ordered observed snapshots",
                        ));
                    }
                    let mut book = Depth {
                        bid_len: bids.len() as u8,
                        ask_len: asks.len() as u8,
                        update_id: integer("update_id")?,
                        seq: integer("cross_sequence")?,
                        venue_ts_ms: (exchange / 1_000_000) as i64,
                        recv_ns: at,
                        ..Depth::default()
                    };
                    book.bids[..bids.len()].copy_from_slice(&bids);
                    book.asks[..asks.len()].copy_from_slice(&asks);
                    Some(Box::new(book))
                } else {
                    None
                };
                self.stats.books += 1;
                HistoricalEvent::Book {
                    symbol,
                    depth,
                    levels,
                }
            }
            _ => return Err(self.malformed("unsupported historical event")),
        };
        self.stats.rows += 1;
        self.stats.first_recv_ns.get_or_insert(at);
        self.stats.last_recv_ns = Some(at);
        Ok(Some((at, event)))
    }
    fn stats(&self) -> &TapeStats {
        &self.stats
    }
}

#[derive(Clone, Copy, Debug, Default)]
pub enum SourceFormat {
    #[default]
    Tape,
    Normalized,
}

type OpenedSource = (Box<dyn HistoricalSource>, Option<HistoryHeader>);
pub fn open(path: &std::path::Path, format: SourceFormat) -> Result<OpenedSource, TapeError> {
    match format {
        SourceFormat::Tape => Ok((Box::new(super::tape::TapeReader::open(path)?), None)),
        SourceFormat::Normalized => {
            let reader = NormalizedReader::open(path)?;
            let header = reader.header.clone();
            Ok((Box::new(reader), Some(header)))
        }
    }
}

pub fn limitations(model: &super::execution::ExecutionModel) -> Vec<String> {
    use super::execution::ExecutionModel;
    let common = "Funding needs timestamped rate/boundary observations; missing funding is unmeasured, not evidence of zero cost. Trade/bar risk quotes use declared spread and participation, not observed depth; liquidation assumes full closure at reference when depth is absent. No market impact, venue outages or independent validation of hypothetical fills.";
    let specific = match model {
        ExecutionModel::Books => "Observed depth/queue model; our fills do not consume recorded book levels.",
        ExecutionModel::Trades { .. } => "Next available print execution; shared per-print participation; passive fills assume zero queue ahead and pay maker fees; other fills pay taker fees; amendments require cancel/replace; last-trade stop trigger proxy; no observed spread/queue/mark.",
        ExecutionModel::Bars { .. } => "Only orders present at candle start use its completed close, with fills delivered at availability; carried stops precede entries at the adverse extreme when intrabar ordering is unknown; no intrabar entry/exit inference; passive fills assume zero queue ahead and pay maker fees; other fills pay taker fees; amendments require cancel/replace; candle prices are not observed liquidity or mark.",
    };
    vec![specific.into(), common.into()]
}
