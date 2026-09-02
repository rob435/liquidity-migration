use serde::Deserialize;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

#[derive(Clone, Debug, Deserialize)]
pub struct CaptureConfig {
    pub venue: VenueConfig,
    #[serde(default)]
    pub storage: StorageConfig,
    #[serde(default)]
    pub budget: BudgetConfig,
    #[serde(default, rename = "tier")]
    pub tiers: Vec<TierConfig>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct VenueConfig {
    pub name: String,
    pub market: String,
}

#[derive(Clone, Debug, Deserialize)]
pub struct StorageConfig {
    #[serde(default = "default_root")]
    pub root: PathBuf,
    #[serde(default = "default_segment_max_mb")]
    pub segment_max_mb: u64,
    #[serde(default = "default_retention_days")]
    pub retention_days: u32,
    #[serde(default = "default_status_interval_seconds")]
    pub status_interval_seconds: u64,
}

impl Default for StorageConfig {
    fn default() -> Self {
        Self {
            root: default_root(),
            segment_max_mb: default_segment_max_mb(),
            retention_days: default_retention_days(),
            status_interval_seconds: default_status_interval_seconds(),
        }
    }
}

fn default_root() -> PathBuf {
    PathBuf::from("/var/lib/liquidity-migration/forward-market")
}
fn default_segment_max_mb() -> u64 {
    64
}
fn default_retention_days() -> u32 {
    30
}
fn default_status_interval_seconds() -> u64 {
    30
}

#[derive(Clone, Debug, Default, Deserialize)]
pub struct BudgetConfig {
    #[serde(default)]
    pub monthly_gb: f64,
    #[serde(default)]
    pub shed: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct TierConfig {
    pub name: String,
    pub feeds: Vec<String>,
    #[serde(default)]
    pub universe: Option<UniverseConfig>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "kind")]
pub enum UniverseConfig {
    #[serde(rename = "file")]
    File { path: PathBuf },
    #[serde(rename = "symbols")]
    Symbols { symbols: Vec<String> },
    #[serde(rename = "top_turnover")]
    TopTurnover {
        #[serde(default)]
        top: usize,
        #[serde(default)]
        leave_top: usize,
        #[serde(default)]
        quote: String,
    },
    #[serde(rename = "funding_below")]
    FundingBelow {
        #[serde(default)]
        threshold_bp: i32,
        #[serde(default)]
        sticky_hours: u32,
        #[serde(default)]
        quote: String,
    },
    #[serde(rename = "listed")]
    Listed {
        #[serde(default)]
        quote: Option<String>,
    },
    #[serde(other)]
    Unknown,
}

impl CaptureConfig {
    pub fn load_from_file(path: impl AsRef<Path>) -> Result<Self, Box<dyn std::error::Error>> {
        let content = std::fs::read_to_string(path)?;
        let config: Self = toml::from_str(&content)?;
        Ok(config)
    }

    /// Resolve initial static symbols across file and symbol universe configs.
    pub fn static_symbols(&self, repo_root: &Path) -> BTreeSet<String> {
        let mut symbols = BTreeSet::new();
        for tier in &self.tiers {
            match &tier.universe {
                Some(UniverseConfig::Symbols { symbols: list }) => {
                    for s in list {
                        symbols.insert(s.to_uppercase());
                    }
                }
                Some(UniverseConfig::File { path }) => {
                    let full_path = if path.is_absolute() {
                        path.clone()
                    } else {
                        repo_root.join(path)
                    };
                    if let Ok(content) = std::fs::read_to_string(&full_path) {
                        for line in content.lines() {
                            let trimmed = line.trim();
                            if !trimmed.is_empty() && !trimmed.starts_with('#') {
                                symbols.insert(trimmed.to_uppercase());
                            }
                        }
                    }
                }
                _ => {}
            }
        }
        symbols
    }
}
