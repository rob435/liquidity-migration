//! The tape, read in its own contract.
//!
//! Rows are `market_tape`'s frozen schema (`market_tape/schema.py`), one JSON
//! object per line, in `local_receive_ts_ns` order: `orderbook_snapshot`,
//! `orderbook_delta`, `public_trade`, `ticker`. `python -m market_tape rows`
//! writes exactly this from an archive, symbols merged. A file ending in
//! `.zst` is read through the `zstd` binary the recorder already requires.
//!
//! A row that does not follow the contract stops the run with its line
//! number: a tape the driver cannot read is not a tape it may guess at.
//! Kinds this replay has no use for (`liquidation`, `kline`) are counted
//! and skipped by name.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader, Read};
use std::path::Path;
use std::process::{Child, Command, Stdio};

use engine_types::{BookLevel, Depth, InstrumentRule, Symbol, BOOK_DEPTH};
use serde_json::Value;

#[derive(Debug, thiserror::Error)]
pub enum TapeError {
    #[error("tape io: {0}")]
    Io(#[from] std::io::Error),
    #[error("tape line {line}: {detail}")]
    Malformed { line: u64, detail: String },
    #[error("tape line {line}: book rows from {venue} cannot be replayed here; this reader chains books by Bybit's monotone update_id, and another venue's brackets would build a book that is not the venue's. Filter the tape to one Bybit source.")]
    UnsupportedVenue { line: u64, venue: String },
    #[error("tape line {line}: local_receive_ts_ns {this} is before the previous row's {previous}; the tape is receive-time ordered")]
    OutOfOrder { line: u64, previous: u64, this: u64 },
    #[error("cannot start `zstd -dc` for {path}: {source}")]
    Zstd {
        path: String,
        source: std::io::Error,
    },
}

/// One book message: a snapshot replaces, a delta changes levels.
#[derive(Clone, Debug, PartialEq)]
pub struct BookRow {
    pub symbol: Symbol,
    pub snapshot: bool,
    pub depth: u32,
    pub recv_ns: u64,
    pub exchange_ts_ns: u64,
    pub bids: Vec<BookLevel>,
    pub asks: Vec<BookLevel>,
    pub update_id: u64,
    pub cross_sequence: u64,
    pub sequence_gap: bool,
}

#[derive(Clone, Debug, PartialEq)]
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
#[derive(Clone, Debug, Default, PartialEq)]
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
pub enum TapeRow {
    Book(BookRow),
    Trade(TradeRow),
    Ticker(TickerRow),
}

#[derive(Clone, Debug, Default, PartialEq, serde::Serialize)]
pub struct TapeStats {
    pub rows: u64,
    pub books: u64,
    pub trades: u64,
    pub tickers: u64,
    pub skipped_by_kind: BTreeMap<String, u64>,
    pub first_recv_ns: Option<u64>,
    pub last_recv_ns: Option<u64>,
}

/// Reads rows in order, one at a time, verifying the contract as it goes.
pub struct TapeReader {
    lines: Box<dyn BufRead + Send>,
    _decoder: Option<Child>,
    line: u64,
    buffer: String,
    pub stats: TapeStats,
}

impl TapeReader {
    pub fn open(path: &Path) -> Result<Self, TapeError> {
        let is_zst = path.extension().is_some_and(|ext| ext == "zst");
        let (lines, decoder): (Box<dyn BufRead + Send>, Option<Child>) = if is_zst {
            let mut child = Command::new("zstd")
                .arg("-dc")
                .arg("--")
                .arg(path)
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .map_err(|source| TapeError::Zstd {
                    path: path.display().to_string(),
                    source,
                })?;
            let stdout = child.stdout.take().expect("piped stdout");
            (Box::new(BufReader::new(stdout)), Some(child))
        } else {
            (Box::new(BufReader::new(File::open(path)?)), None)
        };
        Ok(TapeReader {
            lines,
            _decoder: decoder,
            line: 0,
            buffer: String::new(),
            stats: TapeStats::default(),
        })
    }

    /// The next row this replay uses, with its receive stamp. `None` at the
    /// end of the tape.
    pub fn next_row(&mut self) -> Result<Option<(u64, TapeRow)>, TapeError> {
        loop {
            self.buffer.clear();
            if self.lines.read_line(&mut self.buffer)? == 0 {
                return Ok(None);
            }
            self.line += 1;
            let text = self.buffer.trim();
            if text.is_empty() {
                continue;
            }
            let value: Value = serde_json::from_str(text).map_err(|error| self.malformed(error))?;
            let kind = value
                .get("kind")
                .and_then(Value::as_str)
                .ok_or_else(|| self.malformed("row has no kind"))?;
            let recv_ns =
                u64_field(&value, "local_receive_ts_ns").map_err(|d| self.malformed(d))?;
            if let Some(previous) = self.stats.last_recv_ns {
                if recv_ns < previous {
                    return Err(TapeError::OutOfOrder {
                        line: self.line,
                        previous,
                        this: recv_ns,
                    });
                }
            }
            let row = match kind {
                "orderbook_snapshot" | "orderbook_delta" => {
                    // A book row's meaning is the venue's chaining rule, and
                    // this reader implements Bybit's: monotone `update_id`
                    // against the last one, restarted by a snapshot. Binance
                    // brackets each diff with `first_update_id`/`pu` instead,
                    // so its rows read here would chain wrongly and produce a
                    // plausible book that is not the venue's. Refused rather
                    // than replayed.
                    let venue = value
                        .get("venue")
                        .and_then(Value::as_str)
                        .ok_or_else(|| self.malformed("book row has no venue"))?;
                    if !venue.starts_with("bybit") {
                        return Err(TapeError::UnsupportedVenue {
                            line: self.line,
                            venue: venue.to_string(),
                        });
                    }
                    self.stats.books += 1;
                    TapeRow::Book(
                        parse_book(&value, kind == "orderbook_snapshot", recv_ns)
                            .map_err(|d| self.malformed(d))?,
                    )
                }
                "public_trade" => {
                    self.stats.trades += 1;
                    TapeRow::Trade(parse_trade(&value, recv_ns).map_err(|d| self.malformed(d))?)
                }
                "ticker" => {
                    self.stats.tickers += 1;
                    TapeRow::Ticker(parse_ticker(&value, recv_ns).map_err(|d| self.malformed(d))?)
                }
                other => {
                    *self
                        .stats
                        .skipped_by_kind
                        .entry(other.to_string())
                        .or_insert(0) += 1;
                    continue;
                }
            };
            self.stats.rows += 1;
            self.stats.first_recv_ns.get_or_insert(recv_ns);
            self.stats.last_recv_ns = Some(recv_ns);
            return Ok(Some((recv_ns, row)));
        }
    }

    fn malformed(&self, detail: impl ToString) -> TapeError {
        TapeError::Malformed {
            line: self.line,
            detail: detail.to_string(),
        }
    }
}

fn symbol_field(value: &Value) -> Result<Symbol, String> {
    match value.get("symbol").and_then(Value::as_str) {
        Some(symbol) if !symbol.is_empty() => Ok(symbol.to_string()),
        _ => Err("row lacks a symbol".to_string()),
    }
}

/// Integers on the tape are JSON numbers; a few writers quote them.
fn u64_field(value: &Value, name: &str) -> Result<u64, String> {
    match value.get(name) {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Number(n)) => n
            .as_u64()
            .or_else(|| n.as_f64().filter(|f| *f >= 0.0).map(|f| f as u64))
            .ok_or_else(|| format!("{name} is not a non-negative integer: {n}")),
        Some(Value::String(s)) => s
            .parse()
            .map_err(|_| format!("{name} is not an integer: {s:?}")),
        Some(other) => Err(format!("{name} is not an integer: {other}")),
    }
}

fn f64_value(value: &Value, what: &str) -> Result<f64, String> {
    match value {
        Value::Number(n) => n
            .as_f64()
            .ok_or_else(|| format!("{what} is not a number: {n}")),
        Value::String(s) => s
            .trim()
            .parse()
            .map_err(|_| format!("{what} is not a number: {s:?}")),
        other => Err(format!("{what} is not a number: {other}")),
    }
}

fn levels(value: &Value, name: &str) -> Result<Vec<BookLevel>, String> {
    let Some(raw) = value.get(name) else {
        return Ok(Vec::new());
    };
    if raw.is_null() {
        return Ok(Vec::new());
    }
    let array = raw
        .as_array()
        .ok_or_else(|| format!("{name} is not a list"))?;
    let mut out = Vec::with_capacity(array.len());
    for level in array {
        let pair = level
            .as_array()
            .filter(|pair| pair.len() >= 2)
            .ok_or_else(|| format!("{name} has a malformed level {level}"))?;
        out.push(BookLevel {
            px: f64_value(&pair[0], &format!("{name} price"))?,
            qty: f64_value(&pair[1], &format!("{name} size"))?,
        });
    }
    Ok(out)
}

fn parse_book(value: &Value, snapshot: bool, recv_ns: u64) -> Result<BookRow, String> {
    Ok(BookRow {
        symbol: symbol_field(value)?,
        snapshot,
        depth: u64_field(value, "depth")? as u32,
        recv_ns,
        exchange_ts_ns: u64_field(value, "exchange_system_ts_ns")?,
        bids: levels(value, "bids")?,
        asks: levels(value, "asks")?,
        update_id: u64_field(value, "update_id")?,
        cross_sequence: u64_field(value, "cross_sequence")?,
        sequence_gap: value
            .get("sequence_gap")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    })
}

fn parse_trade(value: &Value, recv_ns: u64) -> Result<TradeRow, String> {
    let side = value
        .get("side")
        .and_then(Value::as_str)
        .ok_or("trade lacks a side")?;
    let buyer_aggressor = match side {
        "Buy" => true,
        "Sell" => false,
        other => return Err(format!("trade side must be Buy or Sell, got {other:?}")),
    };
    let price = f64_value(value.get("price").ok_or("trade lacks a price")?, "price")?;
    let qty = f64_value(value.get("qty").ok_or("trade lacks a qty")?, "qty")?;
    if !price.is_finite() || price <= 0.0 || !qty.is_finite() || qty <= 0.0 {
        return Err(format!(
            "trade price {price} and qty {qty} must be positive"
        ));
    }
    Ok(TradeRow {
        symbol: symbol_field(value)?,
        recv_ns,
        exchange_ts_ns: u64_field(value, "exchange_ts_ns")?,
        price,
        qty,
        buyer_aggressor,
    })
}

fn parse_ticker(value: &Value, recv_ns: u64) -> Result<TickerRow, String> {
    let values = value
        .get("values")
        .and_then(Value::as_object)
        .ok_or("ticker row lacks values")?;
    let number = |name: &str| -> Result<Option<f64>, String> {
        match values.get(name) {
            None | Some(Value::Null) => Ok(None),
            Some(v) => f64_value(v, name).map(Some),
        }
    };
    let next_funding_time_ms = match values.get("next_funding_time_ms") {
        None | Some(Value::Null) => None,
        Some(v) => Some(f64_value(v, "next_funding_time_ms")? as i64),
    };
    Ok(TickerRow {
        symbol: symbol_field(value)?,
        recv_ns,
        exchange_ts_ns: u64_field(value, "exchange_system_ts_ns")?,
        last_price: number("last_price")?,
        mark_price: number("mark_price")?,
        index_price: number("index_price")?,
        funding_rate: number("funding_rate")?,
        next_funding_time_ms,
    })
}

// ------------------------------------------------------------- the book

/// One symbol's book at one depth, rebuilt from its rows.
///
/// The chaining rule is the recorder's own (`market_tape/book.py`, Bybit): a
/// snapshot makes the book good; a delta is applied only when its
/// `update_id` is above the last one applied and the recorder saw no gap
/// before it; otherwise the book is bad until the next snapshot. Level
/// merging is the live feed's (`engine_marketdata::bybit::state`): a zero
/// size removes the level, sides stay sorted, and the book is cut at
/// [`BOOK_DEPTH`].
#[derive(Clone, Debug, Default)]
pub struct BookBuilder {
    depth: Depth,
    last_update_id: u64,
    valid: bool,
}

impl BookBuilder {
    /// Apply one row. `Some(&Depth)` when the book is the venue's book after
    /// it and has both sides; `None` while it is a guess.
    pub fn apply(&mut self, row: &BookRow) -> Option<&Depth> {
        if row.snapshot {
            self.depth = Depth::default();
            replace_side(
                &mut self.depth.bids,
                &mut self.depth.bid_len,
                &row.bids,
                true,
            );
            replace_side(
                &mut self.depth.asks,
                &mut self.depth.ask_len,
                &row.asks,
                false,
            );
            self.valid = true;
        } else {
            if !self.valid {
                return None;
            }
            if row.sequence_gap || row.update_id <= self.last_update_id {
                self.valid = false;
                return None;
            }
            apply_side(
                &mut self.depth.bids,
                &mut self.depth.bid_len,
                &row.bids,
                true,
            );
            apply_side(
                &mut self.depth.asks,
                &mut self.depth.ask_len,
                &row.asks,
                false,
            );
        }
        self.last_update_id = row.update_id;
        self.depth.update_id = row.update_id;
        self.depth.seq = if row.cross_sequence == 0 {
            row.update_id
        } else {
            row.cross_sequence
        };
        self.depth.venue_ts_ms = (row.exchange_ts_ns / 1_000_000) as i64;
        self.depth.recv_ns = row.recv_ns;
        if self.depth.bid_len == 0 || self.depth.ask_len == 0 {
            return None;
        }
        Some(&self.depth)
    }

    pub fn is_valid(&self) -> bool {
        self.valid
    }

    pub fn depth(&self) -> &Depth {
        &self.depth
    }
}

fn replace_side(out: &mut [BookLevel; BOOK_DEPTH], len: &mut u8, levels: &[BookLevel], bids: bool) {
    *out = [BookLevel::default(); BOOK_DEPTH];
    *len = 0;
    apply_side(out, len, levels, bids);
}

fn apply_side(out: &mut [BookLevel; BOOK_DEPTH], len: &mut u8, changes: &[BookLevel], bids: bool) {
    for change in changes {
        let active = *len as usize;
        if let Some(index) = out[..active].iter().position(|level| level.px == change.px) {
            if change.qty > 0.0 {
                out[index].qty = change.qty;
            } else {
                out.copy_within(index + 1..active, index);
                out[active - 1] = BookLevel::default();
                *len -= 1;
            }
            continue;
        }
        if change.qty <= 0.0 {
            continue;
        }
        let insert = out[..active]
            .iter()
            .position(|level| {
                if bids {
                    change.px > level.px
                } else {
                    change.px < level.px
                }
            })
            .unwrap_or(active);
        if insert >= BOOK_DEPTH {
            continue;
        }
        let new_len = (active + 1).min(BOOK_DEPTH);
        if insert + 1 < new_len {
            out.copy_within(insert..new_len - 1, insert + 1);
        }
        out[insert] = BookLevel {
            px: change.px,
            qty: change.qty,
        };
        *len = new_len as u8;
    }
}

// ------------------------------------------------------- instruments

/// The venue's instrument table as the recorder captured it
/// (`_meta/instruments-<stamp>.json[.zst]`): a `snapshot_payload` whose
/// `rows` are Bybit's own `instruments-info` rows. Read with the same four
/// fields the Bybit gateway reads, and the same refusals.
pub fn read_instruments(path: &Path) -> Result<Vec<(Symbol, InstrumentRule)>, TapeError> {
    let mut text = String::new();
    if path.extension().is_some_and(|ext| ext == "zst") {
        let output = Command::new("zstd")
            .arg("-dc")
            .arg("--")
            .arg(path)
            .stderr(Stdio::inherit())
            .output()
            .map_err(|source| TapeError::Zstd {
                path: path.display().to_string(),
                source,
            })?;
        text = String::from_utf8_lossy(&output.stdout).into_owned();
    } else {
        File::open(path)?.read_to_string(&mut text)?;
    }
    let malformed = |detail: String| TapeError::Malformed { line: 1, detail };
    let payload: Value = serde_json::from_str(text.trim()).map_err(|e| malformed(e.to_string()))?;
    let kind = payload.get("kind").and_then(Value::as_str).unwrap_or("");
    if kind != "instruments_snapshot" {
        return Err(malformed(format!(
            "expected an instruments_snapshot payload, found kind {kind:?}"
        )));
    }
    let rows = payload
        .get("rows")
        .and_then(Value::as_array)
        .ok_or_else(|| malformed("instruments_snapshot has no rows".to_string()))?;
    let mut out = Vec::with_capacity(rows.len());
    for row in rows {
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let (Some(price_filter), Some(lot_filter)) =
            (row.get("priceFilter"), row.get("lotSizeFilter"))
        else {
            tracing::warn!(
                symbol,
                "instrument has no price or lot filter; not tradable"
            );
            continue;
        };
        let field = |obj: &Value, name: &str| -> Result<f64, TapeError> {
            let v = obj
                .get(name)
                .ok_or_else(|| malformed(format!("{symbol}: instrument lacks {name}")))?;
            f64_value(v, name).map_err(malformed)
        };
        let tick_size = field(price_filter, "tickSize")?;
        let qty_step = field(lot_filter, "qtyStep")?;
        let min_qty = field(lot_filter, "minOrderQty")?;
        let min_notional = match lot_filter.get("minNotionalValue") {
            None | Some(Value::Null) => 0.0,
            Some(v) => f64_value(v, "minNotionalValue").map_err(malformed)?,
        };
        if tick_size <= 0.0 || qty_step <= 0.0 {
            tracing::warn!(
                symbol,
                tick_size,
                qty_step,
                "instrument has a zero tick or step"
            );
            continue;
        }
        out.push((
            symbol.to_string(),
            InstrumentRule {
                tick_size,
                qty_step,
                min_qty,
                min_notional,
            },
        ));
    }
    Ok(out)
}
