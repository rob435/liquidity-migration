//! A synthetic market in the recorder's own row contract, so the backtest's
//! tape reader, book builder and simulated venue run it unchanged.
//!
//! Per symbol and second: one ticker, one book delta that moves all five
//! levels a side with the mid, one print that sweeps through the touch on a
//! random side so resting quotes get eaten. The mid is a mean-reverting walk
//! with rare jumps, which is what triggers stops and quote-staleness paths.

use std::fs::File;
use std::io::{self, BufWriter, Write};
use std::path::Path;

use super::rng::Rng;

/// The instrument, and the scale of its prices and sizes.
#[derive(Clone, Copy, Debug)]
pub struct SymbolSpec {
    pub name: &'static str,
    pub px0: f64,
    pub tick: f64,
    pub decimals: usize,
    pub qty_step: f64,
    pub qty_decimals: usize,
    pub min_qty: f64,
    pub min_notional: f64,
    /// Displayed size per book level, before the seed's variation.
    pub level_qty: f64,
}

const CATALOG: [SymbolSpec; 3] = [
    SymbolSpec {
        name: "BTCUSDT",
        px0: 50_000.0,
        tick: 0.1,
        decimals: 1,
        qty_step: 0.001,
        qty_decimals: 3,
        min_qty: 0.001,
        min_notional: 5.0,
        level_qty: 2.0,
    },
    SymbolSpec {
        name: "ETHUSDT",
        px0: 3_000.0,
        tick: 0.01,
        decimals: 2,
        qty_step: 0.01,
        qty_decimals: 2,
        min_qty: 0.01,
        min_notional: 5.0,
        level_qty: 20.0,
    },
    SymbolSpec {
        name: "SOLUSDT",
        px0: 150.0,
        tick: 0.001,
        decimals: 3,
        qty_step: 0.1,
        qty_decimals: 1,
        min_qty: 0.1,
        min_notional: 5.0,
        level_qty: 200.0,
    },
];

const LEVELS: usize = 5;
/// One side of a book: `(px, qty)` from the touch outwards.
type Levels = Vec<(f64, f64)>;
const NS_PER_S: u64 = 1_000_000_000;
const VENUE: &str = "bybit-linear";

#[derive(Clone, Debug)]
pub struct MarketPlan {
    pub symbols: Vec<SymbolSpec>,
    pub seconds: u64,
    pub t0_ns: u64,
    pub funding_boundary_ms: i64,
}

impl MarketPlan {
    pub fn new(symbols: usize, seconds: u64) -> Self {
        let t0_ns: u64 = 1_700_000_000_000_000_000;
        let t0_ms = (t0_ns / 1_000_000) as i64;
        MarketPlan {
            symbols: CATALOG[..symbols.clamp(1, CATALOG.len())].to_vec(),
            seconds: seconds.max(2),
            t0_ns,
            funding_boundary_ms: t0_ms + (seconds as i64 / 2) * 1_000,
        }
    }

    pub fn names(&self) -> Vec<String> {
        self.symbols.iter().map(|s| s.name.to_string()).collect()
    }

    pub fn end_ns(&self) -> u64 {
        self.t0_ns + self.seconds * NS_PER_S
    }
}

struct SymbolWalk {
    spec: SymbolSpec,
    mid: f64,
    bids: Levels,
    asks: Levels,
    update_id: u64,
    trades: u64,
}

impl SymbolWalk {
    fn new(spec: SymbolSpec) -> Self {
        SymbolWalk {
            spec,
            mid: spec.px0,
            bids: Vec::new(),
            asks: Vec::new(),
            update_id: 1,
            trades: 0,
        }
    }

    fn on_tick(&self, px: f64) -> f64 {
        (px / self.spec.tick).round() * self.spec.tick
    }

    fn step(&mut self, rng: &mut Rng) {
        let spec = self.spec;
        let shock = (rng.unit() - 0.5) * 2.0 * 0.0004 * self.mid;
        let pull = (spec.px0 - self.mid) * 0.01;
        let jump = if rng.chance(0.02) {
            (rng.unit() - 0.5) * 2.0 * 0.003 * self.mid
        } else {
            0.0
        };
        self.mid = self.on_tick((self.mid + shock + pull + jump).max(spec.tick * 100.0));
    }

    fn sides(&self, rng: &mut Rng) -> (Levels, Levels) {
        let spec = self.spec;
        let size = |rng: &mut Rng| -> f64 {
            let steps = (spec.level_qty / spec.qty_step).round() as u64;
            let n = steps / 2 + rng.below(steps.max(1) * 2);
            n.max(1) as f64 * spec.qty_step
        };
        let bids = (0..LEVELS)
            .map(|k| {
                (
                    self.on_tick(self.mid - spec.tick * (1 + k) as f64),
                    size(rng),
                )
            })
            .collect();
        let asks = (0..LEVELS)
            .map(|k| {
                (
                    self.on_tick(self.mid + spec.tick * (1 + k) as f64),
                    size(rng),
                )
            })
            .collect();
        (bids, asks)
    }

    fn level(&self, px: f64, qty: f64) -> String {
        format!(
            r#"["{:.*}","{:.*}"]"#,
            self.spec.decimals, px, self.spec.qty_decimals, qty
        )
    }

    fn levels(&self, levels: &[(f64, f64)]) -> String {
        levels
            .iter()
            .map(|(px, qty)| self.level(*px, *qty))
            .collect::<Vec<_>>()
            .join(",")
    }

    fn zeroed(&self, levels: &[(f64, f64)]) -> String {
        levels
            .iter()
            .map(|(px, _)| self.level(*px, 0.0))
            .collect::<Vec<_>>()
            .join(",")
    }

    fn snapshot(&mut self, out: &mut impl Write, rng: &mut Rng, t: u64) -> io::Result<()> {
        let (bids, asks) = self.sides(rng);
        self.bids = bids;
        self.asks = asks;
        writeln!(
            out,
            r#"{{"asks":[{a}],"bids":[{b}],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":{t},"exchange_system_ts_ns":{t},"first_update_id":0,"kind":"orderbook_snapshot","local_receive_ts_ns":{t},"previous_update_id":0,"restart_snapshot":false,"sequence_gap":false,"symbol":"{sym}","update_id":{id},"venue":"{VENUE}"}}"#,
            a = self.levels(&self.asks),
            b = self.levels(&self.bids),
            sym = self.spec.name,
            id = self.update_id,
        )
    }

    fn delta(&mut self, out: &mut impl Write, rng: &mut Rng, t: u64) -> io::Result<()> {
        let (bids, asks) = self.sides(rng);
        let previous = self.update_id;
        self.update_id += 1;
        let asks_text = format!("{},{}", self.zeroed(&self.asks), self.levels(&asks));
        let bids_text = format!("{},{}", self.zeroed(&self.bids), self.levels(&bids));
        self.bids = bids;
        self.asks = asks;
        writeln!(
            out,
            r#"{{"asks":[{asks_text}],"bids":[{bids_text}],"cross_sequence":0,"depth":50,"exchange_engine_ts_ns":{t},"exchange_system_ts_ns":{t},"first_update_id":0,"kind":"orderbook_delta","local_receive_ts_ns":{t},"previous_update_id":{previous},"restart_snapshot":false,"sequence_gap":false,"symbol":"{sym}","update_id":{id},"venue":"{VENUE}"}}"#,
            sym = self.spec.name,
            id = self.update_id,
        )
    }

    fn ticker(&self, out: &mut impl Write, t: u64, funding_boundary_ms: i64) -> io::Result<()> {
        let now_ms = (t / 1_000_000) as i64;
        let next_funding = if now_ms < funding_boundary_ms {
            funding_boundary_ms
        } else {
            funding_boundary_ms + 8 * 3_600_000
        };
        writeln!(
            out,
            r#"{{"kind":"ticker","local_receive_ts_ns":{t},"exchange_system_ts_ns":{t},"message_type":"delta","cross_sequence":0,"symbol":"{sym}","values":{{"mark_price":"{mid:.*}","last_price":"{mid:.*}","funding_rate":"0.0001","next_funding_time_ms":{next_funding}}},"venue":"{VENUE}"}}"#,
            self.spec.decimals,
            self.spec.decimals,
            sym = self.spec.name,
            mid = self.mid,
        )
    }

    fn trade(&mut self, out: &mut impl Write, rng: &mut Rng, t: u64) -> io::Result<()> {
        let spec = self.spec;
        let sweep = rng.between(0.5, 1.6) * (self.mid * 2e-4).max(spec.tick * 2.0);
        let (px, side) = if rng.chance(0.5) {
            (self.on_tick(self.mid + sweep), "Buy")
        } else {
            (self.on_tick(self.mid - sweep), "Sell")
        };
        let steps = (spec.level_qty / spec.qty_step).round() as u64;
        let qty = (steps / 4 + rng.below(steps.max(1))).max(1) as f64 * spec.qty_step;
        self.trades += 1;
        writeln!(
            out,
            r#"{{"exchange_ts_ns":{t},"kind":"public_trade","local_receive_ts_ns":{t},"price":{px:.*},"qty":{qty:.*},"side":"{side}","symbol":"{sym}","trade_id":"{sym}-{n}","venue":"{VENUE}"}}"#,
            spec.decimals,
            spec.qty_decimals,
            sym = spec.name,
            n = self.trades,
        )
    }
}

/// Write the whole tape. Rows are in nondecreasing receive order across
/// symbols, as the reader requires.
pub fn write_tape(path: &Path, plan: &MarketPlan, mut rng: Rng) -> io::Result<()> {
    let mut out = BufWriter::new(File::create(path)?);
    let mut walks: Vec<SymbolWalk> = plan.symbols.iter().copied().map(SymbolWalk::new).collect();
    for walk in &mut walks {
        walk.snapshot(&mut out, &mut rng, plan.t0_ns)?;
    }
    for s in 1..=plan.seconds {
        let t = plan.t0_ns + s * NS_PER_S;
        for walk in &mut walks {
            walk.step(&mut rng);
            walk.ticker(&mut out, t, plan.funding_boundary_ms)?;
        }
        for walk in &mut walks {
            walk.delta(&mut out, &mut rng, t + 200_000_000)?;
        }
        for walk in &mut walks {
            walk.trade(&mut out, &mut rng, t + 500_000_000)?;
        }
    }
    out.flush()
}

/// The instruments snapshot in the gateway's four-field shape.
pub fn write_instruments(path: &Path, plan: &MarketPlan) -> io::Result<()> {
    let rows: Vec<String> = plan
        .symbols
        .iter()
        .map(|s| {
            format!(
                r#"{{"symbol":"{}","priceFilter":{{"tickSize":"{:.*}"}},"lotSizeFilter":{{"minOrderQty":"{:.*}","qtyStep":"{:.*}","minNotionalValue":"{}"}}}}"#,
                s.name,
                s.decimals,
                s.tick,
                s.qty_decimals,
                s.min_qty,
                s.qty_decimals,
                s.qty_step,
                s.min_notional
            )
        })
        .collect();
    std::fs::write(
        path,
        format!(
            r#"{{"kind":"instruments_snapshot","venue":"bybit","market":"linear","category":"linear","schema":2,"recorded_at_ns":1,"source":"sim","rows":[{}]}}"#,
            rows.join(",")
        ),
    )
}

/// One quoter over every symbol: a market maker keeps the venue busy with
/// resting quotes, cancels, amends and fills on both sides.
pub fn write_engine_config(path: &Path, plan: &MarketPlan) -> io::Result<()> {
    let symbols = plan
        .names()
        .iter()
        .map(|n| format!("\"{n}\""))
        .collect::<Vec<_>>()
        .join(", ");
    std::fs::write(
        path,
        format!(
            r#"
[engine]
wal_path = "replaced-by-the-harness.wal"
group_flush_ms = 250
account_view_max_age_ms = 5000
max_quote_age_ms = 30000

[risk]
max_account_view_age_s = 120
max_rolling_loss_fraction = 0.1
leverage = 2.0

[risk.envelope]
tracks_equity = true
reference_usdt = 1000000.0
equity_fraction = 1.0
expand_dead_band_fraction = 0.05
gross_notional_multiple = 2.0
disaster_stop_fraction = 0.35
max_component_gross_notional_usdt = 2000000.0
max_initial_margin_usdt = 1000000.0

[[strategy]]
name = "quoter"
sleeve = "quotes"
symbols = [{symbols}]
half_spread_bps = 1.0
requote_bps = 0.5
qty = 0.1
max_position = 0.3
stop_loss_fraction = 0.35
"#
        ),
    )
}
