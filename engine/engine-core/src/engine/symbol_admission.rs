use super::*;
use engine_types::identity::IdentityError;
use engine_types::orders::{
    InstrumentCatalog, InstrumentCatalogCheckpoint, InstrumentCatalogClient,
};
use std::sync::Arc;
use tokio::sync::oneshot;

#[derive(Debug, thiserror::Error)]
pub(super) enum AdmissionFailure {
    #[error(transparent)]
    Identity(IdentityError),
    #[error("instrument catalog unavailable: {0}")]
    Catalog(VenueError),
    #[error("venue symbol installation unavailable: {0}")]
    Install(VenueError),
}

struct RefreshedCatalog {
    catalog: InstrumentCatalog,
    listed: std::collections::BTreeSet<String>,
}

enum MetadataWrite {
    Catalog {
        refreshed: RefreshedCatalog,
        checkpoint: Option<Box<InstrumentCatalogCheckpoint>>,
        identities: engine_types::identity::IdentityState,
    },
    Identities {
        identities: engine_types::identity::IdentityState,
    },
}

enum Phase {
    Idle,
    Fetching(tokio::task::JoinHandle<Result<RefreshedCatalog, VenueError>>),
    Persisting {
        write: Box<MetadataWrite>,
        receive: oneshot::Receiver<Result<(), engine_types::wal::WalError>>,
    },
    Installing {
        wanted: Vec<WantedSymbol>,
        expected: Vec<SymbolId>,
        receive: oneshot::Receiver<Result<Vec<Option<SymbolId>>, VenueError>>,
    },
}

pub(super) struct SymbolAdmission {
    catalog: InstrumentCatalog,
    client: Option<Arc<dyn InstrumentCatalogClient>>,
    installed: bool,
    refresh_required: bool,
    listed: std::collections::BTreeSet<String>,
    /// Wanted names the venue's table does not carry, each said once. A
    /// refreshed table clears it, since a listing may have appeared.
    unlisted: std::collections::BTreeSet<String>,
    pub(super) checkpoint: Option<Box<InstrumentCatalogCheckpoint>>,
    phase: Phase,
    retry_after_ns: u64,
    pub(super) failure: Option<AdmissionFailure>,
}

impl SymbolAdmission {
    pub(super) fn new(
        catalog: InstrumentCatalog,
        client: Option<Arc<dyn InstrumentCatalogClient>>,
        checkpoint: Option<Box<InstrumentCatalogCheckpoint>>,
        refresh_required: bool,
    ) -> Self {
        let listed = if refresh_required {
            Default::default()
        } else {
            catalog.rules.iter().map(|(name, _)| name.clone()).collect()
        };
        Self {
            listed,
            unlisted: Default::default(),
            catalog,
            client,
            checkpoint,
            refresh_required,
            installed: true,
            phase: Phase::Idle,
            retry_after_ns: 0,
            failure: None,
        }
    }

    pub(super) fn contains(&self, name: &str) -> bool {
        matches!(&self.phase, Phase::Installing { wanted, .. } if wanted.iter().any(|row| row.name == name))
    }

    pub(super) fn busy(&self) -> bool {
        self.refresh_required || !self.installed || !matches!(self.phase, Phase::Idle)
    }
    pub(super) fn persisting(&self) -> bool {
        matches!(self.phase, Phase::Persisting { .. })
    }
    pub(super) fn refresh_required(&self) -> bool {
        self.refresh_required
    }
    pub(super) fn listed(&self, name: &str) -> bool {
        self.checkpoint.is_none() || self.listed.contains(name)
    }

    /// A name the venue's table does not carry and the catalog has no rule
    /// for. Nothing can be priced or ordered in it, and a delisted name keeps
    /// its rule through `retain_previous`, so this is a name the venue never
    /// listed.
    pub(super) fn unfollowable(&self, name: &str) -> bool {
        !self.listed(name) && !self.catalog.rules.iter().any(|(listed, _)| listed == name)
    }

    fn refuse(&mut self, failure: AdmissionFailure) {
        if self.failure.as_ref().map(ToString::to_string) != Some(failure.to_string()) {
            tracing::warn!(%failure, "symbol admission retained; existing symbols remain usable");
        }
        self.failure = Some(failure);
    }
}

impl Drop for SymbolAdmission {
    fn drop(&mut self) {
        if let Phase::Fetching(task) = &self.phase {
            task.abort();
        }
    }
}

pub(crate) fn replay_catalog(
    records: &[WalRecord],
) -> Result<Option<Box<InstrumentCatalogCheckpoint>>, EngineError> {
    let mut checkpoint = None;
    for record in records {
        match record {
            WalRecord::InstrumentCatalogCheckpoint {
                checkpoint: current,
                ..
            } => checkpoint = Some(current.clone()),
            WalRecord::SegmentBase {
                instrument_catalog: current,
                ..
            } => {
                if current.is_none() && checkpoint.is_some() {
                    return Err(EngineError::Boot(
                        "rotation discarded durable instrument metadata".into(),
                    ));
                }
                checkpoint = current.clone();
            }
            _ => {}
        }
    }
    if let Some(checkpoint) = &checkpoint {
        checkpoint.validate_bounds()?;
    }
    Ok(checkpoint)
}

impl<W: Wal, R: RiskKernel, V: VenueGateway> Engine<W, R, V> {
    fn start_catalog_refresh(&mut self) {
        if let Some(client) = &self.symbol_admission.client {
            let client = Arc::clone(client);
            let checkpoint = self.symbol_admission.checkpoint.clone();
            self.symbol_admission.phase = Phase::Fetching(tokio::spawn(async move {
                let catalog = client.fetch().await?;
                let listed = catalog.rules.iter().map(|(name, _)| name.clone()).collect();
                let catalog = if let Some(checkpoint) = checkpoint {
                    tokio::task::spawn_blocking(move || catalog.retain_previous(&checkpoint))
                        .await
                        .map_err(|error| VenueError::Transport(error.to_string()))??
                } else {
                    catalog
                };
                Ok(RefreshedCatalog { catalog, listed })
            }));
        }
        self.symbol_admission.retry_after_ns = clock::now_ns().saturating_add(1_000_000_000);
    }

    fn retain_refreshed_catalog(&mut self, refreshed: RefreshedCatalog) -> Result<(), EngineError> {
        let RefreshedCatalog { catalog, listed } = refreshed;
        let checkpoint = match catalog.checkpoint() {
            Ok(checkpoint) => Some(Box::new(checkpoint)),
            Err(VenueError::Unsupported(_))
                if catalog.cache.is_none() && !self.require_exact_instruments =>
            {
                None
            }
            Err(error) => {
                self.symbol_admission
                    .refuse(AdmissionFailure::Catalog(error));
                self.symbol_admission.retry_after_ns =
                    clock::now_ns().saturating_add(1_000_000_000);
                return Ok(());
            }
        };
        let native_symbols = catalog
            .specs
            .iter()
            .map(|(name, spec)| (name.clone(), spec.native_symbol.clone()))
            .collect();
        let plan = match crate::identities::plan_identities(
            &[WalRecord::IdentityState {
                wall_ts_ms: clock::wall_ms(),
                state: self.identities.clone(),
            }],
            &self.host.names,
            None,
            &native_symbols,
            &[],
        ) {
            Ok(plan) => plan,
            Err(error) => {
                self.symbol_admission
                    .refuse(AdmissionFailure::Identity(error));
                self.symbol_admission.retry_after_ns =
                    clock::now_ns().saturating_add(1_000_000_000);
                return Ok(());
            }
        };
        if plan.changed {
            self.wal.append(&WalRecord::IdentityState {
                wall_ts_ms: clock::wall_ms(),
                state: plan.state.clone(),
            })?;
        }
        let changed = checkpoint != self.symbol_admission.checkpoint;
        if changed {
            if let Some(checkpoint) = &checkpoint {
                self.wal.append(&WalRecord::InstrumentCatalogCheckpoint {
                    wall_ts_ms: clock::wall_ms(),
                    checkpoint: checkpoint.clone(),
                })?;
            }
        }
        let write = MetadataWrite::Catalog {
            refreshed: RefreshedCatalog { catalog, listed },
            checkpoint,
            identities: plan.state,
        };
        if plan.changed || changed {
            self.begin_metadata_write(write)?;
        } else {
            self.complete_metadata_write(write);
        }
        Ok(())
    }

    fn begin_metadata_write(&mut self, write: MetadataWrite) -> Result<(), EngineError> {
        let barrier = self.wal.barrier_begin()?;
        if barrier.outstanding() {
            let (send, receive) = oneshot::channel();
            tokio::task::spawn_blocking(move || {
                let _ = send.send(barrier.wait());
            });
            self.symbol_admission.phase = Phase::Persisting {
                write: Box::new(write),
                receive,
            };
        } else {
            barrier.wait()?;
            self.complete_metadata_write(write);
        }
        Ok(())
    }

    fn complete_metadata_write(&mut self, write: MetadataWrite) {
        match write {
            MetadataWrite::Catalog {
                refreshed,
                checkpoint,
                identities,
            } => {
                self.identities = identities;
                self.symbol_admission.checkpoint = checkpoint;
                self.symbol_admission.listed = refreshed.listed;
                self.symbol_admission.unlisted.clear();
                self.symbol_admission.catalog = refreshed.catalog;
                self.symbol_admission.installed = false;
                self.symbol_admission.failure = None;
            }
            MetadataWrite::Identities { identities } => self.identities = identities,
        }
    }

    pub(super) async fn admit_wanted<M: MarketFeed, O: OrderFeed>(
        &mut self,
        market_feed: &mut M,
        order_feed: &mut O,
    ) -> Result<(), EngineError> {
        match std::mem::replace(&mut self.symbol_admission.phase, Phase::Idle) {
            Phase::Fetching(task) if !task.is_finished() => {
                self.symbol_admission.phase = Phase::Fetching(task);
                return Ok(());
            }
            Phase::Fetching(task) => match task.await {
                Ok(Ok(catalog)) => {
                    self.retain_refreshed_catalog(catalog)?;
                }
                result => {
                    let error = match result {
                        Ok(Err(error)) => error,
                        Err(error) => VenueError::Transport(error.to_string()),
                        _ => unreachable!(),
                    };
                    self.symbol_admission
                        .refuse(AdmissionFailure::Catalog(error));
                    self.symbol_admission.retry_after_ns =
                        clock::now_ns().saturating_add(1_000_000_000);
                }
            },
            Phase::Persisting { write, mut receive } => match receive.try_recv() {
                Err(oneshot::error::TryRecvError::Empty) => {
                    self.symbol_admission.phase = Phase::Persisting { write, receive };
                    return Ok(());
                }
                Ok(result) => {
                    result?;
                    self.complete_metadata_write(*write);
                }
                Err(error) => {
                    return Err(EngineError::State(format!(
                        "metadata durability completion unavailable: {error}"
                    )))
                }
            },
            Phase::Installing {
                wanted,
                expected,
                mut receive,
            } => match receive.try_recv() {
                Err(oneshot::error::TryRecvError::Empty) => {
                    self.symbol_admission.phase = Phase::Installing {
                        wanted,
                        expected,
                        receive,
                    };
                    return Ok(());
                }
                result => {
                    let result = match result {
                        Ok(result) => result,
                        Err(error) => Err(VenueError::Transport(error.to_string())),
                    };
                    let actual = match result {
                        Ok(actual) => actual,
                        Err(error) => {
                            self.symbol_admission
                                .refuse(AdmissionFailure::Install(error));
                            self.symbol_admission.retry_after_ns =
                                clock::now_ns().saturating_add(1_000_000_000);
                            self.wanted_symbols.splice(0..0, wanted);
                            return Ok(());
                        }
                    };
                    if actual.len() != expected.len()
                        || actual
                            .iter()
                            .zip(&expected)
                            .any(|(actual, expected)| *actual != Some(*expected))
                    {
                        return Err(EngineError::State(
                            "venue symbol installation disagrees with durable identity ids".into(),
                        ));
                    }
                    for (wanted, expected) in wanted.into_iter().zip(expected) {
                        let core_id = self.books.market.add_symbol(&wanted.name);
                        if core_id != expected {
                            return Err(EngineError::State(
                                "core symbol table disagrees with durable identity ids".into(),
                            ));
                        }
                        for (_, feed) in &wanted.listeners {
                            if market_feed.admit(&wanted.name, *feed) != Some(expected) {
                                return Err(EngineError::State(format!(
                                    "market feed id for {} differs from durable identity {}",
                                    wanted.name, expected.0
                                )));
                            }
                        }
                        order_feed.learn(&wanted.name, expected);
                        self.routing.size_to(self.books.market.table.len());
                        for (strategy, feed) in wanted.listeners {
                            self.routing.add(expected, feed, strategy);
                            let subscription = Subscription {
                                symbol: wanted.name.clone(),
                                feed,
                            };
                            if !self.subscriptions.contains(&subscription) {
                                self.subscriptions.push(subscription);
                            }
                        }
                    }
                    self.recovery
                        .install_catalog(&self.symbol_admission.catalog)?;
                    self.symbol_admission.installed = true;
                    self.symbol_admission.refresh_required = false;
                    self.symbol_admission.failure = None;
                    let names = names_record(&self.host.names, &self.books.market);
                    self.fills.learn(&names);
                }
            },
            Phase::Idle => {}
        }
        if self.symbol_admission.persisting() {
            return Ok(());
        }
        if self.symbol_admission.installed {
            self.books.rules.resize(self.books.market.table.len(), None);
            for (name, rule) in &self.symbol_admission.catalog.rules {
                if let Some(symbol) = self.books.market.table.get(name) {
                    self.books.rules[symbol.idx()] = Some(*rule);
                }
            }
            for (name, spec) in &self.symbol_admission.catalog.specs {
                if let Some(symbol) = self.books.market.table.get(name) {
                    order_feed.learn_instrument(symbol, spec);
                    self.instrument_specs.insert(symbol, spec.clone());
                    self.books.portfolio_symbols.insert(symbol);
                }
            }
        }
        if clock::now_ns() < self.symbol_admission.retry_after_ns {
            return Ok(());
        }
        if !self.symbol_admission.installed {
            match self.venue.dispatch_symbol_admission(
                self.symbol_admission
                    .catalog
                    .cache
                    .is_some()
                    .then(|| self.symbol_admission.catalog.clone()),
                Vec::new(),
            ) {
                Ok(receive) => {
                    self.symbol_admission.phase = Phase::Installing {
                        wanted: Vec::new(),
                        expected: Vec::new(),
                        receive,
                    }
                }
                Err(error) => {
                    self.symbol_admission
                        .refuse(AdmissionFailure::Install(error));
                    self.symbol_admission.retry_after_ns =
                        clock::now_ns().saturating_add(1_000_000_000);
                }
            }
            return Ok(());
        }
        if self.symbol_admission.refresh_required {
            self.start_catalog_refresh();
            return Ok(());
        }
        if self.wanted_symbols.is_empty() {
            return Ok(());
        }
        let mut ready = Vec::new();
        let mut missing_metadata = false;
        let mut available_ids = engine_types::identity::DENSE_ID_CAPACITY
            .saturating_sub(self.identities.instruments.len());
        for wanted in std::mem::take(&mut self.wanted_symbols) {
            let known = self
                .identities
                .instruments
                .iter()
                .any(|binding| binding.symbol == wanted.name);
            if !known && available_ids == 0 {
                self.symbol_admission.refuse(AdmissionFailure::Identity(
                    IdentityError::SymbolIdsExhausted,
                ));
                self.wanted_symbols.push(wanted);
                continue;
            }
            let rule = self
                .symbol_admission
                .catalog
                .rules
                .iter()
                .any(|(name, _)| name == &wanted.name);
            let spec = self
                .symbol_admission
                .catalog
                .specs
                .iter()
                .any(|(name, _)| name == &wanted.name);
            // A name the venue's own table does not carry is not missing
            // metadata: another fetch of the same table cannot supply it, so
            // nothing is asked of the venue either way. With no rule anywhere
            // the name is unfollowable and its subscription is dropped, said
            // once. A delisted name whose rule the catalog retained keeps its
            // subscription: an open position in it still has to exit.
            if self.symbol_admission.unfollowable(&wanted.name) {
                if self.symbol_admission.unlisted.insert(wanted.name.clone()) {
                    tracing::warn!(
                        symbol = %wanted.name,
                        "the venue does not list this instrument; its subscription is dropped and nothing is sent for it"
                    );
                }
                continue;
            }
            if !known && !self.symbol_admission.listed(&wanted.name) {
                if self.symbol_admission.unlisted.insert(wanted.name.clone()) {
                    tracing::warn!(
                        symbol = %wanted.name,
                        "the venue no longer lists this instrument; its subscription waits on the rule the catalog retained"
                    );
                }
                self.wanted_symbols.push(wanted);
                continue;
            }
            if !rule || (self.require_exact_instruments && !spec) {
                self.symbol_admission.refuse(AdmissionFailure::Identity(
                    IdentityError::UnresolvedInstrument(wanted.name.clone()),
                ));
                self.wanted_symbols.push(wanted);
                missing_metadata = true;
                continue;
            }
            if !known {
                available_ids -= 1;
            }
            ready.push(wanted);
        }
        if ready.is_empty() {
            if missing_metadata {
                self.start_catalog_refresh();
            }
            return Ok(());
        }
        let native_symbols = self
            .symbol_admission
            .catalog
            .specs
            .iter()
            .map(|(name, spec)| (name.clone(), spec.native_symbol.clone()))
            .collect();
        let requested = ready
            .iter()
            .map(|wanted| wanted.name.clone())
            .collect::<Vec<_>>();
        let plan = match crate::identities::plan_identities(
            &[WalRecord::IdentityState {
                wall_ts_ms: clock::wall_ms(),
                state: self.identities.clone(),
            }],
            &self.host.names,
            None,
            &native_symbols,
            &requested,
        ) {
            Ok(plan) => plan,
            Err(error) => {
                self.symbol_admission
                    .refuse(AdmissionFailure::Identity(error));
                self.wanted_symbols.splice(0..0, ready);
                return Ok(());
            }
        };
        if plan.changed {
            self.wal.append(&WalRecord::IdentityState {
                wall_ts_ms: clock::wall_ms(),
                state: plan.state.clone(),
            })?;
            self.begin_metadata_write(MetadataWrite::Identities {
                identities: plan.state,
            })?;
            if self.symbol_admission.persisting() {
                self.wanted_symbols.splice(0..0, ready);
                return Ok(());
            }
        }
        ready.sort_by_key(|wanted| {
            self.identities
                .instruments
                .iter()
                .position(|binding| binding.symbol == wanted.name)
        });
        let expected = ready
            .iter()
            .map(|wanted| {
                self.identities
                    .instruments
                    .iter()
                    .position(|binding| binding.symbol == wanted.name)
                    .map(|index| SymbolId(index as u16))
                    .expect("planned symbol identity")
            })
            .collect();
        let catalog = (!self.symbol_admission.installed
            && self.symbol_admission.catalog.cache.is_some())
        .then(|| self.symbol_admission.catalog.clone());
        match self.venue.dispatch_symbol_admission(
            catalog,
            ready.iter().map(|wanted| wanted.name.clone()).collect(),
        ) {
            Ok(receive) => {
                self.symbol_admission.phase = Phase::Installing {
                    wanted: ready,
                    expected,
                    receive,
                }
            }
            Err(error) => {
                self.symbol_admission
                    .refuse(AdmissionFailure::Install(error));
                self.symbol_admission.retry_after_ns =
                    clock::now_ns().saturating_add(1_000_000_000);
                self.wanted_symbols.splice(0..0, ready);
            }
        }
        Ok(())
    }
}

#[cfg(test)]
#[path = "symbol_admission_tests.rs"]
mod tests;
