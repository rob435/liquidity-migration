//! Lossless live operator commands for the single-writer engine.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::Duration;

use engine_types::{
    RuntimeControlError, RuntimeControlFeed, RuntimeControlRequest,
    STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
};
use sha2::{Digest, Sha256};

const FIELD_BYTES_MAX: usize = 256;
const REQUEST_BYTES_MAX: u64 = 64 * 1024;

pub fn content_sha256(request: &RuntimeControlRequest) -> String {
    hex::encode(Sha256::digest(request.canonical_envelope_bytes()))
}

pub fn validate(request: &RuntimeControlRequest) -> Result<(), String> {
    if request.schema_version != STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION {
        return Err(format!(
            "runtime control schema {} is not supported; expected {}",
            request.schema_version, STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION
        ));
    }
    for (name, value) in [
        ("strategy_name", request.strategy_name.as_str()),
        ("request_id", request.request_id.as_str()),
    ] {
        if value.is_empty() || value.len() > FIELD_BYTES_MAX {
            return Err(format!("{name} must contain 1..={FIELD_BYTES_MAX} bytes"));
        }
    }
    if request.content_sha256.len() != 64
        || !request
            .content_sha256
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err("runtime control content_sha256 must be 64 lowercase hex bytes".to_string());
    }
    let calculated = content_sha256(request);
    if calculated != request.content_sha256 {
        return Err(format!(
            "runtime control content hash is {}, calculated {}",
            request.content_sha256, calculated
        ));
    }
    Ok(())
}

pub struct NoControls;

impl RuntimeControlFeed for NoControls {
    async fn next_request(&mut self) -> Result<RuntimeControlRequest, RuntimeControlError> {
        std::future::pending().await
    }
}

/// Immutable command files. A returned file stays until the next poll; the
/// core polls again only after its WAL barrier and in-memory apply complete.
pub struct SpoolRuntimeControlFeed {
    directory: PathBuf,
    returned_path: Option<PathBuf>,
    poll: Duration,
}

impl SpoolRuntimeControlFeed {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            returned_path: None,
            poll: Duration::from_millis(100),
        }
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.poll = poll.max(Duration::from_millis(1));
        self
    }

    fn read_one(path: &Path) -> Result<RuntimeControlRequest, RuntimeControlError> {
        let metadata = std::fs::symlink_metadata(path).map_err(|error| {
            RuntimeControlError::Source(format!(
                "cannot inspect runtime control file {}: {error}",
                path.display()
            ))
        })?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(RuntimeControlError::Source(format!(
                "runtime control file {} is not a regular non-symlink file",
                path.display()
            )));
        }
        if metadata.len() > REQUEST_BYTES_MAX {
            return Err(RuntimeControlError::Source(format!(
                "runtime control file {} is {} bytes; maximum is {}",
                path.display(),
                metadata.len(),
                REQUEST_BYTES_MAX
            )));
        }
        let named_hash = path
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix(".json"))
            .ok_or_else(|| {
                RuntimeControlError::Source(
                    "runtime control filename must be <sha256>.json".to_string(),
                )
            })?;
        let raw = std::fs::read(path).map_err(|error| {
            RuntimeControlError::Source(format!(
                "cannot read runtime control file {}: {error}",
                path.display()
            ))
        })?;
        let request: RuntimeControlRequest = serde_json::from_slice(&raw).map_err(|error| {
            RuntimeControlError::Source(format!(
                "runtime control file {} is not a request: {error}",
                path.display()
            ))
        })?;
        validate(&request).map_err(|error| {
            RuntimeControlError::Source(format!("runtime control file {}: {error}", path.display()))
        })?;
        if request.content_sha256 != named_hash {
            return Err(RuntimeControlError::Source(format!(
                "runtime control file {} name does not match its envelope hash",
                path.display()
            )));
        }
        Ok(request)
    }
}

impl RuntimeControlFeed for SpoolRuntimeControlFeed {
    async fn next_request(&mut self) -> Result<RuntimeControlRequest, RuntimeControlError> {
        if let Some(path) = self.returned_path.take() {
            tokio::task::spawn_blocking(move || match std::fs::remove_file(&path) {
                Ok(()) => Ok(()),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                Err(error) => Err(RuntimeControlError::Source(format!(
                    "cannot retire durable runtime control file {}: {error}",
                    path.display()
                ))),
            })
            .await
            .map_err(|error| {
                RuntimeControlError::Source(format!("runtime control retire task failed: {error}"))
            })??;
        }
        loop {
            let directory = self.directory.clone();
            let paths = tokio::task::spawn_blocking(move || {
                let mut paths = Vec::new();
                for entry in std::fs::read_dir(&directory).map_err(|error| {
                    RuntimeControlError::Source(format!(
                        "cannot scan runtime control spool {}: {error}",
                        directory.display()
                    ))
                })? {
                    let path = entry
                        .map_err(|error| RuntimeControlError::Source(error.to_string()))?
                        .path();
                    if path
                        .extension()
                        .is_some_and(|extension| extension == "json")
                    {
                        paths.push(path);
                    }
                }
                paths.sort();
                Ok::<_, RuntimeControlError>(paths)
            })
            .await
            .map_err(|error| {
                RuntimeControlError::Source(format!("runtime control spool task failed: {error}"))
            })??;
            if let Some(path) = paths.into_iter().next() {
                let read_path = path.clone();
                let request = tokio::task::spawn_blocking(move || Self::read_one(&read_path))
                    .await
                    .map_err(|error| {
                        RuntimeControlError::Source(format!(
                            "runtime control read task failed: {error}"
                        ))
                    })??;
                self.returned_path = Some(path);
                return Ok(request);
            }
            tokio::time::sleep(self.poll).await;
        }
    }
}

/// Publish one request without replacing an earlier unconsumed request.
pub fn submit(directory: &Path, request: &RuntimeControlRequest) -> Result<PathBuf, String> {
    validate(request)?;
    let metadata = std::fs::symlink_metadata(directory).map_err(|error| {
        format!(
            "cannot inspect control spool {}: {error}",
            directory.display()
        )
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(format!(
            "control spool {} is not a non-symlink directory",
            directory.display()
        ));
    }
    let _claim = engine_wal::lock(directory.join(".submit.lock")).map_err(|e| e.to_string())?;
    let raw = serde_json::to_vec(request).map_err(|e| e.to_string())?;
    let final_path = directory.join(format!("{}.json", request.content_sha256));
    for entry in std::fs::read_dir(directory).map_err(|e| e.to_string())? {
        let path = entry.map_err(|e| e.to_string())?.path();
        if path
            .extension()
            .is_some_and(|extension| extension == "json")
        {
            if path == final_path && std::fs::read(&path).map_err(|e| e.to_string())? == raw {
                return Ok(path);
            }
            return Err(format!(
                "runtime control spool already has an unconsumed request at {}",
                path.display()
            ));
        }
    }
    let temp_path = directory.join(format!(
        ".{}.{}.tmp",
        request.content_sha256,
        std::process::id()
    ));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temp_path)
        .map_err(|e| format!("cannot create {}: {e}", temp_path.display()))?;
    if let Err(error) = file.write_all(&raw).and_then(|()| file.sync_all()) {
        let _ = std::fs::remove_file(&temp_path);
        return Err(format!("cannot write {}: {error}", temp_path.display()));
    }
    std::fs::rename(&temp_path, &final_path)
        .map_err(|e| format!("cannot publish {}: {e}", final_path.display()))?;
    Ok(final_path)
}

pub async fn submit_and_wait(
    directory: &Path,
    request: &RuntimeControlRequest,
    timeout: Duration,
) -> Result<(), String> {
    let path = submit(directory, request)?;
    tokio::time::timeout(timeout, async {
        loop {
            match std::fs::symlink_metadata(&path) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
                Err(error) => {
                    return Err(format!(
                        "cannot inspect submitted runtime control {}: {error}",
                        path.display()
                    ));
                }
                Ok(_) => tokio::time::sleep(Duration::from_millis(50)).await,
            }
        }
    })
    .await
    .map_err(|_| {
        format!(
            "runtime control was not acknowledged within {} ms; request remains at {}",
            timeout.as_millis(),
            path.display()
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::{
        RuntimeControlCommand, StrategyId, STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
    };

    fn request(id: &str, enabled: bool) -> RuntimeControlRequest {
        let mut request = RuntimeControlRequest {
            schema_version: STRATEGY_ENTRY_PERMISSION_SCHEMA_VERSION,
            strategy: StrategyId(1),
            strategy_name: "carry".into(),
            request_id: id.into(),
            command: RuntimeControlCommand::SetEntriesEnabled {
                entries_enabled: enabled,
            },
            content_sha256: String::new(),
        };
        request.content_sha256 = content_sha256(&request);
        request
    }

    #[tokio::test]
    async fn spool_retires_only_after_the_next_poll() {
        let directory = crate::testpath::temp_path("runtime-control-spool");
        std::fs::create_dir(directory.path()).unwrap();
        let first = request("pause-1", false);
        let first_path = submit(directory.path(), &first).unwrap();
        let mut feed = SpoolRuntimeControlFeed::new(directory.path())
            .with_poll_interval(Duration::from_millis(1));
        assert_eq!(feed.next_request().await.unwrap(), first);
        assert!(first_path.exists());
        let second = request("resume-1", true);
        assert!(submit(directory.path(), &second)
            .unwrap_err()
            .contains("unconsumed"));
        let wait = tokio::spawn(async move { feed.next_request().await });
        tokio::time::sleep(Duration::from_millis(10)).await;
        assert!(!first_path.exists());
        wait.abort();
        std::fs::remove_file(directory.path().join(".submit.lock")).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    }

    #[test]
    fn exact_submit_retry_is_idempotent_but_conflict_waits() {
        let directory = crate::testpath::temp_path("runtime-control-submit");
        std::fs::create_dir(directory.path()).unwrap();
        let first = request("pause-1", false);
        let path = submit(directory.path(), &first).unwrap();
        assert_eq!(submit(directory.path(), &first).unwrap(), path);
        assert!(submit(directory.path(), &request("pause-2", false)).is_err());
        std::fs::remove_file(path).unwrap();
        std::fs::remove_file(directory.path().join(".submit.lock")).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    }
}
