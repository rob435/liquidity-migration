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

    /// Move a refused request out of the served namespace without deleting
    /// its bytes. A refused file left in place would be re-read on every
    /// poll and on every supervised restart, forever.
    fn quarantine(path: &Path, reason: &str) -> Result<(), RuntimeControlError> {
        let target = rejected_path(path);
        match std::fs::rename(path, &target) {
            Ok(()) => {
                tracing::error!(
                    quarantined = %target.display(),
                    reason,
                    "rejected runtime control request"
                );
                Ok(())
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(RuntimeControlError::Source(format!(
                "cannot quarantine rejected runtime control file {}: {error}",
                path.display()
            ))),
        }
    }
}

fn rejected_path(path: &Path) -> PathBuf {
    let mut name = path.file_name().unwrap_or_default().to_os_string();
    name.push(".rejected");
    path.with_file_name(name)
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
            for path in paths {
                let read_path = path.clone();
                let outcome = tokio::task::spawn_blocking(move || Self::read_one(&read_path))
                    .await
                    .map_err(|error| {
                        RuntimeControlError::Source(format!(
                            "runtime control read task failed: {error}"
                        ))
                    })?;
                match outcome {
                    Ok(request) => {
                        self.returned_path = Some(path);
                        return Ok(request);
                    }
                    // An unreadable file can never be served: a garbled write,
                    // or a request from another generation surviving an
                    // upgrade. Left in place it would fail identically on
                    // every poll and every supervised restart.
                    Err(error) => {
                        let reason = error.to_string();
                        tokio::task::spawn_blocking(move || {
                            Self::quarantine(&path, &reason)
                        })
                        .await
                        .map_err(|error| {
                            RuntimeControlError::Source(format!(
                                "runtime control quarantine task failed: {error}"
                            ))
                        })??;
                    }
                }
            }
            tokio::time::sleep(self.poll).await;
        }
    }

    async fn reject_last(&mut self) -> Result<(), RuntimeControlError> {
        let Some(path) = self.returned_path.take() else {
            return Ok(());
        };
        tokio::task::spawn_blocking(move || Self::quarantine(&path, "refused by the engine core"))
            .await
            .map_err(|error| {
                RuntimeControlError::Source(format!(
                    "runtime control reject task failed: {error}"
                ))
            })?
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
    // Resubmitting exact bytes asks for a fresh verdict; a marker left from
    // an earlier refusal of this hash must not be read as this submission's
    // outcome.
    match std::fs::remove_file(rejected_path(&final_path)) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => {
            return Err(format!(
                "cannot clear stale rejection marker {}: {error}",
                rejected_path(&final_path).display()
            ));
        }
    }
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
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    // The engine retires a request one of two ways: consumed
                    // (removed) or refused (renamed to a rejection marker).
                    return if rejected_path(&path).exists() {
                        Err(format!(
                            "runtime control request was rejected by the engine; \
                             the refused bytes remain at {}",
                            rejected_path(&path).display()
                        ))
                    } else {
                        Ok(())
                    };
                }
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

    #[tokio::test]
    async fn unreadable_spool_files_are_quarantined_not_fatal() {
        let directory = crate::testpath::temp_path("runtime-control-poison");
        std::fs::create_dir(directory.path()).unwrap();
        // A request from another generation surviving an upgrade.
        let mut stale = serde_json::to_value(request("pause-1", false)).unwrap();
        stale["schema_version"] = serde_json::Value::from(u16::MAX);
        let stale_name = format!("{}.json", "0".repeat(64));
        std::fs::write(
            directory.path().join(&stale_name),
            serde_json::to_vec(&stale).unwrap(),
        )
        .unwrap();
        // A garbled write that never was a request.
        let garbage_name = format!("{}.json", "1".repeat(64));
        std::fs::write(directory.path().join(&garbage_name), b"not a request").unwrap();
        let mut feed = SpoolRuntimeControlFeed::new(directory.path())
            .with_poll_interval(Duration::from_millis(1));
        let served = tokio::spawn(async move { feed.next_request().await });
        let stale_marker = directory.path().join(format!("{stale_name}.rejected"));
        let garbage_marker = directory.path().join(format!("{garbage_name}.rejected"));
        for _ in 0..1000 {
            if stale_marker.exists() && garbage_marker.exists() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(1)).await;
        }
        assert!(stale_marker.exists() && garbage_marker.exists());
        let valid = request("resume-1", true);
        let valid_path = submit(directory.path(), &valid).unwrap();
        assert_eq!(served.await.unwrap().unwrap(), valid);
        for path in [stale_marker, garbage_marker, valid_path] {
            std::fs::remove_file(path).unwrap();
        }
        std::fs::remove_file(directory.path().join(".submit.lock")).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    }

    #[tokio::test]
    async fn rejected_request_is_quarantined_and_unblocks_the_spool() {
        let directory = crate::testpath::temp_path("runtime-control-reject");
        std::fs::create_dir(directory.path()).unwrap();
        let refused = request("flatten-1", false);
        let refused_path = submit(directory.path(), &refused).unwrap();
        let mut feed = SpoolRuntimeControlFeed::new(directory.path())
            .with_poll_interval(Duration::from_millis(1));
        assert_eq!(feed.next_request().await.unwrap(), refused);
        feed.reject_last().await.unwrap();
        let marker = rejected_path(&refused_path);
        assert!(!refused_path.exists());
        assert!(marker.exists());
        // The refused request no longer blocks the spool.
        let corrected = request("pause-1", false);
        let corrected_path = submit(directory.path(), &corrected).unwrap();
        std::fs::remove_file(corrected_path).unwrap();
        // Resubmitting the exact refused bytes asks for a fresh verdict.
        assert_eq!(submit(directory.path(), &refused).unwrap(), refused_path);
        assert!(!marker.exists());
        std::fs::remove_file(refused_path).unwrap();
        std::fs::remove_file(directory.path().join(".submit.lock")).unwrap();
        std::fs::remove_dir(directory.path()).unwrap();
    }

    #[tokio::test]
    async fn submit_and_wait_reports_a_rejected_request() {
        let directory = crate::testpath::temp_path("runtime-control-wait-reject");
        std::fs::create_dir(directory.path()).unwrap();
        let refused = request("pause-1", false);
        let spool = directory.path().to_path_buf();
        let engine_stand_in = tokio::spawn(async move {
            let mut feed =
                SpoolRuntimeControlFeed::new(spool).with_poll_interval(Duration::from_millis(1));
            feed.next_request().await.unwrap();
            feed.reject_last().await.unwrap();
        });
        let error = submit_and_wait(directory.path(), &refused, Duration::from_secs(10))
            .await
            .unwrap_err();
        assert!(error.contains("rejected"), "{error}");
        engine_stand_in.await.unwrap();
        let marker = rejected_path(&directory.path().join(format!(
            "{}.json",
            refused.content_sha256
        )));
        std::fs::remove_file(marker).unwrap();
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
