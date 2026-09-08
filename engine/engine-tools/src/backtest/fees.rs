use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Clone, Debug, Serialize)]
pub struct Fees {
    pub taker: f64,
    pub maker: f64,
    pub snapshot_path: Option<PathBuf>,
    pub snapshot_sha256: Option<String>,
    pub oldest_observed_ns: Option<u64>,
    pub selection: &'static str,
}

#[derive(Deserialize)]
struct Snapshot {
    realm: String,
    rates: BTreeMap<String, Rate>,
}
#[derive(Deserialize)]
struct Rate {
    taker: f64,
    maker: f64,
    observed_ns: u64,
}

pub fn default_path() -> PathBuf {
    std::env::var_os("LIQUIDITY_MIGRATION_FEE_SNAPSHOT")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            let relative = PathBuf::from("configs/bybit_fee_rates.json");
            if relative.exists() {
                relative
            } else {
                Path::new(env!("CARGO_MANIFEST_DIR")).join("../../configs/bybit_fee_rates.json")
            }
        })
}

pub fn resolve(taker: Option<f64>, maker: Option<f64>, path: &Path) -> Result<Fees, String> {
    let valid = |rate: f64| rate.is_finite() && (-0.01..=0.01).contains(&rate);
    if taker.is_some_and(|r| !valid(r)) || maker.is_some_and(|r| !valid(r)) {
        return Err("invalid explicit research fee rate".into());
    }
    if let (Some(taker), Some(maker)) = (taker, maker) {
        return Ok(Fees {
            taker,
            maker,
            snapshot_path: None,
            snapshot_sha256: None,
            oldest_observed_ns: None,
            selection: "explicit scenario",
        });
    }
    let bytes = std::fs::read(path).map_err(|e| format!("fee snapshot {}: {e}", path.display()))?;
    let snapshot: Snapshot =
        serde_json::from_slice(&bytes).map_err(|e| format!("fee snapshot: {e}"))?;
    if snapshot.realm != "mainnet"
        || snapshot.rates.is_empty()
        || snapshot.rates.iter().any(|(symbol, r)| {
            symbol.is_empty() || !valid(r.taker) || !valid(r.maker) || r.observed_ns == 0
        })
    {
        return Err("expected a non-empty authenticated mainnet fee snapshot with valid rates and observation times".into());
    }
    Ok(Fees {
        taker: taker.unwrap_or_else(|| {
            snapshot
                .rates
                .values()
                .map(|r| r.taker)
                .fold(f64::NEG_INFINITY, f64::max)
        }),
        maker: maker.unwrap_or_else(|| {
            snapshot
                .rates
                .values()
                .map(|r| r.maker)
                .fold(f64::NEG_INFINITY, f64::max)
        }),
        snapshot_path: Some(path.to_path_buf()),
        snapshot_sha256: Some(hex::encode(Sha256::digest(&bytes))),
        oldest_observed_ns: snapshot.rates.values().map(|r| r.observed_ns).min(),
        selection: "maximum observed rate for unspecified fees; unobserved symbols are not covered",
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_reload_authenticated_rates_and_keep_explicit_scenarios() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("fees.json");
        for taker in [0.001, 0.0012] {
            std::fs::write(
                &path,
                serde_json::json!({"realm":"mainnet","rates":{
                    "BTCUSDT":{"maker":0.00036,"taker":taker,"observed_ns":123},
                    "ETHUSDT":{"maker":0.0004,"taker":0.0009,"observed_ns":124}
                }})
                .to_string(),
            )
            .unwrap();
            let fees = resolve(None, None, &path).unwrap();
            assert_eq!(fees.taker, taker);
            assert_eq!(fees.maker, 0.0004);
            assert_eq!(fees.oldest_observed_ns, Some(123));
            assert!(fees.snapshot_sha256.is_some());
        }
        std::fs::remove_file(&path).unwrap();
        assert!(resolve(None, None, &path).is_err());
        assert_eq!(
            resolve(Some(0.00055), Some(0.0002), &path).unwrap().taker,
            0.00055
        );
        assert!(resolve(Some(f64::NAN), Some(0.0), &path).is_err());
    }
}
