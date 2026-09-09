//! Controlled physical-account identity, separate from a rotating credential.
use std::collections::{HashMap, HashSet};
use std::io::Read;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};

use engine_types::VenueError;
use serde::Deserialize;
use sha2::{Digest, Sha256};

pub(crate) const BINDINGS_PATH: &str = "/etc/liquidity-migration/mexc-account-bindings.json";
const MAX_BYTES: u64 = 64 * 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Registry {
    schema_version: u8,
    realm: String,
    accounts: Vec<Account>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Account {
    /// The actual trading account/subaccount UID verified during provisioning.
    account_uid: String,
    credential_sha256: Vec<String>,
}

#[derive(Clone)]
pub(crate) struct AccountBinding {
    user_id: String,
    credential_sha256: String,
}

fn bad(detail: &str) -> VenueError {
    VenueError::BadRequest(format!("MEXC account binding: {detail}"))
}

fn fingerprint(key: &str) -> String {
    hex::encode(Sha256::digest(key.as_bytes()))
}

impl AccountBinding {
    pub(crate) fn load(key: &str) -> Result<Self, VenueError> {
        let file = std::fs::OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(BINDINGS_PATH)
            .map_err(|_| bad("a root-controlled account registry is required before authentication or mutation"))?;
        let metadata = file
            .metadata()
            .map_err(|_| bad("cannot verify registry metadata"))?;
        if !metadata.is_file() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
            return Err(bad(
                "registry must be a root-owned regular file, not group/world writable",
            ));
        }
        let mut bytes = Vec::new();
        file.take(MAX_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| bad("cannot read account registry"))?;
        if bytes.len() as u64 > MAX_BYTES {
            return Err(bad("account registry exceeds 64 KiB"));
        }
        Self::parse(&bytes, key)
    }

    pub(crate) fn parse(bytes: &[u8], key: &str) -> Result<Self, VenueError> {
        if key.is_empty() || bytes.len() as u64 > MAX_BYTES {
            return Err(bad("empty credential or oversized registry"));
        }
        let registry: Registry =
            serde_json::from_slice(bytes).map_err(|_| bad("invalid account registry schema"))?;
        if registry.schema_version != 1 || registry.realm != "mexc_mainnet" {
            return Err(bad("unsupported account registry version or realm"));
        }
        let mut accounts = HashSet::new();
        let mut credentials = HashMap::new();
        for account in registry.accounts {
            let uid = &account.account_uid;
            if uid.is_empty()
                || uid.len() > 40
                || uid.starts_with('0')
                || !uid.bytes().all(|byte| byte.is_ascii_digit())
                || !accounts.insert(uid.clone())
                || account.credential_sha256.is_empty()
            {
                return Err(bad(
                    "each physical account needs one canonical positive UID and bound credentials",
                ));
            }
            for digest in account.credential_sha256 {
                if digest.len() != 64
                    || !digest
                        .bytes()
                        .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
                    || credentials.insert(digest, uid.clone()).is_some()
                {
                    return Err(bad(
                        "credential fingerprints must be unique lowercase SHA-256 values",
                    ));
                }
            }
        }
        let credential_sha256 = fingerprint(key);
        let account_uid = credentials
            .get(&credential_sha256)
            .ok_or_else(|| bad("credential is not bound to a reviewed physical account"))?;
        Ok(Self {
            user_id: format!("uid-{account_uid}"),
            credential_sha256,
        })
    }

    pub(crate) fn identity_for(&self, key: &str) -> Result<&str, VenueError> {
        if fingerprint(key) != self.credential_sha256 {
            return Err(bad("credential/account mismatch"));
        }
        Ok(&self.user_id)
    }

    /// Synthetic physical account, only for existing local-fixture constructors.
    pub(crate) fn fixture(key: &str) -> Self {
        Self {
            user_id: "uid-42".into(),
            credential_sha256: fingerprint(key),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn registry() -> Vec<u8> {
        serde_json::to_vec(&json!({"schema_version":1,"realm":"mexc_mainnet","accounts":[
            {"account_uid":"42","credential_sha256":[fingerprint("old-key"),fingerprint("rotated-key")]},
            {"account_uid":"43","credential_sha256":[fingerprint("subaccount-key")]}
        ]})).unwrap()
    }

    #[test]
    fn rotation_preserves_account_identity_and_subaccounts_remain_distinct() {
        let a = AccountBinding::parse(&registry(), "old-key").unwrap();
        let b = AccountBinding::parse(&registry(), "rotated-key").unwrap();
        let sub = AccountBinding::parse(&registry(), "subaccount-key").unwrap();
        let path = |binding: &AccountBinding| {
            crate::lease::canonical_path("mexc", "mexc_mainnet", &binding.user_id)
        };
        assert_eq!(path(&a), path(&b));
        assert_ne!(path(&a), path(&sub));
        assert!(
            a.identity_for("rotated-key").is_err(),
            "a credential cannot reuse another credential's binding object"
        );
        assert!(AccountBinding::parse(&registry(), "unbound-key").is_err());
        assert!(AccountBinding::parse(&registry(), "").is_err());

        let directory = std::env::temp_dir().join(format!(
            "mexc-binding-{}-{}",
            std::process::id(),
            crate::mono_ns()
        ));
        let local = directory.join(path(&a).file_name().unwrap());
        let lease = crate::lease::acquire_at(&local, "mexc_mainnet", "old-key-fixture").unwrap();
        assert!(matches!(
            crate::lease::acquire_at(&local, "mexc_mainnet", "rotated-key-fixture"),
            Err(crate::lease::LeaseError::AlreadyHeld { .. })
        ));
        drop(lease);
        std::fs::remove_file(local).unwrap();
        std::fs::remove_dir(directory).unwrap();
    }

    #[test]
    fn ambiguous_noncanonical_and_wrong_realm_registries_are_rejected() {
        let baseline: serde_json::Value = serde_json::from_slice(&registry()).unwrap();
        for mutation in 0..6 {
            let mut page = baseline.clone();
            match mutation {
                0 => {
                    page["accounts"][1]["credential_sha256"] =
                        page["accounts"][0]["credential_sha256"].clone()
                }
                1 => page["accounts"][0]["account_uid"] = json!("042"),
                2 => page["accounts"][0]["account_uid"] = json!("../42"),
                3 => page["accounts"][0]["credential_sha256"] = json!([]),
                4 => page["realm"] = json!("another_realm"),
                _ => page["schema_version"] = json!(2),
            }
            assert!(AccountBinding::parse(&serde_json::to_vec(&page).unwrap(), "old-key").is_err());
        }
    }
}
