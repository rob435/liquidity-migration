//! The venue task. The engine owns state; this task owns blocking venue I/O.

use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, AtomicU8, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};

use engine_types::{
    authority_refusal, AccountIdentity, AccountInventory, AccountView, AmendRequest, AmendSpec,
    AuthorityEpoch, CommandAuthority, InstrumentRule, OrderAck, OrderRequest, QueuedCommand,
    Symbol, SymbolId, VenueCaps, VenueError, VenueGateway, VenueMutationTiming, VenueOrder,
};

pub const COMMAND_CAPACITY: usize = 4096;
pub const URGENT_CAPACITY: usize = 1024;
/// Ordinary commands the ready set holds before the task stops taking more
/// from the ordinary mailbox. The urgent mailbox is never throttled.
pub const READY_ORDINARY_CAPACITY: usize = COMMAND_CAPACITY;
pub const COMPLETION_CAPACITY: usize = 4096;
/// Amends coalesced into one gateway call, as the venue's batch endpoints cap it.
const MAX_AMEND_BATCH: usize = 10;
/// Requests one command may carry. `MAX_ORDERS_PER_BATCH` and
/// `MAX_CANCELS_PER_BATCH` (engine.rs) are both 10, and Bybit refuses a larger
/// group before the wire (`ORDER_CREATES_PER_SECOND`, `MAX_CANCEL_BATCH_ITEMS`).
pub const MAX_REQUESTS_PER_COMMAND: usize = 10;

const ORDINARY_LANE: &str = "ordinary";
const URGENT_LANE: &str = "urgent";
/// Every full-lane refusal starts with this, so a caller can tell the engine's
/// own backpressure from a venue transport fault.
const LANE_FULL: &str = "venue dispatch lane full";

/// The detail of a refusal the client made because its lane is full, or
/// `None` for every other error.
pub fn lane_full(error: &VenueError) -> Option<&str> {
    match error {
        VenueError::Transport(detail) if detail.starts_with(LANE_FULL) => Some(detail.as_str()),
        _ => None,
    }
}

/// What a queued mutation is for, and therefore what it may wait behind.
///
/// Declaration order is the priority order: risk-off reaches the venue first,
/// administration last. One mutation is in flight at a time, so this decides
/// only which of the commands already waiting goes next, and only among those
/// the venue's own request quota would take now.
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

/// What the venue task is holding, updated by the task each turn and read by
/// whoever reports on it. Refusals are counted by the client.
#[derive(Debug)]
pub struct VenueQueueGauges {
    ready_ordinary: AtomicUsize,
    ready_urgent: AtomicUsize,
    oldest_ready_queued_ns: AtomicU64,
    in_flight_since_ns: AtomicU64,
    /// 0 is nothing in flight; otherwise the [`DispatchClass`] position + 1.
    in_flight_class: AtomicU8,
    refused_ordinary: AtomicU64,
    refused_urgent: AtomicU64,
    pub ordinary_capacity: usize,
    pub urgent_capacity: usize,
}

/// One reading of [`VenueQueueGauges`], ages resolved against a clock.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct VenueQueueSnapshot {
    pub ready_ordinary: usize,
    pub ready_urgent: usize,
    pub oldest_ready_ms: u64,
    pub in_flight_ms: u64,
    pub in_flight_class: Option<DispatchClass>,
    pub refused_ordinary: u64,
    pub refused_urgent: u64,
    pub ordinary_capacity: usize,
    pub urgent_capacity: usize,
}

fn class_code(class: DispatchClass) -> u8 {
    match class {
        DispatchClass::RiskReducing => 1,
        DispatchClass::Amend => 2,
        DispatchClass::Opening => 3,
        DispatchClass::Administration => 4,
    }
}

fn class_of_code(code: u8) -> Option<DispatchClass> {
    match code {
        1 => Some(DispatchClass::RiskReducing),
        2 => Some(DispatchClass::Amend),
        3 => Some(DispatchClass::Opening),
        4 => Some(DispatchClass::Administration),
        _ => None,
    }
}

impl VenueQueueGauges {
    fn new() -> Self {
        Self {
            ready_ordinary: AtomicUsize::new(0),
            ready_urgent: AtomicUsize::new(0),
            oldest_ready_queued_ns: AtomicU64::new(0),
            in_flight_since_ns: AtomicU64::new(0),
            in_flight_class: AtomicU8::new(0),
            refused_ordinary: AtomicU64::new(0),
            refused_urgent: AtomicU64::new(0),
            ordinary_capacity: COMMAND_CAPACITY,
            urgent_capacity: URGENT_CAPACITY,
        }
    }

    pub fn snapshot(&self, now_ns: u64) -> VenueQueueSnapshot {
        let age_ms = |since: u64| match since {
            0 => 0,
            since => now_ns.saturating_sub(since) / 1_000_000,
        };
        VenueQueueSnapshot {
            ready_ordinary: self.ready_ordinary.load(Ordering::Relaxed),
            ready_urgent: self.ready_urgent.load(Ordering::Relaxed),
            oldest_ready_ms: age_ms(self.oldest_ready_queued_ns.load(Ordering::Relaxed)),
            in_flight_ms: age_ms(self.in_flight_since_ns.load(Ordering::Relaxed)),
            in_flight_class: class_of_code(self.in_flight_class.load(Ordering::Relaxed)),
            refused_ordinary: self.refused_ordinary.load(Ordering::Relaxed),
            refused_urgent: self.refused_urgent.load(Ordering::Relaxed),
            ordinary_capacity: self.ordinary_capacity,
            urgent_capacity: self.urgent_capacity,
        }
    }

    fn observe_ready(&self, ordinary: usize, urgent: usize, oldest_queued_ns: u64) {
        self.ready_ordinary.store(ordinary, Ordering::Relaxed);
        self.ready_urgent.store(urgent, Ordering::Relaxed);
        self.oldest_ready_queued_ns
            .store(oldest_queued_ns, Ordering::Relaxed);
    }

    fn enter_flight(&self, class: DispatchClass, now_ns: u64) {
        self.in_flight_class
            .store(class_code(class), Ordering::Relaxed);
        self.in_flight_since_ns.store(now_ns, Ordering::Relaxed);
    }

    fn leave_flight(&self) {
        self.in_flight_class.store(0, Ordering::Relaxed);
        self.in_flight_since_ns.store(0, Ordering::Relaxed);
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

pub(crate) enum Command {
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

/// One command waiting for its turn at the venue.
pub(crate) struct Queued {
    pub(crate) arrival: u64,
    pub(crate) queued_ns: u64,
    pub(crate) command: Command,
}

pub struct VenueClient {
    caps: VenueCaps,
    lookups: Option<mpsc::Sender<LookupRequest>>,
    /// Risk-off only, so a cancel never queues behind openings the engine has
    /// already handed over.
    urgent: mpsc::Sender<Command>,
    commands: mpsc::Sender<Command>,
    gauges: Arc<VenueQueueGauges>,
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
        let (urgent_tx, urgent_rx) = mpsc::channel(URGENT_CAPACITY);
        let (completion_tx, completion_rx) = mpsc::channel(COMPLETION_CAPACITY);
        let gauges = Arc::new(VenueQueueGauges::new());
        tokio::spawn(run(
            venue,
            urgent_rx,
            command_rx,
            completion_tx,
            authority,
            gauges.clone(),
        ));
        (
            Self {
                caps,
                lookups,
                urgent: urgent_tx,
                commands: command_tx,
                gauges,
                next_command_id: 1,
            },
            completion_rx,
        )
    }

    pub fn gauges(&self) -> Arc<VenueQueueGauges> {
        self.gauges.clone()
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
        refuse_oversized("placement", requests.len())?;
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
        refuse_oversized("cancel", requests.len())?;
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
        let (lane, capacity, sender, refused) = if class_of(&command) == DispatchClass::RiskReducing
        {
            (
                URGENT_LANE,
                URGENT_CAPACITY,
                &self.urgent,
                &self.gauges.refused_urgent,
            )
        } else {
            (
                ORDINARY_LANE,
                COMMAND_CAPACITY,
                &self.commands,
                &self.gauges.refused_ordinary,
            )
        };
        match sender.try_send(command) {
            Ok(()) => Ok(()),
            Err(mpsc::error::TrySendError::Full(_)) => {
                refused.fetch_add(1, Ordering::Relaxed);
                Err(VenueError::Transport(format!(
                    "{LANE_FULL}: the {lane} lane holds its {capacity} commands"
                )))
            }
            Err(error @ mpsc::error::TrySendError::Closed(_)) => Err(VenueError::Transport(
                format!("venue task queue unavailable: {error}"),
            )),
        }
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

/// One command carries at most [`MAX_REQUESTS_PER_COMMAND`] requests, so the
/// bytes one lane can hold follow from its capacity.
fn refuse_oversized(what: &str, requests: usize) -> Result<(), VenueError> {
    if requests > MAX_REQUESTS_PER_COMMAND {
        return Err(VenueError::BadRequest(format!(
            "{what} command carries {requests} requests; one command takes at most {MAX_REQUESTS_PER_COMMAND}"
        )));
    }
    Ok(())
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

/// Every client id a placement still waiting in this ready set will ask the
/// venue to create. Built once a turn; every dependency check reads it.
pub(crate) fn queued_send_ids(ready: &[Queued]) -> HashSet<&str> {
    ready
        .iter()
        .flat_map(|queued| match &queued.command {
            Command::SendOrders { requests, .. } | Command::SendOrdersWait { requests, .. } => {
                requests.as_slice()
            }
            _ => &[][..],
        })
        .map(|request| request.client_order_id.as_str())
        .collect()
}

/// Whether a cancel or amend names an order whose placement is still waiting
/// in this ready set. Cancelling an order the venue has not been told about
/// cannot work, so it waits behind that send — and only behind that send.
fn depends_on_a_queued_send(command: &Command, sends: &HashSet<&str>) -> bool {
    match command {
        Command::CancelOrders { requests, .. } | Command::CancelOrdersWait { requests, .. } => {
            requests.iter().any(|(_, id)| sends.contains(id.as_str()))
        }
        Command::Amend {
            client_order_id, ..
        }
        | Command::AmendWait {
            client_order_id, ..
        } => sends.contains(client_order_id.as_str()),
        _ => false,
    }
}

/// The same answer as [`selectable`], read off the ready set itself rather
/// than off the index. The test oracle the index is proved against.
#[cfg(test)]
pub(crate) fn selectable_by_scan(ready: &[Queued]) -> Vec<usize> {
    let queued = |id: &str| {
        ready.iter().any(|other| match &other.command {
            Command::SendOrders { requests, .. } | Command::SendOrdersWait { requests, .. } => {
                requests.iter().any(|request| request.client_order_id == id)
            }
            _ => false,
        })
    };
    let blocked = |command: &Command| match command {
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
    };
    let unblocked: Vec<usize> = (0..ready.len())
        .filter(|index| !blocked(&ready[*index].command))
        .collect();
    if unblocked.is_empty() {
        (0..ready.len()).collect()
    } else {
        unblocked
    }
}

/// What one queued command will ask the venue to do, for an adapter pricing
/// it against the venue's request quota.
fn queued_shape(command: &Command) -> QueuedCommand {
    match command {
        Command::SendOrders {
            requests, class, ..
        }
        | Command::SendOrdersWait {
            requests, class, ..
        } => {
            let requests = requests.len();
            match class {
                DispatchClass::RiskReducing => QueuedCommand::Reducing { requests },
                _ => QueuedCommand::Opening { requests },
            }
        }
        Command::CancelOrders { requests, .. } | Command::CancelOrdersWait { requests, .. } => {
            QueuedCommand::Cancel {
                requests: requests.len(),
            }
        }
        Command::Amend { .. } | Command::AmendWait { .. } => QueuedCommand::Amend { requests: 1 },
        Command::DispatchStop { .. } | Command::SetStop { .. } => QueuedCommand::PositionStop,
        Command::DispatchLeverage { .. }
        | Command::SetLeverage { .. }
        | Command::AdmitSymbols { .. }
        | Command::AccountIdentity(_)
        | Command::AccountView(_)
        | Command::WorkingOrders(_)
        | Command::AccountInventory(_)
        | Command::Executions { .. } => QueuedCommand::Administration,
    }
}

/// The commands that may be picked this turn: those not waiting on a send
/// that is itself still queued, and everything when that leaves nothing.
///
/// A send is never blocked, so whenever anything blocks something there is
/// also something selectable; the fallback exists so no arrangement of the
/// ready set can leave the worker with nothing to do.
pub(crate) fn selectable(ready: &[Queued], sends: &HashSet<&str>) -> Vec<usize> {
    let unblocked: Vec<usize> = (0..ready.len())
        .filter(|index| !depends_on_a_queued_send(&ready[*index].command, sends))
        .collect();
    if unblocked.is_empty() {
        (0..ready.len()).collect()
    } else {
        unblocked
    }
}

/// The amends that may go out with the one at `head` in a single call:
/// distinct client ids, arrival order, none waiting on a placement still
/// queued here, capped at [`MAX_AMEND_BATCH`].
///
/// Planned before pricing, because what the venue's quota is asked to take is
/// the whole group, not the head alone.
fn amend_batch(ready: &[Queued], head: usize, sends: &HashSet<&str>) -> Vec<usize> {
    let Command::Amend {
        client_order_id, ..
    } = &ready[head].command
    else {
        return vec![head];
    };
    let mut ids = vec![client_order_id.as_str()];
    let mut chosen = vec![head];
    for (index, queued) in ready.iter().enumerate() {
        if chosen.len() >= MAX_AMEND_BATCH {
            break;
        }
        let Command::Amend {
            client_order_id, ..
        } = &queued.command
        else {
            continue;
        };
        if index == head
            || ids.contains(&client_order_id.as_str())
            || depends_on_a_queued_send(&queued.command, sends)
        {
            continue;
        }
        ids.push(client_order_id.as_str());
        chosen.push(index);
    }
    chosen
}

/// The next command to hand the venue: one the venue's quota will take now,
/// then lowest class, then arrival order.
fn choose(ready: &[Queued], waits: &[Duration], eligible: &[usize]) -> usize {
    eligible
        .iter()
        .copied()
        .min_by_key(|index| {
            let queued = &ready[*index];
            (
                !waits[*index].is_zero(),
                class_of(&queued.command),
                queued.arrival,
            )
        })
        .unwrap_or_default()
}

/// The permission a queued command would be refused on, if it carries one.
fn queued_authority(command: &Command) -> Option<CommandAuthority> {
    match command {
        Command::SendOrders { authority, .. } | Command::Amend { authority, .. } => *authority,
        _ => None,
    }
}

/// How long to hold the whole ready set back when even the command the worker
/// would pick is over the venue's quota: the shortest wait among the commands
/// it may pick. `None` dispatches now.
fn held_for(waits: &[Duration], eligible: &[usize], index: usize) -> Option<Duration> {
    if waits[index].is_zero() {
        return None;
    }
    eligible
        .iter()
        .map(|index| waits[*index])
        .min()
        .filter(|hold| !hold.is_zero())
}

/// Answer the amends whose authority is already spent, then hand the rest to
/// the gateway as one call. A refusal costs no quota: the venue never sees it
/// either way, so it is answered now rather than a request window later.
async fn dispatch_amends<V: VenueGateway>(
    venue: &mut V,
    completions: &mpsc::Sender<MutationCompletion>,
    epoch: &AuthorityEpoch,
    batch: Vec<(u64, AmendRequest)>,
) {
    let mut ids = Vec::with_capacity(batch.len());
    let mut requests = Vec::with_capacity(batch.len());
    for (command_id, request) in batch {
        let refusal = request
            .authority
            .and_then(|held| authority_refusal(epoch, held, engine_types::clock::mono_ns()));
        if let Some(reason) = refusal {
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
        requests.push(request);
    }
    if ids.is_empty() {
        return;
    }
    let started_ns = engine_types::clock::mono_ns();
    let replies = venue.amend_orders_under(&requests, epoch).await;
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

async fn run<V: VenueGateway>(
    mut venue: V,
    mut urgent: mpsc::Receiver<Command>,
    mut commands: mpsc::Receiver<Command>,
    completions: mpsc::Sender<MutationCompletion>,
    epoch: AuthorityEpoch,
    gauges: Arc<VenueQueueGauges>,
) {
    let refusal = |authority: Option<CommandAuthority>| {
        authority.and_then(|authority| {
            authority_refusal(&epoch, authority, engine_types::clock::mono_ns())
        })
    };
    let mut ready: Vec<Queued> = Vec::new();
    let mut arrival = 0u64;
    // Set when the engine has dropped that sender. Everything taken from a
    // lane is still answered or refused, never dropped; once both are closed
    // nothing can arrive to preempt a hold any more, so the drain does not
    // park and the adapter's own pacer serves the quota wait inside the call.
    let mut urgent_closed = false;
    let mut commands_closed = false;
    loop {
        let mut take = |command, ready: &mut Vec<Queued>| {
            ready.push(Queued {
                arrival,
                queued_ns: engine_types::clock::mono_ns(),
                command,
            });
            arrival = arrival.wrapping_add(1);
        };
        // Risk-off first and without limit: nothing the engine has handed
        // over may sit in front of a cancel.
        loop {
            match urgent.try_recv() {
                Ok(command) => take(command, &mut ready),
                Err(mpsc::error::TryRecvError::Empty) => break,
                Err(mpsc::error::TryRecvError::Disconnected) => {
                    urgent_closed = true;
                    break;
                }
            }
        }
        // The ordinary mailbox is the engine's backpressure: the ready set
        // takes no more than it can name, and the client refuses the rest.
        let mut ordinary = ready
            .iter()
            .filter(|queued| class_of(&queued.command) != DispatchClass::RiskReducing)
            .count();
        while ordinary < READY_ORDINARY_CAPACITY {
            match commands.try_recv() {
                Ok(command) => {
                    take(command, &mut ready);
                    ordinary += 1;
                }
                Err(mpsc::error::TryRecvError::Empty) => break,
                Err(mpsc::error::TryRecvError::Disconnected) => {
                    commands_closed = true;
                    break;
                }
            }
        }
        gauges.observe_ready(
            ordinary,
            ready.len() - ordinary,
            ready.first().map_or(0, |queued| queued.queued_ns),
        );
        let closed = urgent_closed && commands_closed;
        if ready.is_empty() {
            if closed {
                break;
            }
            tokio::select! {
                biased;
                received = urgent.recv(), if !urgent_closed => match received {
                    Some(command) => take(command, &mut ready),
                    None => urgent_closed = true,
                },
                received = commands.recv(), if !commands_closed => match received {
                    Some(command) => take(command, &mut ready),
                    None => commands_closed = true,
                },
            }
            continue;
        }
        let waits: Vec<Duration> = ready
            .iter()
            .map(|queued| venue.quota_wait(queued_shape(&queued.command)))
            .collect();
        let sends = queued_send_ids(&ready);
        let eligible = selectable(&ready, &sends);
        let index = choose(&ready, &waits, &eligible);
        // The venue would hold this call back anyway, and it would hold it
        // inside the gateway where nothing else can be picked. Wait out here
        // instead, where a cancel that arrives meanwhile is chosen next.
        //
        // A command whose authority is already spent is not held at all: the
        // venue never sees it either way, so it is answered now rather than a
        // quota window later, and the engine's reservation is released with it.
        let hold = (!closed)
            .then(|| held_for(&waits, &eligible, index))
            .flatten()
            .filter(|_| refusal(queued_authority(&ready[index].command)).is_none());
        if let Some(hold) = hold {
            drop(sends);
            tokio::select! {
                biased;
                received = urgent.recv(), if !urgent_closed => match received {
                    Some(command) => take(command, &mut ready),
                    None => urgent_closed = true,
                },
                _ = tokio::time::sleep(hold) => {}
                received = commands.recv(),
                    if !commands_closed && ordinary < READY_ORDINARY_CAPACITY => match received {
                    Some(command) => take(command, &mut ready),
                    None => commands_closed = true,
                },
            }
            continue;
        }
        // One amend is priced as the smallest group it can shrink to, so the
        // group the quota is actually asked for is settled here: the largest
        // prefix of the planned batch the venue would take now.
        if matches!(ready[index].command, Command::Amend { .. }) {
            let mut batch = amend_batch(&ready, index, &sends);
            while batch.len() > 1
                && !venue
                    .quota_wait(QueuedCommand::Amend {
                        requests: batch.len(),
                    })
                    .is_zero()
            {
                batch.pop();
            }
            batch.sort_unstable();
            drop(sends);
            let mut taken: Vec<(u64, AmendRequest)> = batch
                .iter()
                .rev()
                .map(|index| match ready.remove(*index).command {
                    Command::Amend {
                        command_id,
                        symbol,
                        client_order_id,
                        spec,
                        authority,
                    } => (
                        command_id,
                        AmendRequest {
                            symbol,
                            client_order_id,
                            spec,
                            authority,
                        },
                    ),
                    _ => unreachable!("amend_batch selects only amends"),
                })
                .collect();
            taken.reverse();
            gauges.enter_flight(DispatchClass::Amend, engine_types::clock::mono_ns());
            dispatch_amends(&mut venue, &completions, &epoch, taken).await;
            gauges.leave_flight();
            continue;
        }
        drop(sends);
        let command = ready.remove(index).command;
        gauges.enter_flight(class_of(&command), engine_types::clock::mono_ns());
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
            Command::Amend { .. } => {
                unreachable!("every queued amend leaves through the batch above")
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
        gauges.leave_flight();
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
