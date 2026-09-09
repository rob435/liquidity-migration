//! The venue task. The engine owns state; this task owns blocking venue I/O.

use tokio::sync::{mpsc, oneshot};

use engine_types::{
    authority_refusal, AccountIdentity, AccountInventory, AccountView, AmendSpec, AuthorityEpoch,
    CommandAuthority, InstrumentRule, OrderAck, OrderRequest, Symbol, SymbolId, VenueCaps,
    VenueError, VenueGateway, VenueMutationTiming, VenueOrder,
};

const COMMAND_CAPACITY: usize = 4096;
const COMPLETION_CAPACITY: usize = 4096;
/// Amends coalesced into one gateway call, as the venue's batch endpoints cap it.
const MAX_AMEND_BATCH: usize = 10;

/// What a queued mutation is for, and therefore what it may wait behind.
///
/// Declaration order is the priority order: risk-off reaches the venue first,
/// administration last. One mutation is in flight at a time, so this decides
/// only which of the commands already waiting goes next.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub enum DispatchClass {
    /// Cancels, position stops, and sends whose every request reduces the
    /// physical position.
    RiskReducing,
    Amend,
    Opening,
    /// Leverage, symbol admission, and the account reads boot makes.
    Administration,
}

/// Which class a group of placements belongs to.
///
/// `OrderRequest::reduce_only` is what the planner wrote after deciding the
/// physical effect, so a virtual sleeve reduction that grows the physical
/// position reads as an opening here — which is what it is. A batch mixing
/// openings and reductions is an opening.
pub fn send_class(requests: &[OrderRequest]) -> DispatchClass {
    if !requests.is_empty() && requests.iter().all(|request| request.reduce_only) {
        DispatchClass::RiskReducing
    } else {
        DispatchClass::Opening
    }
}

#[derive(Debug)]
pub enum MutationCompletion {
    Leverage {
        command_id: u64,
        started_ns: u64,
        completed_ns: u64,
        reply: Result<(), VenueError>,
    },
    SetStop {
        command_id: u64,
        started_ns: u64,
        completed_ns: u64,
        rate_wait_ns: Option<u64>,
        reply: Result<(), VenueError>,
    },
    Orders {
        command_id: u64,
        started_ns: u64,
        completed_ns: u64,
        rate_wait_ns: Option<u64>,
        replies: Vec<Result<OrderAck, VenueError>>,
    },
    Cancels {
        command_id: u64,
        started_ns: u64,
        completed_ns: u64,
        timing: Option<VenueMutationTiming>,
        rate_wait_ns: Option<u64>,
        replies: Vec<Result<(), VenueError>>,
    },
    Amend {
        command_id: u64,
        started_ns: u64,
        completed_ns: u64,
        timing: Option<VenueMutationTiming>,
        rate_wait_ns: Option<u64>,
        reply: Result<(), VenueError>,
    },
}

enum Command {
    DispatchLeverage {
        command_id: u64,
        symbol: SymbolId,
        leverage: f64,
    },
    DispatchStop {
        command_id: u64,
        symbol: SymbolId,
        trigger_px: f64,
        exact: Option<engine_types::order_terms::ExactStopTerms>,
    },
    AdmitSymbols {
        catalog: Option<engine_types::orders::InstrumentCatalog>,
        names: Vec<String>,
        reply: oneshot::Sender<Result<Vec<Option<SymbolId>>, VenueError>>,
    },
    SendOrders {
        command_id: u64,
        requests: Vec<OrderRequest>,
        class: DispatchClass,
        /// `None` is a command that can never be refused at the send
        /// boundary. Risk-off must always reach the venue, so the engine
        /// mints an authority only for a group that opens exposure.
        authority: Option<CommandAuthority>,
    },
    CancelOrders {
        command_id: u64,
        requests: Vec<(SymbolId, String)>,
    },
    Amend {
        command_id: u64,
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        authority: Option<CommandAuthority>,
    },
    SendOrdersWait {
        requests: Vec<OrderRequest>,
        class: DispatchClass,
        reply: oneshot::Sender<Vec<Result<OrderAck, VenueError>>>,
    },
    CancelOrdersWait {
        requests: Vec<(SymbolId, String)>,
        reply: oneshot::Sender<Vec<Result<(), VenueError>>>,
    },
    AmendWait {
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        reply: oneshot::Sender<Result<(), VenueError>>,
    },
    AccountIdentity(oneshot::Sender<Result<AccountIdentity, VenueError>>),
    SetStop {
        symbol: SymbolId,
        trigger_px: f64,
        reply: oneshot::Sender<Result<(), VenueError>>,
    },

    SetLeverage {
        symbol: SymbolId,
        leverage: f64,
        reply: oneshot::Sender<Result<(), VenueError>>,
    },
    AccountView(oneshot::Sender<Result<AccountView, VenueError>>),
    WorkingOrders(oneshot::Sender<Result<Vec<VenueOrder>, VenueError>>),
    AccountInventory(oneshot::Sender<Result<AccountInventory, VenueError>>),
    Executions {
        start_ms: i64,
        end_ms: i64,
        reply: oneshot::Sender<Result<engine_types::ExecutionHistory, VenueError>>,
    },
}

struct LookupRequest {
    symbol: String,
    client_order_id: String,
    reply: oneshot::Sender<Result<engine_types::orders::OrderLookup, VenueError>>,
}

pub(crate) type SymbolAdmissionReceiver =
    oneshot::Receiver<Result<Vec<Option<SymbolId>>, VenueError>>;

pub struct VenueClient {
    caps: VenueCaps,
    lookups: Option<mpsc::Sender<LookupRequest>>,
    commands: mpsc::Sender<Command>,
    next_command_id: u64,
}

impl VenueClient {
    /// `authority` is the engine's own epoch; the worker reads the same
    /// counter the engine advances, so a command it is holding is refused the
    /// instant the engine loses permission to open.
    pub fn spawn<V: VenueGateway>(
        venue: V,
        authority: AuthorityEpoch,
    ) -> (Self, mpsc::Receiver<MutationCompletion>) {
        let caps = venue.caps();
        let lookups = venue.order_lookup_client().map(|client| {
            let (send, receive) = mpsc::channel(1);
            tokio::spawn(run_lookups(client, receive));
            send
        });
        let (command_tx, command_rx) = mpsc::channel(COMMAND_CAPACITY);
        let (completion_tx, completion_rx) = mpsc::channel(COMPLETION_CAPACITY);
        tokio::spawn(run(venue, command_rx, completion_tx, authority));
        (
            Self {
                caps,
                lookups,
                commands: command_tx,
                next_command_id: 1,
            },
            completion_rx,
        )
    }

    pub fn dispatch_leverage(
        &mut self,
        symbol: SymbolId,
        leverage: f64,
    ) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        self.send(Command::DispatchLeverage {
            command_id,
            symbol,
            leverage,
        })?;
        Ok(command_id)
    }

    pub fn dispatch_orders(
        &mut self,
        requests: Vec<OrderRequest>,
        authority: Option<CommandAuthority>,
    ) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        let class = send_class(&requests);
        self.send(Command::SendOrders {
            command_id,
            requests,
            class,
            authority,
        })?;
        Ok(command_id)
    }

    pub fn dispatch_cancels(
        &mut self,
        requests: Vec<(SymbolId, String)>,
    ) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        self.send(Command::CancelOrders {
            command_id,
            requests,
        })?;
        Ok(command_id)
    }

    pub fn dispatch_amend(
        &mut self,
        symbol: SymbolId,
        client_order_id: String,
        spec: AmendSpec,
        authority: Option<CommandAuthority>,
    ) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        self.send(Command::Amend {
            command_id,
            symbol,
            client_order_id,
            spec,
            authority,
        })?;
        Ok(command_id)
    }

    pub fn dispatch_stop(
        &mut self,
        symbol: SymbolId,
        trigger_px: f64,
        exact: Option<engine_types::order_terms::ExactStopTerms>,
    ) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        self.send(Command::DispatchStop {
            command_id,
            symbol,
            trigger_px,
            exact,
        })?;
        Ok(command_id)
    }

    pub fn dispatch_order_status(
        &self,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<oneshot::Receiver<Result<engine_types::orders::OrderLookup, VenueError>>, VenueError>
    {
        let (reply, receive) = oneshot::channel();
        if let Some(lookups) = &self.lookups {
            lookups
                .try_send(LookupRequest {
                    symbol: symbol.into(),
                    client_order_id: client_order_id.into(),
                    reply,
                })
                .map_err(|error| {
                    VenueError::Transport(format!("order lookup queue unavailable: {error}"))
                })?;
        } else {
            let _ = reply.send(Ok(engine_types::orders::OrderLookup::Unavailable));
        }
        Ok(receive)
    }

    pub async fn order_status_named(
        &self,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<engine_types::orders::OrderLookup, VenueError> {
        self.dispatch_order_status(symbol, client_order_id)?
            .await
            .map_err(worker_gone)?
    }

    pub fn dispatch_symbol_admission(
        &self,
        catalog: Option<engine_types::orders::InstrumentCatalog>,
        names: Vec<String>,
    ) -> Result<SymbolAdmissionReceiver, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::AdmitSymbols {
            catalog,
            names,
            reply,
        })?;
        Ok(receive)
    }

    fn mint_command_id(&mut self) -> u64 {
        let id = self.next_command_id;
        self.next_command_id = self.next_command_id.wrapping_add(1).max(1);
        id
    }

    fn send(&self, command: Command) -> Result<(), VenueError> {
        self.commands.try_send(command).map_err(|error| {
            VenueError::Transport(format!("venue task queue unavailable: {error}"))
        })
    }
}

#[engine_types::async_trait]
impl VenueGateway for VenueClient {
    fn caps(&self) -> VenueCaps {
        self.caps
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::AccountIdentity(reply))?;
        receive.await.map_err(worker_gone)?
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        let mut replies = self.send_orders(std::slice::from_ref(req)).await;
        replies.pop().unwrap_or_else(|| {
            Err(VenueError::BadReply(
                "venue task returned no placement result".to_string(),
            ))
        })
    }

    async fn send_orders(&mut self, reqs: &[OrderRequest]) -> Vec<Result<OrderAck, VenueError>> {
        let (reply, receive) = oneshot::channel();
        let requests = reqs.to_vec();
        let class = send_class(&requests);
        if let Err(error) = self.send(Command::SendOrdersWait {
            requests: requests.clone(),
            class,
            reply,
        }) {
            return requests
                .into_iter()
                .map(|_| Err(copy_error(&error)))
                .collect();
        }
        receive.await.unwrap_or_else(|_| {
            let stopped = VenueError::Transport("venue task stopped before replying".to_string());
            requests
                .into_iter()
                .map(|_| Err(copy_error(&stopped)))
                .collect()
        })
    }

    async fn cancel_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
    ) -> Result<(), VenueError> {
        let mut replies = self
            .cancel_orders(&[(symbol, client_order_id.to_string())])
            .await;
        replies.pop().unwrap_or_else(|| {
            Err(VenueError::BadReply(
                "venue task returned no cancellation result".to_string(),
            ))
        })
    }

    async fn cancel_orders(
        &mut self,
        requests: &[(SymbolId, String)],
    ) -> Vec<Result<(), VenueError>> {
        let requests = requests.to_vec();
        let (reply, receive) = oneshot::channel();
        if let Err(error) = self.send(Command::CancelOrdersWait {
            requests: requests.clone(),
            reply,
        }) {
            return requests
                .into_iter()
                .map(|_| Err(copy_error(&error)))
                .collect();
        }
        receive.await.unwrap_or_else(|_| {
            let stopped = VenueError::Transport("venue task stopped before replying".to_string());
            requests
                .into_iter()
                .map(|_| Err(copy_error(&stopped)))
                .collect()
        })
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        client_order_id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::AmendWait {
            symbol,
            client_order_id: client_order_id.to_string(),
            spec,
            reply,
        })?;
        receive.await.map_err(worker_gone)?
    }

    async fn set_stop(&mut self, symbol: SymbolId, trigger_px: f64) -> Result<(), VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::SetStop {
            symbol,
            trigger_px,
            reply,
        })?;
        receive.await.map_err(worker_gone)?
    }

    async fn set_leverage(&mut self, symbol: SymbolId, leverage: f64) -> Result<(), VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::SetLeverage {
            symbol,
            leverage,
            reply,
        })?;
        receive.await.map_err(worker_gone)?
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::AccountView(reply))?;
        receive.await.map_err(worker_gone)?
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        Err(VenueError::Unsupported(
            "live metadata requires the independent instrument catalog client".into(),
        ))
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::WorkingOrders(reply))?;
        receive.await.map_err(worker_gone)?
    }

    async fn account_inventory(&mut self) -> Result<AccountInventory, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::AccountInventory(reply))?;
        receive.await.map_err(worker_gone)?
    }

    async fn executions(
        &mut self,
        start_ms: i64,
        end_ms: i64,
    ) -> Result<engine_types::ExecutionHistory, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::Executions {
            start_ms,
            end_ms,
            reply,
        })?;
        receive.await.map_err(worker_gone)?
    }
}

async fn run_lookups(
    client: Box<dyn engine_types::orders::OrderLookupClient>,
    mut requests: mpsc::Receiver<LookupRequest>,
) {
    while let Some(LookupRequest {
        symbol,
        client_order_id,
        mut reply,
    }) = requests.recv().await
    {
        tokio::select! {
            biased;
            _ = reply.closed() => {}
            result = client.lookup(&symbol, &client_order_id) => { let _ = reply.send(result); }
        }
    }
}

/// Which class one queued command belongs to.
fn class_of(command: &Command) -> DispatchClass {
    match command {
        Command::SendOrders { class, .. } | Command::SendOrdersWait { class, .. } => *class,
        Command::CancelOrders { .. }
        | Command::CancelOrdersWait { .. }
        | Command::DispatchStop { .. }
        | Command::SetStop { .. } => DispatchClass::RiskReducing,
        Command::Amend { .. } | Command::AmendWait { .. } => DispatchClass::Amend,
        Command::DispatchLeverage { .. }
        | Command::SetLeverage { .. }
        | Command::AdmitSymbols { .. }
        | Command::AccountIdentity(_)
        | Command::AccountView(_)
        | Command::WorkingOrders(_)
        | Command::AccountInventory(_)
        | Command::Executions { .. } => DispatchClass::Administration,
    }
}

/// Whether a cancel or amend names an order whose placement is still waiting
/// in this ready set. Cancelling an order the venue has not been told about
/// cannot work, so it waits behind that send — and only behind that send.
fn depends_on_a_queued_send(command: &Command, ready: &[(u64, Command)]) -> bool {
    let queued = |id: &str| {
        ready.iter().any(|(_, other)| match other {
            Command::SendOrders { requests, .. } | Command::SendOrdersWait { requests, .. } => {
                requests.iter().any(|request| request.client_order_id == id)
            }
            _ => false,
        })
    };
    match command {
        Command::CancelOrders { requests, .. } | Command::CancelOrdersWait { requests, .. } => {
            requests.iter().any(|(_, id)| queued(id))
        }
        Command::Amend {
            client_order_id, ..
        }
        | Command::AmendWait {
            client_order_id, ..
        } => queued(client_order_id),
        _ => false,
    }
}

/// The next command to hand the venue: lowest class, then arrival order,
/// among those not waiting on a send that is itself still queued.
///
/// A send is never blocked, so whenever anything blocks something there is
/// also something selectable; the second pass exists so no arrangement of the
/// ready set can leave the worker with nothing to do.
fn choose(ready: &[(u64, Command)]) -> usize {
    ready
        .iter()
        .enumerate()
        .filter(|(_, (_, command))| !depends_on_a_queued_send(command, ready))
        .min_by_key(|(_, (arrival, command))| (class_of(command), *arrival))
        .or_else(|| {
            ready
                .iter()
                .enumerate()
                .min_by_key(|(_, (arrival, command))| (class_of(command), *arrival))
        })
        .map(|(index, _)| index)
        .unwrap_or_default()
}

async fn run<V: VenueGateway>(
    mut venue: V,
    mut commands: mpsc::Receiver<Command>,
    completions: mpsc::Sender<MutationCompletion>,
    epoch: AuthorityEpoch,
) {
    let refusal = |authority: Option<CommandAuthority>| {
        authority.and_then(|authority| {
            authority_refusal(&epoch, authority, engine_types::clock::mono_ns())
        })
    };
    let mut ready: Vec<(u64, Command)> = Vec::new();
    let mut arrival = 0u64;
    loop {
        // Everything already in the channel competes for this turn; only an
        // empty ready set waits.
        while let Ok(command) = commands.try_recv() {
            ready.push((arrival, command));
            arrival = arrival.wrapping_add(1);
        }
        if ready.is_empty() {
            match commands.recv().await {
                Some(command) => {
                    ready.push((arrival, command));
                    arrival = arrival.wrapping_add(1);
                    continue;
                }
                None => break,
            }
        }
        let (_, command) = ready.remove(choose(&ready));
        match command {
            Command::DispatchLeverage {
                command_id,
                symbol,
                leverage,
            } => {
                let started_ns = engine_types::clock::mono_ns();
                let reply = venue.set_leverage(symbol, leverage).await;
                let completed_ns = engine_types::clock::mono_ns();
                let _ = completions
                    .send(MutationCompletion::Leverage {
                        command_id,
                        started_ns,
                        completed_ns,
                        reply,
                    })
                    .await;
            }
            Command::SendOrders {
                command_id,
                requests,
                authority,
                ..
            } => {
                // The last point at which nothing has been signed. A halt, a
                // lost stream or a replaced strategy since this was queued
                // means it is not sent at all; the engine's never-sent path
                // releases the reservation.
                if let Some(reason) = refusal(authority) {
                    let at = engine_types::clock::mono_ns();
                    let _ = completions
                        .send(MutationCompletion::Orders {
                            command_id,
                            started_ns: at,
                            completed_ns: at,
                            rate_wait_ns: None,
                            replies: requests
                                .iter()
                                .map(|_| Err(VenueError::BadRequest(reason.clone())))
                                .collect(),
                        })
                        .await;
                    continue;
                }
                let started_ns = engine_types::clock::mono_ns();
                let replies = venue
                    .send_orders_under(&requests, authority.map(|held| (&epoch, held)))
                    .await;
                let rate_wait_ns = venue.take_rate_wait_ns();
                let completed_ns = engine_types::clock::mono_ns();
                let _ = completions
                    .send(MutationCompletion::Orders {
                        command_id,
                        started_ns,
                        completed_ns,
                        rate_wait_ns,
                        replies,
                    })
                    .await;
            }
            Command::CancelOrders {
                command_id,
                requests,
            } => {
                let started_ns = engine_types::clock::mono_ns();
                let replies = venue.cancel_orders(&requests).await;
                let timing = venue.take_mutation_timing();
                let rate_wait_ns = venue.take_rate_wait_ns();
                let completed_ns = engine_types::clock::mono_ns();
                let _ = completions
                    .send(MutationCompletion::Cancels {
                        command_id,
                        started_ns,
                        completed_ns,
                        timing,
                        rate_wait_ns,
                        replies,
                    })
                    .await;
            }
            Command::Amend {
                command_id,
                symbol,
                client_order_id,
                spec,
                authority,
            } => {
                if let Some(reason) = refusal(authority) {
                    let at = engine_types::clock::mono_ns();
                    let _ = completions
                        .send(MutationCompletion::Amend {
                            command_id,
                            started_ns: at,
                            completed_ns: at,
                            timing: None,
                            rate_wait_ns: None,
                            reply: Err(VenueError::BadRequest(reason)),
                        })
                        .await;
                    continue;
                }
                let mut ids = vec![command_id];
                let mut requests = vec![(symbol, client_order_id, spec)];
                while requests.len() < MAX_AMEND_BATCH {
                    let next = ready.iter().position(|(_, waiting)| {
                        matches!(waiting, Command::Amend { client_order_id, .. }
                            if !requests.iter().any(|(_, held, _)| held == client_order_id))
                            && !depends_on_a_queued_send(waiting, &ready)
                    });
                    let Some(index) = next else { break };
                    let Command::Amend {
                        command_id,
                        symbol,
                        client_order_id,
                        spec,
                        authority,
                    } = ready.remove(index).1
                    else {
                        unreachable!("the position above matched an amend")
                    };
                    if let Some(reason) = refusal(authority) {
                        let at = engine_types::clock::mono_ns();
                        let _ = completions
                            .send(MutationCompletion::Amend {
                                command_id,
                                started_ns: at,
                                completed_ns: at,
                                timing: None,
                                rate_wait_ns: None,
                                reply: Err(VenueError::BadRequest(reason)),
                            })
                            .await;
                        continue;
                    }
                    ids.push(command_id);
                    requests.push((symbol, client_order_id, spec));
                }
                let started_ns = engine_types::clock::mono_ns();
                let replies = venue.amend_orders(&requests).await;
                let timing = venue.take_mutation_timing();
                let mut rate_wait_ns = venue.take_rate_wait_ns();
                let completed_ns = engine_types::clock::mono_ns();
                let replies = if replies.len() == ids.len() {
                    replies
                } else {
                    ids.iter()
                        .map(|_| {
                            Err(VenueError::BadReply(
                                "amend response count differs from request count".into(),
                            ))
                        })
                        .collect()
                };
                for (command_id, reply) in ids.into_iter().zip(replies) {
                    let _ = completions
                        .send(MutationCompletion::Amend {
                            command_id,
                            started_ns,
                            completed_ns,
                            timing,
                            rate_wait_ns: rate_wait_ns.take(),
                            reply,
                        })
                        .await;
                }
            }
            Command::DispatchStop {
                command_id,
                symbol,
                trigger_px,
                exact,
            } => {
                let started_ns = engine_types::clock::mono_ns();
                let reply = match exact {
                    Some(terms) => venue.set_stop_exact(symbol, &terms).await,
                    None => venue.set_stop(symbol, trigger_px).await,
                };
                let rate_wait_ns = venue.take_rate_wait_ns();
                let completed_ns = engine_types::clock::mono_ns();
                let _ = completions
                    .send(MutationCompletion::SetStop {
                        command_id,
                        started_ns,
                        completed_ns,
                        rate_wait_ns,
                        reply,
                    })
                    .await;
            }
            Command::SendOrdersWait {
                requests, reply, ..
            } => {
                let _ = reply.send(venue.send_orders(&requests).await);
            }
            Command::CancelOrdersWait { requests, reply } => {
                let _ = reply.send(venue.cancel_orders(&requests).await);
            }
            Command::AmendWait {
                symbol,
                client_order_id,
                spec,
                reply,
            } => {
                let _ = reply.send(venue.amend_order(symbol, &client_order_id, spec).await);
            }
            Command::AccountIdentity(reply) => {
                let _ = reply.send(venue.account_identity().await);
            }
            Command::SetStop {
                symbol,
                trigger_px,
                reply,
            } => {
                let _ = reply.send(venue.set_stop(symbol, trigger_px).await);
            }
            Command::AdmitSymbols {
                catalog,
                names,
                reply,
            } => {
                let result = match catalog {
                    Some(catalog) => venue.install_instrument_catalog(&catalog),
                    None => Ok(()),
                }
                .map(|()| names.iter().map(|name| venue.add_symbol(name)).collect());
                let _ = reply.send(result);
            }

            Command::SetLeverage {
                symbol,
                leverage,
                reply,
            } => {
                let _ = reply.send(venue.set_leverage(symbol, leverage).await);
            }
            Command::AccountView(reply) => {
                let _ = reply.send(venue.account_view().await);
            }
            Command::WorkingOrders(reply) => {
                let _ = reply.send(venue.working_orders().await);
            }
            Command::AccountInventory(reply) => {
                let _ = reply.send(venue.account_inventory().await);
            }
            Command::Executions {
                start_ms,
                end_ms,
                reply,
            } => {
                let _ = reply.send(venue.executions(start_ms, end_ms).await);
            }
        }
    }
}

fn worker_gone(_: oneshot::error::RecvError) -> VenueError {
    VenueError::Transport("venue task stopped before replying".to_string())
}

fn copy_error(error: &VenueError) -> VenueError {
    match error {
        VenueError::Unsupported(detail) => VenueError::Unsupported(detail.clone()),
        VenueError::BadRequest(detail) => VenueError::BadRequest(detail.clone()),
        VenueError::Transport(detail) => VenueError::Transport(detail.clone()),
        VenueError::Rejected { code, message } => VenueError::Rejected {
            code: *code,
            message: message.clone(),
        },
        VenueError::BadReply(detail) => VenueError::BadReply(detail.clone()),
        VenueError::Credentials(detail) => VenueError::Credentials(detail.clone()),
    }
}
