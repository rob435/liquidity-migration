use super::*;
use engine_types::{
    SignalReadinessRequest, SignalReadinessResponse, SignalSourceFrontier,
    SIGNAL_READINESS_REQUEST_FILE, SIGNAL_READINESS_RESPONSE_FILE, SIGNAL_READINESS_SCHEMA_VERSION,
};
use std::hash::BuildHasher;

type ReadinessResult = Result<Vec<SignalSourceFrontier>, String>;

pub(super) struct ReadinessExchange {
    receiver: tokio::sync::watch::Receiver<Option<ReadinessResult>>,
    task: tokio::task::JoinHandle<()>,
}

impl ReadinessExchange {
    pub(super) fn start(directory: PathBuf, poll: Duration) -> Self {
        let nonce = format!(
            "{:016x}",
            std::collections::hash_map::RandomState::new()
                .hash_one((std::process::id(), crate::clock::wall_ms()))
        );
        let request = SignalReadinessRequest {
            schema_version: SIGNAL_READINESS_SCHEMA_VERSION,
            boot_nonce: nonce,
        };
        let (sender, receiver) = tokio::sync::watch::channel(None);
        let task = tokio::spawn(async move {
            let result = exchange(directory, request, poll).await;
            if result.is_err() {
                tokio::time::sleep(poll).await;
            }
            let _ = sender.send(Some(result));
        });
        Self { receiver, task }
    }

    pub(super) fn receiver(&self) -> tokio::sync::watch::Receiver<Option<ReadinessResult>> {
        self.receiver.clone()
    }
}

impl Drop for ReadinessExchange {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub(super) async fn receive(
    mut receiver: tokio::sync::watch::Receiver<Option<ReadinessResult>>,
) -> Result<Vec<SignalSourceFrontier>, SignalError> {
    loop {
        if let Some(result) = receiver.borrow_and_update().clone() {
            return result.map_err(SignalError::Source);
        }
        receiver.changed().await.map_err(|_| {
            SignalError::Source("producer readiness task ended without a response".into())
        })?;
    }
}

async fn exchange(
    directory: PathBuf,
    request: SignalReadinessRequest,
    poll: Duration,
) -> ReadinessResult {
    let path = directory.join(SIGNAL_READINESS_REQUEST_FILE);
    let encoded = serde_json::to_vec(&request).map_err(|error| error.to_string())?;
    let temporary = directory.join(format!(".input-readiness-{}.tmp", request.boot_nonce));
    tokio::task::spawn_blocking(move || -> Result<(), String> {
        use std::io::Write;
        let result = (|| {
            let mut file = std::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(&temporary)
                .map_err(|error| error.to_string())?;
            file.write_all(&encoded)
                .map_err(|error| error.to_string())?;
            file.sync_all().map_err(|error| error.to_string())?;
            std::fs::rename(&temporary, &path).map_err(|error| error.to_string())
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&temporary);
        }
        result
    })
    .await
    .map_err(|error| error.to_string())??;
    loop {
        let path = directory.join(SIGNAL_READINESS_RESPONSE_FILE);
        let response = tokio::task::spawn_blocking(
            move || -> Result<Option<SignalReadinessResponse>, String> {
                let file = match std::fs::File::open(&path) {
                    Ok(file) => file,
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
                    Err(error) => return Err(error.to_string()),
                };
                if file.metadata().map_err(|error| error.to_string())?.len()
                    > engine_types::MAX_STRATEGY_STATE_BYTES as u64
                {
                    return Err("producer readiness response exceeds its metadata budget".into());
                }
                serde_json::from_reader(file)
                    .map(Some)
                    .map_err(|error| error.to_string())
            },
        )
        .await
        .map_err(|error| error.to_string())??;
        if let Some(response) = response {
            if response.schema_version == SIGNAL_READINESS_SCHEMA_VERSION
                && response.boot_nonce == request.boot_nonce
            {
                return Ok(response.sources);
            }
        }
        tokio::time::sleep(poll).await;
    }
}
