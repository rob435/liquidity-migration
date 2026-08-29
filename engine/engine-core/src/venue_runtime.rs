//! The venue task. The engine owns state; this task owns blocking venue I/O.

use tokio::sync::{mpsc, oneshot};

use engine_types::{
    AccountIdentity, AccountInventory, AccountView, AmendSpec, InstrumentRule, OrderAck,
    OrderRequest, Symbol, SymbolId, VenueCaps, VenueError, VenueExecution, VenueGateway,
    VenueMutationTiming, VenueOrder,
};

const COMMAND_CAPACITY: usize = 4096;
const COMPLETION_CAPACITY: usize = 4096;

#[derive(Debug)]
pub enum MutationCompletion {
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
    SendOrders {
        command_id: u64,
        requests: Vec<OrderRequest>,
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
    },
    SendOrdersWait {
        requests: Vec<OrderRequest>,
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
    AddSymbol {
        symbol: String,
        reply: oneshot::Sender<Option<SymbolId>>,
    },
    SetLeverage {
        symbol: SymbolId,
        leverage: f64,
        reply: oneshot::Sender<Result<(), VenueError>>,
    },
    AccountView(oneshot::Sender<Result<AccountView, VenueError>>),
    InstrumentRules(oneshot::Sender<Result<Vec<(Symbol, InstrumentRule)>, VenueError>>),
    WorkingOrders(oneshot::Sender<Result<Vec<VenueOrder>, VenueError>>),
    AccountInventory(oneshot::Sender<Result<AccountInventory, VenueError>>),
    Executions {
        start_ms: i64,
        end_ms: i64,
        reply: oneshot::Sender<Result<Vec<VenueExecution>, VenueError>>,
    },
}

pub struct VenueClient {
    caps: VenueCaps,
    commands: mpsc::Sender<Command>,
    next_command_id: u64,
}

impl VenueClient {
    pub fn spawn<V: VenueGateway>(venue: V) -> (Self, mpsc::Receiver<MutationCompletion>) {
        let caps = venue.caps();
        let (command_tx, command_rx) = mpsc::channel(COMMAND_CAPACITY);
        let (completion_tx, completion_rx) = mpsc::channel(COMPLETION_CAPACITY);
        tokio::spawn(run(venue, command_rx, completion_tx));
        (
            Self {
                caps,
                commands: command_tx,
                next_command_id: 1,
            },
            completion_rx,
        )
    }

    pub fn dispatch_orders(&mut self, requests: Vec<OrderRequest>) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        self.send(Command::SendOrders {
            command_id,
            requests,
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
    ) -> Result<u64, VenueError> {
        let command_id = self.mint_command_id();
        self.send(Command::Amend {
            command_id,
            symbol,
            client_order_id,
            spec,
        })?;
        Ok(command_id)
    }

    pub async fn add_symbol_async(&mut self, symbol: &str) -> Result<Option<SymbolId>, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::AddSymbol {
            symbol: symbol.to_string(),
            reply,
        })?;
        receive.await.map_err(worker_gone)
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
        if let Err(error) = self.send(Command::SendOrdersWait {
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
        let (reply, receive) = oneshot::channel();
        self.send(Command::InstrumentRules(reply))?;
        receive.await.map_err(worker_gone)?
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
    ) -> Result<Vec<VenueExecution>, VenueError> {
        let (reply, receive) = oneshot::channel();
        self.send(Command::Executions {
            start_ms,
            end_ms,
            reply,
        })?;
        receive.await.map_err(worker_gone)?
    }
}

async fn run<V: VenueGateway>(
    mut venue: V,
    mut commands: mpsc::Receiver<Command>,
    completions: mpsc::Sender<MutationCompletion>,
) {
    while let Some(command) = commands.recv().await {
        match command {
            Command::SendOrders {
                command_id,
                requests,
            } => {
                let started_ns = engine_types::clock::mono_ns();
                let replies = venue.send_orders(&requests).await;
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
            } => {
                let started_ns = engine_types::clock::mono_ns();
                let reply = venue.amend_order(symbol, &client_order_id, spec).await;
                let timing = venue.take_mutation_timing();
                let rate_wait_ns = venue.take_rate_wait_ns();
                let completed_ns = engine_types::clock::mono_ns();
                let _ = completions
                    .send(MutationCompletion::Amend {
                        command_id,
                        started_ns,
                        completed_ns,
                        timing,
                        rate_wait_ns,
                        reply,
                    })
                    .await;
            }
            Command::SendOrdersWait { requests, reply } => {
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
            Command::AddSymbol { symbol, reply } => {
                let _ = reply.send(venue.add_symbol(&symbol));
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
            Command::InstrumentRules(reply) => {
                let _ = reply.send(venue.instrument_rules().await);
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
