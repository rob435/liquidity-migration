use super::*;

#[derive(Debug, PartialEq, Eq)]
pub enum SignalSendError {
    Full(Box<SignalObservation>),
    Closed(Box<SignalObservation>),
}

impl SignalSendError {
    pub fn into_inner(self) -> SignalObservation {
        match self {
            Self::Full(observation) | Self::Closed(observation) => *observation,
        }
    }
}

impl std::fmt::Display for SignalSendError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(match self {
            SignalSendError::Full(_) => "signal queue is full",
            SignalSendError::Closed(_) => "signal receiver is closed",
        })
    }
}

impl std::error::Error for SignalSendError {}

pub struct SignalSender(Arc<SignalChannel>);

pub struct SignalReceiver(pub(super) Arc<SignalChannel>);

pub(super) struct SignalChannel {
    state: Mutex<ChannelState>,
    changed: tokio::sync::Notify,
}

pub(super) struct ChannelState {
    pub(super) queued: VecDeque<(SignalObservation, bool)>,
    pub(super) gaps: Vec<SignalGapRequest>,
    pub(super) blocked_destinations: Vec<StrategyId>,
    pub(super) outstanding: Option<(DeliveryIdentity, usize, bool)>,
    pub(super) ordinary_rows: usize,
    pub(super) ordinary_bytes: usize,
    pub(super) recovery_used: bool,
    pub(super) senders: usize,
    pub(super) receiver_alive: bool,
}

impl SignalChannel {
    pub(super) fn lock(&self) -> std::sync::MutexGuard<'_, ChannelState> {
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

pub fn signal_channel() -> (SignalSender, SignalReceiver) {
    let shared = Arc::new(SignalChannel {
        state: Mutex::new(ChannelState {
            queued: VecDeque::new(),
            gaps: Vec::new(),
            blocked_destinations: Vec::new(),
            outstanding: None,
            ordinary_rows: 0,
            ordinary_bytes: 0,
            recovery_used: false,
            senders: 1,
            receiver_alive: true,
        }),
        changed: tokio::sync::Notify::new(),
    });
    (SignalSender(shared.clone()), SignalReceiver(shared))
}

impl Clone for SignalSender {
    fn clone(&self) -> Self {
        self.0.lock().senders += 1;
        Self(self.0.clone())
    }
}

impl Drop for SignalSender {
    fn drop(&mut self) {
        self.0.lock().senders -= 1;
        self.0.changed.notify_one();
    }
}

impl Drop for SignalReceiver {
    fn drop(&mut self) {
        self.0.lock().receiver_alive = false;
    }
}

impl SignalSender {
    pub fn try_send(&self, observation: SignalObservation) -> Result<(), SignalSendError> {
        let bytes = retained_bytes(&observation);
        let mut state = self.0.lock();
        if !state.receiver_alive {
            return Err(SignalSendError::Closed(Box::new(observation)));
        }
        if bytes > MAX_SIGNAL_RETAINED_BYTES {
            return Err(SignalSendError::Full(Box::new(observation)));
        }
        let ordinary = state.ordinary_rows < SIGNAL_CHANNEL_CAPACITY
            && state.ordinary_bytes.saturating_add(bytes) <= SIGNAL_CHANNEL_BYTES;
        let recovery = !ordinary
            && !state.recovery_used
            && signal_requested(&state.gaps, &observation)
            && signal_available(&observation, crate::clock::wall_ms());
        if ordinary {
            state.ordinary_rows += 1;
            state.ordinary_bytes += bytes;
        } else if recovery {
            state.recovery_used = true;
        } else {
            return Err(SignalSendError::Full(Box::new(observation)));
        }
        state.queued.push_back((observation, recovery));
        drop(state);
        self.0.changed.notify_one();
        Ok(())
    }
}

impl SignalFeed for SignalReceiver {
    fn set_gap_requests(
        &mut self,
        gaps: &[SignalGapRequest],
        blocked_destinations: &[StrategyId],
    ) -> Result<(), SignalError> {
        let gaps = ordered_gap_requests(gaps)?;
        let blocked_destinations = ordered_blocked_destinations(blocked_destinations)?;
        let mut state = self.0.lock();
        state.gaps = gaps;
        state.blocked_destinations = blocked_destinations;
        drop(state);
        self.0.changed.notify_one();
        Ok(())
    }

    fn acknowledge_last(&mut self) -> Result<(), SignalError> {
        let mut state = self.0.lock();
        let (_, bytes, recovery) = state
            .outstanding
            .take()
            .ok_or_else(|| protocol_error("no row to acknowledge"))?;
        if recovery {
            state.recovery_used = false;
        } else {
            state.ordinary_rows -= 1;
            state.ordinary_bytes -= bytes;
        }
        Ok(())
    }

    fn defer_last(&mut self, observation: SignalObservation) -> Result<(), SignalError> {
        let mut state = self.0.lock();
        let (identity, bytes, recovery) = state
            .outstanding
            .as_ref()
            .ok_or_else(|| protocol_error("no row to defer"))?;
        let returned_bytes = retained_bytes(&observation);
        if !identity.matches(&observation) || returned_bytes > *bytes {
            return Err(protocol_error(
                "deferred row differs from the outstanding delivery",
            ));
        }
        let recovery = *recovery;
        let bytes = *bytes;
        if !recovery {
            state.ordinary_bytes -= bytes - returned_bytes;
        }
        state.outstanding = None;
        state.queued.push_front((observation, recovery));
        Ok(())
    }

    async fn next_observation(&mut self) -> Result<SignalObservation, SignalError> {
        loop {
            let changed = self.0.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            let next_available = {
                let mut state = self.0.lock();
                if state.outstanding.is_some() {
                    return Err(protocol_error(
                        "previous row was neither acknowledged nor deferred",
                    ));
                }
                let wall_ms = crate::clock::wall_ms();
                let index = state
                    .queued
                    .iter()
                    .position(|(observation, _)| {
                        signal_available(observation, wall_ms)
                            && signal_requested(&state.gaps, observation)
                    })
                    .or_else(|| {
                        state.queued.iter().position(|(observation, _)| {
                            signal_available(observation, wall_ms)
                                && signal_eligible(
                                    &state.gaps,
                                    &state.blocked_destinations,
                                    observation,
                                )
                        })
                    });
                if let Some(index) = index {
                    let (observation, recovery) =
                        state.queued.remove(index).expect("queued index exists");
                    state.outstanding = Some((
                        DeliveryIdentity::of(&observation),
                        retained_bytes(&observation),
                        recovery,
                    ));
                    return Ok(observation);
                }
                if state.senders == 0 && state.queued.is_empty() {
                    return Err(SignalError::Closed);
                }
                state
                    .queued
                    .iter()
                    .filter(|(observation, _)| {
                        signal_eligible(&state.gaps, &state.blocked_destinations, observation)
                    })
                    .map(|(observation, _)| observation.available_wall_ts_ms)
                    .min()
            };
            if let Some(available_ms) = next_available {
                tokio::select! {
                    _ = changed => {},
                    _ = tokio::time::sleep(availability_wait(available_ms, crate::clock::wall_ms())) => {},
                }
            } else {
                changed.await;
            }
        }
    }
}
