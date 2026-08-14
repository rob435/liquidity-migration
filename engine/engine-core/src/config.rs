//! `engine.toml`: what the engine reads at boot.
//!
//! The file never contains a venue address. `venue` names one of the adapters
//! compiled into the venue crate, and every host that crate knows is a demo
//! host; config chooses strategies, sizes, timings, and which adapter runs,
//! but nothing about the destination.

use std::path::{Path, PathBuf};

use serde::Deserialize;
use sha2::{Digest, Sha256};

#[derive(Debug)]
pub enum ConfigError {
    Read { path: String, source: std::io::Error },
    Parse { path: String, detail: String },
}

impl std::fmt::Display for ConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ConfigError::Read { path, source } => write!(f, "cannot read {path}: {source}"),
            ConfigError::Parse { path, detail } => write!(f, "cannot understand {path}: {detail}"),
        }
    }
}

impl std::error::Error for ConfigError {}

#[derive(Clone, Debug, Deserialize)]
pub struct Config {
    pub engine: EngineSection,
    /// Passed to the risk kernel untouched. The engine does not read inside it.
    #[serde(default)]
    pub risk: toml::Table,
    #[serde(default, rename = "strategy")]
    pub strategies: Vec<StrategyConfig>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct EngineSection {
    pub wal_path: PathBuf,
    /// Which venue adapter to run, by name — not an address. Left out means
    /// the Bybit demo account, which is what every config meant before this
    /// key existed. An unknown name is refused at assembly, not defaulted.
    #[serde(default = "default_venue")]
    pub venue: String,
    /// How often buffered log bytes are pushed to the OS.
    #[serde(default = "default_group_flush_ms")]
    pub group_flush_ms: u64,
    /// How old an account reading may be before the risk kernel should treat
    /// it as unknown. The engine refreshes at half this age.
    #[serde(default = "default_account_view_max_age_ms")]
    pub account_view_max_age_ms: u64,
    /// True: compute and log orders, send nothing.
    #[serde(default = "default_shadow")]
    pub shadow: bool,
    /// Where the research system writes its target book. Left out means no
    /// watcher is started at all — which is *no decision*, not an empty book:
    /// a follower plug holds whatever it holds.
    #[serde(default)]
    pub target_book_path: Option<PathBuf>,
    /// Where to write the heartbeat file — one line saying how this engine
    /// is, for something outside the process to read. Left out means none is
    /// written and nothing is said about it: an engine nobody asked to report
    /// on itself is not a fault.
    #[serde(default)]
    pub heartbeat_path: Option<PathBuf>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StrategyConfig {
    pub name: String,
    pub capital_usdt: f64,
    /// Everything else in the block, handed to the strategy as written.
    #[serde(flatten)]
    pub params: toml::Table,
}

/// Spelled out rather than imported so this layer stays ignorant of the venue
/// crate — `assembly.rs` is the one place that names the concrete crates. A
/// test below pins the two spellings together, so they cannot drift apart
/// without the suite saying so.
fn default_venue() -> String {
    "bybit_demo".to_string()
}

fn default_group_flush_ms() -> u64 {
    250
}

fn default_account_view_max_age_ms() -> u64 {
    5_000
}

fn default_shadow() -> bool {
    true
}

/// A config file plus the hash that goes in the log's Boot record.
#[derive(Clone, Debug)]
pub struct LoadedConfig {
    pub config: Config,
    pub sha256: String,
    pub path: PathBuf,
}

pub fn load(path: &Path) -> Result<LoadedConfig, ConfigError> {
    let bytes = std::fs::read(path).map_err(|source| ConfigError::Read {
        path: path.display().to_string(),
        source,
    })?;
    let sha256 = sha256_hex(&bytes);
    let text = String::from_utf8_lossy(&bytes).into_owned();
    let config = toml::from_str::<Config>(&text).map_err(|e| ConfigError::Parse {
        path: path.display().to_string(),
        detail: e.to_string(),
    })?;
    Ok(LoadedConfig {
        config,
        sha256,
        path: path.to_path_buf(),
    })
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
[engine]
wal_path = "engine.wal"
account_view_max_age_ms = 4000

[risk]
loss_floor_frac = 0.8
max_symbols = 4

[[strategy]]
name = "quote_taker"
capital_usdt = 250.0
every_nth_quote = 20
symbols = ["BTCUSDT"]
"#;

    #[test]
    fn defaults_apply_and_extras_reach_the_strategy() {
        let cfg: Config = toml::from_str(SAMPLE).unwrap();
        assert_eq!(cfg.engine.group_flush_ms, 250);
        assert!(cfg.engine.shadow, "shadow is the default");
        assert_eq!(
            cfg.engine.venue, engine_venue::BYBIT_DEMO,
            "a config written before the key existed still means the demo venue"
        );
        assert_eq!(
            cfg.engine.target_book_path, None,
            "no path means no watcher, which is no decision"
        );
        assert_eq!(
            cfg.engine.heartbeat_path, None,
            "no path means no heartbeat is written and nothing is said about it"
        );
        assert_eq!(cfg.engine.account_view_max_age_ms, 4000);
        assert_eq!(cfg.risk.get("max_symbols").unwrap().as_integer(), Some(4));
        let s = &cfg.strategies[0];
        assert_eq!(s.name, "quote_taker");
        assert_eq!(s.capital_usdt, 250.0);
        assert_eq!(s.params.get("every_nth_quote").unwrap().as_integer(), Some(20));
        assert!(s.params.get("name").is_none(), "name is not a param");
    }

    #[test]
    fn a_named_target_book_path_is_read_as_written() {
        let src = SAMPLE.replace(
            "wal_path = \"engine.wal\"",
            "wal_path = \"engine.wal\"\ntarget_book_path = \"var/targets/carry.json\"",
        );
        let cfg: Config = toml::from_str(&src).unwrap();
        assert_eq!(
            cfg.engine.target_book_path,
            Some(PathBuf::from("var/targets/carry.json"))
        );
    }

    #[test]
    fn a_named_heartbeat_path_is_read_as_written() {
        let src = SAMPLE.replace(
            "wal_path = \"engine.wal\"",
            "wal_path = \"engine.wal\"\nheartbeat_path = \"var/engine-heartbeat.json\"",
        );
        let cfg: Config = toml::from_str(&src).unwrap();
        assert_eq!(
            cfg.engine.heartbeat_path,
            Some(PathBuf::from("var/engine-heartbeat.json"))
        );
    }

    #[test]
    fn a_named_venue_is_read_as_written() {
        let src = SAMPLE.replace(
            "wal_path = \"engine.wal\"",
            "wal_path = \"engine.wal\"\nvenue = \"some_other_venue\"",
        );
        let cfg: Config = toml::from_str(&src).unwrap();
        assert_eq!(cfg.engine.venue, "some_other_venue");
    }

    #[test]
    fn hash_is_stable_and_content_bound() {
        assert_eq!(sha256_hex(b""), sha256_hex(b""));
        assert_ne!(sha256_hex(b"a"), sha256_hex(b"b"));
        assert_eq!(sha256_hex(b"").len(), 64);
    }
}
