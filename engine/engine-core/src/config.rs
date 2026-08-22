//! `engine.toml`: what the engine reads at boot.
//!
//! The file never contains a venue address. `venue` names one of the adapters
//! compiled into the venue crate; config chooses strategies, sizes, timings,
//! and which adapter runs, but never an endpoint.
//!
//! One of the names it can pick is `bybit_mainnet`, the funded account. That
//! is not a hole in the above — naming it is not permission to trade it. The
//! gateway refuses to build unless `REAL_MONEY` is armed in the host's
//! credential file by the account owner, so the worst a wrong name in this
//! file can do is stop the engine starting.

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
    /// Rotate the log into a fresh segment once the current one passes this
    /// many megabytes, checked on the group-flush tick. Old segments are
    /// archived in place, never deleted — retention is the owner's decision.
    /// Left out means 256, so a config written before rotation existed still
    /// boots and still rotates. Zero turns rotation off.
    #[serde(default = "default_wal_rotate_mb")]
    pub wal_rotate_mb: u64,
    /// How old an account reading may be before the risk kernel should treat
    /// it as unknown. The engine refreshes at half this age.
    #[serde(default = "default_account_view_max_age_ms")]
    pub account_view_max_age_ms: u64,
    /// How old the quote a decision was priced against may be before an
    /// entry is refused. Exits always flow. Left out means 30 seconds; a
    /// symbol that has never quoted at all is refused for entries the same
    /// way, because the absence of a price is the stalest price there is.
    #[serde(default = "default_max_quote_age_ms")]
    pub max_quote_age_ms: u64,
    /// Who else can change leverage on this account.
    ///
    /// `"shared"` (the default, and the behavior every config before this key
    /// meant): a hand may retune a symbol while it sits flat, so the engine
    /// forgets what it set the moment a symbol goes flat and re-confirms with
    /// the venue before the next entry — one round trip (~172 ms measured)
    /// per entry from flat. `"sole"`: this engine holds the account's single
    /// mutation lease and is the only leverage writer, so what it set stays
    /// trusted across flat spells, entries skip the round trip, and instead
    /// every HELD position's leverage is read back off the venue's own
    /// position rows and any mismatch alarms and evicts the trust.
    #[serde(default)]
    pub leverage_authority: LeverageAuthority,
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

/// See [`EngineSection::leverage_authority`].
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LeverageAuthority {
    #[default]
    Shared,
    Sole,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StrategyConfig {
    pub name: String,
    /// What this block is called in the log's id table, the heartbeat and the
    /// cost report. Defaults to `name`, which is right whenever the strategy
    /// is named after its sleeve — and is not, when two blocks run one plug.
    #[serde(default)]
    pub sleeve: Option<String>,
    /// Where the producer writes this strategy's target book.
    ///
    /// Books are routed, not broadcast: this one reaches this strategy and no
    /// other. Two sleeves on one account each name their own file, which is
    /// the only thing that keeps them from acting on each other's decisions.
    #[serde(default)]
    pub book_path: Option<PathBuf>,
    /// Everything else in the block, handed to the strategy as written.
    #[serde(flatten)]
    pub params: toml::Table,
}

impl StrategyConfig {
    /// The name this block goes by.
    pub fn sleeve_name(&self) -> &str {
        self.sleeve.as_deref().unwrap_or(&self.name)
    }
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

fn default_wal_rotate_mb() -> u64 {
    256
}

fn default_account_view_max_age_ms() -> u64 {
    5_000
}

fn default_max_quote_age_ms() -> u64 {
    30_000
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
sleeve = "quotes"
every_nth_quote = 20
symbols = ["BTCUSDT"]
"#;

    #[test]
    fn defaults_apply_and_extras_reach_the_strategy() {
        let cfg: Config = toml::from_str(SAMPLE).unwrap();
        assert_eq!(cfg.engine.group_flush_ms, 250);
        assert_eq!(
            cfg.engine.wal_rotate_mb, 256,
            "a config written before rotation existed must still boot, and still rotate"
        );
        assert_eq!(
            cfg.engine.max_quote_age_ms, 30_000,
            "a config written before the quote bound existed must still boot, bounded"
        );
        assert_eq!(
            cfg.engine.leverage_authority,
            LeverageAuthority::Shared,
            "a config written before the key existed still means a hand may retune leverage"
        );
        let sole: Config =
            toml::from_str(&SAMPLE.replace("[risk]", "leverage_authority = \"sole\"\n\n[risk]"))
                .unwrap();
        assert_eq!(sole.engine.leverage_authority, LeverageAuthority::Sole);
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
        assert_eq!(s.sleeve_name(), "quotes");
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
