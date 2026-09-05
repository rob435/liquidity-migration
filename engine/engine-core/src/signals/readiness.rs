use super::*;
use engine_types::{
    SignalReadinessRequest, SignalReadinessResponse, SignalSourceFrontier,
    SIGNAL_READINESS_REQUEST_FILE, SIGNAL_READINESS_RESPONSE_FILE, SIGNAL_READINESS_SCHEMA_VERSION,
};
use std::hash::BuildHasher;

type ReadinessResult = Result<engine_types::SignalFeedEvent, String>;

#[derive(serde::Deserialize)]
#[serde(untagged)]
enum Response {
    Legacy(SignalReadinessResponse),
    Lifecycle(engine_types::SignalLifecycleResponse),
}

pub(super) struct ReadinessExchange {
    receiver: tokio::sync::watch::Receiver<Option<ReadinessResult>>,
    task: tokio::task::JoinHandle<()>,
    continuous: bool,
}

impl ReadinessExchange {
    pub(super) fn start(
        directory: PathBuf,
        poll: Duration,
        lifecycle: Option<(
            Vec<engine_types::SignalProducerLifecycle>,
            Vec<SignalSourceFrontier>,
        )>,
    ) -> Self {
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
        let continuous = lifecycle.is_some();
        let task = tokio::spawn(async move {
            if let Err(reason) = exchange(directory, request, lifecycle, poll, &sender).await {
                tokio::time::sleep(poll).await;
                let _ = sender.send(Some(Err(reason)));
            }
        });
        Self {
            receiver,
            task,
            continuous,
        }
    }

    pub(super) fn receiver(&self) -> tokio::sync::watch::Receiver<Option<ReadinessResult>> {
        self.receiver.clone()
    }

    pub(super) fn acknowledged(
        &mut self,
        receiver: tokio::sync::watch::Receiver<Option<ReadinessResult>>,
    ) -> bool {
        self.receiver = receiver;
        self.continuous
    }
}

impl Drop for ReadinessExchange {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub(super) async fn receive(
    mut receiver: tokio::sync::watch::Receiver<Option<ReadinessResult>>,
) -> (
    Result<engine_types::SignalFeedEvent, SignalError>,
    tokio::sync::watch::Receiver<Option<ReadinessResult>>,
) {
    let result = loop {
        if receiver.changed().await.is_err() {
            break Err(SignalError::Source(
                "producer readiness task ended without a response".into(),
            ));
        }
        let result = receiver.borrow_and_update().clone();
        if let Some(result) = result {
            break result.map_err(SignalError::Source);
        }
    };
    (result, receiver)
}

async fn exchange(
    directory: PathBuf,
    request: SignalReadinessRequest,
    lifecycle: Option<(
        Vec<engine_types::SignalProducerLifecycle>,
        Vec<SignalSourceFrontier>,
    )>,
    poll: Duration,
    sender: &tokio::sync::watch::Sender<Option<ReadinessResult>>,
) -> Result<(), String> {
    let path = directory.join(SIGNAL_READINESS_REQUEST_FILE);
    let lifecycle_requested = lifecycle.is_some();
    let encoded = match lifecycle {
        Some((producers, legacy_sources)) => {
            serde_json::to_vec(&engine_types::SignalLifecycleRequest {
                schema_version: engine_types::SIGNAL_LIFECYCLE_SCHEMA_VERSION,
                boot_nonce: request.boot_nonce.clone(),
                producers,
                legacy_sources,
            })
        }
        None => serde_json::to_vec(&request),
    }
    .map_err(|error| error.to_string())?;
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
    let mut previous = None;
    loop {
        let path = directory.join(SIGNAL_READINESS_RESPONSE_FILE);
        let response = tokio::task::spawn_blocking(move || -> Result<Option<Response>, String> {
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
        })
        .await
        .map_err(|error| error.to_string())??;
        let event = match response {
            Some(Response::Legacy(response))
                if !lifecycle_requested
                    && response.schema_version == SIGNAL_READINESS_SCHEMA_VERSION
                    && response.boot_nonce == request.boot_nonce =>
            {
                Some(engine_types::SignalFeedEvent::Ready(response.sources))
            }
            Some(Response::Lifecycle(response))
                if lifecycle_requested
                    && response.schema_version == engine_types::SIGNAL_LIFECYCLE_SCHEMA_VERSION
                    && response.boot_nonce == request.boot_nonce =>
            {
                Some(engine_types::SignalFeedEvent::LifecycleReady(response))
            }
            _ => None,
        };
        if let Some(event) = event {
            if previous.as_ref() != Some(&event) {
                previous = Some(event.clone());
                sender
                    .send(Some(Ok(event)))
                    .map_err(|_| "readiness receiver closed".to_string())?;
            }
            if !lifecycle_requested {
                return Ok(());
            }
        }
        tokio::time::sleep(poll).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn readiness_acknowledges_only_the_selected_version_when_a_seal_races_delivery() {
        let (sender, receiver) = tokio::sync::watch::channel(None);
        let mut exchange = ReadinessExchange {
            receiver,
            continuous: true,
            task: tokio::spawn(std::future::pending()),
        };
        let first = engine_types::SignalFeedEvent::Ready(Vec::new());
        sender.send(Some(Ok(first.clone()))).unwrap();
        let (observed, selected) = receive(exchange.receiver()).await;
        assert_eq!(observed.unwrap(), first);
        let seal = engine_types::SignalFeedEvent::ReadinessUnavailable {
            reason: "later seal".into(),
        };
        sender.send(Some(Ok(seal.clone()))).unwrap();
        assert!(exchange.acknowledged(selected));
        let (observed, _) =
            tokio::time::timeout(Duration::from_millis(50), receive(exchange.receiver()))
                .await
                .expect("a newer response cannot be acknowledged by an older delivery");
        assert_eq!(observed.unwrap(), seal);
    }
}
