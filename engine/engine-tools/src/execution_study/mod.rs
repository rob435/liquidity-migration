//! Paired one-sided execution research on observed order intentions and public tape.

use std::collections::BTreeMap;
use std::path::PathBuf;

use engine_types::{InstrumentRule, Intent, OrderRequest};
use serde::{Deserialize, Serialize};

pub mod observed;
mod report;
pub mod runner;
pub mod sim;

pub fn run(args: &[String]) -> Result<()> {
    let value = |flag: &str| {
        args.windows(2)
            .find(|pair| pair[0] == flag)
            .map(|pair| pair[1].as_str())
    };
    if let Some(path) = value("--config") {
        return runner::run(std::path::Path::new(path));
    }
    let family = PathBuf::from(value("--wal").ok_or("execution-study needs --wal PATH")?);
    let output = PathBuf::from(value("--state").ok_or("execution-study needs --state PATH")?);
    let since_ns: u64 = value("--since-ns")
        .ok_or("execution-study needs --since-ns UNIX_NS")?
        .parse()?;
    let mut state: ObservedState = if output.exists() {
        serde_json::from_slice(&std::fs::read(&output)?)?
    } else {
        ObservedState::default()
    };
    observed::scan(&family, since_ns, &mut state)?;
    if let Some(parent) = output.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let temporary = output.with_extension(format!("{}.tmp", std::process::id()));
    std::fs::write(&temporary, serde_json::to_vec(&state)?)?;
    std::fs::rename(temporary, &output)?;
    println!(
        "execution-study orders={} timed={} segment={} offset={} records={}",
        state.orders.len(),
        state
            .orders
            .values()
            .filter(|o| o.decision_ns.is_some())
            .count(),
        state.segment,
        state.offset,
        state.records_read
    );
    Ok(())
}

pub type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ActualFill {
    pub exec_id: String,
    pub at_ns: u64,
    pub qty: f64,
    pub price: f64,
    pub fee: Option<f64>,
    pub maker: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ObservedOrder {
    pub request: OrderRequest,
    pub intent: Option<Intent>,
    pub symbol: String,
    pub sleeve: String,
    pub engine_commit: Option<String>,
    pub source_segment: u64,
    pub source_offset: u64,
    pub process_epoch_ms: i64,
    pub wire_mono_ns: u64,
    pub decision_ns: Option<u64>,
    pub socket_write_ns: Option<u64>,
    pub transport_rtt_ns: Option<u64>,
    pub arrival_mid: f64,
    pub rule: Option<InstrumentRule>,
    pub fills: BTreeMap<String, ActualFill>,
    pub terminal: Option<String>,
    pub amends: u64,
    pub cancels: u64,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct ObservedState {
    pub schema: u32,
    pub family: PathBuf,
    pub segment: u64,
    pub offset: u64,
    pub since_ns: u64,
    pub process_epoch_ms: i64,
    pub engine_commit: Option<String>,
    pub symbols: Vec<String>,
    pub strategies: Vec<String>,
    pub rules: BTreeMap<String, InstrumentRule>,
    pub orders: BTreeMap<String, ObservedOrder>,
    pub records_read: u64,
    pub unresolved_clocks: u64,
}
