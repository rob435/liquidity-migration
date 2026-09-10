//! A synthetic market in the recorder's own row contract, so the backtest's
//! tape reader, book builder and simulated venue run it unchanged.
//!
//! Per symbol and step: one ticker, one book delta that moves all five
//! levels a side with the mid, one print that sweeps through the touch on a
//! random side so resting quotes get eaten. The mid is a mean-reverting walk
//! with rare jumps, which is what triggers stops and quote-staleness paths.
//! A seeded [`Shock`] re-anchors one symbol far below its start so a native
//! position stop triggers on the mark and fills by walking the book.

use std::collections::BTreeMap;
use std::fmt::Write as _;
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

/// One symbol falls `fall_fraction` over `fall_over_s` and stays there for
/// `hold_s`, then walks back to its start. The mean reversion is re-anchored
/// for the hold, or the fall decays before a stop can trigger.
#[derive(Clone, Copy, Debug)]
pub struct Shock {
    pub symbol_index: usize,
    pub start_s: u64,
    pub fall_fraction: f64,
    pub fall_over_s: u64,
    pub hold_s: u64,
}

impl Shock {
    /// Where the mean reversion pulls, for this symbol, at this step.
    fn anchor(&self, index: usize, px0: f64, at_s: u64) -> f64 {
        if index != self.symbol_index || at_s < self.start_s {
            return px0;
        }
        let fallen = at_s - self.start_s;
        if fallen >= self.fall_over_s + self.hold_s {
            return px0;
        }
        let progress = (fallen as f64 / self.fall_over_s as f64).min(1.0);
        px0 * (1.0 - self.fall_fraction * progress)
    }

    /// The mid is put on the ramp itself while it falls: the 1 % pull alone
    /// lags the ramp by minutes and the stop would never see the low.
    fn ramp(&self, index: usize, px0: f64, at_s: u64) -> Option<f64> {
        (index == self.symbol_index
            && at_s >= self.start_s
            && at_s - self.start_s <= self.fall_over_s)
            .then(|| self.anchor(index, px0, at_s))
    }
}

#[derive(Clone, Debug)]
pub struct MarketPlan {
    pub symbols: Vec<SymbolSpec>,
    pub seconds: u64,
    /// Seconds between emitted rows. Must stay inside the engine's
    /// `max_quote_age_ms`, or every entry is refused on a stale quote.
    pub step_s: u64,
    pub t0_ns: u64,
    pub funding_boundary_ms: i64,
    pub shock: Option<Shock>,
}

impl MarketPlan {
    pub fn new(symbols: usize, seconds: u64) -> Self {
        let t0_ns: u64 = 1_700_000_000_000_000_000;
        let t0_ms = (t0_ns / 1_000_000) as i64;
        MarketPlan {
            symbols: CATALOG[..symbols.clamp(1, CATALOG.len())].to_vec(),
            seconds: seconds.max(2),
            step_s: 1,
            t0_ns,
            funding_boundary_ms: t0_ms + (seconds as i64 / 2) * 1_000,
            shock: None,
        }
    }

    pub fn t0_ms(&self) -> i64 {
        (self.t0_ns / 1_000_000) as i64
    }

    pub fn end_ms(&self) -> i64 {
        (self.end_ns() / 1_000_000) as i64
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
    index: usize,
    mid: f64,
    bids: Levels,
    asks: Levels,
    update_id: u64,
    trades: u64,
}

impl SymbolWalk {
    fn new(index: usize, spec: SymbolSpec) -> Self {
        SymbolWalk {
            spec,
            index,
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

    fn step(&mut self, rng: &mut Rng, at_s: u64, shock: Option<&Shock>) {
        let spec = self.spec;
        let noise = (rng.unit() - 0.5) * 2.0 * 0.0004 * self.mid;
        let anchor = shock.map_or(spec.px0, |s| s.anchor(self.index, spec.px0, at_s));
        let pull = (anchor - self.mid) * 0.01;
        let jump = if rng.chance(0.02) {
            (rng.unit() - 0.5) * 2.0 * 0.003 * self.mid
        } else {
            0.0
        };
        let next = match shock.and_then(|s| s.ramp(self.index, spec.px0, at_s)) {
            Some(level) => level + noise,
            None => self.mid + noise + pull + jump,
        };
        self.mid = self.on_tick(next.max(spec.tick * 100.0));
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

/// Every mid the tape published, so a producer can state the close and the
/// mark it would have observed instead of inventing one.
#[derive(Clone, Debug, Default)]
pub struct TapeSummary {
    mids: BTreeMap<(String, u64), f64>,
}

impl TapeSummary {
    /// The newest published mid at or before `at_ns`; the symbol's opening
    /// price when `at_ns` predates the tape.
    pub fn mid_at(&self, symbol: &str, at_ns: u64) -> Option<f64> {
        self.mids
            .range((symbol.to_string(), 0)..=(symbol.to_string(), at_ns))
            .next_back()
            .map(|(_, mid)| *mid)
            .or_else(|| {
                self.mids
                    .range((symbol.to_string(), 0)..)
                    .next()
                    .filter(|((name, _), _)| name == symbol)
                    .map(|(_, mid)| *mid)
            })
    }

    pub fn mid_at_ms(&self, symbol: &str, at_ms: i64) -> Option<f64> {
        self.mid_at(symbol, (at_ms.max(0) as u64).saturating_mul(1_000_000))
    }
}

/// Write the whole tape. Rows are in nondecreasing receive order across
/// symbols, as the reader requires.
pub fn write_tape(path: &Path, plan: &MarketPlan, mut rng: Rng) -> io::Result<TapeSummary> {
    let mut out = BufWriter::new(File::create(path)?);
    let mut summary = TapeSummary::default();
    let mut walks: Vec<SymbolWalk> = plan
        .symbols
        .iter()
        .copied()
        .enumerate()
        .map(|(index, spec)| SymbolWalk::new(index, spec))
        .collect();
    for walk in &mut walks {
        walk.snapshot(&mut out, &mut rng, plan.t0_ns)?;
        summary
            .mids
            .insert((walk.spec.name.to_string(), plan.t0_ns), walk.mid);
    }
    let step = plan.step_s.max(1);
    let mut s = step;
    while s <= plan.seconds {
        let t = plan.t0_ns + s * NS_PER_S;
        for walk in &mut walks {
            walk.step(&mut rng, s, plan.shock.as_ref());
            walk.ticker(&mut out, t, plan.funding_boundary_ms)?;
            summary
                .mids
                .insert((walk.spec.name.to_string(), t), walk.mid);
        }
        for walk in &mut walks {
            walk.delta(&mut out, &mut rng, t + 200_000_000)?;
        }
        for walk in &mut walks {
            walk.trade(&mut out, &mut rng, t + 500_000_000)?;
        }
        s += step;
    }
    out.flush()?;
    Ok(summary)
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

/// Which strategy blocks the sim runs.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SimStrategies {
    /// One market maker over the catalogue, and no producer.
    #[default]
    Quoter,
    /// A deployed realm's own generated blocks, with the synthetic producer.
    Realm(Realm),
}

impl SimStrategies {
    pub fn as_str(&self) -> &'static str {
        match self {
            SimStrategies::Quoter => "quoter",
            SimStrategies::Realm(realm) => realm.as_str(),
        }
    }

    pub fn parse(name: &str) -> Option<SimStrategies> {
        match name {
            "quoter" => Some(SimStrategies::Quoter),
            other => Realm::parse(other).map(SimStrategies::Realm),
        }
    }

    pub fn realm(&self) -> Option<Realm> {
        match self {
            SimStrategies::Quoter => None,
            SimStrategies::Realm(realm) => Some(*realm),
        }
    }
}

/// A deployed engine config, compiled in from `deploy/`. The template file is
/// the only copy: `engine-tools sim --strategies mexc` runs the same reviewed
/// bytes the fleet installs, from any working directory.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Realm {
    Demo,
    Mainnet,
    Mexc,
    Hyperliquid,
}

impl Realm {
    pub fn as_str(&self) -> &'static str {
        match self {
            Realm::Demo => "demo",
            Realm::Mainnet => "mainnet",
            Realm::Mexc => "mexc",
            Realm::Hyperliquid => "hyperliquid",
        }
    }

    pub fn parse(name: &str) -> Option<Realm> {
        match name {
            "demo" => Some(Realm::Demo),
            "mainnet" => Some(Realm::Mainnet),
            "mexc" => Some(Realm::Mexc),
            "hyperliquid" => Some(Realm::Hyperliquid),
            _ => None,
        }
    }

    pub fn template(&self) -> &'static str {
        match self {
            Realm::Demo => include_str!("../../../../deploy/engine.demo.toml.template"),
            Realm::Mainnet => include_str!("../../../../deploy/engine.mainnet.toml.template"),
            Realm::Mexc => include_str!("../../../../deploy/engine.mexc.toml.template"),
            Realm::Hyperliquid => {
                include_str!("../../../../deploy/engine.hyperliquid.toml.template")
            }
        }
    }
}

/// The fleet's own capital limits, as `[risk] operational_profile_path` reads
/// them on a host.
pub const OPERATIONAL_PROFILE: &str = include_str!("../../../../configs/operational.json");

const BLOCKS_BEGIN: &str = "# BEGIN GENERATED NATIVE DIRECTIONAL STRATEGIES\n";
const BLOCKS_END: &str = "# END GENERATED NATIVE DIRECTIONAL STRATEGIES";

/// The `[[strategy]]` blocks a deploy generates, verbatim between its markers.
pub fn generated_strategy_blocks(template: &str) -> io::Result<&str> {
    let bad = |what: &str| io::Error::other(format!("the deployed template {what}"));
    if template.matches(BLOCKS_BEGIN).count() != 1 || template.matches(BLOCKS_END).count() != 1 {
        return Err(bad("does not carry exactly one generated-strategy region"));
    }
    let start = template.find(BLOCKS_BEGIN).expect("counted above") + BLOCKS_BEGIN.len();
    let end = template.find(BLOCKS_END).expect("counted above");
    if end < start {
        return Err(bad("closes its generated-strategy region before it opens"));
    }
    Ok(&template[start..end])
}

fn number(table: &toml::Table, path: &[&str]) -> io::Result<toml::Value> {
    let mut value = toml::Value::Table(table.clone());
    for key in path {
        value = value
            .get(*key)
            .cloned()
            .ok_or_else(|| io::Error::other(format!("the template has no {}", path.join("."))))?;
    }
    Ok(value)
}

fn integer(table: &toml::Table, path: &[&str]) -> io::Result<i64> {
    number(table, path)?
        .as_integer()
        .ok_or_else(|| io::Error::other(format!("{} is not an integer", path.join("."))))
}

fn float(table: &toml::Table, path: &[&str]) -> io::Result<f64> {
    let value = number(table, path)?;
    value
        .as_float()
        .or_else(|| value.as_integer().map(|n| n as f64))
        .ok_or_else(|| io::Error::other(format!("{} is not a number", path.join("."))))
}

fn text(table: &toml::Table, path: &[&str]) -> io::Result<String> {
    number(table, path)?
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| io::Error::other(format!("{} is not a string", path.join("."))))
}

/// The engine config for one seed.
///
/// `Quoter` writes one market maker over the catalogue: a maker keeps the
/// venue busy with resting quotes, cancels, amends and fills on both sides.
/// `Realm` writes the deployed template's generated blocks unchanged, the
/// `[engine]` keys that shape the order path, and its `[risk]` block pointed
/// at `profile_path`, where the harness has put the fleet's own profile.
pub fn write_engine_config(
    path: &Path,
    plan: &MarketPlan,
    strategies: SimStrategies,
    profile_path: &Path,
) -> io::Result<()> {
    let Some(realm) = strategies.realm() else {
        let symbols = plan
            .names()
            .iter()
            .map(|n| format!("\"{n}\""))
            .collect::<Vec<_>>()
            .join(", ");
        return std::fs::write(
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
        );
    };
    let template = realm.template();
    let blocks = generated_strategy_blocks(template)?;
    let parsed: toml::Table =
        toml::from_str(template).map_err(|error| io::Error::other(error.to_string()))?;
    let mut out = String::new();
    let _ = write!(
        out,
        r#"
[engine]
wal_path = "replaced-by-the-harness.wal"
group_flush_ms = {group_flush_ms}
account_view_max_age_ms = {account_view_max_age_ms}
max_quote_age_ms = {max_quote_age_ms}
leverage_authority = "{leverage_authority}"
execution_limits = {{ mark_collar_bps = {mark_collar_bps:?}, reject_limit = {reject_limit}, reject_window_ms = {reject_window_ms} }}

[risk]
operational_profile_path = "{profile}"
disaster_stop_fraction = {disaster_stop_fraction:?}
max_account_view_age_s = {max_account_view_age_s}

{blocks}"#,
        group_flush_ms = integer(&parsed, &["engine", "group_flush_ms"])?,
        account_view_max_age_ms = integer(&parsed, &["engine", "account_view_max_age_ms"])?,
        max_quote_age_ms = integer(&parsed, &["engine", "max_quote_age_ms"])?,
        leverage_authority = text(&parsed, &["engine", "leverage_authority"])?,
        mark_collar_bps = float(&parsed, &["engine", "execution_limits", "mark_collar_bps"])?,
        reject_limit = integer(&parsed, &["engine", "execution_limits", "reject_limit"])?,
        reject_window_ms = integer(&parsed, &["engine", "execution_limits", "reject_window_ms"])?,
        profile = profile_path.display(),
        disaster_stop_fraction = float(&parsed, &["risk", "disaster_stop_fraction"])?,
        max_account_view_age_s = integer(&parsed, &["risk", "max_account_view_age_s"])?,
    );
    std::fs::write(path, out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_deployed_template_yields_its_blocks_byte_for_byte() {
        for realm in [Realm::Demo, Realm::Mainnet, Realm::Mexc, Realm::Hyperliquid] {
            let template = realm.template();
            let blocks = generated_strategy_blocks(template).expect("the region is there");
            assert!(template.contains(blocks), "{}", realm.as_str());
            assert!(
                blocks.contains("name = \"long_native\""),
                "{}",
                realm.as_str()
            );
            assert!(!blocks.contains("BEGIN GENERATED"), "{}", realm.as_str());
            assert!(!blocks.contains("END GENERATED"), "{}", realm.as_str());
            let directory = crate::testpath::temp_path("sim-realm-config");
            std::fs::create_dir_all(directory.path()).unwrap();
            let path = directory.path().join("engine.toml");
            let profile = directory.path().join("operational-profile.json");
            std::fs::write(&profile, OPERATIONAL_PROFILE).unwrap();
            write_engine_config(
                &path,
                &MarketPlan::new(3, 600),
                SimStrategies::Realm(realm),
                &profile,
            )
            .unwrap();
            let written = std::fs::read_to_string(&path).unwrap();
            assert!(
                written.contains(blocks),
                "{} config lost the template's bytes",
                realm.as_str()
            );
            let loaded = crate::config::load(&path).expect("the sim config loads");
            assert_eq!(loaded.config.strategies.len(), 3, "{}", realm.as_str());
            assert!(loaded.config.engine.signal_spool_path.is_none());
            crate::assembly::risk(&loaded.config.risk).expect("the profile is the kernel's");
            std::fs::remove_dir_all(directory.path()).unwrap();
        }
    }

    #[test]
    fn the_quoter_config_and_tape_do_not_move() {
        let directory = crate::testpath::temp_path("sim-quoter-config");
        std::fs::create_dir_all(directory.path()).unwrap();
        let path = directory.path().join("engine.toml");
        let plan = MarketPlan::new(2, 300);
        write_engine_config(&path, &plan, SimStrategies::Quoter, Path::new("unused")).unwrap();
        let written = std::fs::read(&path).unwrap();
        assert_eq!(
            hex::encode(<sha2::Sha256 as sha2::Digest>::digest(&written)),
            "0a447a9200b94159cebd7a8b9ad21e226a7c61c442f5ed3c94bd24867a3adb39"
        );
        let tape = directory.path().join("tape.jsonl");
        let summary = write_tape(&tape, &plan, Rng::new(7)).unwrap();
        assert!(summary.mid_at("BTCUSDT", plan.t0_ns).is_some());
        assert_eq!(
            hex::encode(<sha2::Sha256 as sha2::Digest>::digest(
                std::fs::read(&tape).unwrap()
            )),
            "2018c6988eb2801f1c0365a61404b8cf9f0d7c06b1a54b884dd2b114f7310fd3"
        );
        std::fs::remove_dir_all(directory.path()).unwrap();
    }
}
