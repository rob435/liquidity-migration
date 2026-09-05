use super::*;

/// Streaming signal feed over an AF_UNIX domain socket.
///
/// Observations are framed as `[u32 length_le][raw JSON bytes of SignalObservation]`.
/// The frame being read lives in `frame`, not on the future's stack, because
/// the core drops this future every time another `select!` branch wins.
pub struct UnixSignalFeed {
    socket_path: PathBuf,
    listener: tokio::net::UnixListener,
    pub(super) active_stream: Option<tokio::net::UnixStream>,
    frame: Frame,
}

/// One frame in progress. `body` is sized once the four length bytes are in.
#[derive(Default)]
struct Frame {
    len_buf: [u8; 4],
    len_filled: usize,
    body: Vec<u8>,
    body_filled: usize,
}

impl Frame {
    fn started(&self) -> bool {
        self.len_filled > 0
    }
}

impl UnixSignalFeed {
    pub fn bind(socket_path: impl Into<PathBuf>) -> std::io::Result<Self> {
        let socket_path = socket_path.into();
        let _ = std::fs::remove_file(&socket_path);
        let listener = tokio::net::UnixListener::bind(&socket_path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o770));
        }
        Ok(Self {
            socket_path,
            listener,
            active_stream: None,
            frame: Frame::default(),
        })
    }

    pub fn socket_path(&self) -> &Path {
        &self.socket_path
    }

    fn drop_stream(&mut self) {
        self.active_stream = None;
        self.frame = Frame::default();
    }

    async fn next_doorbell(&mut self) -> Result<(), SignalError> {
        use tokio::io::AsyncReadExt;
        let mut chunk = [0u8; 8192];
        loop {
            let Some(stream) = self.active_stream.as_mut() else {
                let (stream, _) = self.listener.accept().await.map_err(|error| {
                    SignalError::Source(format!("cannot accept signal doorbell: {error}"))
                })?;
                self.active_stream = Some(stream);
                continue;
            };
            match stream.read(&mut chunk).await {
                Ok(0) | Err(_) => self.drop_stream(),
                Ok(_) => return Ok(()),
            }
        }
    }
}

impl UnixSignalFeed {
    pub async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        use tokio::io::AsyncReadExt;
        loop {
            let Some(stream) = self.active_stream.as_mut() else {
                match self.listener.accept().await {
                    Ok((stream, _)) => self.active_stream = Some(stream),
                    Err(err) => {
                        return Err(SignalError::Source(format!(
                            "cannot accept Unix signal socket connection on {}: {err}",
                            self.socket_path.display()
                        )));
                    }
                }
                continue;
            };
            let frame = &mut self.frame;
            // `read` is cancel-safe: a poll that returns Pending has taken
            // nothing off the socket, so a dropped future loses no bytes.
            let read = if frame.len_filled < 4 {
                stream.read(&mut frame.len_buf[frame.len_filled..]).await
            } else {
                stream.read(&mut frame.body[frame.body_filled..]).await
            };
            match read {
                Ok(0) => {
                    if frame.started() {
                        tracing::warn!(
                            length_bytes = frame.len_filled,
                            body_bytes = frame.body_filled,
                            "signal client disconnected during frame read"
                        );
                    } else {
                        tracing::debug!("signal client disconnected; waiting for next connection");
                    }
                    self.drop_stream();
                }
                Ok(read) if frame.len_filled < 4 => {
                    frame.len_filled += read;
                    if frame.len_filled == 4 {
                        let len = u32::from_le_bytes(frame.len_buf) as usize;
                        if len == 0 || len > MAX_SIGNAL_OBSERVATION_BYTES {
                            // The frame is only the doorbell: the row is on
                            // disk and the spool poll delivers it.
                            tracing::warn!(
                                length_bytes = len,
                                max_bytes = MAX_SIGNAL_OBSERVATION_BYTES,
                                "invalid signal frame size; dropping the stream"
                            );
                            self.drop_stream();
                            continue;
                        }
                        frame.body = vec![0u8; len];
                        frame.body_filled = 0;
                    }
                }
                Ok(read) => {
                    frame.body_filled += read;
                    if frame.body_filled == frame.body.len() {
                        let body = std::mem::take(&mut frame.body);
                        *frame = Frame::default();
                        let observation: SignalObservation = serde_json::from_slice(&body)
                            .map_err(|err| {
                                SignalError::Source(format!("malformed signal frame JSON: {err}"))
                            })?;
                        validate(&observation).map_err(SignalError::Source)?;
                        return Ok(observation);
                    }
                }
                Err(err) => {
                    tracing::debug!(error = %err, "signal client read failed; waiting for next connection");
                    self.drop_stream();
                }
            }
        }
    }
}

impl Drop for UnixSignalFeed {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.socket_path);
    }
}

/// The socket wakes the spool reader; only immutable spool files deliver rows.
pub struct HybridSignalFeed {
    /// Absent when `stream.sock` could not be bound; the spool is then
    /// polled on its own interval.
    unix: Option<UnixSignalFeed>,
    pub(super) spool: SpoolSignalFeed,
}

impl HybridSignalFeed {
    pub fn new(directory: impl Into<PathBuf>) -> std::io::Result<Self> {
        let directory = directory.into();
        let unix = UnixSignalFeed::bind(directory.join("stream.sock"))?;
        Ok(Self {
            unix: Some(unix),
            spool: SpoolSignalFeed::new(directory),
        })
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.spool = self.spool.with_poll_interval(poll);
        self
    }

    /// The production feed: the spool, woken by the socket when it can be
    /// bound and polled on its own when it cannot. The spool is the delivery
    /// either way; the socket only shortens the wait.
    pub fn for_directory(directory: impl Into<PathBuf>) -> Self {
        let directory = directory.into();
        match Self::new(&directory) {
            Ok(feed) => feed,
            Err(error) => {
                tracing::warn!(
                    error = %error,
                    path = %directory.display(),
                    "falling back to pure file spool signal feed"
                );
                Self {
                    unix: None,
                    spool: SpoolSignalFeed::new(directory),
                }
            }
        }
    }
}

impl SignalFeed for HybridSignalFeed {
    fn request_readiness(&mut self) -> Result<(), SignalError> {
        self.spool.request_readiness()
    }

    fn set_sleeve_keys(
        &mut self,
        keys: Vec<engine_types::identity::SleeveKey>,
    ) -> Result<(), SignalError> {
        self.spool.set_sleeve_keys(keys)
    }

    fn request_lifecycle(
        &mut self,
        producers: Vec<engine_types::SignalProducerLifecycle>,
        legacy_sources: Vec<engine_types::SignalSourceFrontier>,
    ) -> Result<(), SignalError> {
        self.spool.request_lifecycle(producers, legacy_sources)
    }

    async fn next_event(&mut self) -> Result<engine_types::SignalFeedEvent, SignalError> {
        let Some(unix) = self.unix.as_mut() else {
            return self.spool.next_event().await;
        };
        loop {
            tokio::select! {
                biased;
                event = self.spool.next_event() => return event,
                frame = unix.next_doorbell() => {
                    if let Err(error) = frame {
                        tracing::warn!(%error, "signal doorbell rejected; durable spool remains authoritative");
                    }
                    self.spool.wake();
                }
            }
        }
    }

    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        self.spool.set_gap_requests(gaps, blocked_destinations)
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        self.spool.acknowledge_last()
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        self.spool.defer_last(observation)
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        let Some(unix) = self.unix.as_mut() else {
            return self.spool.next_observation().await;
        };
        loop {
            tokio::select! {
                biased;
                observation = self.spool.next_observation() => return observation,
                frame = unix.next_doorbell() => {
                    if let Err(error) = frame {
                        tracing::warn!(%error, "signal doorbell rejected; durable spool remains authoritative");
                    }
                    self.spool.wake();
                }
            }
        }
    }
}
