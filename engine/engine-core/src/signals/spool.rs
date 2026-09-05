use super::*;

/// Read immutable, ordered JSON envelopes from one spool directory.
///
/// A complete filename is `<sequence:020>-<content_sha256>.json`. The signal
/// worker writes elsewhere and renames into this name only after closing it.
/// The reader performs filesystem work on Tokio's blocking pool, so a slow
/// disk cannot stall private-order or market processing on the core thread.
pub struct SpoolSignalFeed {
    directory: PathBuf,
    readiness: Option<super::readiness::ReadinessExchange>,
    pub(super) returned: Option<(PathBuf, DeliveryIdentity)>,
    acknowledged: Option<PathBuf>,
    pub(super) retirement: Option<tokio::task::JoinHandle<Result<(), SignalError>>>,
    pub(super) scanner: Option<SpoolScanner>,
    pub(super) selection: Option<tokio::task::JoinHandle<ScanResult>>,
    gaps: Vec<SignalGapRequest>,
    blocked_destinations: Vec<StrategyId>,
    poll: Duration,
    next_scan: tokio::time::Instant,
    wake_generation: u64,
    scan_generation: u64,
}

type SelectedRow = Option<(PathBuf, SignalObservation)>;
type ScanResult = (
    SpoolScanner,
    Vec<SignalGapRequest>,
    Vec<StrategyId>,
    Result<SelectedRow, SignalError>,
);

#[derive(Default)]
pub(super) struct SpoolScanner {
    pub(super) deferred: BTreeMap<PathBuf, DeliveryIdentity>,
    next_available_ms: Option<i64>,
}

impl SpoolScanner {
    fn wait_until(&mut self, available_ms: i64) {
        self.next_available_ms = Some(
            self.next_available_ms
                .map_or(available_ms, |known| known.min(available_ms)),
        );
    }

    fn remember(&mut self, path: PathBuf, observation: &SignalObservation) {
        if !self.deferred.contains_key(&path) && self.deferred.len() == SPOOL_METADATA_CAPACITY {
            self.deferred.pop_first();
        }
        self.deferred
            .insert(path, DeliveryIdentity::of(observation));
    }

    pub(super) fn page(
        directory: &Path,
        after: Option<&Path>,
        exact: Option<&BTreeSet<u64>>,
    ) -> Result<Vec<PathBuf>, SignalError> {
        let entries = std::fs::read_dir(directory).map_err(|error| {
            SignalError::Source(format!(
                "cannot scan signal spool {}: {error}",
                directory.display()
            ))
        })?;
        let mut paths = BTreeSet::new();
        for entry in entries {
            let path = entry
                .map_err(|error| SignalError::Source(error.to_string()))?
                .path();
            if path.extension().is_none_or(|extension| extension != "json")
                || path
                    .file_name()
                    .and_then(|name| name.to_str())
                    .is_some_and(|name| {
                        name == engine_types::SIGNAL_READINESS_REQUEST_FILE
                            || name == engine_types::SIGNAL_READINESS_RESPONSE_FILE
                    })
                || after.is_some_and(|after| path.as_path() <= after)
            {
                continue;
            }
            if let Some(exact) = exact {
                let (sequence, _) = SpoolSignalFeed::parse_name(&path)?;
                if !exact.contains(&sequence) {
                    continue;
                }
            }
            paths.insert(path);
            if paths.len() > SPOOL_SCAN_PAGE {
                paths.pop_last();
            }
        }
        Ok(paths.into_iter().collect())
    }

    fn select_pass(
        &mut self,
        directory: &Path,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
        exact: bool,
        wall_ms: i64,
    ) -> Result<SelectedRow, SignalError> {
        let sequences = exact.then(|| {
            gaps.iter()
                .map(|gap| gap.next_sequence)
                .collect::<BTreeSet<_>>()
        });
        let mut after = None;
        loop {
            let page = Self::page(directory, after.as_deref(), sequences.as_ref())?;
            if page.is_empty() {
                return Ok(None);
            }
            after = page.last().cloned();
            for path in page {
                if let Some(known) = self.deferred.get(&path) {
                    let next = requested_sequence(gaps, &known.source);
                    if (exact && next != Some(known.sequence))
                        || (!exact
                            && !identity_eligible(
                                gaps,
                                blocked_destinations,
                                &known.source,
                                known.sequence,
                                known.destination,
                            ))
                    {
                        continue;
                    }
                    if known.available_wall_ts_ms > wall_ms {
                        self.wait_until(known.available_wall_ts_ms);
                        continue;
                    }
                }
                let Some(observation) = SpoolSignalFeed::read_one(&path)? else {
                    self.deferred.remove(&path);
                    continue;
                };
                let eligible = if exact {
                    signal_requested(gaps, &observation)
                } else {
                    signal_eligible(gaps, blocked_destinations, &observation)
                };
                if eligible && signal_available(&observation, wall_ms) {
                    self.deferred.remove(&path);
                    return Ok(Some((path, observation)));
                }
                if eligible {
                    self.wait_until(observation.available_wall_ts_ms);
                }
                self.remember(path, &observation);
            }
        }
    }

    pub(super) fn select(
        &mut self,
        directory: &Path,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
        wall_ms: i64,
    ) -> Result<SelectedRow, SignalError> {
        self.next_available_ms = None;
        if !gaps.is_empty() {
            if let Some(row) =
                self.select_pass(directory, gaps, blocked_destinations, true, wall_ms)?
            {
                return Ok(Some(row));
            }
        }
        self.select_pass(directory, gaps, blocked_destinations, false, wall_ms)
    }
}

impl SpoolSignalFeed {
    pub fn new(directory: impl Into<PathBuf>) -> Self {
        Self {
            directory: directory.into(),
            readiness: None,
            returned: None,
            acknowledged: None,
            retirement: None,
            scanner: Some(SpoolScanner::default()),
            selection: None,
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            poll: Duration::from_millis(100),
            next_scan: tokio::time::Instant::now(),
            wake_generation: 0,
            scan_generation: 0,
        }
    }

    pub fn with_poll_interval(mut self, poll: Duration) -> Self {
        self.poll = poll.max(Duration::from_millis(1));
        self
    }

    pub(super) fn wake(&mut self) {
        self.wake_generation = self.wake_generation.wrapping_add(1);
        self.next_scan = tokio::time::Instant::now();
    }

    async fn retire_acknowledged(&mut self) -> Result<(), SignalError> {
        let Some(path) = self.acknowledged.as_ref() else {
            return Ok(());
        };
        if self.retirement.is_none() {
            let path = path.clone();
            self.retirement = Some(tokio::task::spawn_blocking(
                move || match std::fs::remove_file(&path) {
                    Ok(()) => Ok(()),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                    Err(error) => Err(SignalError::Source(format!(
                        "cannot retire durable signal file {}: {error}",
                        path.display()
                    ))),
                },
            ));
        }
        let result = self.retirement.as_mut().expect("retirement exists").await;
        self.retirement = None;
        result.map_err(|error| {
            SignalError::Source(format!("signal retire task failed: {error}"))
        })??;
        self.acknowledged = None;
        Ok(())
    }

    /// The immutable spool filename includes the sequence and canonical hash.
    pub fn path_for(&self, observation: &SignalObservation) -> PathBuf {
        self.directory.join(format!(
            "{:020}-{}.json",
            observation.sequence, observation.content_sha256
        ))
    }

    fn parse_name(path: &Path) -> Result<(u64, &str), SignalError> {
        let file_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| SignalError::Source("signal filename is not UTF-8".into()))?;
        let stem = file_name
            .strip_suffix(".json")
            .ok_or_else(|| SignalError::Source("signal file must end in .json".into()))?;
        let (sequence, hash) = stem.split_once('-').ok_or_else(|| {
            SignalError::Source(format!(
                "signal file {file_name} must be <sequence>-<sha256>.json"
            ))
        })?;
        if sequence.len() != 20 || !sequence.bytes().all(|byte| byte.is_ascii_digit()) {
            return Err(SignalError::Source(format!(
                "signal file {file_name} sequence must be 20 decimal digits"
            )));
        }
        let sequence = sequence.parse::<u64>().map_err(|error| {
            SignalError::Source(format!("signal file {file_name} has bad sequence: {error}"))
        })?;
        Ok((sequence, hash))
    }

    pub fn read_one(path: &Path) -> Result<Option<SignalObservation>, SignalError> {
        let (named_sequence, hash) = Self::parse_name(path)?;
        let file = match std::fs::File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => {
                return Err(SignalError::Source(format!(
                    "cannot read signal file {}: {error}",
                    path.display()
                )))
            }
        };
        let length = file
            .metadata()
            .map_err(|error| SignalError::Source(error.to_string()))?
            .len();
        if length > MAX_SIGNAL_FILE_BYTES {
            return Err(SignalError::Source(format!(
                "signal file {} exceeds {MAX_SIGNAL_FILE_BYTES} bytes",
                path.display()
            )));
        }
        let mut raw = Vec::with_capacity(length as usize);
        file.take(MAX_SIGNAL_FILE_BYTES + 1)
            .read_to_end(&mut raw)
            .map_err(|error| {
                SignalError::Source(format!(
                    "cannot read signal file {}: {error}",
                    path.display()
                ))
            })?;
        if raw.len() as u64 > MAX_SIGNAL_FILE_BYTES {
            return Err(SignalError::Source(format!(
                "signal file {} exceeds {MAX_SIGNAL_FILE_BYTES} bytes",
                path.display()
            )));
        }
        let observation: SignalObservation = serde_json::from_slice(&raw).map_err(|error| {
            SignalError::Source(format!(
                "signal file {} is not an observation: {error}",
                path.display()
            ))
        })?;
        validate(&observation).map_err(|error| {
            SignalError::Source(format!("signal file {}: {error}", path.display()))
        })?;
        if observation.sequence != named_sequence || observation.content_sha256 != hash {
            return Err(SignalError::Source(format!(
                "signal file {} name does not match its sequence/hash envelope",
                path.display()
            )));
        }
        Ok(Some(observation))
    }
}

impl SignalFeed for SpoolSignalFeed {
    fn request_readiness(&mut self) -> Result<(), SignalError> {
        self.readiness = Some(super::readiness::ReadinessExchange::start(
            self.directory.clone(),
            self.poll,
        ));
        Ok(())
    }

    async fn next_event(&mut self) -> Result<engine_types::SignalFeedEvent, SignalError> {
        let Some(receiver) = self.readiness.as_ref().map(|exchange| exchange.receiver()) else {
            return self
                .next_observation()
                .await
                .map(engine_types::SignalFeedEvent::Observation);
        };
        tokio::select! {
            biased;
            frontiers = super::readiness::receive(receiver) => {
                self.readiness = None;
                Ok(match frontiers {
                    Ok(frontiers) => engine_types::SignalFeedEvent::Ready(frontiers),
                    Err(error) => engine_types::SignalFeedEvent::ReadinessUnavailable { reason: error.to_string() },
                })
            }
            observation = self.next_observation() => observation.map(engine_types::SignalFeedEvent::Observation),
        }
    }

    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        let gaps = ordered_gap_requests(gaps)?;
        let blocked_destinations = ordered_blocked_destinations(blocked_destinations)?;
        if self.gaps != gaps || self.blocked_destinations != blocked_destinations {
            self.gaps = gaps;
            self.blocked_destinations = blocked_destinations;
            self.wake();
        }
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        let (path, _) = self
            .returned
            .take()
            .ok_or_else(|| protocol_error("no row to acknowledge"))?;
        self.acknowledged = Some(path);
        self.wake();
        Ok(())
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        let (path, identity) = self
            .returned
            .as_ref()
            .ok_or_else(|| protocol_error("no row to defer"))?;
        if !identity.matches(&observation) {
            return Err(protocol_error(
                "deferred row differs from the outstanding delivery",
            ));
        }
        self.scanner
            .as_mut()
            .expect("delivery restored its scanner")
            .remember(path.clone(), &observation);
        self.returned = None;
        self.wake();
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        if self.returned.is_some() {
            return Err(protocol_error(
                "previous row was neither acknowledged nor deferred",
            ));
        }
        self.retire_acknowledged().await?;
        loop {
            if self.selection.is_none() {
                let mut deadline = self.next_scan;
                if let Some(available_ms) = self
                    .scanner
                    .as_ref()
                    .and_then(|scanner| scanner.next_available_ms)
                {
                    deadline = deadline.min(
                        tokio::time::Instant::now()
                            + availability_wait(available_ms, crate::clock::wall_ms()),
                    );
                }
                tokio::time::sleep_until(deadline).await;
                let mut scanner = self
                    .scanner
                    .take()
                    .expect("one scanner is retained across polls");
                let directory = self.directory.clone();
                let gaps = self.gaps.clone();
                let blocked_destinations = self.blocked_destinations.clone();
                // The engine's virtual clock is thread-local, so the blocking
                // pool must receive this reading rather than sampling its own.
                let wall_ms = crate::clock::wall_ms();
                self.scan_generation = self.wake_generation;
                self.selection = Some(tokio::task::spawn_blocking(move || {
                    let selected =
                        scanner.select(&directory, &gaps, &blocked_destinations, wall_ms);
                    (scanner, gaps, blocked_destinations, selected)
                }));
            }
            let joined = self.selection.as_mut().expect("selection exists").await;
            self.selection = None;
            let (scanner, selected_gaps, selected_blocked, selected) = match joined {
                Ok(result) => result,
                Err(error) => {
                    self.scanner = Some(SpoolScanner::default());
                    return Err(SignalError::Source(format!(
                        "signal scan task failed: {error}"
                    )));
                }
            };
            self.scanner = Some(scanner);
            let selected = selected?;
            if selected_gaps != self.gaps || selected_blocked != self.blocked_destinations {
                self.wake();
                continue;
            }
            if let Some((path, observation)) = selected {
                // Policy or wall time can change while a cancelled scan finishes.
                if !signal_eligible(&self.gaps, &self.blocked_destinations, &observation)
                    || !signal_available(&observation, crate::clock::wall_ms())
                {
                    self.scanner
                        .as_mut()
                        .expect("scanner restored")
                        .remember(path, &observation);
                    self.wake();
                    continue;
                }
                self.returned = Some((path, DeliveryIdentity::of(&observation)));
                return Ok(observation);
            }
            self.next_scan = if self.scan_generation == self.wake_generation {
                tokio::time::Instant::now() + self.poll
            } else {
                tokio::time::Instant::now()
            };
        }
    }
}
