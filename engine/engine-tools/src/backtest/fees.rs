use std::collections::BTreeMap;
use std::path::PathBuf;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// The authenticated mainnet snapshot the binary carries, so a packaged
/// `engine sim` prices fills the same way outside a checkout.
const EMBEDDED: &[u8] = include_bytes!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../configs/bybit_fee_rates.json"
));

/// Where the snapshot bytes came from.
pub enum FeeSource {
    Embedded,
    Path(PathBuf),
}

#[derive(Clone, Debug, Serialize)]
pub struct Fees {
    pub taker: f64,
    pub maker: f64,
    /// The file the rates were read from; `None` for the embedded snapshot
    /// and for explicit scenario rates.
    pub snapshot_path: Option<PathBuf>,
    pub snapshot_source: &'static str,
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

/// `LIQUIDITY_MIGRATION_FEE_SNAPSHOT` names a file; without it the rates are
/// the embedded snapshot. There is no search: the working directory decides
/// nothing.
pub fn default_source() -> FeeSource {
    match std::env::var_os("LIQUIDITY_MIGRATION_FEE_SNAPSHOT") {
        Some(path) => FeeSource::Path(PathBuf::from(path)),
        None => FeeSource::Embedded,
    }
}

pub fn resolve(taker: Option<f64>, maker: Option<f64>, source: FeeSource) -> Result<Fees, String> {
    let valid = |rate: f64| rate.is_finite() && (-0.01..=0.01).contains(&rate);
    if taker.is_some_and(|r| !valid(r)) || maker.is_some_and(|r| !valid(r)) {
        return Err("invalid explicit research fee rate".into());
    }
    if let (Some(taker), Some(maker)) = (taker, maker) {
        return Ok(Fees {
            taker,
            maker,
            snapshot_path: None,
            snapshot_source: "none",
            snapshot_sha256: None,
            oldest_observed_ns: None,
            selection: "explicit scenario",
        });
    }
    let (bytes, path) = match &source {
        FeeSource::Embedded => (EMBEDDED.to_vec(), None),
        FeeSource::Path(path) => (
            std::fs::read(path).map_err(|e| format!("fee snapshot {}: {e}", path.display()))?,
            Some(path.clone()),
        ),
    };
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
        snapshot_source: match source {
            FeeSource::Embedded => "embedded",
            FeeSource::Path(_) => "LIQUIDITY_MIGRATION_FEE_SNAPSHOT",
        },
        snapshot_path: path,
        snapshot_sha256: Some(hex::encode(Sha256::digest(&bytes))),
        oldest_observed_ns: snapshot.rates.values().map(|r| r.observed_ns).min(),
        selection: "maximum observed rate for unspecified fees; unobserved symbols are not covered",
    })
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::*;

    #[test]
    fn a_named_snapshot_reloads_authenticated_rates_and_keeps_explicit_scenarios() {
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
            let fees = resolve(None, None, FeeSource::Path(path.clone())).unwrap();
            assert_eq!(fees.taker, taker);
            assert_eq!(fees.maker, 0.0004);
            assert_eq!(fees.oldest_observed_ns, Some(123));
            assert_eq!(fees.snapshot_path, Some(path.clone()));
            assert_eq!(fees.snapshot_source, "LIQUIDITY_MIGRATION_FEE_SNAPSHOT");
            assert!(fees.snapshot_sha256.is_some());
            // The named file is the whole answer: the embedded snapshot is
            // not consulted and does not price anything here.
            assert_ne!(
                fees.snapshot_sha256,
                resolve(None, None, FeeSource::Embedded)
                    .unwrap()
                    .snapshot_sha256
            );
        }
        std::fs::remove_file(&path).unwrap();
        assert!(resolve(None, None, FeeSource::Path(path.clone())).is_err());
        assert_eq!(
            resolve(Some(0.00055), Some(0.0002), FeeSource::Path(path.clone()))
                .unwrap()
                .taker,
            0.00055
        );
        assert!(resolve(Some(f64::NAN), Some(0.0), FeeSource::Path(path)).is_err());
    }

    /// The packaged binary carries the committed snapshot, so it prices fills
    /// with no checkout, no working directory and no file to install.
    #[test]
    fn the_embedded_default_is_the_committed_snapshot() {
        let committed =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../../configs/bybit_fee_rates.json");
        let embedded = resolve(None, None, FeeSource::Embedded).unwrap();
        let from_file = resolve(None, None, FeeSource::Path(committed)).unwrap();
        assert_eq!(
            embedded.snapshot_sha256,
            Some(hex::encode(Sha256::digest(EMBEDDED)))
        );
        assert_eq!(embedded.snapshot_sha256, from_file.snapshot_sha256);
        assert_eq!(embedded.taker, from_file.taker);
        assert_eq!(embedded.maker, from_file.maker);
        assert_eq!(embedded.oldest_observed_ns, from_file.oldest_observed_ns);
        assert_eq!(embedded.snapshot_source, "embedded");
        assert_eq!(embedded.snapshot_path, None);
    }
}
