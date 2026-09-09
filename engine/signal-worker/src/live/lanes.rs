use super::*;

pub(super) struct LaneContext<'a> {
    pub(super) stream: &'a mut Box<dyn PublicStream>,
    pub(super) pending: &'a mut BTreeMap<(String, i64), ConfirmedKline>,
    pub(super) lane_tx: &'a mpsc::Sender<LaneCompletion>,
    pub(super) lanes: &'a mut LaneState,
}

impl LiveRunner {
    pub(super) fn handle_lane_completion(
        &mut self,
        completion: LaneCompletion,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        match completion {
            LaneCompletion::Instruments(result) => self.complete_instruments(result, context),
            LaneCompletion::Gate(result) => self.complete_gate(result, context),
            LaneCompletion::Tickers(result) => self.complete_tickers(result, context),
            LaneCompletion::FundingChunk { result, resume } => {
                self.complete_funding_chunk(result, resume)
            }
            LaneCompletion::FundingFinished { succeeded } => {
                self.complete_funding_finished(succeeded, context)
            }
            LaneCompletion::WhaleChunk { result, resume } => {
                self.complete_whale_chunk(result, resume)
            }
            LaneCompletion::WhaleFinished => self.complete_whale_finished(context),
            LaneCompletion::RepairChunk { result, resume } => {
                self.complete_repair_chunk(result, resume, context)
            }
            LaneCompletion::RepairFinished { end_ms, epoch } => {
                self.complete_repair_finished(end_ms, epoch, context)
            }
        }
    }

    fn complete_instruments(
        &mut self,
        result: Result<FetchedUniverseInputs, WorkerError>,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        let LaneContext {
            stream,
            lane_tx,
            lanes,
            ..
        } = context;
        lanes.instruments = false;
        match result {
            Ok(fetched) => {
                if let Err(error) = self.commit_universe_inputs(fetched) {
                    lane_source_failure("instrument lane", error)?;
                    lanes.instruments_ready = !self.durable.worker().state().instruments.is_empty();
                    return Ok(());
                }
                if let Err(error) = self.validate_candidate_instruments() {
                    eprintln!("signal-worker: instrument inventory degraded: {error}");
                }
                self.reconfigure_stream(stream)?;
                lanes.instruments_ready = true;
                if !lanes.repair {
                    self.start_kline_repair(lane_tx, lanes, None)?;
                }
                let long_end_ms = closed_kline_end(wall_ms()?);
                if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                    self.long_watermark(long_end_ms, gap_symbols)?;
                }
                if !lanes.funding {
                    lanes.funding = true;
                    self.spawn_funding_lane(lane_tx.clone())?;
                }
            }
            Err(error) => {
                lane_source_failure("instrument lane", error)?;
                lanes.instruments_ready = !self.durable.worker().state().instruments.is_empty();
            }
        }
        Ok(())
    }

    fn complete_gate(
        &mut self,
        result: Result<Option<FetchedGate>, WorkerError>,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        let LaneContext { lanes, .. } = context;
        lanes.gate = false;
        match result {
            Ok(Some(fetched)) => {
                if self.last_gate_decision_ts_ms == Some(fetched.decision_ts_ms) {
                    return Ok(());
                }
                let candidates = fetched.rows.len();
                self.commit(WireEvent::LlmGateCandidates {
                    schema_version: SCHEMA_VERSION,
                    sequence: self.next_sequence()?,
                    observed_ts_ms: fetched.decision_ts_ms.min(fetched.read_at_ms),
                    available_at_ms: fetched.read_at_ms,
                    decision_ts_ms: fetched.decision_ts_ms,
                    valid_until_ms: fetched.valid_until_ms,
                    rows: fetched.rows,
                })?;
                self.last_gate_decision_ts_ms = Some(fetched.decision_ts_ms);
                self.last_gate_candidates = candidates;
            }
            Ok(None) => {}
            Err(error) => lane_source_failure("LLM gate lane", error)?,
        }
        Ok(())
    }

    fn complete_tickers(
        &mut self,
        result: Result<FetchedTickers, WorkerError>,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        let LaneContext { stream, lanes, .. } = context;
        lanes.tickers = false;
        match result {
            Ok(mut tickers) => {
                if let Err(error) = validate_fetched_tickers(&tickers) {
                    lane_source_failure("ticker fallback lane", error)?;
                    self.rest_ticker_failure_count =
                        self.rest_ticker_failure_count.saturating_add(1);
                    self.rest_ticker_last_failure_wall_ts_ms = Some(wall_ms()?);
                    return Ok(());
                }
                let health = stream.health();
                if health.connected {
                    stream.reconcile_tickers(
                        health.epoch,
                        &tickers.rows,
                        tickers.request_started_at_ms,
                        tickers.available_at_ms,
                    );
                    if let Some(sample) = stream.sample_tickers(
                        tickers.available_at_ms,
                        self.config.sources.mark_max_age_ms,
                    ) {
                        tickers.observed_ts_ms = sample.observed_ts_ms;
                        tickers.available_at_ms = sample.available_at_ms;
                        tickers.rows = sample.rows;
                    }
                }
                self.commit(WireEvent::BybitTickerSnapshot {
                    schema_version: SCHEMA_VERSION,
                    sequence: self.next_sequence()?,
                    observed_ts_ms: tickers.observed_ts_ms,
                    available_at_ms: tickers.available_at_ms,
                    rows: tickers.rows,
                })?;
                self.rest_ticker_success_count = self.rest_ticker_success_count.saturating_add(1);
                self.rest_ticker_last_success_wall_ts_ms = Some(wall_ms()?);
            }
            Err(error) => {
                lane_source_failure("ticker fallback lane", error)?;
                self.rest_ticker_failure_count = self.rest_ticker_failure_count.saturating_add(1);
                self.rest_ticker_last_failure_wall_ts_ms = Some(wall_ms()?);
            }
        }
        Ok(())
    }

    fn complete_funding_chunk(
        &mut self,
        result: Result<FetchedFunding, WorkerError>,
        resume: oneshot::Sender<bool>,
    ) -> Result<(), WorkerError> {
        let continue_lane = match result {
            Ok(fetched) => {
                if let Err(error) =
                    validate_funding_source_against_state(self.durable.worker().state(), &fetched)
                {
                    lane_source_failure("funding lane chunk", error)?;
                    let _ = resume.send(false);
                    return Ok(());
                }
                let failure_count = fetched.failures.len();
                let samples = fetched
                    .failures
                    .iter()
                    .take(3)
                    .map(|(symbol, error)| format!("{symbol}: {error}"))
                    .collect::<Vec<_>>()
                    .join("; ");
                let committed = self.commit_funding_batches(fetched.batches)?;
                if failure_count > 0 {
                    eprintln!(
                        "signal-worker: funding lane chunk: {failure_count} symbol failures; {samples}"
                    );
                }
                committed
            }
            Err(error) => {
                lane_source_failure("funding lane chunk", error)?;
                false
            }
        };
        let _ = resume.send(continue_lane);
        Ok(())
    }

    fn complete_funding_finished(
        &mut self,
        succeeded: bool,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        let LaneContext { lane_tx, lanes, .. } = context;
        lanes.funding = false;
        lanes.funding_ready = succeeded;
        // Before the carry attempt, which may spawn the next funding
        // pass: a refresh the cadence already asked for outranks it.
        self.start_instrument_lane_if_due(lane_tx, lanes);
        self.try_carry_watermark(lanes, Some(lane_tx))?;
        Ok(())
    }

    fn complete_whale_chunk(
        &mut self,
        result: Result<FetchedWhales, WorkerError>,
        resume: oneshot::Sender<bool>,
    ) -> Result<(), WorkerError> {
        let continue_lane = match result {
            Ok(fetched) => {
                if let Err(error) =
                    validate_whale_source_against_state(self.durable.worker().state(), &fetched)
                {
                    lane_source_failure("Binance whale lane chunk", error)?;
                    true
                } else {
                    self.commit_whale_batch(fetched)?;
                    true
                }
            }
            Err(error) => {
                lane_source_failure("Binance whale lane chunk", error)?;
                false
            }
        };
        let _ = resume.send(continue_lane);
        Ok(())
    }

    fn complete_whale_finished(&mut self, context: LaneContext<'_>) -> Result<(), WorkerError> {
        let LaneContext { lane_tx, lanes, .. } = context;
        lanes.whales = false;
        self.try_carry_watermark(lanes, Some(lane_tx))?;
        Ok(())
    }

    fn complete_repair_chunk(
        &mut self,
        result: Result<FetchedKlineJobs, WorkerError>,
        resume: oneshot::Sender<bool>,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        let LaneContext { lanes, .. } = context;
        let continue_lane = match result {
            Ok(fetched) => {
                if let Err(error) =
                    validate_kline_source_against_state(self.durable.worker().state(), &fetched)
                {
                    let sample = error.to_string();
                    lane_source_failure("kline repair lane chunk", error)?;
                    lanes.repair_failure_count = lanes.repair_failure_count.saturating_add(1);
                    if lanes.repair_failure_samples.len() < 3 {
                        lanes
                            .repair_failure_samples
                            .push(("lane".to_owned(), sample.chars().take(160).collect()));
                    }
                    let long_end_ms = closed_kline_end(wall_ms()?);
                    if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                        self.long_watermark(long_end_ms, gap_symbols)?;
                    }
                    let _ = resume.send(false);
                    return Ok(());
                }
                let committed = self.commit_kline_batches(fetched.batches)?;
                lanes.repair_failure_count = lanes
                    .repair_failure_count
                    .saturating_add(fetched.failures.len());
                for (symbol, error) in fetched.failures {
                    if lanes.repair_failure_samples.len() < 3 {
                        lanes
                            .repair_failure_samples
                            .push((symbol, error.chars().take(160).collect()));
                    }
                }
                if committed {
                    let long_end_ms = closed_kline_end(wall_ms()?);
                    if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                        self.long_watermark(long_end_ms, gap_symbols)?;
                    }
                }
                committed
            }
            Err(error) => {
                let sample = error.to_string();
                lane_source_failure("kline repair lane chunk", error)?;
                lanes.repair_failure_count = lanes.repair_failure_count.saturating_add(1);
                if lanes.repair_failure_samples.len() < 3 {
                    lanes
                        .repair_failure_samples
                        .push(("lane".to_owned(), sample.chars().take(160).collect()));
                }
                let long_end_ms = closed_kline_end(wall_ms()?);
                if let Some(gap_symbols) = self.long_gap_symbols(long_end_ms) {
                    self.long_watermark(long_end_ms, gap_symbols)?;
                }
                false
            }
        };
        let _ = resume.send(continue_lane);
        Ok(())
    }

    fn complete_repair_finished(
        &mut self,
        end_ms: i64,
        epoch: Option<u64>,
        context: LaneContext<'_>,
    ) -> Result<(), WorkerError> {
        let LaneContext {
            stream,
            pending,
            lane_tx,
            lanes,
        } = context;
        lanes.repair = false;
        let repaired_epoch = lanes.repair_epoch.or(epoch);
        let pending_committed =
            self.flush_pending_klines_or_recover(stream, pending, lane_tx, lanes)?;
        if !pending_committed {
            return Ok(());
        }
        let current_end = closed_kline_end(wall_ms()?);
        let coverage_complete = self.kline_repair_jobs(current_end).is_empty();
        if let Some(epoch) = repaired_epoch {
            if coverage_complete {
                stream.mark_gap_repaired(epoch);
            }
        }
        if lanes.repair_failure_count > 0 {
            let samples = lanes
                .repair_failure_samples
                .iter()
                .map(|(symbol, error)| format!("{symbol}: {error}"))
                .collect::<Vec<_>>()
                .join("; ");
            eprintln!(
                "signal-worker: kline repair through {end_ms}: {} failures; {samples}",
                lanes.repair_failure_count
            );
        }
        lanes.repair_failure_count = 0;
        lanes.repair_failure_samples.clear();
        if let Some(gap_symbols) = self.long_gap_symbols(end_ms) {
            self.long_watermark(end_ms, gap_symbols)?;
        }
        self.try_carry_watermark(lanes, Some(lane_tx))?;
        Ok(())
    }
}
